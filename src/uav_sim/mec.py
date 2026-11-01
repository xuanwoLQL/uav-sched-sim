"""
mec.py —— MEC 边缘计算（老师改造建议第五章 B）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 勘查现场的数据不是"一股脑全回中心"：高频轻量的运维数据（GPS/姿态/心跳/告警）就地在小基站（边缘）处理掉，只有重勘查数据（影像/多光谱/LiDAR点云）才回传到中心云做重算 |
| OS 原理层   | **计算卸载（computation offloading）+ 两级存储层次**：边缘 = 靠近 CPU 的 Cache/本地内存（快、小、近），中心 = 远端大容量主存（慢、大、远）；"哪些数据放近端处理"= 数据放置策略；**通信开销 vs 计算开销的权衡** = 卸载决策的本质 |
| 算法设计层 | 任务分级（轻量化 / 重载化）→ 决定落边缘还是回中心 → 端到端时延 = 接入时延 + 计算时延 + 回传时延 |
| 代码实现层 | `MecConfig` / `MecController`（分级、落点、端到端时延估计、分流统计） |

--------------------------------------------------------------------------
核心模型（"通信开销 vs 计算开销"的权衡，报告要用）
--------------------------------------------------------------------------
一条任务的端到端时延（仿真毫秒）::

    T_e2e = 接入时延 access_ms(落点)                # 数据从无人机到站
          + 计算时延 data_mb / compute_mbps()×1000   # 在该站算多久
          + 回传时延 sync_ratio×data_mb / backhaul_mbps×1000  # 只有"要回中心入库"的部分才付

边缘的优势：``edge_access_ms < center_access_ms``，且边缘算力足够处理轻量任务；
中心的优势：``center_compute_mbps >> edge_compute_mbps``，重任务回中心更快。

于是存在一个**最优点**：轻量任务在边缘算（省接入 + 省回传），重任务回中心
（省计算），这正是"端-边-云"要证明的结论。把 ``edge_compute_mbps`` 调小或
``backhaul_mbps`` 调小，边缘就失去优势 —— 这就是可复现的权衡实验。

**默认关闭（enable=false）不改变任何调度行为**：此时所有任务按原逻辑落
中心站，MEC 指标退化为"纯中心"口径，既有 15 组结果不受影响。
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.models import Task, TaskType

logger = logging.getLogger(__name__)

#: 任务分级：轻量运维任务（LIGHT）/ 重勘查任务（HEAVY）
LIGHT: str = "LIGHT"
HEAVY: str = "HEAVY"

#: 默认分级表（可被 config ``mec.edge_task_types`` / ``center_task_types`` 覆盖）
_DEFAULT_EDGE_TYPES: Set[str] = {"GPS", "ATTITUDE", "HEARTBEAT", "ALARM"}
_DEFAULT_CENTER_TYPES: Set[str] = {"IMAGERY", "MULTISPECTRAL", "LIDAR"}


def classify_task(task_type, cfg: Optional[dict] = None) -> str:
    """**任务分级**（模块级纯函数）：返回 ``"LIGHT"``（下沉边缘）或 ``"HEAVY"``（回中心）。

    默认清单与 ``config.yaml`` 的 ``mec.edge_task_types`` / ``mec.center_task_types``
    一致（口径单一来源）；传入 ``cfg`` 时以配置为准。
    未知类型按 HEAVY 处理（保守：宁可回中心，也不在边缘算错）。
    """
    name = task_type.value if isinstance(task_type, TaskType) else str(task_type).upper()
    edge_types = _DEFAULT_EDGE_TYPES
    center_types = _DEFAULT_CENTER_TYPES
    if isinstance(cfg, dict):
        mec = cfg.get("mec") or {}
        if mec.get("edge_task_types"):
            edge_types = {str(x).upper() for x in mec["edge_task_types"]}
        if mec.get("center_task_types"):
            center_types = {str(x).upper() for x in mec["center_task_types"]}
    if name in edge_types:
        return LIGHT
    if name in center_types:
        return HEAVY
    return HEAVY


def desired_role_for(task_type, cfg: Optional[dict] = None) -> str:
    """该任务**希望**落到的站点角色：轻量 -> ``EDGE``，重载 -> ``CENTER``。"""
    return "EDGE" if classify_task(task_type, cfg) == LIGHT else "CENTER"


@dataclass
class MecConfig:
    """MEC 参数（全部来自 ``config.yaml`` 的 ``mec`` 节，可运行期调）。"""

    enable: bool = False
    edge_compute_mbps: float = 120.0
    center_compute_mbps: float = 400.0
    backhaul_mbps: float = 60.0
    sync_ratio: float = 0.15
    edge_access_ms: float = 18.0
    center_access_ms: float = 46.0
    edge_task_types: tuple = tuple(sorted(_DEFAULT_EDGE_TYPES))
    center_task_types: tuple = tuple(sorted(_DEFAULT_CENTER_TYPES))

    @classmethod
    def from_config(cls, cfg: dict) -> "MecConfig":
        mec = (cfg or {}).get("mec") or {}
        edge_types = mec.get("edge_task_types") or sorted(_DEFAULT_EDGE_TYPES)
        center_types = mec.get("center_task_types") or sorted(_DEFAULT_CENTER_TYPES)
        return cls(
            enable=bool(mec.get("enable", False)),
            edge_compute_mbps=float(mec.get("edge_compute_mbps", 120.0)),
            center_compute_mbps=float(mec.get("center_compute_mbps", 400.0)),
            backhaul_mbps=float(mec.get("backhaul_mbps", 60.0)),
            sync_ratio=float(mec.get("sync_ratio", 0.15)),
            edge_access_ms=float(mec.get("edge_access_ms", 18.0)),
            center_access_ms=float(mec.get("center_access_ms", 46.0)),
            edge_task_types=tuple(str(x).upper() for x in edge_types),
            center_task_types=tuple(str(x).upper() for x in center_types),
        )


class MecController:
    """MEC 卸载控制器：任务分级、落点决策、端到端时延估计、分流统计。

    与调度器解耦：它只回答"这条任务属于哪一级、该落哪类站、端到端大概多久"，
    真正的派发仍由调度策略决定（MEC 通过 ``should_offload`` 把"轻量任务优先
    下沉边缘"作为站选择的一路输入，但绝不替代跨节点负载均衡）。
    """

    def __init__(self, cfg: dict, station_roles: Optional[Dict[int, str]] = None) -> None:
        self.cfg_obj = MecConfig.from_config(cfg)
        #: 站点 id -> 角色（CENTER/EDGE），由 Simulator 注入
        self.station_roles: Dict[int, str] = dict(station_roles or {})
        #: 运行期浮动参数（可在 /api/start 或实验里覆盖，实现"可调"）
        self.edge_compute_mbps: float = self.cfg_obj.edge_compute_mbps
        self.backhaul_mbps: float = self.cfg_obj.backhaul_mbps

        # ---- 分流统计 ----
        self.edge_tasks: int = 0
        self.center_tasks: int = 0
        self.backhaul_used_mb: float = 0.0
        self._edge_e2e_ms_sum: float = 0.0
        self._center_e2e_ms_sum: float = 0.0
        self._n_edge_e2e: int = 0
        self._n_center_e2e: int = 0
        # ---- 反事实对照（同一批任务全下沉边缘 / 全回中心的时延） ----
        self._if_edge_sum: float = 0.0
        self._if_center_sum: float = 0.0
        self._if_n: int = 0
        # 按任务类别拆分（LIGHT / HEAVY）的反事实累计
        self._light_edge_sum: float = 0.0
        self._light_center_sum: float = 0.0
        self._light_n: int = 0
        self._heavy_edge_sum: float = 0.0
        self._heavy_center_sum: float = 0.0
        self._heavy_n: int = 0

    # ------------------------------------------------------------------ 基本
    @property
    def enabled(self) -> bool:
        return bool(self.cfg_obj.enable)

    def set_topology(self, station_roles: Dict[int, str]) -> None:
        """注入站点角色表（多站拓扑建立后调用）。"""
        self.station_roles = dict(station_roles)

    def tune(self, edge_compute_mbps: Optional[float] = None,
             backhaul_mbps: Optional[float] = None) -> None:
        """运行期调整边缘算力 / 回传带宽（权衡实验的"可调参数"落点）。"""
        if edge_compute_mbps is not None:
            self.edge_compute_mbps = max(1e-6, float(edge_compute_mbps))
        if backhaul_mbps is not None:
            self.backhaul_mbps = max(1e-6, float(backhaul_mbps))

    def edge_stations(self) -> List[int]:
        return sorted(sid for sid, role in self.station_roles.items()
                      if str(role).upper() == "EDGE")

    def center_stations(self) -> List[int]:
        return sorted(sid for sid, role in self.station_roles.items()
                      if str(role).upper() == "CENTER")

    # ------------------------------------------------------------------ 分级
    def task_class(self, task_type: TaskType | str) -> str:
        """任务分级：轻量运维（LIGHT）/ 重勘查（HEAVY）。"""
        name = task_type.value if isinstance(task_type, TaskType) else str(task_type).upper()
        if name in self.cfg_obj.edge_task_types:
            return LIGHT
        if name in self.cfg_obj.center_task_types:
            return HEAVY
        # 未知类型按"重勘查"处理（保守：宁可回中心，也不在边缘算错）
        return HEAVY

    def task_class_of(self, task: Task) -> str:
        return self.task_class(task.task_type)

    def desired_role(self, task: Task) -> str:
        """任务**希望**落到的站点角色：轻量→EDGE，重载→CENTER。

        注意：这只是一个"倾向"，多站负载均衡策略可以因过载而把它放到别处
        （否则边缘被打爆时会违背"负载均衡"的初衷）。
        """
        return "EDGE" if self.task_class_of(task) == LIGHT else "CENTER"

    def wants_edge(self, task: Task) -> bool:
        """MEC 是否建议该任务下沉边缘（供 cross_node 策略作为选站偏好）。"""
        return self.enabled and self.task_class_of(task) == LIGHT

    # ------------------------------------------------------------------ 时延
    def compute_mbps(self, station_role: str) -> float:
        """该角色站点的"计算速率"（MB/s）：边缘可调，中心固定。"""
        return (self.edge_compute_mbps if str(station_role).upper() == "EDGE"
                else self.cfg_obj.center_compute_mbps)

    def access_ms(self, station_role: str) -> float:
        return (self.cfg_obj.edge_access_ms if str(station_role).upper() == "EDGE"
                else self.cfg_obj.center_access_ms)

    def e2e_ms(self, data_size_mb: float, station_role: str) -> float:
        """端到端时延估计（仿真毫秒）= 接入 + 计算 + 回传。

        这是报告里"通信开销 vs 计算开销"权衡的直接量化：
        边缘算力↓ 或 回传带宽↓ 时，边缘的 e2e 会迅速变差并超过中心。
        """
        data = max(0.0, float(data_size_mb))
        role = str(station_role).upper()
        access = self.access_ms(role)
        compute = data / max(1e-9, self.compute_mbps(role)) * 1000.0
        sync_mb = data * max(0.0, float(self.cfg_obj.sync_ratio))
        backhaul = sync_mb / max(1e-9, self.backhaul_mbps) * 1000.0
        return float(access + compute + backhaul)

    # ------------------------------------------------------------------ 记账
    def note_dispatch(self, task: Task, station_role: str) -> float:
        """记录一次派发：累计分流统计与端到端时延，返回本次 e2e(ms)。

        **每个任务只计一次**（用 ``task._mec_counted`` 打标）：任务被抢占/重传后
        会再次派发，若每次都计数，"边缘分流比例"会变成"按派发次数加权"，
        与"有多少任务下沉到了边缘"这个业务含义不符。
        """
        role = str(station_role).upper()
        e2e = self.e2e_ms(task.data_size_mb, role)
        if getattr(task, "_mec_counted", False):
            return e2e
        task._mec_counted = True

        # ---- 反事实对照：**同一批任务**如果全下沉边缘 / 全回中心，时延各是多少 ----
        # 为什么需要它：直接比较"边缘任务的平均 e2e"与"中心任务的平均 e2e"会把
        # "任务大小差异"和"落点差异"混在一起（重勘查任务本来就大）。用同一批任务
        # 算两个反事实值，才是干净的「通信开销 vs 计算开销」权衡曲线。
        data = float(task.data_size_mb)
        self._if_edge_sum += self.e2e_ms(data, "EDGE")
        self._if_center_sum += self.e2e_ms(data, "CENTER")
        self._if_n += 1
        # ---- 按类别拆分的反事实（这才是"该不该下沉"的干净判据） ----
        # 汇总所有任务会把 900MB 的重勘查任务与 1MB 的心跳混在一起，平均下来
        # 永远是"中心更快"，看不出卸载价值。按类别拆开后才能看到：
        # **小数据量的轻量任务下沉边缘更划算，大数据量任务回中心更划算** ——
        # 两者相等的数据量就是"卸载决策的分水岭"（见 crossing_data_mb()）。
        if self.task_class_of(task) == LIGHT:
            self._light_edge_sum += self.e2e_ms(data, "EDGE")
            self._light_center_sum += self.e2e_ms(data, "CENTER")
            self._light_n += 1
        else:
            self._heavy_edge_sum += self.e2e_ms(data, "EDGE")
            self._heavy_center_sum += self.e2e_ms(data, "CENTER")
            self._heavy_n += 1

        if role == "EDGE":
            self.edge_tasks += 1
            self._edge_e2e_ms_sum += e2e
            self._n_edge_e2e += 1
        else:
            self.center_tasks += 1
            self._center_e2e_ms_sum += e2e
            self._n_center_e2e += 1
        self.backhaul_used_mb += data * max(0.0, self.cfg_obj.sync_ratio)
        return e2e

    # ------------------------------------------------------------------ 分水岭
    def crossing_data_mb(self) -> float:
        """**卸载分水岭**：数据量小于它时下沉边缘更快，大于它时回中心更快。

        解 ``edge_access + d/edge_compute + d·sync/backhaul
              = center_access + d/center_compute`` 得::

            d* = (center_access - edge_access) / (1/edge_compute + sync/backhaul - 1/center_compute)

        分母 ≤ 0 时边缘永远不划算（无正分水岭，返回 0）。
        这个 d* 是报告里"通信开销 vs 计算开销"最凝练的一句话：
        调大边缘算力或回传带宽，d* 就变大，边缘能接住的业务范围随之扩大。
        """
        a_e, a_c = self.access_ms("EDGE"), self.access_ms("CENTER")
        inv_edge = 1.0 / max(1e-9, self.edge_compute_mbps)
        inv_center = 1.0 / max(1e-9, self.cfg_obj.center_compute_mbps)
        inv_bh = max(0.0, float(self.cfg_obj.sync_ratio)) / max(1e-9, self.backhaul_mbps)
        denom = inv_edge + inv_bh - inv_center
        if denom <= 1e-12:
            return 0.0
        # access 与 compute 的单位不同（ms vs MB/s），统一到 ms/MB：
        #   access 差(ms) / (ms per MB) = MB
        return max(0.0, (a_c - a_e) / (denom * 1000.0))

    # ------------------------------------------------------------------ 快照
    def snapshot(self) -> Dict[str, Any]:
        """``/api/state.live.mec`` 快照（契约键名不可改）。"""
        total = self.edge_tasks + self.center_tasks
        offload = (self.edge_tasks / total) if total > 0 else 0.0
        avg_edge = (self._edge_e2e_ms_sum / self._n_edge_e2e) if self._n_edge_e2e else 0.0
        avg_center = (self._center_e2e_ms_sum / self._n_center_e2e) if self._n_center_e2e else 0.0
        sample: Dict[str, str] = {}
        for t in TaskType:
            sample[t.value] = self.task_class(t)
        return {
            "enabled": self.enabled,
            "edge_tasks": int(self.edge_tasks),
            "center_tasks": int(self.center_tasks),
            "backhaul_used_mbps": round(float(self.backhaul_used_mb), 3),
            "edge_offload_ratio": round(float(offload), 4),
            "avg_e2e_edge_ms": round(float(avg_edge), 2),
            "avg_e2e_center_ms": round(float(avg_center), 2),
            "avg_e2e_ms": round(float(
                (self._edge_e2e_ms_sum + self._center_e2e_ms_sum)
                / max(1, self._n_edge_e2e + self._n_center_e2e)), 2),
            # ---- 反事实对照：同一批任务"全下沉边缘"vs"全回中心"的端到端时延 ----
            # 两者的差就是"通信开销 vs 计算开销"的净效应（越接近 0 越说明落点不敏感）
            "e2e_if_edge_ms": round(self._if_edge_sum / max(1, self._if_n), 2),
            "e2e_if_center_ms": round(self._if_center_sum / max(1, self._if_n), 2),
            "e2e_light_if_edge_ms": round(self._light_edge_sum / max(1, self._light_n), 2),
            "e2e_light_if_center_ms": round(self._light_center_sum / max(1, self._light_n), 2),
            "e2e_heavy_if_edge_ms": round(self._heavy_edge_sum / max(1, self._heavy_n), 2),
            "e2e_heavy_if_center_ms": round(self._heavy_center_sum / max(1, self._heavy_n), 2),
            "crossing_data_mb": round(self.crossing_data_mb(), 3),
            # 分级计数：报告必须用它交代"边缘分流比例"的分母构成
            "light_tasks": int(self._light_n),
            "heavy_tasks": int(self._heavy_n),
            "counted_tasks": int(self._if_n),
            "edge_compute_mbps": round(float(self.edge_compute_mbps), 2),
            "backhaul_mbps": round(float(self.backhaul_mbps), 2),
            "task_class": sample,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<MecController enable={self.enabled} edge={self.edge_tasks} "
                f"center={self.center_tasks} edge_compute={self.edge_compute_mbps}>")
