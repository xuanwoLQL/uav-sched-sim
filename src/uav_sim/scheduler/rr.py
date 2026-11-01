"""
rr.py —— 固定时间片轮转调度器（经典基线 2）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 为了让每台无人机都能"轮到"上传，把每个任务限制在一段时长内，到点就让位 |
| OS 原理层   | **RR（Round Robin）时间片轮转**：每个任务最多获得一个时间片 `q`；时间片耗尽仍未完成 -> 剥夺 CPU，放回就绪队列尾 |
| 算法设计层 | 固定时间片 + 未完成重新入队；时间片大小 `q` 是唯一可调参数 |
| 代码实现层 | `RRScheduler.step()`（把 `q` 通过 `limit_s` 交给 Simulator 实现真实抢占） |

教学要点（PDF 实验 3.2 思考题 2 的直接答案）：
    * `q` 太小（如 0.2 s）：上下文切换开销陡增，大量任务在"传一点-被踢走-再排队"之间
      反复折腾，平均等待时延反而上升（**切换开销 > 并行收益**）；
    * `q` 太大（如 5 s）：退化成 FCFS，大任务照样把后面的任务堵死，护航效应重现；
    * 理想 `q`：略大于"典型 IO 交互时长"，使多数任务一个时间片内完成 ——
      但对本项目这种数据量跨度 4 个数量级（0.5 MB ~ 2 GB）的异构负载，
      **不存在一个对所有任务都合适的固定 `q`**，这正是静态时间片方案的死穴。

实现说明：真实抢占由 `Simulator.dispatch(task, limit_s=...)` 完成 ——
任务跑满 `limit_s` 仍未完成则被放回就绪队列尾（`task.preempt_count += 1`）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.models import SCHEDULER_LABELS, Task  # noqa: E402
from uav_sim.scheduler.base import BaseScheduler  # noqa: E402


class RRScheduler(BaseScheduler):
    """固定时间片轮转调度器（契约 §4.7）。"""

    name: str = "rr"
    label: str = SCHEDULER_LABELS["rr"]
    link_aware: bool = False
    #: 轮转语义：时间片耗尽后必须排到队尾，否则就退化成"同一任务一直跑"
    preempt_requeue: str = "tail"

    def __init__(self, sim, cfg: dict) -> None:
        super().__init__(sim, cfg)
        #: 固定时间片（毫秒，仿真时间口径）
        self.time_slice_ms: float = float(self.cfg_get("rr.time_slice_ms", 500.0) or 500.0)
        self.step_budget: int = max(1, int(self.cfg_get("runtime.worker_threads", 8) or 8))
        # 契约 §4.7 要求 get_stats() 暴露当前时间片
        self._high_slice_ms = self.time_slice_ms
        self._low_slice_ms = self.time_slice_ms

    def step(self) -> int:
        """按全局到达顺序取任务，各给一个固定时间片。"""
        now = self.now()
        dispatched = 0
        for _ in range(self.step_budget):
            task: Optional[Task] = self._take_global_fifo(now)
            if task is None:
                break
            rc = self._admit(task)
            if rc == "OK":
                # 关键：把时间片交给 Simulator -> 到点抢占（真实上下文切换）
                if self.dispatch_admitted(task, now, slice_ms=self.time_slice_ms):
                    dispatched += 1
                continue
            if rc == "REJECT_LARGE":
                self._split_and_enqueue(task, now)
                continue
            self.push_front(task, now)
            break
        return dispatched

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["high_time_slice_ms"] = float(self.time_slice_ms)
        stats["low_time_slice_ms"] = float(self.time_slice_ms)
        return stats
