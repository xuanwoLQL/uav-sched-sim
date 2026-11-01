"""
fcfs.py —— FCFS 先来先服务调度器（经典基线 1）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 地勘企业现有后台最朴素的调度方式：谁的数据先到就先传谁的，不看重要性 |
| OS 原理层   | **FCFS（First Come First Served）**：单一就绪队列，严格按到达顺序执行，**不区分优先级** |
| 算法设计层 | 跨两个物理队列做「全局到达顺序」比较，实现真正的单一 FIFO |
| 代码实现层 | `FCFSScheduler.step()` |

教学要点（要能在实验里量化出来）：
    1. **公平 ≠ 合理**：FCFS 对每个任务一视同仁，于是 0.5 MB 的心跳包和 2 GB 的点云
       排同一个队。一旦某个大任务排到队首，后面所有任务（包括告警）都要等它传完
       —— 这就是"护航效应（convoy effect）"。
    2. 低优先级运维任务**反而不会饥饿**（大家都是 FIFO），但它们的时延随大任务波动剧烈，
       表现为 P95 时延很高而均值不高 —— 这正好说明"饥饿率单一指标不足以刻画公平性"。
    3. 高优先级核心勘查任务的时延**没有保障**：无法体现业务重要性差异。
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


class FCFSScheduler(BaseScheduler):
    """先来先服务调度器（契约 §4.7）。"""

    name: str = "fcfs"
    label: str = SCHEDULER_LABELS["fcfs"]
    #: 无链路感知：弱网任务失败后原样重传，持续抢占槽位（复现痛点①）
    link_aware: bool = False

    def __init__(self, sim, cfg: dict) -> None:
        super().__init__(sim, cfg)
        #: 单次 step() 最多派发的任务数（= 可用资源单元数，避免一轮吃太多）
        self.step_budget: int = max(1, int(self.cfg_get("runtime.worker_threads", 8) or 8))

    def step(self) -> int:
        """每轮按「到达最早优先」派发，直到队列空 / 槽位满。"""
        now = self.now()
        dispatched = 0
        for _ in range(self.step_budget):
            task: Optional[Task] = self._take_global_fifo(now)
            if task is None:
                break
            rc = self._admit(task)
            if rc == "OK":
                if self.dispatch_admitted(task, now):
                    dispatched += 1
                continue
            if rc == "REJECT_LARGE":
                # 超大任务：拒绝 + 切分（保持业务完整率）
                self._split_and_enqueue(task, now)
                continue
            # THROTTLE（并发满）/ QUOTA（单机配额满）
            # -> 错峰：原样放回队首，不改变排队顺序、不计 requeue
            self.push_front(task, now)
            break
        return dispatched
