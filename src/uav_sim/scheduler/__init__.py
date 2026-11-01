"""
scheduler 包 —— 五种调度策略的注册表
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 地面站「调度策略可切换」：同一批集群任务，换一种调度规则就换一种资源分配结果 |
| OS 原理层   | CPU 调度算法族：FCFS / RR / 静态优先级 / 多级反馈队列 / 动态优先级 |
| 算法设计层 | 五种策略统一接口 `step()`，便于**控制变量**横向对照 |
| 代码实现层 | `SCHEDULERS` 注册表 + `create_scheduler()` 工厂 |

注册名必须与 `models.SCHEDULER_NAMES` 完全一致（顺序即报告表格/图例顺序）：
    ("fcfs", "rr", "priority", "mlfq", "dynamic")
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Type

_SRC_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.models import SCHEDULER_NAMES  # noqa: E402
from uav_sim.scheduler.base import BaseScheduler  # noqa: E402
from uav_sim.scheduler.cross_node import CrossNodeScheduler  # noqa: E402
from uav_sim.scheduler.dynamic import DynamicWeightScheduler  # noqa: E402
from uav_sim.scheduler.fcfs import FCFSScheduler  # noqa: E402
from uav_sim.scheduler.mlfq import MLFQScheduler  # noqa: E402
from uav_sim.scheduler.priority import PriorityScheduler  # noqa: E402
from uav_sim.scheduler.rr import RRScheduler  # noqa: E402

#: 策略注册表：注册名 -> 调度器类
SCHEDULERS: Dict[str, Type[BaseScheduler]] = {
    "fcfs": FCFSScheduler,
    "rr": RRScheduler,
    "priority": PriorityScheduler,
    "mlfq": MLFQScheduler,
    "dynamic": DynamicWeightScheduler,
    "cross_node": CrossNodeScheduler,
}

#: 防御性校验：注册表必须覆盖 models.SCHEDULER_NAMES 的全部策略
_missing = [n for n in SCHEDULER_NAMES if n not in SCHEDULERS]
if _missing:  # pragma: no cover - 开发期立即暴露
    raise RuntimeError(f"调度器注册不全，缺少：{_missing}")

__all__ = [
    "BaseScheduler",
    "FCFSScheduler",
    "RRScheduler",
    "PriorityScheduler",
    "MLFQScheduler",
    "DynamicWeightScheduler",
    "CrossNodeScheduler",
    "SCHEDULERS",
    "create_scheduler",
]


def create_scheduler(name: str, sim, cfg: dict) -> BaseScheduler:
    """按注册名实例化调度器（契约 §4.7）。

    参数：
        name: "fcfs" / "rr" / "priority" / "mlfq" / "dynamic"
        sim : Simulator 实例（调度器只通过它访问队列 / 限流器 / 链路 / 时钟）
        cfg : 全局配置
    """
    key = str(name).strip().lower()
    if key not in SCHEDULERS:
        raise KeyError(f"未知调度策略 {name!r}，可选：{', '.join(SCHEDULERS)}")
    return SCHEDULERS[key](sim, cfg)
