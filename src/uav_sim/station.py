"""
station.py —— 多基站 / 多服务器节点（老师改造建议第五章 A）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 野外勘查不是"一台服务器管 30 架机"：矿区会布置**多个地面站**（1 个中心云站 + 若干边缘站），无人机按位置/信号接入最近的一个，各站各自有处理通道与缓存队列 |
| OS 原理层   | 从**单核（单机调度）**演进到**多处理器（多节点调度）**：每站 = 一个独立的 CPU 资源域（自己的 worker 数 = 核数、自己的就绪队列、自己的并发限流器）；"任务该放哪个站"= 多处理器负载均衡；"任务从 A 站搬到 B 站"= **进程迁移（process migration）**，必须付出代价 |
| 算法设计层 | 站级资源域抽象 + 两种选站算法（**最少连接**、**一致性哈希**）+ **带代价的任务迁移**（迁移耗时 + 带宽损耗）+ 站级指标 |
| 代码实现层 | `StationSpec` / `StationNode` / `ClusterQueue` / `ClusterLimiter` / `StationCluster` |

--------------------------------------------------------------------------
为什么要有 `ClusterQueue` / `ClusterLimiter` 这两个"门面"
--------------------------------------------------------------------------
多站改造最大的风险是**打坏既有 5 个策略和 62 个测试**。它们的代码里到处是
``sim.queue.xxx()`` / ``sim.limiter.xxx()``（见 ``scheduler/base.py`` 的属性）。

因此本模块不要求上层改调用点，而是提供两个**与单站接口完全兼容**的门面：

    * ``ClusterQueue``   —— 暴露 ``MultiLevelQueue`` 的全部方法/属性，
      按 ``task.station_id`` 把入队/重传/回队首**路由到对应站**；
      出队时在所有站之间取"到达最早"的那个（多处理器下的全局 FIFO）。
    * ``ClusterLimiter`` —— 暴露 ``ResourceLimiter`` 的全部方法/属性，
      按 ``task.station_id`` 路由准入/释放/分片。

关键性质（本改造的控制变量硬约束）：**只有 1 个站时，两个门面的每一个返回值
都与直接使用单站对象逐位相同** —— 既有 15 组实验结论不受任何影响。

--------------------------------------------------------------------------
任务迁移为什么要"有代价"（不是瞬移）
--------------------------------------------------------------------------
真实系统里跨节点迁移一个未完成任务，至少要付两笔钱：

    ① **在途耗时**：数据/上下文要经过回传链路搬到目标站，这段时间任务
       既不在源站队列、也不在目标站队列，**不可被派发**（不能"两边同时占坑"）；
    ② **带宽损耗**：跨节点回传可能重传已接收部分，剩余服务时长被放大。

本实现把 ① 建模为 ``in_transit`` 在途表（到期后由 ``flush_migrations()`` 投入
目标站队列），把 ② 建模为 ``service_s ×= (1 + migration_bw_penalty)``。
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.config import cfg_get_float, cfg_get_int
from uav_sim.models import QueueSnapshot, Task, TaskPriority
from uav_sim.queue_manager import MultiLevelQueue
from uav_sim.resource_limiter import ResourceLimiter

logger = logging.getLogger(__name__)

#: 节点角色：中心（云）站 / 边缘站
CENTER: str = "CENTER"
EDGE: str = "EDGE"
STATION_ROLES: Tuple[str, ...] = (CENTER, EDGE)

#: 站配色（后端给定，保证 3D 场景里的"站色"与前端图例一致）
STATION_COLORS: Tuple[str, ...] = ("#4ea1ff", "#ffb454", "#59d999", "#c792ea", "#ff7a8a")

EPS = 1e-9


# ===========================================================================
# 站点规格
# ===========================================================================
@dataclass(frozen=True)
class StationSpec:
    """一个服务器节点（基站）的静态规格 —— 对应 OS 里"一个 CPU 资源域"。"""

    id: int
    name: str
    role: str = CENTER
    x: float = 0.0           # 3D 世界坐标（地图平面）
    z: float = 0.0
    workers: int = 8         # 该站的并发处理通道数（= 核数）
    bandwidth_mbps: float = 150.0    # 该站单通道上行带宽
    backhaul_mbps: float = 200.0     # 该站回传（边缘->中心）带宽
    edge_compute_mbps: float = 0.0   # 边缘算力（CENTER 站为 0，表示不做边缘卸载）
    #: 该站并发上限（限流器许可数）。None = 取 ``workers``。
    max_concurrent: Optional[int] = None

    @property
    def is_edge(self) -> bool:
        return str(self.role).upper() == EDGE

    @property
    def concurrency(self) -> int:
        """限流器并发上限（未显式指定时等于 workers）。"""
        return int(self.max_concurrent if self.max_concurrent else self.workers)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": int(self.id), "name": str(self.name),
            "role": str(self.role).upper(), "x": float(self.x), "z": float(self.z),
            "workers": int(self.workers), "bandwidth_mbps": float(self.bandwidth_mbps),
            "backhaul_mbps": float(self.backhaul_mbps),
            "edge_compute_mbps": float(self.edge_compute_mbps),
        }


def _spec_from_entry(entry: Dict[str, Any], idx: int) -> StationSpec:
    """把 config.yaml 里的一条站点字典变成 ``StationSpec``（容错解析）。"""
    role = str(entry.get("role", CENTER)).upper()
    if role not in STATION_ROLES:
        role = CENTER
    return StationSpec(
        id=int(entry.get("id", idx + 1)),
        name=str(entry.get("name", f"站点{idx + 1}")),
        role=role,
        x=float(entry.get("x", 0.0)),
        z=float(entry.get("z", 0.0)),
        workers=max(1, int(entry.get("workers", 8))),
        bandwidth_mbps=float(entry.get("bandwidth_mbps", 150.0)),
        backhaul_mbps=float(entry.get("backhaul_mbps", 200.0)),
        edge_compute_mbps=float(entry.get("edge_compute_mbps", 0.0)),
        max_concurrent=(int(entry["max_concurrent"])
                        if entry.get("max_concurrent") else None),
    )


def _synth_specs(count: int, cfg: dict) -> List[StationSpec]:
    """按"总容量守恒"合成 ``count`` 个站（1 个 CENTER + 若干 EDGE）。

    容量守恒很关键：多站实验若总并发与单站不同，就无法判断指标变化来自
    "跨节点均衡"还是"机器变多了"。这里把 ``runtime.worker_threads`` 平均切分。
    """
    total = max(1, cfg_get_int(cfg, "runtime.worker_threads", 8))
    n = max(2, int(count))
    bw = cfg_get_float(cfg, "runtime.worker_bandwidth_mbps", 150.0)
    center_w = max(1, total - (n - 1))
    specs = [StationSpec(id=1, name="中心站A", role=CENTER, x=0.0, z=0.0,
                         workers=center_w, bandwidth_mbps=bw, backhaul_mbps=bw * 1.3)]
    radius = 70.0
    for i in range(2, n + 1):
        specs.append(StationSpec(
            id=i, name=f"边缘站{chr(64 + i)}", role=EDGE,
            x=round(radius * (1.0 if i % 2 else -1), 1),
            z=round(radius * 0.6 * (1.0 if i % 3 else -1), 1),
            workers=1, bandwidth_mbps=bw * 0.6, backhaul_mbps=bw * 0.4,
            edge_compute_mbps=120.0,
        ))
    return specs


def resolve_station_specs(cfg: dict, spec: Optional[Any] = None) -> List[StationSpec]:
    """把 ``station_spec`` 参数解析成站点规格列表。

    参数可以是：

    * ``None``            -> 取 ``cfg["stations"]["nodes"]``（默认 = 单个中心站）
    * ``int``             -> 站数（1 = 单站；>1 = 自动合成拓扑）
    * ``str``             -> ``cfg["stations"]["topologies"]`` 里的拓扑名（如 ``"triple"``）
    * ``list[dict]``      -> 显式站点列表

    **单站模式的容量一律以 ``runtime.*`` / ``server.max_concurrent`` 为准**
    （而不是读 ``stations.nodes`` 里的字面值）：这样只要保持默认配置，
    多站改造对既有 15 组结果就是"逐位零影响"，符合控制变量要求。
    """
    stations_cfg = (cfg or {}).get("stations") or {}
    raw: Optional[List[dict]] = None
    if spec is None:
        nodes = stations_cfg.get("nodes")
        if isinstance(nodes, list) and nodes:
            raw = [dict(x) for x in nodes if isinstance(x, dict)]
    elif isinstance(spec, bool):
        raw = None
    elif isinstance(spec, int):
        if int(spec) <= 1:
            raw = None
        else:
            topo = stations_cfg.get("topologies") or {}
            if int(spec) == 3 and isinstance(topo.get("triple"), list):
                return [_spec_from_entry(dict(e), i)
                        for i, e in enumerate(topo["triple"]) if isinstance(e, dict)]
            return _synth_specs(int(spec), cfg)
    elif isinstance(spec, str):
        name = spec.strip()
        if name.isdigit():
            return resolve_station_specs(cfg, int(name))
        topo = stations_cfg.get("topologies") or {}
        entries = topo.get(name)
        if isinstance(entries, list) and entries:
            return [_spec_from_entry(dict(e), i)
                    for i, e in enumerate(entries) if isinstance(e, dict)]
        logger.warning("未知站点拓扑 %r，回退单站", name)
        raw = None
    elif isinstance(spec, (list, tuple)):
        return [_spec_from_entry(dict(e), i)
                for i, e in enumerate(spec) if isinstance(e, dict)]

    if not raw:
        # ---- 单站（默认）：容量完全由 runtime/server 决定，保证与改造前逐位一致 ----
        return [StationSpec(
            id=1, name="中心站A", role=CENTER, x=0.0, z=0.0,
            workers=max(1, cfg_get_int(cfg, "runtime.worker_threads", 8)),
            bandwidth_mbps=cfg_get_float(cfg, "runtime.worker_bandwidth_mbps", 150.0),
            backhaul_mbps=cfg_get_float(cfg, "runtime.worker_bandwidth_mbps", 150.0),
            edge_compute_mbps=0.0,
            max_concurrent=cfg_get_int(cfg, "server.max_concurrent", 0) or None,
        )]

    specs = [_spec_from_entry(e, i) for i, e in enumerate(raw)]
    if len(specs) == 1:
        # 单站：强制以 runtime/server 为准（见 docstring 的控制变量说明）
        s = specs[0]
        return [StationSpec(
            id=s.id, name=s.name, role=s.role, x=s.x, z=s.z,
            workers=max(1, cfg_get_int(cfg, "runtime.worker_threads", 8)),
            bandwidth_mbps=cfg_get_float(cfg, "runtime.worker_bandwidth_mbps", 150.0),
            backhaul_mbps=s.backhaul_mbps, edge_compute_mbps=s.edge_compute_mbps,
            max_concurrent=cfg_get_int(cfg, "server.max_concurrent", 0) or None,
        )]
    return specs


# ===========================================================================
# 单站资源域
# ===========================================================================
class StationNode:
    """一个服务器节点的**运行期**资源域：独立队列 + 独立限流器 + 独立线程池。

    OS 类比：多处理器系统里的一个 CPU（含自己的 runqueue）。因此这里的
    ``free_slots`` / ``slot_of`` 是**站内**编号（0..workers-1），
    3D 面板上显示为该站的通道号。
    """

    def __init__(self, spec: StationSpec, cfg: dict, color: str,
                 capacity: int, congestion_ref: float,
                 starvation_threshold_ms: float,
                 max_task_memory_mb: float, per_uav_quota: int,
                 enable_chunking: bool) -> None:
        self.spec = spec
        self.cfg = cfg or {}
        self.color = color
        self.load_horizon_s: float = 2.0

        # ---- 独立就绪队列（多级队列，与单站实现完全同一套类） ----
        self.queue = MultiLevelQueue(
            capacity=int(capacity),
            starvation_threshold_ms=float(starvation_threshold_ms),
            congestion_ref=float(congestion_ref),
        )
        # ---- 独立并发限流器（该站的处理通道数） ----
        self.limiter = ResourceLimiter(
            max_concurrent=spec.concurrency,
            max_task_memory_mb=float(max_task_memory_mb),
            per_uav_quota=int(per_uav_quota),
            enable_chunking=bool(enable_chunking),
        )

        # ---- 站内并发槽位（= 该站的"核号"） ----
        self.free_slots: List[int] = list(range(spec.concurrency))
        self.slot_of: Dict[str, int] = {}
        self._slots_lock = threading.Lock()

        self.executor: Optional[ThreadPoolExecutor] = None

        # ---- 站点可用性（混沌：宕机 / 网络分区 / 脑裂） ----
        self.online: bool = True
        #: 分区组号：0 = 未分区（全网可达）；>=1 = 该组内互通、跨组不可达。
        self.partition_group: int = 0
        self.down_until: float = 0.0
        self.crashed_at: float = 0.0

        # ---- 最小 quorum 共识（教学用 P4）：本站在当前 term 内的投票状态 ----
        #: 最近一次投票所属的任期号（与 ``StationCluster.term`` 对齐）。
        self.voted_term: int = 0
        #: 本任期是否投出赞成票。简化模型：新任期开始时**全站一致赞成**，
        #: 不做选举超时 / 拉票 / 日志比较（见 docs/11 §5 与 §8 诚实清单）。
        self.vote_granted: bool = True
        #: 所属簇的回引 —— 只为 ``to_dict()`` 计算 ``is_majority``（未挂簇时视为有多数派）。
        self.cluster: Optional["StationCluster"] = None

        # ---- 接入的无人机集合 ----
        self.uavs: set = set()

        # ---- 站级统计（改造建议第五章 A4） ----
        self.done: int = 0
        self.dispatched: int = 0
        self.migrations_in: int = 0
        self.migrations_out: int = 0
        self.migration_cost_ms_total: float = 0.0
        self.crash_rescheduled: int = 0

    # ------------------------------------------------------------------ 站内槽位
    def take_slot(self, task_id: str) -> int:
        with self._slots_lock:
            slot = self.free_slots.pop(0) if self.free_slots else -1
            self.slot_of[str(task_id)] = slot
            return slot

    def release_slot(self, task_id: str) -> int:
        with self._slots_lock:
            slot = self.slot_of.pop(str(task_id), -1)
            if slot >= 0:
                self.free_slots.append(slot)
                self.free_slots.sort()
            return slot

    @property
    def active(self) -> int:
        """当前占用该站通道的任务数（由限流器持有许可数给出）。"""
        return int(self.limiter.running)

    # ------------------------------------------------------------------ 负载
    def pending_service_s(self, extra_running_s: float = 0.0) -> float:
        """该站未完成的工作总量（仿真秒）= 本站队列需求 + 本站正在跑的剩余。"""
        return float(self.queue.total_remaining_s()) + max(0.0, float(extra_running_s))

    def load(self, extra_running_s: float = 0.0) -> float:
        """该站归一化负载：积压秒 / (本站核数 × 视界)。

        **口径与 ``Simulator.system_load()`` 完全一致**：``<1`` 表示按满负荷能在
        视界内排空，``>1`` 表示**过载倍数**（例如 32 表示积压需要 32 个视界才排空）。
        因此这个字段不是 0~1 的百分比，前端展示时应截断绘制、并保留真值文字。
        """
        return self.pending_service_s(extra_running_s) / max(
            1e-9, float(self.spec.workers) * max(1e-9, self.load_horizon_s))

    def to_dict(self, extra_running_s: float = 0.0,
                elapsed_s: float = 0.0) -> Dict[str, Any]:
        """站级快照（= ``/api/state.live.stations[]`` 的一条，契约键名不可改）。

        P4 只**增**两个键（不动既有键）：
            * ``partition_group``：当前分区组号（0 = 未分区）；
            * ``is_majority``：该站所在分区组是否达到多数派（未挂簇时视为 True，
              单站时 majority=1 -> True，与改造前等价）。
        """
        q = self.queue
        cluster = self.cluster
        return {
            "id": int(self.spec.id),
            "name": str(self.spec.name),
            "role": str(self.spec.role).upper(),
            "x": float(self.spec.x), "z": float(self.spec.z),
            "online": bool(self.online),
            "partitioned": bool(self.partition_group > 0),
            "partition_group": int(self.partition_group),
            # 该站所在分区组是否达到多数派（脑裂截图里"两侧各自是否自认多数"的读数）
            "is_majority": (bool(cluster.group_has_quorum(int(self.spec.id)))
                            if cluster is not None else True),
            "workers": int(self.spec.workers),
            "active": int(self.active),
            "idle": max(0, int(self.spec.concurrency) - int(self.active)),
            "queue_depth": int(q.total_depth),
            "queue_high": int(q.high_depth),
            "queue_low": int(q.low_depth),
            "load": round(float(self.load(extra_running_s)), 4),
            "uav_count": int(len(self.uavs)),
            "uavs": sorted(int(u) for u in self.uavs),
            "migrations_in": int(self.migrations_in),
            "migrations_out": int(self.migrations_out),
            "migration_cost_ms": round(float(self.migration_cost_ms_total), 2),
            "done": int(self.done),
            "throughput": round(float(self.done) / max(1e-9, float(elapsed_s)), 3),
            "edge_compute_mbps": float(self.spec.edge_compute_mbps),
            "backhaul_mbps": float(self.spec.backhaul_mbps),
            "color": str(self.color),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<StationNode {self.spec.id} {self.spec.name} {self.spec.role} "
                f"workers={self.spec.workers} online={self.online} "
                f"queue={self.queue.total_depth}>")


# ===========================================================================
# 门面一：跨站就绪队列
# ===========================================================================
class ClusterQueue:
    """跨站就绪队列门面 —— 暴露与 ``MultiLevelQueue`` **完全相同**的接口。

    路由规则：
        * **入队类**（enqueue/requeue/push_front）按 ``task.station_id`` 路由；
        * **出队类**（dequeue_*/peek_*）在所有站之间取"到达最早"的那一个
          —— 这就是多处理器调度里"全局最早就绪任务优先"的自然推广；
        * **聚合类**（depth/congestion/total_remaining_s/snapshot）对所有站求和/取最值。

    单站时上述每一条都退化为直接调用该站队列，返回值逐位相同。
    """

    def __init__(self, cluster: "StationCluster") -> None:
        self._cluster = cluster
        self._lock = threading.RLock()

    # ------------------------------------------------------------- 基础属性
    @property
    def capacity(self) -> int:
        return int(sum(n.queue.capacity for n in self._cluster.nodes))

    @property
    def starvation_threshold_ms(self) -> float:
        return float(self._cluster.nodes[0].queue.starvation_threshold_ms)

    @starvation_threshold_ms.setter
    def starvation_threshold_ms(self, value: float) -> None:
        for n in self._cluster.nodes:
            n.queue.starvation_threshold_ms = float(value)

    @property
    def congestion_ref(self) -> float:
        return float(sum(n.queue.congestion_ref for n in self._cluster.nodes))

    @property
    def lock_enabled(self) -> bool:
        return bool(self._cluster.nodes[0].queue.lock_enabled)

    # ------------------------------------------------------------- 路由与统计
    def _queue_for(self, task: Task) -> MultiLevelQueue:
        return self._cluster.node_of_task(task).queue

    def _all_queues(self) -> List[MultiLevelQueue]:
        return [n.queue for n in self._cluster.nodes]

    @staticmethod
    def _arrival_key(task: Task) -> Tuple[float, float, str]:
        """全局 FIFO 排序键（与 ``scheduler/base.py`` 的口径一致）。"""
        return (float(getattr(task, "enqueue_ts", 0.0) or 0.0),
                float(getattr(task, "submit_ts", 0.0) or 0.0),
                str(getattr(task, "task_id", "")))

    def _oldest_head(self, priority: Optional[TaskPriority]) -> Optional[Task]:
        """跨站取指定优先级里"到达最早"的队首（只读，不出队）。"""
        best: Optional[Task] = None
        best_key: Optional[Tuple[float, float, str]] = None
        for q in self._all_queues():
            head = q.peek(priority) if priority is not None else None
            if priority is None:
                for prio in (TaskPriority.HIGH, TaskPriority.LOW):
                    cand = q.peek(prio)
                    if cand is None:
                        continue
                    key = self._arrival_key(cand)
                    if best_key is None or key < best_key:
                        best, best_key = cand, key
                continue
            if head is None:
                continue
            key = self._arrival_key(head)
            if best_key is None or key < best_key:
                best, best_key = head, key
        return best

    # ------------------------------------------------------------- 入队/出队
    def enqueue(self, task: Task, now: float) -> bool:
        if task is None:
            return False
        return bool(self._queue_for(task).enqueue(task, now))

    def requeue(self, task: Task, now: float) -> bool:
        if task is None:
            return False
        return bool(self._queue_for(task).requeue(task, now))

    def push_front(self, task: Task, now: float) -> None:
        if task is None:
            return
        self._queue_for(task).push_front(task, now)

    def _pop_oldest(self, priority: TaskPriority, now: float) -> Optional[Task]:
        """跨站出队：找到持有最早到达任务的站，并从该站出队。

        只有调度器线程会出队（生产者只入队），因此"找站 -> 出队"之间不存在
        与本函数竞争的出队者；仍加一把簇级锁以防未来出现多调度线程。
        """
        with self._lock:
            target: Optional[MultiLevelQueue] = None
            best_key: Optional[Tuple[float, float, str]] = None
            for q in self._all_queues():
                head = q.peek(priority)
                if head is None:
                    continue
                key = self._arrival_key(head)
                if best_key is None or key < best_key:
                    target, best_key = q, key
            if target is None:
                return None
            if priority is TaskPriority.HIGH:
                return target.dequeue_high(now)
            return target.dequeue_low(now)

    def dequeue_high(self, now: float) -> Optional[Task]:
        return self._pop_oldest(TaskPriority.HIGH, now)

    def dequeue_low(self, now: float) -> Optional[Task]:
        return self._pop_oldest(TaskPriority.LOW, now)

    def dequeue(self, prefer_high: bool = True, now: float = 0.0) -> Optional[Task]:
        if prefer_high:
            t = self._pop_oldest(TaskPriority.HIGH, now)
            return t if t is not None else self._pop_oldest(TaskPriority.LOW, now)
        t = self._pop_oldest(TaskPriority.LOW, now)
        return t if t is not None else self._pop_oldest(TaskPriority.HIGH, now)

    def dequeue_starving(self, now: float, threshold_ms: float) -> Optional[Task]:
        """饥饿补偿通道：跨站取第一个等待超阈值的任务。

        单站时本方法**恰好**等于 ``MultiLevelQueue.dequeue_starving()``
        （直接转发，不引入任何额外语义），多站时按站顺序取第一个命中的，
        保证"补偿通道"在任一拓扑下都有确定的出路。
        """
        for node in self._cluster.nodes:
            got = node.queue.dequeue_starving(now, threshold_ms)
            if got is not None:
                return got
        return None

    # ------------------------------------------------------------- 窥视
    def peek_high(self) -> Optional[Task]:
        return self._oldest_head(TaskPriority.HIGH)

    def peek_low(self) -> Optional[Task]:
        return self._oldest_head(TaskPriority.LOW)

    def peek(self, priority: TaskPriority) -> Optional[Task]:
        return self._oldest_head(priority)

    # ------------------------------------------------------------- 深度/拥堵
    @property
    def high_depth(self) -> int:
        return int(sum(q.high_depth for q in self._all_queues()))

    @property
    def low_depth(self) -> int:
        return int(sum(q.low_depth for q in self._all_queues()))

    @property
    def total_depth(self) -> int:
        return int(sum(q.total_depth for q in self._all_queues()))

    def depth_of(self, priority: TaskPriority) -> int:
        return int(sum(q.depth_of(priority) for q in self._all_queues()))

    def congestion(self) -> float:
        return min(1.0, self.total_depth / max(1e-9, self.congestion_ref))

    def congestion_of(self, priority: TaskPriority) -> float:
        return min(1.0, self.depth_of(priority) / max(1e-9, self.congestion_ref))

    # ------------------------------------------------------------- 统计聚合
    def starving_count(self, now: float, threshold_ms: Optional[float] = None) -> int:
        return int(sum(q.starving_count(now, threshold_ms) for q in self._all_queues()))

    def oldest_wait_ms(self, priority: TaskPriority, now: float) -> float:
        vals = [q.oldest_wait_ms(priority, now) for q in self._all_queues()]
        return float(max(vals)) if vals else 0.0

    def total_remaining_s(self) -> float:
        return float(sum(q.total_remaining_s() for q in self._all_queues()))

    def tasks(self) -> List[Task]:
        out: List[Task] = []
        for q in self._all_queues():
            out.extend(q.tasks())
        return out

    def drain(self) -> List[Task]:
        out: List[Task] = []
        for q in self._all_queues():
            out.extend(q.drain())
        return out

    def snapshot_tasks(self, limit: int = 40, now: float = 0.0) -> List[dict]:
        """跨站合并的"传送带"快照（按站顺序拼接，附带 station_id）。"""
        out: List[dict] = []
        for node in self._cluster.nodes:
            for item in node.queue.snapshot_tasks(limit=limit, now=now):
                item["station_id"] = int(node.spec.id)
                item["station"] = str(node.spec.name)
                out.append(item)
        return out

    def snapshot(self, now: float, running_count: int, blocked_count: int,
                 system_load: float, high_slice_ms: float, low_slice_ms: float,
                 high_weight: float, low_weight: float) -> QueueSnapshot:
        return QueueSnapshot(
            ts=float(now),
            high_depth=self.high_depth,
            low_depth=self.low_depth,
            running_count=int(running_count),
            blocked_count=int(blocked_count),
            system_load=float(system_load),
            high_time_slice_ms=float(high_slice_ms),
            low_time_slice_ms=float(low_slice_ms),
            high_weight=float(high_weight),
            low_weight=float(low_weight),
        )

    # ---- 计数器透传（聚合） ----
    @property
    def enqueued_total(self) -> int:
        return int(sum(q.enqueued_total for q in self._all_queues()))

    @property
    def dequeued_total(self) -> int:
        return int(sum(q.dequeued_total for q in self._all_queues()))

    @property
    def dropped_full(self) -> int:
        return int(sum(q.dropped_full for q in self._all_queues()))

    @property
    def max_depth_seen(self) -> int:
        return int(max((q.max_depth_seen for q in self._all_queues()), default=0))

    @property
    def max_high_seen(self) -> int:
        return int(max((q.max_high_seen for q in self._all_queues()), default=0))

    @property
    def max_low_seen(self) -> int:
        return int(max((q.max_low_seen for q in self._all_queues()), default=0))

    def __len__(self) -> int:
        return self.total_depth

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<ClusterQueue stations={len(self._cluster.nodes)} "
                f"depth={self.total_depth} capacity={self.capacity}>")


# ===========================================================================
# 门面二：跨站限流器
# ===========================================================================
class ClusterLimiter:
    """跨站限流器门面 —— 暴露与 ``ResourceLimiter`` 相同的接口。

    准入/释放按 ``task.station_id`` 路由到该站的限流器：**每个站各自限流**
    （这就是"每站独立限流"的落点），``get_status()`` 汇总给面板与指标。
    """

    def __init__(self, cluster: "StationCluster") -> None:
        self._cluster = cluster

    def _limiter_for(self, task: Task) -> ResourceLimiter:
        return self._cluster.node_of_task(task).limiter

    def try_admit(self, task: Task) -> str:
        if task is None:
            return "THROTTLE"
        return str(self._limiter_for(task).try_admit(task))

    def acquire(self, task: Task, timeout_s: float = 0.0) -> bool:
        if task is None:
            return False
        return bool(self._limiter_for(task).acquire(task, timeout_s))

    def release(self, task: Task) -> None:
        if task is None:
            return
        try:
            self._limiter_for(task).release(task)
        except Exception:  # pragma: no cover - 释放失败不得打断收尾
            logger.exception("ClusterLimiter.release 异常 task=%s",
                             getattr(task, "task_id", "?"))

    def is_admitted(self, task: Task) -> bool:
        if task is None:
            return False
        return bool(self._limiter_for(task).is_admitted(task))

    def split_task(self, task: Task) -> List[Task]:
        """大任务分片：切完必须**给每片盖上站戳**，否则分片会漂到默认站。"""
        if task is None:
            return []
        limiter = self._limiter_for(task)
        segs = list(limiter.split_task(task) or [])
        for seg in segs:
            seg.station_id = int(getattr(task, "station_id", self._cluster.default_id))
            seg.home_station_id = int(
                getattr(task, "home_station_id", seg.station_id))
        return segs

    @property
    def running(self) -> int:
        return int(sum(n.limiter.running for n in self._cluster.nodes))

    @property
    def max_concurrent(self) -> int:
        return int(sum(n.spec.concurrency for n in self._cluster.nodes))

    @property
    def throttled(self) -> int:
        return int(sum(n.limiter.throttled for n in self._cluster.nodes))

    @property
    def rejected_large(self) -> int:
        return int(sum(n.limiter.rejected_large for n in self._cluster.nodes))

    @property
    def chunked(self) -> int:
        return int(sum(n.limiter.chunked for n in self._cluster.nodes))

    @property
    def segments_created(self) -> int:
        return int(sum(n.limiter.segments_created for n in self._cluster.nodes))

    def is_saturated(self) -> bool:
        return all(n.limiter.is_saturated() for n in self._cluster.nodes)

    def available(self) -> int:
        return int(sum(n.limiter.available() for n in self._cluster.nodes))

    def get_status(self) -> Dict[str, object]:
        """聚合限流状态（键与 ``ResourceLimiter.get_status`` 保持同名同义）。"""
        running = sum(n.limiter.running for n in self._cluster.nodes)
        return {
            "running": int(running),
            "available": self.available(),
            "max_concurrent": self.max_concurrent,
            "throttled": self.throttled,
            "rejected_large": self.rejected_large,
            "chunked": self.chunked,
            "released": int(sum(n.limiter.released for n in self._cluster.nodes)),
            "per_uav_running": {},
            "admitted": int(sum(n.limiter.admitted for n in self._cluster.nodes)),
            "quota_blocked": int(sum(n.limiter.quota_blocked for n in self._cluster.nodes)),
            "segments_created": self.segments_created,
            "peak_running": int(max((n.limiter.peak_running
                                     for n in self._cluster.nodes), default=0)),
            "max_task_memory_mb": float(self._cluster.nodes[0].limiter.max_task_memory_mb),
            "per_uav_quota": int(self._cluster.nodes[0].limiter.per_uav_quota),
            "stations": {int(n.spec.id): int(n.limiter.running)
                         for n in self._cluster.nodes},
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<ClusterLimiter running={self.running}/{self.max_concurrent} "
                f"stations={len(self._cluster.nodes)}>")


# ===========================================================================
# 站点簇
# ===========================================================================
def _hash32(text: str) -> int:
    """稳定 32 位哈希 —— **不用内置 hash()**（它对 str 有进程级随机盐，不可复现）。"""
    return int.from_bytes(hashlib.md5(str(text).encode("utf-8")).digest()[:4], "big")


class StationCluster:
    """全部服务器节点 + 选站算法 + 迁移调度。

    线程安全：``nodes`` 只读；每站的队列/限流器自带锁；``in_transit`` 用本类锁。
    """

    def __init__(self, specs: Sequence[StationSpec], cfg: dict) -> None:
        self.cfg = cfg or {}
        self.load_horizon_s: float = 2.0

        # ---- 每站容量参数（单站时与改造前完全一致） ----
        # 抽成实例字段，是为了让运行期 ``add_node()`` 能复用**同一份**构造逻辑
        # （见 ``_make_node``），而不是在第二处再抄一遍参数解析。
        self._cap_capacity = cfg_get_int(cfg, "server.queue_capacity", 5000)
        self._cap_starvation_ms = cfg_get_float(cfg, "server.starvation_threshold_ms", 3000.0)
        self._cap_mem = cfg_get_float(cfg, "server.max_task_memory_mb", 2048.0)
        self._cap_quota = cfg_get_int(cfg, "server.per_uav_quota", 2)
        self._cap_chunking = bool((cfg.get("server") or {}).get("enable_chunking", True))
        # 拥堵度基准按"核数占比"分摊：单站时恰好等于 congestion_reference_depth
        self._ref_total = cfg_get_float(
            cfg, "server.congestion_reference_depth",
            4.0 * max(1, cfg_get_int(cfg, "runtime.worker_threads", 8)))

        self._lock = threading.RLock()
        self.nodes: List[StationNode] = []
        self.by_id: Dict[int, StationNode] = {}
        self.default_id: int = int(specs[0].id) if specs else 1
        workers_total = max(1, sum(int(s.workers) for s in specs))
        for i, spec in enumerate(specs):
            self.nodes.append(self._make_node(spec, i, workers_total=workers_total))
        self._reindex()

        # ---- 迁移参数（"迁移不是瞬移"的三个旋钮） ----
        self.migration_latency_ms: float = cfg_get_float(
            cfg, "stations.migration_latency_ms", 120.0)
        self.migration_bw_penalty: float = cfg_get_float(
            cfg, "stations.migration_bw_penalty", 0.15)
        self.migration_load_ratio: float = cfg_get_float(
            cfg, "stations.migration_load_ratio", 1.25)
        self.migration_batch: int = max(1, cfg_get_int(cfg, "stations.migration_batch", 8))
        #: 在途迁移表：``(就绪时刻, task)`` —— 在途任务不占任何站的队列/槽位
        self.in_transit: List[Tuple[float, Task]] = []
        self.migrations_total: int = 0
        self.migration_cost_ms_total: float = 0.0
        #: 脑裂标志（分区两侧各自认为自己是主）
        self.split_brain: bool = False

        # ---- 最小 quorum 共识（教学用 P4） ----
        #: 任期号。**只在分区/脑裂注入与恢复时 +1** —— 这是"教学用最小共识"的简化：
        #: 不做选举超时、不做选票拉取、不做日志复制与成员变更（见 docs/11 §8）。
        #: 目的不是实现 Raft，而是让"分区两侧各自处于哪个任期、各自是否还是多数派"
        #: 变成可观测、可截图的读数。
        self.term: int = 0
        self._refresh_votes()

        # ---- 一致性哈希环（跨节点负载均衡的"亲和性"通道） ----
        self._ring: List[int] = []
        self._owner: Dict[int, int] = {}
        self._build_ring()

    # ------------------------------------------------------------------ 节点构造
    def _make_node(self, spec: StationSpec, idx: int,
                   workers_total: Optional[int] = None) -> StationNode:
        """按站点规格造一个 ``StationNode``（**唯一**的构造工厂）。

        ``__init__`` 与运行期 ``add_node()`` 都走这里，避免"两处各写一遍容量参数"
        而在后续改动中悄悄分叉。``idx`` 决定配色（按站序循环取 palette）。
        """
        total = max(1, int(workers_total if workers_total else spec.workers))
        ref = self._ref_total * (float(spec.workers) / float(total))
        node = StationNode(
            spec=spec, cfg=self.cfg, color=STATION_COLORS[idx % len(STATION_COLORS)],
            capacity=self._cap_capacity, congestion_ref=max(1e-9, ref),
            starvation_threshold_ms=self._cap_starvation_ms,
            max_task_memory_mb=self._cap_mem, per_uav_quota=self._cap_quota,
            enable_chunking=self._cap_chunking,
        )
        node.cluster = self
        return node

    def _reindex(self) -> None:
        """按 id 升序整理节点表，并按"核数占比"重算各站拥堵度基准。

        为什么增删站后要重算拥堵度基准：``congestion_ref`` 的口径是
        "本站在全簇核数中的占比 × 基准深度"，站数变了占比就变了；不重算会让
        "总拥堵度基准随站数漂移"，破坏"多站 = 把同样的容量切给更多节点"这一
        控制变量前提。既有节点的队列/限流器对象**原地保留**（任务与限流状态不丢）。
        """
        if not self.nodes:
            raise ValueError("站点簇至少要保留 1 个站")
        # 整体换引用（而不是原地 sort）：运行期增删站时，仿真主循环可能正在遍历
        # self.nodes，换引用让读端"要么看到旧表、要么看到新表"，不会读到半排序状态。
        ordered = sorted(self.nodes, key=lambda n: int(n.spec.id))
        workers_total = max(1, sum(int(n.spec.workers) for n in ordered))
        for n in ordered:
            n.queue.congestion_ref = max(
                1e-9, self._ref_total * (float(n.spec.workers) / float(workers_total)))
            n.cluster = self
        self.nodes = ordered
        self.by_id = {int(n.spec.id): n for n in ordered}
        self.default_id = int(ordered[0].spec.id)

    # ------------------------------------------------------------------ 哈希环
    def _build_ring(self) -> None:
        vnodes = max(1, cfg_get_int(self.cfg, "stations.hash_virtual_nodes", 64))
        ring: List[int] = []
        owner: Dict[int, int] = {}
        for node in self.nodes:
            for i in range(vnodes):
                key = _hash32(f"{node.spec.id}#{i}")
                while key in owner:      # 极罕见的哈希碰撞：线性后移
                    key = (key + 1) % (1 << 32)
                ring.append(key)
                owner[key] = int(node.spec.id)
        order = sorted(range(len(ring)), key=lambda i: ring[i])
        self._ring = [ring[i] for i in order]
        self._owner = owner

    def rebuild_ring(self) -> int:
        """按**当前**节点集合重建一致性哈希环（P2 运行期再平衡的入口）。

        返回环上虚拟节点总数（= 站数 × ``stations.hash_virtual_nodes``）。
        为什么必须显式重建：环原先只在 ``__init__`` 建一次，增/删站后若不重建，
        ``hash_owner()`` 会把新站"视而不见"（永远收不到 key），再平衡也就无从验证。
        """
        self._build_ring()
        return len(self._ring)

    def add_node(self, spec: StationSpec, rebuild: bool = True) -> StationNode:
        """运行期加入一个新站（P2）：建节点 -> 重排 -> 重建环。

        容量参数复用 ``_make_node``（与单站/初始多站同一份逻辑）；既有节点的
        队列、限流器、在线状态、接入无人机集合**一律不动**（只有拥堵度基准按新
        核数占比重算，见 ``_reindex``）。

        ``id`` 重复时抛 ``ValueError``（不静默覆盖一个活站）。
        """
        if int(spec.id) in self.by_id:
            raise ValueError(f"站点 id {int(spec.id)} 已存在，不能重复加入")
        workers_total = sum(int(n.spec.workers) for n in self.nodes) + int(spec.workers)
        node = self._make_node(spec, len(self.nodes), workers_total=workers_total)
        self.nodes.append(node)
        self._reindex()
        if rebuild:
            self.rebuild_ring()
        return node

    def remove_node(self, station_id: int, now: float = 0.0,
                    evacuate: bool = False) -> Tuple[bool, str]:
        """运行期摘除一个站（P2）：返回 ``(是否成功, 说明)``。

        拒绝条件（**拒绝时不改动任何状态**，返回原因供调用方决策）：
            * 未知站号；或只剩最后一个站（簇里至少要留 1 个站）；
            * 该站仍有"排队中"的任务 —— ``evacuate=True`` 时先尝试疏散到同分区
              存活站（走 ``evacuate_station``，带迁移代价），仍剩任务则拒绝；
            * 该站仍有"在跑"的任务 —— 运行中的任务不能搬，必须等它跑完再删。

        成功时：节点出表 -> 重建环；在途迁往该站的任务由 ``flush_migrations``
        自动改派（它本就对"目标站不存在"做了兜底，任务不会丢）。
        """
        sid = int(station_id)
        node = self.by_id.get(sid)
        if node is None:
            return False, f"未知站点 {sid}"
        if len(self.nodes) <= 1:
            return False, "至少保留 1 个站：不能删除簇里最后一个站"
        evacuated = 0
        if node.queue.total_depth > 0:
            if not evacuate:
                return False, (f"站点 {sid} 仍有 {node.queue.total_depth} 个排队任务；"
                               "请先疏散（evacuate=True）或等其排空")
            evacuated = self.evacuate_station(sid, now)
            if node.queue.total_depth > 0:
                return False, (f"站点 {sid} 仍有 {node.queue.total_depth} 个排队任务"
                               "（同分区内没有可用站可接管），暂不能删除")
        if int(node.limiter.running) > 0:
            return False, (f"站点 {sid} 仍有 {int(node.limiter.running)} 个任务在跑；"
                           "运行中的任务不能搬，请等它跑完再删")
        self.nodes = [n for n in self.nodes if int(n.spec.id) != sid]
        self._reindex()
        self.rebuild_ring()
        return True, f"已删除站点 {sid}（疏散排队任务 {evacuated} 个）"

    def hash_owner(self, key: Any) -> int:
        """一致性哈希定位（纯函数、可复现）：同一 key 永远落到同一站。

        注意：本方法只看**环**，不看在线状态（纯映射，测试单调性用它）；
        "跳过不可用站"由 ``pick_by_hash`` 负责。
        """
        if not self._ring:
            return self.default_id
        h = _hash32(str(key))
        idx = bisect.bisect_left(self._ring, h)
        if idx >= len(self._ring):
            idx = 0
        return int(self._owner[self._ring[idx]])


    # ------------------------------------------------------------------ 可达性
    def node_of_task(self, task: Task) -> StationNode:
        """任务当前所属站（未知/非法 id 时回退默认站，绝不抛异常）。"""
        sid = int(getattr(task, "station_id", self.default_id) or self.default_id)
        node = self.by_id.get(sid)
        if node is None:
            node = self.by_id[self.default_id]
            task.station_id = int(node.spec.id)
        return node

    def node(self, station_id: int) -> Optional[StationNode]:
        return self.by_id.get(int(station_id))

    def can_reach(self, a: int, b: int) -> bool:
        """两站当前是否互通（同时考虑宕机与网络分区）。

        分区语义：``partition_group == 0`` 表示"未分区"；一旦分组，
        只有**同组**互通 —— 因此未分区时任意两站都通，退化正确。
        """
        na, nb = self.by_id.get(int(a)), self.by_id.get(int(b))
        if na is None or nb is None:
            return False
        if not na.online or not nb.online:
            return False
        return int(na.partition_group) == int(nb.partition_group)

    def reachable_nodes(self, from_id: int) -> List[StationNode]:
        """从某站出发当前可迁往的站（在线 + 同分区组）。

        脑裂时跨组迁移同样被 ``can_reach`` 挡掉（两组已分属不同 group），
        因此这里不需要为 split_brain 单独加分支。
        """
        return [n for n in self.nodes
                if int(n.spec.id) != int(from_id)
                and self.can_reach(int(from_id), int(n.spec.id))]

    def alive_nodes(self) -> List[StationNode]:
        return [n for n in self.nodes if n.online]

    def partitions_view(self) -> List[List[int]]:
        """当前分区视图：未分区返回 ``[]``；分区则返回各组成员 id。"""
        groups: Dict[int, List[int]] = {}
        for n in self.nodes:
            if int(n.partition_group) > 0:
                groups.setdefault(int(n.partition_group), []).append(int(n.spec.id))
        if not groups:
            return []
        return [sorted(v) for _, v in sorted(groups.items())]

    def nodes_down(self) -> List[int]:
        return sorted(int(n.spec.id) for n in self.nodes if not n.online)

    # ------------------------------------------------------------------ 选站
    def pick_least_connections(self, running_by_station: Dict[int, float],
                               candidates: Optional[Iterable[int]] = None,
                               exclude: Optional[Iterable[int]] = None) -> int:
        """最少连接：优先"占用率最低"，其次"队列最短"，最后按 id 稳定 tie-break。

        为什么用**占用率**（active/workers）而不是绝对连接数：各站核数不同
        （中心 6 核 vs 边缘 3 核），比绝对值会让小站被系统性压垮。
        """
        excl = {int(x) for x in (exclude or ())}
        pool = [n for n in self.nodes
                if n.online and int(n.spec.id) not in excl
                and (candidates is None or int(n.spec.id) in {int(c) for c in candidates})]
        if not pool:
            return self.default_id
        def key(n: StationNode) -> Tuple[float, int, int]:
            active = float(running_by_station.get(int(n.spec.id), 0.0))
            ratio = active / max(1.0, float(n.spec.concurrency))
            return (ratio, int(n.queue.total_depth), int(n.spec.id))
        return int(min(pool, key=key).spec.id)

    def pick_by_hash(self, key: Any, candidates: Optional[Iterable[int]] = None) -> int:
        """一致性哈希选站；若命中站不可用，则顺时针找下一个可用站。

        "同一 key 稳定映射"是缓存亲和性/会话粘性的来源 —— 这是与最少连接
        （纯动态）互补的一路输入，两者加权后才是完整的跨节点策略。
        """
        if not self._ring:
            return self.default_id
        allowed = None if candidates is None else {int(c) for c in candidates}
        h = _hash32(str(key))
        idx = bisect.bisect_left(self._ring, h)
        for step in range(len(self._ring)):
            k = self._ring[(idx + step) % len(self._ring)]
            sid = int(self._owner[k])
            node = self.by_id.get(sid)
            if node is None or not node.online:
                continue
            if allowed is not None and sid not in allowed:
                continue
            return sid
        return self.default_id

    def home_station_for(self, uav_id: int) -> int:
        """无人机接入点归属（= 一致性哈希）：设备上线时确定，之后可被迁移改变。"""
        return self.pick_by_hash(f"uav:{int(uav_id)}")

    # ------------------------------------------------------------------ 迁移
    def _detach(self, task: Task) -> bool:
        """把任务从它当前站的队列里**摘掉**（按对象身份，避免误伤同值任务）。

        只对"排队中"的任务有效：正在运行 / 在途 / 已完成的返回 False ——
        这是"迁移只搬队列里的活，不抢正在跑的活"的实现约束，
        同时也保证了**迁移期间任务不会同时占两个站的槽位**。
        """
        node = self.by_id.get(int(getattr(task, "station_id", self.default_id)))
        if node is None:
            return False
        q = node.queue
        with q._guard():  # noqa: SLF001 - 项目内已建立的队列内部约定
            dq = q._high_queue if task.priority is TaskPriority.HIGH else q._low_queue
            for i, other in enumerate(dq):
                if other is task:
                    del dq[i]
                    q.enqueued_total = max(0, q.enqueued_total - 1)
                    return True
        return False

    def migrate_task(self, task: Task, to_id: int, now: float,
                     latency_ms: Optional[float] = None,
                     bw_penalty: Optional[float] = None) -> bool:
        """把一个**排队中**的任务迁移到 ``to_id`` 站 —— 迁移有代价，不是瞬移。

        代价两笔（对应契约 §stations.migration_*）：

        1. **在途耗时**：任务进入 ``in_transit``，要等 ``migration_latency_ms``
           才被 ``flush_migrations()`` 投入目标站队列；在途期间**不占任何槽位**；
        2. **带宽损耗**：跨节点回传要重传已收部分，``service_s`` 按
           ``(1 + bw_penalty)`` 放大，反映"同一份数据多花的时间"。

        返回是否真的迁移成功（不可达 / 任务不在队列里都会返回 False）。
        """
        if task is None:
            return False
        src_id = int(getattr(task, "station_id", self.default_id))
        dst_id = int(to_id)
        if src_id == dst_id:
            return False
        if not self.can_reach(src_id, dst_id):
            return False
        src = self.by_id.get(src_id)
        dst = self.by_id.get(dst_id)
        if src is None or dst is None:
            return False
        if not self._detach(task):
            return False

        lat = float(self.migration_latency_ms if latency_ms is None else latency_ms)
        pen = float(self.migration_bw_penalty if bw_penalty is None else bw_penalty)
        lat = max(0.0, lat)

        # ---- 代价②：带宽损耗 -> 剩余工作量变大 ----
        before_s = float(task.service_s)
        if pen > 0:
            task.service_s = before_s * (1.0 + pen)
        # 已经跑过的那部分不因迁移而"白跑"，但放大后的总时长会体现在剩余量上
        task.served_s = min(float(task.served_s), float(task.service_s))
        extra_ms = max(0.0, (float(task.service_s) - before_s)) * 1000.0

        # ---- 代价①：在途耗时 ----
        task.station_id = dst_id
        task.migrate_count = int(task.migrate_count) + 1
        cost_ms = lat + extra_ms
        task.migration_cost_ms = float(task.migration_cost_ms) + cost_ms

        with self._lock:
            self.in_transit.append((float(now) + lat / 1000.0, task))
            self.migrations_total += 1
            self.migration_cost_ms_total += cost_ms
        src.migrations_out += 1
        dst.migrations_in += 1
        dst.migration_cost_ms_total += cost_ms
        return True

    def flush_migrations(self, now: float) -> int:
        """把到期的在途任务投入目标站队列（对应"迁移完成、进程在新节点就绪"）。"""
        with self._lock:
            if not self.in_transit:
                return 0
            due = [t for t in self.in_transit if t[0] <= float(now)]
            if not due:
                return 0
            self.in_transit = [t for t in self.in_transit if t[0] > float(now)]
        n = 0
        for _, task in due:
            node = self.by_id.get(int(getattr(task, "station_id", self.default_id)))
            if node is None or not node.online:
                # 目标站也挂了：退回任意可用站（保证任务不丢）
                alt = self.pick_least_connections({}, exclude=[int(getattr(task, "station_id", 0))])
                node = self.by_id.get(alt) or self.by_id[self.default_id]
                task.station_id = int(node.spec.id)
            if node.queue.enqueue(task, now):
                n += 1
        return n

    def evacuate_station(self, station_id: int, now: float) -> int:
        """节点宕机时的**任务疏散**：把该站队列里的任务改派到其它可达站。

        这是"节点宕机 -> 任务重调度"这条观察链的实现落点。疏散同样要付迁移
        代价（它本质上就是一次强制迁移）。返回成功疏散的任务数。

        两条边界（都在测试里被验证过）：

        * **遵守网络分区**：只迁往与宕机站**同分区组**的存活站 —— 否则"分区"
          这个混沌项会被节点宕机悄悄绕过，跨区照样搬任务。
        * **MEC 角色约束在此处放宽为 best-effort**：节点已经死了，"任务必须
          落到某个活站"优先于"必须落到正确角色的站"；因此疏散不检查角色，
          只按最少连接选。正常情况下（无宕机）角色约束由调度器严格保证。
        """
        node = self.by_id.get(int(station_id))
        if node is None:
            return 0
        pending = node.queue.drain()
        moved = 0
        for task in pending:
            # 只考虑与宕机站同分区组的存活站（受分区约束）
            targets = [n.spec.id for n in self.nodes
                       if int(n.spec.id) != int(station_id) and n.online
                       and int(n.partition_group) == int(node.partition_group)]
            if not targets:
                # 该分区内无可用站：任务先放回原站队列（不丢），等分区恢复/站恢复
                node.queue.enqueue(task, now)
                continue
            best = self.pick_least_connections({}, candidates=targets)
            # 直接把 station_id 改过去（不再走 _detach：任务已被 drain 出来）
            task.station_id = int(best)
            task.migrate_count = int(task.migrate_count) + 1
            lat = float(self.migration_latency_ms)
            task.migration_cost_ms = float(task.migration_cost_ms) + lat
            dst = self.by_id[int(best)]
            dst.migrations_in += 1
            node.migrations_out += 1
            with self._lock:
                self.in_transit.append((float(now) + lat / 1000.0, task))
                self.migrations_total += 1
                self.migration_cost_ms_total += lat
            moved += 1
        node.crash_rescheduled += moved
        return moved

    # ------------------------------------------------------------------ 混沌
    def set_online(self, station_id: int, online: bool, now: float = 0.0) -> bool:
        """上线/下线某站（混沌「节点宕机」）。下线时记录崩溃时刻供看门狗使用。"""
        node = self.by_id.get(int(station_id))
        if node is None:
            return False
        node.online = bool(online)
        if online:
            node.crashed_at = 0.0
        else:
            node.crashed_at = float(now)
        return True

    def set_partition(self, groups: Sequence[Sequence[int]]) -> List[List[int]]:
        """设置网络分区：每个子列表是一组互通站点，跨组不可达。

        分区 = **进入新任期**（``term += 1``）：两侧各自开始一个任期、各自
        重新投票（教学用简化，见 ``bump_term``）。
        """
        for node in self.nodes:
            node.partition_group = 0
        for gi, group in enumerate(groups, start=1):
            for sid in group:
                node = self.by_id.get(int(sid))
                if node is not None:
                    node.partition_group = gi
        self.split_brain = False
        self.bump_term("partition")
        return self.partitions_view()

    def clear_partition(self) -> None:
        """解除分区 = 合并回一个任期（``term += 1``），并清脑裂标志。"""
        for node in self.nodes:
            node.partition_group = 0
        self.split_brain = False
        self.bump_term("heal")

    def recover_all(self) -> None:
        """一键恢复（全部上线 + 解除分区 + 清脑裂）= 合并回一个任期。"""
        for node in self.nodes:
            node.online = True
            node.partition_group = 0
            node.down_until = 0.0
            node.crashed_at = 0.0
        self.split_brain = False
        self.bump_term("recover")

    # ------------------------------------------------------------------ 最小 quorum 共识（P4）
    @property
    def majority(self) -> int:
        """多数派门槛 = ``floor(N/2) + 1``，**按总站数 N** 计算（Raft 口径）。

        刻意**不按在线站数**算：否则"3 站挂掉 2 站后剩下那 1 站自认多数派"，
        共识语义立刻失效（Raft 里多数派随成员数走，不随存活数走）。
        """
        return len(self.nodes) // 2 + 1

    def bump_term(self, reason: str = "") -> int:
        """进入新任期（分区/脑裂注入与恢复时调用），并刷新各站投票状态。

        这是"教学用最小共识"的**唯一**任期推进点：不实现选举超时、拉票与日志
        比较，只让"现在处于第几任、各站投过谁"可观测。``reason`` 仅用于日志/追溯。
        """
        self.term = int(self.term) + 1
        self._refresh_votes()
        logger.debug("StationCluster 进入新任期 term=%d（%s）", self.term, reason or "-")
        return self.term

    def _refresh_votes(self) -> None:
        """新任期开始时的投票状态刷新（简化：要求全站投出赞成票）。"""
        for n in self.nodes:
            n.voted_term = int(self.term)
            n.vote_granted = True

    def _quorum_group_of(self, node: StationNode) -> List[StationNode]:
        """该站当前所属的"在线分区组"（同 ``partition_group`` 的在线站）。

        未分区时全体在线站天然属于同一组（``partition_group`` 都是 0），
        因此不需要为"未分区"单开分支 —— 退化正确。
        """
        return [n for n in self.nodes if n.online
                and int(n.partition_group) == int(node.partition_group)]

    def group_has_quorum(self, station_id: int) -> bool:
        """某站所在分区组是否达到多数派（写入确认的判据）。

        站已离线 / 未知 -> ``False``：离线的站不属于任何**在线**组，
        它上面的写确认当然不可能通过（脑裂里"失联侧"正是这个语义）。
        """
        node = self.by_id.get(int(station_id))
        if node is None or not node.online:
            return False
        return len(self._quorum_group_of(node)) >= self.majority

    def quorum_view(self) -> Dict[str, Any]:
        """当前 quorum 视图（``/api/state.live.quorum`` 与站卡片 ``is_majority`` 共用）。

        结构（键名冻结，前端 app3d.html / os_inspect.js 均按此消费）::

            {"term": int,
             "majority": int,                     # floor(N/2)+1，按总站数
             "groups": [{"ids": [int,...], "size": int, "has_quorum": bool}, ...],
             "has_majority_anywhere": bool}

        分组口径：按 ``partition_group`` 聚合**在线**站（未分区 = 全体在线站一组）；
        单站退化：``majority=1``、唯一一组 ``{1}``、``has_quorum=True``。
        """
        maj = self.majority
        buckets: Dict[int, List[int]] = {}
        for n in self.nodes:
            if not n.online:
                continue
            buckets.setdefault(int(n.partition_group), []).append(int(n.spec.id))
        groups: List[Dict[str, Any]] = []
        for _gid, ids in sorted(buckets.items()):
            ordered = sorted(ids)
            groups.append({"ids": ordered, "size": len(ordered),
                           "has_quorum": len(ordered) >= maj})
        return {
            "term": int(self.term),
            "majority": int(maj),
            "groups": groups,
            "has_majority_anywhere": any(g["has_quorum"] for g in groups),
        }

    # ------------------------------------------------------------------ 视图
    def snapshot(self, running_remaining_by_station: Dict[int, float],
                 elapsed_s: float) -> List[dict]:
        """站级快照列表（按 id 升序），供 ``/api/state`` 与 3D 面板消费。"""
        out = []
        for node in sorted(self.nodes, key=lambda x: int(x.spec.id)):
            out.append(node.to_dict(
                extra_running_s=float(running_remaining_by_station.get(int(node.spec.id), 0.0)),
                elapsed_s=float(elapsed_s)))
        return out

    def load_imbalance(self, running_remaining_by_station: Dict[int, float]) -> float:
        """站间负载不均衡度 = 各站负载的**变异系数**（σ/均值，0 = 完全均衡）。

        为什么不用"最大-最小"：极差对单点毛刺过敏；变异系数是归一化量，
        跨拓扑/跨工况可比，这正是"跨节点负载均衡"效果的直接量化指标。
        """
        loads = [n.load(float(running_remaining_by_station.get(int(n.spec.id), 0.0)))
                 for n in self.nodes]
        if len(loads) < 2:
            return 0.0
        mean = sum(loads) / len(loads)
        if mean <= EPS:
            return 0.0
        var = sum((x - mean) ** 2 for x in loads) / len(loads)
        return float((var ** 0.5) / mean)

    @property
    def total_workers(self) -> int:
        return int(sum(n.spec.workers for n in self.nodes))

    @property
    def is_multi(self) -> bool:
        return len(self.nodes) > 1

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<StationCluster stations={len(self.nodes)} "
                f"workers={self.total_workers} down={self.nodes_down()}>")
