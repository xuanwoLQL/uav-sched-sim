"""
priority.py —— 静态优先级调度器（经典基线 3，也是"任务饥饿"的教科书样本）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 地勘行业现有系统的实际做法：勘查影像/点云标为高优先级，GPS/心跳/告警标为低优先级，**优先级写死** |
| OS 原理层   | **静态优先级调度（Strict Priority）**：只要高优先级队列非空，就绝不调度低优先级 —— 即"绝对优先"策略 |
| 算法设计层 | 两级队列严格优先出队，无 aging、无补偿、无时间片 |
| 代码实现层 | `PriorityScheduler.step()` |

教学要点 —— **本策略的存在意义就是复现 PDF 痛点②「任务饥饿」**：

    只要高优先级（勘查）任务的到达率 ≥ 系统服务率，低优先级（运维）队列就
    永远等不到 CPU。在 PEAK 工况（负载率 1.38）下这是必然发生的：
    地面站持续有勘查数据在排队 -> GPS/心跳/告警**永久得不到上传**
    -> 地面运维系统失去无人机状态感知 -> 业务上的"设备失联"风险。

量化证据（实验四会看到）：
    * `priority` 的「低优先级饥饿率」显著高于其它策略；
    * `priority` 的「低优先级平均等待时延」可能就是仿真总时长量级；
    * 但 `priority` 的「高优先级平均等待时延」往往**不是**最优 ——
      因为它没有限流/错峰，大任务照样互相堵（这说明"只加优先级"并不够，
      还需要重传抑制、动态时间片与过载稳流）。
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


class PriorityScheduler(BaseScheduler):
    """静态优先级调度器（契约 §4.7）。"""

    name: str = "priority"
    label: str = SCHEDULER_LABELS["priority"]
    #: 无链路感知：弱网任务反复重传照样抢占槽位（痛点①）
    link_aware: bool = False

    def __init__(self, sim, cfg: dict) -> None:
        super().__init__(sim, cfg)
        self.step_budget: int = max(1, int(self.cfg_get("runtime.worker_threads", 8) or 8))
        #: 低优先级被"饿死"的轮数（诊断指标：本轮一次都没轮到低优先级）
        self.low_starved_steps: int = 0

    def step(self) -> int:
        """严格优先：高优先级队列非空时，低优先级任务一轮都拿不到 CPU。"""
        now = self.now()
        dispatched = 0
        for _ in range(self.step_budget):
            task: Optional[Task] = self._pop(TaskPriority.HIGH, now)
            if task is None:
                task = self._pop(TaskPriority.LOW, now)
                if task is None:
                    break
            elif self.queue is not None and self.queue.low_depth > 0:
                # 记录"低优先级明明有任务却被高优先级挡住"的次数
                self.low_starved_steps += 1

            rc = self._admit(task)
            if rc == "OK":
                # 静态优先级 + 固定优先级，不给时间片（跑完为止）
                if self.dispatch_admitted(task, now):
                    dispatched += 1
                continue
            if rc == "REJECT_LARGE":
                self._split_and_enqueue(task, now)
                continue
            # THROTTLE / QUOTA -> 错峰，本轮结束
            self.push_front(task, now)
            break
        return dispatched

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["low_starved_steps"] = int(self.low_starved_steps)
        return stats
