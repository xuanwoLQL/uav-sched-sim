"""
mlfq.py —— 多级反馈队列调度器（经典基线 4，缓解饥饿的中间方案）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 给"被大任务挡住的小任务"一条制度化的上升通道：等太久就自动提权，不再纯靠人工配置优先级 |
| OS 原理层   | **多级反馈队列（MLFQ）**：任务按层级排队，层级决定时间片；低层任务等待过久 -> **aging 升权**（老化提权），防止饿死 |
| 算法设计层 | 两级反馈队列 + 分层差异化时间片（上层短时间片快周转、下层长时间片少切换）+ aging 升权通道 |
| 代码实现层 | `MLFQScheduler.step()` |

教学要点 —— MLFQ 是"比静态优先级好、但不如动态权重"的对照组：

    * 它靠 **aging 升权** 确实能让低优先级任务**最终**得到服务（饥饿率从"永久饿死"
      降到"时延仍然很长"），这是相对 `priority` 的核心改进；
    * 但它的参数（升权阈值、各层时间片）**全部写死**，不感知链路质量和系统负载，
      所以弱网任务照样反复重传抢槽位，过载时队列照样堆积；
    * 在 PEAK 工况下，动态权重策略会把资源**主动**倾斜给拥堵队列并给弱网任务降权，
      MLFQ 做不到 —— 这就是实验四里两者拉开差距的地方。

实现简化（诚实说明，写进报告）：
    本项目物理上只有高/低两个就绪队列，因此 MLFQ 按"业务优先级 = 初始层级"落地：
        level-0（HIGH 队列，短时间片）-> 未完成放回队尾（等价于反馈降级排队）；
        level-1（LOW 队列，长时间片）-> 等待超过 aging 阈值时经升权通道优先调度。
    这保留了 MLFQ 的三个本质特征：**分层时间片、反馈重排、aging 防饿**。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.models import SCHEDULER_LABELS, Task, TaskPriority  # noqa: E402
from uav_sim.scheduler.base import BaseScheduler  # noqa: E402


class MLFQScheduler(BaseScheduler):
    """多级反馈队列调度器（契约 §4.7）。"""

    name: str = "mlfq"
    label: str = SCHEDULER_LABELS["mlfq"]
    link_aware: bool = False
    #: 轮转语义：时间片耗尽后必须排到队尾，否则就退化成"同一任务一直跑"
    preempt_requeue: str = "tail"

    def __init__(self, sim, cfg: dict) -> None:
        super().__init__(sim, cfg)
        #: level-0（高优先级队列）时间片：短，保证快周转
        self.slice_level0_ms: float = float(
            self.cfg_get("mlfq.slice_level0_ms", 300.0) or 300.0)
        #: level-1（低优先级队列）时间片：长，减少切换开销
        self.slice_level1_ms: float = float(
            self.cfg_get("mlfq.slice_level1_ms", 800.0) or 800.0)
        #: aging 升权阈值（毫秒，仿真时间）：低队列等待超过它就被提权调度
        self.aging_ms: float = float(self.cfg_get("mlfq.aging_ms", 1500.0) or 1500.0)

        self.step_budget: int = max(1, int(self.cfg_get("runtime.worker_threads", 8) or 8))
        self.promotions: int = 0   # aging 升权次数（教学量：MLFQ 的防饿机制在工作）

        self._high_slice_ms = self.slice_level0_ms
        self._low_slice_ms = self.slice_level1_ms

    def step(self) -> int:
        """先走 aging 升权通道，再按层级调度。"""
        now = self.now()
        dispatched = 0

        for _ in range(self.step_budget):
            # ---------- ① aging 升权通道（MLFQ 防饿死的核心） ----------
            rescued: Optional[Task] = self.queue.dequeue_starving(now, self.aging_ms) \
                if self.queue is not None else None
            if rescued is not None:
                rc = self._admit(rescued)
                if rc == "OK":
                    if self.dispatch_admitted(rescued, now,
                                              slice_ms=self.slice_level1_ms):
                        dispatched += 1
                        self.promotions += 1
                    continue
                if rc == "REJECT_LARGE":
                    self._split_and_enqueue(rescued, now)
                    continue
                # 槽位满 -> 放回，本轮结束（等下一次调度）
                self.push_front(rescued, now)
                break

            # ---------- ② 分层调度：先 level-0，再 level-1 ----------
            task: Optional[Task] = self._pop(TaskPriority.HIGH, now)
            slice_ms = self.slice_level0_ms
            if task is None:
                task = self._pop(TaskPriority.LOW, now)
                slice_ms = self.slice_level1_ms
            if task is None:
                break

            rc = self._admit(task)
            if rc == "OK":
                if self.dispatch_admitted(task, now, slice_ms=slice_ms):
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
        stats["high_time_slice_ms"] = float(self.slice_level0_ms)
        stats["low_time_slice_ms"] = float(self.slice_level1_ms)
        stats["promotions"] = int(self.promotions)
        return stats
