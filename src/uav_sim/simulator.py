"""
simulator.py —— 仿真器主控 / 系统内核
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 地面运维后台：30 台无人机同时作业、统一上传链路回传数据、后台服务器解析入库 |
| OS 原理层   | **本类 = 操作系统内核**：① 无人机 = 生产者线程（`UAVWorker`）；② 工作线程池 = CPU 核数；③ 全局时钟 = 系统节拍；④ 就绪队列 + 限流器 = 调度基础设施；⑤ 弱网失败重传 = IO 阻塞后重新入就绪队列 |
| 算法设计层 | 仿真时钟换算（仿真秒 ↔ 真实秒）、任务服务时长标定、弱网失败判定与退避重传、过载时长统计 |
| 代码实现层 | `Simulator` + `UAVWorker` + `ThreadPoolExecutor` |

--------------------------------------------------------------------------
时间模型（全项目统一，务必理解）
--------------------------------------------------------------------------
真实时间与仿真时间严格成比例::

    仿真时刻 now() = (perf_counter() - t0) / time_scale
    真实休眠时长   = 仿真时长 × time_scale

`time_scale=0.1` 表示「1 仿真秒 = 100 真实毫秒」：
峰值工况 90 仿真秒只需真实跑 9 秒。因为**所有**时长（任务到达、服务、重传退避）
都按同一系数缩放，等待时延、吞吐量等**相对指标与缩放无关**，
所以实验结论不会因 time_scale 改变而改变（可用不同 time_scale 复跑验证）。

多线程 + sleep 模拟 IO：`time.sleep` 会释放 GIL，因此 8 个工作线程是**真并发**，
不会退化成单线程串行（这是本方案能真实复现"并发争用"的前提）。

--------------------------------------------------------------------------
弱网失败与重传（痛点①「劣质链路长期抢占资源」的代码级复现）
--------------------------------------------------------------------------
任务在工作线程中「上传完成」后做一次失败判定::

    p_fail = (1 - health/100) × retry_gain       # clamp 到 [0, 1]

失败后数据作废（`served_s` 归零）、`retry_count += 1`，然后延迟重新入队：

    * 静态策略（`link_aware=False`）：原样退避重传 —— 劣质链路任务反复抢槽位，
      优质链路任务被挤在后面。**这正是 PDF 痛点①的复现机制。**
    * 动态策略（`link_aware=True`）且 health < 缓存阈值：进入 `CACHED`
      「本地缓存」状态，退避更久且不占并发槽位 —— 把资源让给优质链路任务，
      等链路恢复再补传。**这就是"弱网自适应"的代码落点。**

--------------------------------------------------------------------------
锁约定（防止死锁）
--------------------------------------------------------------------------
获取顺序固定为 ``Simulator._lock -> queue._lock / limiter._lock``，
任何地方都**不允许**先持 queue/limiter 锁再去拿 Simulator 锁。
"""

from __future__ import annotations

import logging
import random
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.config import cfg_get_float, cfg_get_int
from uav_sim.link_monitor import LinkMonitor
from uav_sim.mec import MecController, desired_role_for
from uav_sim.models import (
    LinkSample,
    QueueSnapshot,
    Task,
    TaskPriority,
    TaskState,
    TaskType,
    UAVState,
    UAVStatus,
    new_task_id,
)
from uav_sim.queue_manager import MultiLevelQueue  # noqa: F401 - 契约再导出（外部按旧路径引用）
from uav_sim.resource_limiter import ResourceLimiter  # noqa: F401 - 契约再导出
from uav_sim.scenarios import Scenario, TraceEvent, WorkloadTrace
from uav_sim.station import (
    ClusterLimiter,
    ClusterQueue,
    StationCluster,
    resolve_station_specs,
)
from uav_sim.weather import WeatherModel, config_preset

logger = logging.getLogger(__name__)


def process_cpu_times() -> Tuple[float, float]:
    """返回进程的 (用户态秒, 内核态秒)。

    为什么需要它：老师要求「真实的内核态/用户态切换时间」。Python 没有跨平台的
    线程级 CPU 时间接口，但**进程级**的 user/kernel 拆分在 Windows 上可以通过
    ``GetProcessTimes`` 拿到（POSIX 走 ``os.times()``）。拿不到时退回
    ``time.process_time()``（用户+内核合计）并把 kernel 记为 0 —— 不编造数据。
    """
    try:  # Windows
        import ctypes
        from ctypes import wintypes

        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD),
                        ("dwHighDateTime", wintypes.DWORD)]

        def _sec(ft: "_FILETIME") -> float:
            return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        created, exited, kern, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(created),
                               ctypes.byref(exited), ctypes.byref(kern),
                               ctypes.byref(user)):
            return _sec(user), _sec(kern)
    except Exception:  # pragma: no cover - 非 Windows / 受限环境
        pass
    try:  # POSIX：os.times() 直接给 user/system
        import os

        t = os.times()
        return float(t.user), float(t.system)
    except Exception:  # pragma: no cover
        return float(time.process_time()), 0.0

EPS = 1e-9

#: 系统负载的参考视界（仿真秒）。
#: 含义：把「按满负荷也要 X 秒以上才能排空的积压」视为满载 ——
#: ``system_load = 积压服务秒 / (工作线程数 × LOAD_HORIZON_S)``，
#: 于是 load≈0 表示空闲、load≈1 表示刚好排满、load>1 表示积压排不空（过载）。
#: 该归一化是动态权重 S 因子与「过载持续时长」指标的共同输入。
LOAD_HORIZON_S: float = 2.0


class UAVWorker(threading.Thread):
    """无人机仿真节点 —— 映射为 OS 中的一个**生产者线程**。

    每台无人机一个线程，按预生成轨迹（`WorkloadTrace`）的到达时刻醒来，
    生成任务并提交给仿真器（对应线程创建/运行/销毁的生命周期）。

    响应 ``stop_event``：用 ``Event.wait()`` 而非裸 ``sleep()``，
    保证仿真结束时线程能立刻退出（不留下僵尸线程影响进程退出）。
    """

    def __init__(self, uav_id: int, sim: "Simulator", events: List[TraceEvent]) -> None:
        super().__init__(name=f"UAVWorker-{uav_id}", daemon=True)
        self.uav_id = uav_id
        self.sim = sim
        self.events = events
        self.generated = 0          # 本线程生成的任务数
        self.skipped_offline = 0    # 因返航下线跳过的任务数

    def run(self) -> None:  # noqa: D401 - 线程入口
        sim = self.sim
        for ev in self.events:
            if sim.stop_event.is_set():
                return
            wait_real = (ev.t - sim.now()) * sim.time_scale
            if wait_real > 0:
                # wait() 返回 True 表示收到停止信号 -> 立刻退出
                if sim.stop_event.wait(wait_real):
                    return
            if not sim.is_uav_online(self.uav_id):
                # 设备已返航/下线 -> 本时刻不产生任务（对应线程被销毁期间不生产）
                self.skipped_offline += 1
                continue
            task = sim.make_task(self.uav_id, ev.task_type, ev.data_size_mb, sim.now())
            if sim.submit_task(task):
                self.generated += 1
        sim.mark_uav_returned(self.uav_id)


class Simulator:
    """仿真器主控（契约 §4.6）。

    Parameters
    ----------
    scenario : Scenario
        工况定义（时长、机群规模、弱网档位…）
    trace : WorkloadTrace
        预生成的任务到达 + 上下线轨迹（**控制变量的物质基础**）
    cfg : dict
        全局配置
    scheduler_name : str
        本次使用的调度策略注册名（仅用于记录）
    seed : int
        随机种子（决定弱网失败判定的随机流）
    weather_preset : str | None
        天气预设（clear/rain/snow/dust）。``None`` = 取 config ``weather.default_preset``
        （默认 clear，链路扣分恒 0，对既有 15 组结果零影响）。天气通过
        ``WeatherModel`` 作用到 ``LinkMonitor`` 的健康度曲线上，进而进入
        动态权重（弱网降权）、失败重传概率与服务时长标定。
    """

    def __init__(self, scenario: Scenario, trace: WorkloadTrace, cfg: dict,
                 scheduler_name: str, seed: int,
                 weather_preset: Optional[str] = None,
                 station_spec: Optional[object] = None,
                 mec_enabled: Optional[bool] = None) -> None:
        self.scenario = scenario
        self.trace = trace
        self.cfg = cfg or {}
        self.scheduler_name = scheduler_name
        self.seed = int(seed)

        # ---- 时间基 ----
        self.time_scale: float = max(1e-6, cfg_get_float(cfg, "runtime.time_scale", 0.1))
        self._t0: Optional[float] = None

        # ---- 系统容量 ----
        # 注意：单站时 workers = runtime.worker_threads（与改造前一致）；
        # 多站时下面会被覆盖为"所有站核数之和"。
        self.workers: int = cfg_get_int(cfg, "runtime.worker_threads", 8)
        self.bandwidth_mbps: float = cfg_get_float(cfg, "runtime.worker_bandwidth_mbps", 150.0)
        self.bandwidth_floor: float = cfg_get_float(
            cfg, "dynamic_weight.weaknet_bandwidth_floor", 0.25)

        # ---- 弱网与重传 ----
        self.retry_gain: float = cfg_get_float(cfg, "weaknet.retry_gain", 1.2)
        self.retry_backoff_base_s: float = cfg_get_float(
            cfg, "weaknet.retry_backoff_base_s", 0.5)
        self.cache_health_threshold: float = cfg_get_float(
            cfg, "dynamic_weight.cache_health_threshold", 40.0)
        self.cache_retry_backoff_s: float = cfg_get_float(
            cfg, "dynamic_weight.cache_retry_backoff_s", 2.0)

        # ---- 采样与节拍 ----
        self.queue_sample_interval_s: float = max(
            0.01, cfg_get_float(cfg, "simulation.queue_sample_interval_s", 0.5))
        self.scheduler_tick_real_s: float = max(
            0.0005, cfg_get_float(cfg, "simulation.scheduler_tick_real_s", 0.002))
        self.overload_threshold: float = cfg_get_float(cfg, "server.overload_threshold", 1.0)
        self.starvation_threshold_ms: float = cfg_get_float(
            cfg, "server.starvation_threshold_ms", 3000.0)

        # ---- 调度基础设施（对所有策略一视同仁） ----
        uav_ids = list(range(1, int(scenario.uav_count) + 1))
        # ---- 多基站 / MEC（改造建议第五章）----
        # 关键：单站（默认）时 ClusterQueue/ClusterLimiter 的每个返回值与直接使用
        # 单个 MultiLevelQueue/ResourceLimiter 逐位相同 -> 既有 15 组结果零影响。
        self.station_specs = resolve_station_specs(self.cfg, station_spec)
        self.stations = StationCluster(self.station_specs, self.cfg)
        #: 多站时 workers 反映所有站核数之和（供系统负载 S 因子归一化）
        self.workers = int(self.stations.total_workers)
        self.queue = ClusterQueue(self.stations)
        self.limiter = ClusterLimiter(self.stations)

        # 每台无人机接入站点（一致性哈希决定原生归属，之后可被迁移改变）
        self._uav_station: Dict[int, int] = {
            uid: int(self.stations.home_station_for(uid)) for uid in uav_ids}
        for uid, sid in self._uav_station.items():
            node = self.stations.node(sid)
            if node is not None:
                node.uavs.add(int(uid))

        # ---- MEC 边缘计算 ----
        self.mec = MecController(self.cfg, station_roles={
            int(n.spec.id): str(n.spec.role) for n in self.stations.nodes})
        if mec_enabled is not None:
            self.mec.cfg_obj.enable = bool(mec_enabled)

        # ---- 天气模型（3D 渲染升级：天气 -> 弱网 -> 链路健康度 -> 调度权重） ----
        # 构造顺序很关键：先建 WeatherModel，再传给 LinkMonitor —— 后者在
        # register_uav() 时会用 weather.at(t) 对健康度网格施加衰减。
        self.weather_preset: str = config_preset(self.cfg, weather_preset)
        self.weather = WeatherModel(
            seed=self.seed, preset=self.weather_preset,
            duration_s=float(scenario.duration_s), cfg=cfg,
        )
        self.link_monitor = LinkMonitor(
            seed=self.seed, weaknet_level=scenario.weaknet_level,
            duration_s=float(scenario.duration_s), uav_ids=uav_ids, cfg=cfg,
            weather=self.weather,
            station_ids=[int(n.spec.id) for n in self.stations.nodes],
        )

        # ---- 运行期状态 ----
        self.stop_event = threading.Event()
        self._lock = threading.RLock()
        self._tasks: List[Task] = []                 # 全部任务（提交顺序）
        self._by_id: Dict[str, Task] = {}
        self._running: Dict[str, Tuple[Task, float]] = {}   # task_id -> (task, 起跑仿真时刻)
        self._retry_pending: List[Tuple[float, Task]] = []  # (到期仿真时刻, task)
        self._rng = random.Random(self.seed * 7919 + 13)     # 弱网失败判定随机流
        self._executor: Optional[ThreadPoolExecutor] = None
        self._scheduler = None
        self._uav_online: Dict[int, bool] = {uid: True for uid in uav_ids}
        self._uav_status: Dict[int, UAVStatus] = {
            uid: UAVStatus(uav_id=uid, state=UAVState.OFFLINE) for uid in uav_ids}
        self._churn_idx = 0
        self._queue_samples: List[QueueSnapshot] = []
        self._load_cache: Tuple[float, float] = (-1.0, 0.0)
        self._overload_seconds = 0.0
        self._blocked_count = 0
        self._preempt_count = 0
        self._dropped_in_queue = 0
        self._done_count = 0          # 已完成任务数（可视化面板高频读取，用计数器比每次遍历更快）
        self._frozen_ts = None        # 仿真结束后冻结的时刻（见 now() 的说明）
        self._last_ts = 0.0

        # ---- OS 微观透视：事件日志 / 并发槽位 / 上下文切换开销 ----
        #: 事件日志（有界环形缓冲）。前端「Trace」面板与 /api/trace 导出都读它。
        self._event_log: deque = deque(
            maxlen=max(1000, cfg_get_int(cfg, "simulation.trace_max_events", 20000)))
        #: 并发槽位（= CPU 核号）。dispatch 时分配，完成/抢占时归还。
        self._free_slots: List[int] = list(range(self.workers))
        self._slot_of: Dict[str, int] = {}
        #: 每架无人机上一次任务结束的仿真时刻 —— 用来算"上下文切换开销"。
        self._last_finish_ts: Dict[int, float] = {}
        self._last_switch_ms: Dict[int, float] = {}
        self._deadlock_recovered = 0
        self._deadlock_suspects = 0
        #: 混沌注入状态（/api/chaos）
        self._chaos: Dict[str, object] = {
            "weaknet_until": 0.0, "weaknet_uavs": set(),
            "burst": 0, "deadlock_until": 0.0, "deadlock_task": None,
        }

        self._events_by_uav: Dict[int, List[TraceEvent]] = {
            uid: trace.events_of(uid) for uid in uav_ids}

    # ==================================================================
    # 时钟
    # ==================================================================
    def now(self) -> float:
        """当前仿真时刻（秒）。未开始时为 0.0；仿真结束后**冻结**在结束时刻。

        为什么结束要冻结：可视化面板在仿真完成后仍会持续轮询快照，
        若时钟继续走，面板上的"仿真时间"会一直涨到 130s/90s 这种荒谬值，
        同时 system_load / 各项时序也会漂移。
        """
        if self._frozen_ts is not None:
            return self._frozen_ts
        if self._t0 is None:
            return 0.0
        return (time.perf_counter() - self._t0) / self.time_scale

    def set_now(self, ts: float) -> None:
        """仅测试用：把时钟原点挪一下，模拟时间推进（不参与正式仿真）。"""
        self._t0 = time.perf_counter() - float(ts) * self.time_scale

    # ==================================================================
    # 天气（运行中切换，/api/weather 与 CLI 共用）
    # ==================================================================
    def set_weather(self, preset: str) -> dict:
        """运行中切换天气（契约 §4.6.3）：立刻作用到链路健康度。

        实现：**新建**一个同 seed 的 WeatherModel（而不是改旧模型），再让
        ``LinkMonitor.set_weather`` 用新曲线重建全部 uav 的健康度序列。
        这样"何时切换"不影响曲线本身（同 seed/preset 的曲线是确定的），
        5 个调度策略回放同一条天气曲线的前提不被破坏。

        返回切换后的 ``/api/state.weather`` 快照（供 HTTP 接口直接回包）。
        """
        new_preset = config_preset(self.cfg, preset)
        self.weather = WeatherModel(
            seed=self.seed, preset=new_preset,
            duration_s=float(self.scenario.duration_s), cfg=self.cfg,
        )
        self.weather_preset = new_preset
        self.link_monitor.set_weather(self.weather)
        logger.info("Simulator 天气切换为 %s（%s）", new_preset, self.weather.label)
        return self.weather.snapshot(self.now())

    def weather_snapshot(self) -> dict:
        """当前时刻的天气快照（= ``/api/state`` 的 weather 字段）。"""
        return self.weather.snapshot(self.now())

    def weather_samples(self) -> List[dict]:
        """整条天气曲线的 dict 列表（仿真结束时写入 ``RunResult.weather_samples``）。"""
        return [s.to_dict() for s in self.weather.samples()]

    # ==================================================================
    # 任务工厂与服务时长标定
    # ==================================================================
    def link_factor(self, uav_id: int, t: float, station_id: Optional[int] = None) -> float:
        """链路折损系数 ∈ [floor, 1]：链路越差，同一份数据要占用越长服务时间。

        ``station_id=None`` 查默认站（单站模式下语义与数值完全不变）。
        """
        health = self.link_monitor.health_at(uav_id, t, station_id)
        return self.bandwidth_floor + (1.0 - self.bandwidth_floor) * max(0.0, min(100.0, health)) / 100.0

    def compute_service_s(self, data_size_mb: float, uav_id: int, t: float,
                          station_id: Optional[int] = None) -> float:
        """任务服务时长（仿真秒）= 数据量 / (单线程带宽 × 链路折损系数)。

        带宽取**任务归属站**的带宽（边缘站带宽更低，这正是"下沉边缘更近
        但更薄"的代价）；单站时等于 ``runtime.worker_bandwidth_mbps``，
        与改造前逐位一致。
        """
        bw = self.bandwidth_mbps
        node = self.stations.node(int(station_id)) if station_id is not None else None
        if node is not None:
            bw = float(node.spec.bandwidth_mbps)
        eff_bw = bw * self.link_factor(uav_id, t, station_id)
        return max(1e-6, float(data_size_mb) / max(1e-9, eff_bw))

    def _mec_target_station(self, task_type: TaskType) -> Optional[int]:
        """MEC 卸载决策：按任务分级挑一个"该角色且在线"的站（最空闲优先）。

        返回 None 表示"不干预"（MEC 关闭 / 单站 / 该角色当前无可用站）——
        此时任务落在无人机原生归属站，与改造前行为一致。这就是**优雅降级**：
        边缘站全挂了不会让轻量任务无家可归，而是照旧回中心。
        """
        if not self.mec.enabled or not self.stations.is_multi:
            return None
        role = desired_role_for(task_type, self.cfg)
        cands = [n for n in self.stations.nodes
                 if n.online and str(n.spec.role).upper() == role]
        if not cands:
            return None
        rr = self._running_remaining_by_station()
        best = min(cands, key=lambda n: (n.load(float(rr.get(int(n.spec.id), 0.0))),
                                        int(n.spec.id)))
        return int(best.spec.id)

    def make_task(self, uav_id: int, task_type: TaskType, data_size_mb: float,
                  now: float) -> Task:
        """按当前链路健康度把一条到达事件变成真正的 `Task`。

        **不要绕过本方法直接 new Task**，否则服务时长标定会与系统容量模型不一致。

        多基站 + MEC 开启时，这里同时完成**初始放置**：轻量运维任务下沉边缘站、
        重勘查任务回中心站（放置不是迁移，故 ``home_station_id`` 与之一致，
        ``migrate_count`` 保持 0 —— 迁移只统计"事后被搬走"）。
        """
        station_id = int(self._uav_station.get(int(uav_id), self.stations.default_id))
        mec_sid = self._mec_target_station(task_type)
        if mec_sid is not None:
            station_id = mec_sid
        health = self.link_monitor.health_at(uav_id, now, station_id)
        task = Task(
            task_id=new_task_id("T"),
            uav_id=int(uav_id),
            priority=task_type.priority,
            task_type=task_type,
            data_size_mb=float(data_size_mb),
            submit_ts=float(now),
            service_s=self.compute_service_s(data_size_mb, uav_id, now, station_id),
            station_id=station_id,
            home_station_id=station_id,
        )
        task.enqueue_ts = float(now)
        task.link_health = float(health)
        return task

    # ==================================================================
    # 提交与派发
    # ==================================================================
    # ------------------------------------------------------------------
    # OS 微观透视：事件日志（Trace）
    # ------------------------------------------------------------------
    def _log_event(self, kind: str, task: Optional[Task] = None, **extra) -> None:
        """追加一条 trace 事件（有界环形缓冲；异常一律吞掉，不影响仿真主流程）。"""
        try:
            ev: dict = {"t": round(self.now(), 3), "k": kind}
            if task is not None:
                ev["tid"] = task.task_id
                ev["uav"] = int(task.uav_id)
                ev["prio"] = task.priority.name
                ev["type"] = task.task_type.value
            if extra:
                ev.update(extra)
            self._event_log.append(ev)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # 混沌工程：弱网风暴 / 并发洪峰 / 死锁注入
    # ------------------------------------------------------------------
    def inject_weaknet(self, duration_s: float = 20.0, frac: float = 0.4) -> dict:
        """让前 frac 比例的无人机进入"弱网风暴"：失败率放大 2.5 倍。"""
        now = self.now()
        n = max(1, int(round(self.scenario.uav_count * max(0.05, min(1.0, float(frac))))))
        uavs = set(range(1, n + 1))
        self._chaos["weaknet_until"] = now + max(1.0, float(duration_s))
        self._chaos["weaknet_uavs"] = uavs
        self._log_event("chaos_weaknet", None, duration_s=duration_s, uav_count=n)
        return {"ok": True, "kind": "weaknet", "uav_count": n,
                "until": round(now + float(duration_s), 2)}

    def inject_burst(self, n: int = 500) -> dict:
        """瞬间注入 n 个高优勘查任务（并发洪峰），压测限流/削峰。"""
        now = self.now()
        n = max(1, min(5000, int(n)))
        made = 0
        for i in range(n):
            uav = (i % max(1, int(self.scenario.uav_count))) + 1
            try:
                t = self.make_task(uav, TaskType.IMAGERY, 900.0, now)
                if self.submit_task(t):
                    made += 1
            except Exception:  # pragma: no cover
                break
        self._chaos["burst"] = int(self._chaos.get("burst", 0)) + made
        self._log_event("chaos_burst", None, requested=n, injected=made)
        return {"ok": True, "kind": "burst", "injected": made}

    def inject_deadlock(self, hold_s: float = 20.0) -> dict:
        """把当前剩余量最大的运行中任务"卡死" hold_s 秒；看门狗会在 3 秒后强制回收。"""
        now = self.now()
        with self._lock:
            running = list(self._running.values())
        if not running:
            return {"ok": False, "msg": "当前没有运行中的任务，无法注入死锁"}
        task = max(running, key=lambda x: x[0].remaining_s)[0]
        task._hang_until = now + max(2.0, float(hold_s))
        task._hang_since = now
        self._chaos["deadlock_until"] = now + max(2.0, float(hold_s))
        self._chaos["deadlock_task"] = task.task_id
        self._log_event("chaos_deadlock", task, hold_s=float(hold_s))
        return {"ok": True, "kind": "deadlock", "task_id": task.task_id,
                "uav": int(task.uav_id)}

    # ------------------------------------------------------------------
    # 混沌工程扩展：节点宕机 / 网络分区 / 脑裂（改造建议第五章 C）
    # ------------------------------------------------------------------
    def inject_node_down(self, station_id: Optional[int] = None,
                         duration_s: float = 20.0) -> dict:
        """让某个服务器节点宕机（默认 2 号边缘站；单站模式拒绝执行）。

        宕机后：该站 ``online=False``，站上排队任务由 ``_process_station_down``
        疏散到存活站（**带迁移代价**），运行中的任务在模拟里继续跑完 —— 因此
        "任务重调度"这条链在迁移次数与站级 done 上都能看到。
        ``duration_s<=0`` 表示不自动恢复，需手动 ``recover``。
        """
        if not self.stations.is_multi:
            return {"ok": False, "msg": "当前是单站模式，无节点可宕机（请用 stations=triple）"}
        now = self.now()
        sid = int(station_id) if station_id else int(self.stations.nodes[-1].spec.id)
        node = self.stations.node(sid)
        if node is None:
            return {"ok": False, "msg": f"未知站点 {sid}"}
        self.stations.set_online(sid, False, now)
        dur = float(duration_s)
        if dur > 0:
            self._chaos["node_down_until"] = dict(self._chaos.get("node_down_until") or {})
            self._chaos["node_down_until"][sid] = now + dur
        self._log_event("chaos_node_down", None, station=sid,
                        duration_s=dur)
        return {"ok": True, "kind": "node_down", "station": sid,
                "until": round(now + dur, 2) if dur > 0 else None}

    @staticmethod
    def _as_ids(value) -> List[int]:
        """把 ``None`` / ``1`` / ``"2,3"`` / ``[2, 3]`` 一律规整成 ``[2, 3]``。"""
        if value is None:
            return []
        if isinstance(value, (int, float)):
            return [int(value)]
        if isinstance(value, str):
            parts = value.split(",")
        else:
            try:
                parts = list(value)
            except TypeError:
                return []
        out: List[int] = []
        for p in parts:
            try:
                out.append(int(str(p).strip()))
            except (TypeError, ValueError):
                continue
        return out

    def inject_partition(self, a: Optional[Iterable[int]] = None,
                         b: Optional[Iterable[int]] = None,
                         duration_s: float = 20.0) -> dict:
        """制造网络分区：``a`` 组与 ``b`` 组互不可达（跨区不再迁移）。

        缺省 ``a=[1]``、``b=其余站`` —— 即"边缘站与中心云失联"这一最典型场景。
        ``a``/``b`` 接受 int、``"2,3"`` 字符串或 id 列表。
        """
        if not self.stations.is_multi:
            return {"ok": False, "msg": "当前是单站模式，无分区可制造（请用 stations=triple）"}
        now = self.now()
        ids = [int(n.spec.id) for n in self.stations.nodes]
        ga = self._as_ids(a) or [ids[0]]
        gb = self._as_ids(b) or [i for i in ids if i not in ga]
        if not gb:
            return {"ok": False, "msg": "分区两组不能有一组为空"}
        groups = self.stations.set_partition([ga, gb])
        dur = float(duration_s)
        if dur > 0:
            self._chaos["partition_until"] = now + dur
        self._chaos["partition_groups"] = groups
        self._log_event("chaos_partition", None, groups=groups, duration_s=dur)
        return {"ok": True, "kind": "partition", "groups": groups,
                "until": round(now + dur, 2) if dur > 0 else None}

    def inject_split_brain(self, duration_s: float = 20.0) -> dict:
        """制造脑裂：分区两侧各自认为自己是主，各自独立调度且拒绝跨区迁移。

        实现上 = 先把中心站与边缘站分成两组，再置 ``split_brain=True``。
        与普通分区的区别在语义与面板呈现（两侧都"自认主"），可达性判定一致。
        """
        if not self.stations.is_multi:
            return {"ok": False, "msg": "当前是单站模式，无脑裂可制造（请用 stations=triple）"}
        ids = [int(n.spec.id) for n in self.stations.nodes]
        centers = [int(n.spec.id) for n in self.stations.nodes
                   if str(n.spec.role).upper() == "CENTER"] or [ids[0]]
        edges = [i for i in ids if i not in centers] or ids[1:]
        groups = self.stations.set_partition([centers, edges])
        self.stations.split_brain = True
        now = self.now()
        dur = float(duration_s)
        if dur > 0:
            self._chaos["partition_until"] = now + dur
        self._chaos["partition_groups"] = groups
        self._log_event("chaos_split_brain", None, groups=groups, duration_s=dur)
        return {"ok": True, "kind": "split_brain", "groups": groups,
                "until": round(now + dur, 2) if dur > 0 else None}

    def recover_nodes(self) -> dict:
        """一键恢复：全部站点上线 + 解除分区 + 清除脑裂。"""
        up = self.stations.nodes_down()
        self.stations.recover_all()
        self._chaos["node_down_until"] = {}
        self._chaos["partition_until"] = 0.0
        self._chaos["partition_groups"] = []
        self._log_event("chaos_recover", None, restored=up)
        return {"ok": True, "kind": "recover", "restored": up}

    def _process_chaos_recovery(self, now: float) -> None:
        """到期自动恢复：节点宕机时长到点自动上线，分区/脑裂到点自动解除。"""
        until_map = self._chaos.get("node_down_until") or {}
        if isinstance(until_map, dict) and until_map:
            left = {}
            for sid, ts in list(until_map.items()):
                if float(ts) <= now:
                    self.stations.set_online(int(sid), True, now)
                    self._log_event("chaos_node_up", None, station=int(sid))
                else:
                    left[int(sid)] = float(ts)
            self._chaos["node_down_until"] = left
        p_until = float(self._chaos.get("partition_until", 0.0) or 0.0)
        if p_until > 0 and now >= p_until:
            self.stations.clear_partition()
            self._chaos["partition_until"] = 0.0
            self._chaos["partition_groups"] = []
            self._log_event("chaos_partition_heal", None)

    def submit_task(self, task: Task) -> bool:
        """生产者线程提交任务（线程安全）；队列满则丢弃并计数。"""
        if task is None:
            return False
        with self._lock:
            if task.task_id not in self._by_id:
                self._by_id[task.task_id] = task
                self._tasks.append(task)
        ok = self.queue.enqueue(task, self.now())
        self._log_event("enqueue", task, size_mb=round(float(task.data_size_mb), 1))
        if not ok:
            with self._lock:
                self._dropped_in_queue += 1
            task.state = TaskState.DROPPED
            task.drop_reason = "queue_full"
            task.finish_ts = self.now()
        return ok

    def dispatch(self, task: Task, limit_s: Optional[float] = None,
                 station_id: Optional[int] = None) -> bool:
        """把已获得限流许可的任务交给工作线程池（契约 §4.6）。

        ``limit_s``：本轮最多允许运行的仿真秒数（RR / MLFQ 的时间片，None = 跑完）。
        ``station_id``：指定由哪个站的线程池执行；None = 取任务当前归属站。
        返回 False 表示未受理（仿真已停止 / 线程池已关闭），调用方应释放限流许可。
        """
        if task is None:
            return False
        now = self.now()
        slot = -1
        # 解析目标站（单站时恒为默认站，行为与改造前一致）
        sid = int(station_id if station_id is not None else
                  getattr(task, "station_id", self.stations.default_id))
        node = self.stations.node(sid)
        if node is None:
            node = self.stations.node(self.stations.default_id)
            sid = int(node.spec.id) if node is not None else self.stations.default_id
            task.station_id = sid
        if node is not None and not node.online:
            # 目标站宕机：拒绝派发，让调用方释放许可、任务留队等重调度
            return False
        with self._lock:
            if self.stop_event.is_set():
                return False
            if task.task_id in self._running:
                return False
            remaining = task.remaining_s
            exec_s = remaining if limit_s is None else min(remaining, float(limit_s))
            if exec_s <= EPS:
                exec_s = remaining
            if exec_s <= EPS:
                # 已经跑完的任务不应再进线程池
                task.state = TaskState.DONE
                task.finish_ts = now
                if node is not None:
                    node.done += 1
                return False
            self._running[task.task_id] = (task, now)
            # 分配并发槽位（= 该站绑定的"CPU 核号"），并计算本次上下文切换开销
            slot = node.take_slot(task.task_id) if node is not None else -1
            self._slot_of[task.task_id] = slot
            prev_finish = self._last_finish_ts.get(int(task.uav_id))
            if prev_finish is not None:
                self._last_switch_ms[int(task.uav_id)] = round(
                    max(0.0, now - prev_finish) * 1000.0, 2)
            task.state = TaskState.RUNNING
            if node is not None:
                node.dispatched += 1
        self._log_event("dispatch", task, slot=slot, station=sid,
                        slice_ms=(None if limit_s is None else round(float(limit_s) * 1000.0, 1)))
        executor = node.executor if node is not None else self._executor
        if executor is None:
            executor = self._executor
        try:
            executor.submit(self._run_task, task, exec_s)  # type: ignore[union-attr]
        except RuntimeError:  # 线程池已关闭
            with self._lock:
                self._running.pop(task.task_id, None)
            return False
        # ---- MEC 端到端时延记账（**与策略无关**，对所有策略统一口径） ----
        # 关键设计：e2e 是"度量"，不是"策略"。mec.enable 只控制**卸载决策**
        # （轻量任务是否下沉边缘），不控制是否统计时延。否则"纯中心"模式下
        # avg_e2e_ms 恒为 0，"纯中心 vs 端-边-云"就没有可比口径了。
        if node is not None:
            try:
                self.mec.note_dispatch(task, str(node.spec.role))
            except Exception:  # pragma: no cover - 记账失败不影响仿真
                pass
        return True

    def _release_slot(self, task: Task, now: Optional[float] = None) -> None:
        """归还并发槽位（= 释放 CPU 核），并记录该机本次任务结束时刻。

        上下文切换开销 = 本机上一次任务结束 → 本次任务被派发 之间的仿真时间差，
        在 ``dispatch()`` 里算好写进 ``_last_switch_ms``。
        多站时归还到**任务归属站**的槽位表（站内编号）。
        """
        try:
            with self._lock:
                self._slot_of.pop(task.task_id, -1)
                self._last_finish_ts[int(task.uav_id)] = float(
                    now if now is not None else self.now())
            node = self.stations.node(int(getattr(task, "station_id",
                                                    self.stations.default_id)))
            if node is not None:
                node.release_slot(task.task_id)
        except Exception:  # pragma: no cover
            logger.exception("_release_slot 异常 task=%s", getattr(task, "task_id", "?"))

    def _reap_deadlocks(self, now: float) -> int:
        """死锁看门狗：把"注入后卡住超过 3 仿真秒"的任务强制回收。

        真实系统里「任务占着 IO 不返回」会永久占住一个并发槽位，最终拖垮线程池。
        这里模拟完整闭环：**超时检测 → 标记可疑 → 强制回收 → 记事件**。
        前端「混沌」面板会看到 ``deadlock_recovered`` 事件与计数变化。
        """
        recovered = 0
        suspects = 0
        with self._lock:
            items = list(self._running.items())
        for tid, (task, _disp_ts) in items:
            since = float(getattr(task, "_hang_since", 0.0) or 0.0)
            if since <= 0.0:
                continue
            if now - since < 3.0:
                suspects += 1
                continue
            with self._lock:
                self._running.pop(tid, None)
            try:
                self.limiter.release(task)
            except Exception:  # pragma: no cover
                pass
            task.state = TaskState.DONE
            task.finish_ts = now
            task.drop_reason = "deadlock_recovered"
            task._hang_since = 0.0
            task._hang_until = 0.0
            self._deadlock_recovered += 1
            self._log_event("deadlock_recovered", task, held_s=round(now - since, 2))
            self._release_slot(task, now)
            recovered += 1
        self._deadlock_suspects = suspects
        return recovered

    # ------------------------------------------------------------------
    # 工作线程体：模拟一次「上传」
    # ------------------------------------------------------------------
    def _run_task(self, task: Task, exec_s: float) -> None:
        """工作线程：sleep 模拟 IO 传输 -> 判完成 / 判弱网失败 / 判时间片耗尽。"""
        try:
            sleep_real = exec_s * self.time_scale
            if sleep_real > 0:
                time.sleep(sleep_real)

            # ---- 混沌工程：死锁注入（任务被"卡住"不返回） ----
            hang_until = float(getattr(task, "_hang_until", 0.0) or 0.0)
            if hang_until > 0:
                extra_s = max(0.0, hang_until - self.now())
                if extra_s > 0:
                    time.sleep(extra_s * self.time_scale)

            now = self.now()
            if task.state is TaskState.DONE:
                # 看门狗已判定死锁并强制回收 —— 本线程只负责安静退出，不重复记账
                self._release_slot(task, now)
                return
            task.served_s = min(task.service_s, task.served_s + exec_s)
            finished = task.served_s + 1e-9 >= task.service_s

            with self._lock:
                self._running.pop(task.task_id, None)
            self._release_slot(task, now)

            if not finished:
                # ---- 时间片耗尽 / 被抢占 ----
                # 归位方式由策略声明（`preempt_requeue`）：
                #   "tail"  -> RR / MLFQ 语义：被剥夺 CPU 后重新排到队尾，
                #              让其它同优先级任务获得机会（轮转的本质）；
                #   "front" -> 动态权重语义：**保住本优先级内的排队位置**。
                #              为什么需要"front"：勘查任务服务时长 ~11 仿真秒，
                #              若每次都排到 400 个任务的队尾，峰值工况下永远轮不完
                #              —— 实测高优先级任务完成率会从 44% 掉到 24%。
                #              回到队首则"片间不失优先级"，同时片与片之间会释放
                #              并发槽位给运维任务使用，做到长短任务兼顾。
                self._preempt_count += 1
                task.preempt_count += 1
                self.limiter.release(task)
                self._log_event("preempt", task, served_s=round(float(task.served_s), 3))
                if getattr(self._scheduler, "preempt_requeue", "front") == "tail":
                    self.queue.requeue(task, now)
                else:
                    self.queue.push_front(task, now)
                return

            # ---- 上传完成 -> 弱网失败判定 ----
            # 链路健康度按**任务归属站**取（多站时"该机到该站"的链路）；
            # 单站时等价于改造前的 health_at(uav, now)，逐位一致。
            health = self.link_monitor.health_at(
                task.uav_id, now, int(getattr(task, "station_id", self.stations.default_id)))
            p_fail = self._failure_probability(health, task.uav_id)
            self.limiter.release(task)

            if p_fail > 0 and self._rng.random() < p_fail:
                self._on_upload_failed(task, health, now)
                return

            task.state = TaskState.DONE
            task.finish_ts = now
            with self._lock:
                self._done_count += 1
            done_node = self.stations.node(int(getattr(task, "station_id",
                                                       self.stations.default_id)))
            if done_node is not None:
                done_node.done += 1
            self._log_event("done", task, wait_ms=round(float(task.wait_ms_total), 1),
                            retry=int(task.retry_count))
            if self._scheduler is not None:
                try:
                    self._scheduler.add_task_finished(task, task.service_s)
                except Exception:  # pragma: no cover - 回调异常不能弄死工作线程
                    logger.exception("add_task_finished 回调异常 task=%s", task.task_id)
        except Exception:  # pragma: no cover - 兜底，绝不让工作线程静默死亡
            logger.exception("工作线程执行任务异常 task=%s", getattr(task, "task_id", "?"))
            try:
                self.limiter.release(task)
            except Exception:
                pass

    def _failure_probability(self, health: float, uav_id: Optional[int] = None) -> float:
        """弱网失败概率 p_fail = (1 - health/100) × retry_gain，clamp 到 [0,1]。

        物理意义：链路越差，一次上传被丢包/超时打断的概率越高。
        ``retry_gain`` 是"重传放大系数"，对应 PDF「任务反复重传"的现象。
        """
        raw = (1.0 - max(0.0, min(100.0, health)) / 100.0) * self.retry_gain
        # 混沌工程「弱网风暴」：被选中的无人机，失败率放大 2.5 倍
        try:
            if (uav_id is not None
                    and self.now() < float(self._chaos.get("weaknet_until", 0.0))
                    and int(uav_id) in self._chaos.get("weaknet_uavs", set())):
                raw *= 2.5
        except Exception:  # pragma: no cover
            pass
        return max(0.0, min(1.0, raw))

    def _on_upload_failed(self, task: Task, health: float, now: float) -> None:
        """上传失败 -> 退避重传；动态策略在严重弱网下改走本地缓存（CACHED）。"""
        task.retry_count += 1
        task.served_s = 0.0  # 数据作废，需整包重传
        with self._lock:
            self._blocked_count += 1

        link_aware = bool(getattr(self._scheduler, "link_aware", False))
        if link_aware and health < self.cache_health_threshold:
            # 弱网自适应：本地缓存，不占并发槽位，等链路恢复再补传
            task.cached = True
            task.state = TaskState.CACHED
            delay = self.cache_retry_backoff_s
            self._log_event("cache", task, health=round(float(health), 1),
                            delay_s=round(float(delay), 2))
        else:
            task.state = TaskState.BLOCKED
            # 指数退避（避免失败任务在同一毫秒内反复重试打爆线程池）
            # 上限 2^3 = 8 倍基数：退避太长会让任务"消失"于队列之外，
            # 既不真实（真实重传是立即重试）也会稀释饥饿与队列堆积的统计。
            delay = self.retry_backoff_base_s * (2 ** min(max(0, task.retry_count - 1), 3))
            self._log_event("retry", task, health=round(float(health), 1),
                            delay_s=round(float(delay), 2),
                            retry=int(task.retry_count))
        with self._lock:
            self._retry_pending.append((now + delay, task))

    def _flush_retries(self, now: float) -> None:
        """把到期的重传任务重新投入就绪队列（对应 IO 完成后进程被唤醒）。"""
        with self._lock:
            if not self._retry_pending:
                return
            due = [item for item in self._retry_pending if item[0] <= now]
            if not due:
                return
            self._retry_pending = [item for item in self._retry_pending if item[0] > now]
        for _, task in due:
            self.queue.enqueue(task, now)

    # ==================================================================
    # 上下线与状态
    # ==================================================================
    def is_uav_online(self, uav_id: int) -> bool:
        return self._uav_online.get(int(uav_id), True)

    def mark_uav_returned(self, uav_id: int) -> None:
        """生产者线程自然结束（= 线程销毁）时登记状态。"""
        with self._lock:
            st = self._uav_status.get(int(uav_id))
            if st is not None and st.state is not UAVState.OFFLINE:
                st.state = UAVState.OFFLINE

    def _process_churn(self, now: float) -> None:
        """处理设备动态上下线事件（对应 OS 线程的创建与销毁）。"""
        churn = self.trace.churn
        while self._churn_idx < len(churn) and churn[self._churn_idx].t <= now:
            ev = churn[self._churn_idx]
            self._churn_idx += 1
            uid = int(ev.uav_id)
            if ev.action == "OFFLINE":
                self._uav_online[uid] = False
                self.link_monitor.unregister_uav(uid)
                with self._lock:
                    self._uav_status[uid].state = UAVState.RETURNING
            else:
                self._uav_online[uid] = True
                self.link_monitor.register_uav(uid)
                with self._lock:
                    self._uav_status[uid].state = UAVState.ONLINE

    # ==================================================================
    # 负载与统计
    # ==================================================================
    def busy_workers(self) -> int:
        """当前占用工作线程的任务数。"""
        with self._lock:
            return len(self._running)

    def pending_service_s(self) -> float:
        """尚未完成的工作总量（仿真秒）：就绪队列 + 正在运行。"""
        total = self.queue.total_remaining_s()
        with self._lock:
            total += sum(t.remaining_s for t, _ in self._running.values())
        return total

    def system_load(self) -> float:
        """系统负载（归一化）：积压服务秒 / (工作线程数 × LOAD_HORIZON_S)。

        >1 表示按满负荷也要超过视界才能排空 —— 即**过载**。
        带 50 ms 缓存：主循环每 2 ms 调一次，而积压量变化不快，
        没有缓存会在 2700 任务的峰值工况下产生可观的重复求和开销。
        """
        now = self.now()
        cached_at, cached_val = self._load_cache
        if now - cached_at < 0.05 and now >= cached_at:
            return cached_val
        pending = self.pending_service_s()
        load = pending / max(1e-9, float(self.workers) * LOAD_HORIZON_S)
        self._load_cache = (now, load)
        return load

    def is_overloaded(self) -> bool:
        return self.system_load() > self.overload_threshold

    def all_tasks(self) -> List[Task]:
        """全部任务快照（含未完成），供指标计算。"""
        with self._lock:
            return list(self._tasks)

    def blocked_count(self) -> int:
        with self._lock:
            return self._blocked_count

    def queue_samples(self) -> List[QueueSnapshot]:
        return list(self._queue_samples)

    def link_samples(self) -> List[LinkSample]:
        return self.link_monitor.samples()

    def uav_status(self) -> Dict[int, UAVStatus]:
        with self._lock:
            return dict(self._uav_status)

    # ==================================================================
    # 主循环
    # ==================================================================
    def run(self, scheduler, duration_s: Optional[float] = None) -> dict:
        """跑完整仿真（契约 §4.6）。

        流程：启动生产者线程 -> 循环「处理上下线 / 冲刷重传 / 推进链路 /
        调度一轮 / 采样 / 累计过载」 -> 停止 -> 收尾。
        """
        duration = float(duration_s if duration_s is not None else self.scenario.duration_s)
        self._scheduler = scheduler
        self.stop_event.clear()
        # ---- 线程池：单站时 1 个（与改造前一致）；多站时每站一个独立线程池 ----
        if self.stations.is_multi:
            for node in self.stations.nodes:
                node.executor = ThreadPoolExecutor(
                    max_workers=max(1, int(node.spec.workers)),
                    thread_name_prefix=f"Station-{node.spec.id}")
            self._executor = None
        else:
            self.stations.nodes[0].executor = ThreadPoolExecutor(
                max_workers=max(1, self.workers), thread_name_prefix="UAV-Worker")
            self._executor = self.stations.nodes[0].executor

        workers: List[UAVWorker] = []
        t_start = time.perf_counter()
        self._t0 = t_start

        # ---- 启动全部无人机生产者线程（= 创建 30 个进程/线程） ----
        for uid, events in self._events_by_uav.items():
            with self._lock:
                self._uav_status[uid].state = UAVState.ONLINE
                self._uav_status[uid].online_ts = 0.0
            w = UAVWorker(uid, self, events)
            w.start()
            workers.append(w)

        next_sample_ts = 0.0
        prev_ts: Optional[float] = None

        try:
            while True:
                now = self.now()
                if now >= duration:
                    break
                self._last_ts = now

                self._process_churn(now)
                self._flush_retries(now)
                self.stations.flush_migrations(now)   # 到期的迁移任务投入目标站
                self.link_monitor.tick(now)
                self._reap_deadlocks(now)
                self._process_chaos_recovery(now)     # 宕机/分区到期自动恢复
                self._process_station_down(now)       # 节点宕机 -> 疏散该站队列

                # ---- 调度一轮（策略实现，绝不阻塞） ----
                try:
                    scheduler.step()
                except Exception:  # pragma: no cover - 策略异常不得终止仿真
                    logger.exception("调度器 step() 异常（策略=%s）", self.scheduler_name)

                # ---- 过载持续时长累计 ----
                if prev_ts is not None and self.is_overloaded():
                    self._overload_seconds += max(0.0, now - prev_ts)
                prev_ts = now

                # ---- 队列时序采样 ----
                if now >= next_sample_ts:
                    try:
                        stats = scheduler.get_stats()
                    except Exception:  # pragma: no cover
                        stats = {}
                    self._queue_samples.append(self.queue.snapshot(
                        now=now,
                        running_count=self.busy_workers(),
                        blocked_count=self._blocked_count,
                        system_load=self.system_load(),
                        high_slice_ms=float(stats.get("high_time_slice_ms", 0.0)),
                        low_slice_ms=float(stats.get("low_time_slice_ms", 0.0)),
                        high_weight=float(stats.get("high_weight", 0.0)),
                        low_weight=float(stats.get("low_weight", 0.0)),
                    ))
                    next_sample_ts += self.queue_sample_interval_s

                time.sleep(self.scheduler_tick_real_s)
        finally:
            # ---- 收尾：停生产者 -> 停线程池 ----
            self.stop_event.set()
            for w in workers:
                w.join(timeout=1.0)
            for node in self.stations.nodes:
                if node.executor is not None:
                    node.executor.shutdown(wait=True)
            if self._executor is not None:
                self._executor.shutdown(wait=True)
            # 正常跑完 -> 取 duration（不要取"多跑了一帧"的 now，否则面板会显示 90.5/90）；
            # 被提前停止 -> 取当前时刻。两种情况都冻结下来供后续读取。
            end_ts = min(self.now(), duration)
            self._frozen_ts = end_ts

        # ---- 清点 ----
        tasks = self.all_tasks()
        done = [t for t in tasks if t.state is TaskState.DONE]
        failed_net = [t for t in tasks if t.retry_count > 0]
        still_in_queue = self.queue.total_depth

        return {
            "duration_s": duration,
            "end_ts": end_ts,
            "tasks": tasks,
            "submitted": len(tasks),
            "done": len(done),
            "queue_samples": self.queue_samples(),
            "link_samples": self.link_samples(),
            "context_switches": int(getattr(scheduler, "context_switches", 0)),
            "overload_seconds": float(self._overload_seconds),
            "limiter_status": self.limiter.get_status(),
            "dropped_in_queue": int(self._dropped_in_queue),
            "blocked_count": int(self._blocked_count),
            "preempt_count": int(self._preempt_count),
            "retried_tasks": len(failed_net),
            "still_in_queue": int(still_in_queue),
            "scheduler_stats": (scheduler.get_stats() if hasattr(scheduler, "get_stats") else {}),
            "uav_status": self.uav_status(),
            # 天气曲线快照（0.5 s 网格逐点 dict；ExperimentRunner 写入 RunResult）
            "weather_samples": self.weather_samples(),
            # ---- 多基站 / MEC 收尾快照（供 run.py mec / 报告使用） ----
            "stations": self.stations.snapshot({}, end_ts),
            "station_metrics": self._station_metrics(end_ts),
            "mec": self.mec.snapshot(),
            "stop_reason": "duration_reached",
        }

    # ==================================================================
    # 多基站辅助：站级指标 / 宕机疏散
    # ==================================================================
    def _running_remaining_by_station(self) -> Dict[int, float]:
        """每站正在运行任务的剩余服务秒之和（站级负载的"在途"部分）。"""
        out: Dict[int, float] = {}
        with self._lock:
            items = list(self._running.values())
        for task, _ in items:
            sid = int(getattr(task, "station_id", self.stations.default_id))
            out[sid] = out.get(sid, 0.0) + float(task.remaining_s)
        return out

    def _station_of_uav(self) -> Dict[int, int]:
        """每架无人机**当前接入**的站 id（契约 ``live.station_of_uav``）。

        接入点不是一成不变的：它所属的站宕机后，该机会被**改接到存活站**
        （一致性哈希顺时针找下一个可用站）。这正是 3D 场景里"切换站点过渡动画"
        的数据来源，也让"节点宕机 -> 任务重调度"在每机层面可见。
        """
        out: Dict[int, int] = {}
        for uid in range(1, int(self.scenario.uav_count) + 1):
            sid = int(self._uav_station.get(uid, self.stations.default_id))
            node = self.stations.node(sid)
            if node is not None and node.online:
                out[uid] = sid
                continue
            new_sid = int(self.stations.pick_by_hash(f"uav:{uid}"))
            target = self.stations.node(new_sid)
            if target is None or not target.online:
                alive = self.stations.alive_nodes()
                new_sid = int(alive[0].spec.id) if alive else int(self.stations.default_id)
            if new_sid != sid:
                if node is not None:
                    node.uavs.discard(uid)
                self._uav_station[uid] = new_sid
                nn = self.stations.node(new_sid)
                if nn is not None:
                    nn.uavs.add(uid)
                self._log_event("uav_rehome", None, uav=int(uid), frm=sid, to=new_sid)
            out[uid] = int(self._uav_station.get(uid, sid))
        return out

    def _station_metrics(self, end_ts: float) -> Dict[str, float]:
        """站级 / 迁移 / MEC 汇总指标（供 metrics.compute_metrics 与报告使用）。"""
        rrs = self._running_remaining_by_station()
        stations = self.stations.snapshot(rrs, end_ts)
        max_q = max((s["queue_depth"] for s in stations), default=0)
        load_imb = self.stations.load_imbalance(rrs)
        tasks = self.all_tasks()
        migrated = [t for t in tasks if int(getattr(t, "migrate_count", 0)) > 0]
        mig_cost = ([float(t.migration_cost_ms) for t in migrated])
        mec = self.mec.snapshot()
        return {
            "station_max_queue_depth": float(max_q),
            "station_load_imbalance": float(load_imb),
            "migration_count": float(self.stations.migrations_total),
            "migration_cost_ms": float(sum(mig_cost) / len(mig_cost)) if mig_cost else 0.0,
            "cross_node_comm_ms": float(sum(mig_cost)),
            "edge_offload_ratio": float(mec.get("edge_offload_ratio", 0.0)) * 100.0,
            "avg_e2e_ms": float(mec.get("avg_e2e_ms", 0.0)),
            "edge_e2e_ms": float(mec.get("avg_e2e_edge_ms", 0.0)),
            "center_e2e_ms": float(mec.get("avg_e2e_center_ms", 0.0)),
            "e2e_if_edge_ms": float(mec.get("e2e_if_edge_ms", 0.0)),
            "e2e_if_center_ms": float(mec.get("e2e_if_center_ms", 0.0)),
            "e2e_light_if_edge_ms": float(mec.get("e2e_light_if_edge_ms", 0.0)),
            "e2e_light_if_center_ms": float(mec.get("e2e_light_if_center_ms", 0.0)),
            "e2e_heavy_if_edge_ms": float(mec.get("e2e_heavy_if_edge_ms", 0.0)),
            "e2e_heavy_if_center_ms": float(mec.get("e2e_heavy_if_center_ms", 0.0)),
            "mec_crossing_data_mb": float(mec.get("crossing_data_mb", 0.0)),
            # 任务分级计数（报告必须交代口径：本工况 HIGH:LOW 配比决定"边缘分流比例"
            # 的可解释性，否则 98% 的边缘分流会被误读成"MEC 很成功"）。
            "mec_light_tasks": float(mec.get("light_tasks", 0)),
            "mec_heavy_tasks": float(mec.get("heavy_tasks", 0)),
        }

    def _process_station_down(self, now: float) -> None:
        """节点宕机看门狗：站 offline 后，把其队列里的任务疏散到存活站。

        与死锁看门狗同构的"检测 -> 处置 -> 记事件"闭环：
        疏散通过 ``StationCluster.evacuate_station`` 走**带代价的迁移**，因此
        "节点宕机 -> 任务重调度"整条链在指标上可见（迁移次数上升 + 站 done 下降）。
        """
        for node in self.stations.nodes:
            if node.online:
                continue
            if node.queue.total_depth <= 0:
                continue
            try:
                moved = self.stations.evacuate_station(int(node.spec.id), now)
            except Exception:  # pragma: no cover
                moved = 0
            if moved:
                self._log_event("node_evacuate", None, station=int(node.spec.id),
                                moved=int(moved))

    # ==================================================================
    # 实时快照（供可视化面板高频轮询，必须轻量）
    # ==================================================================
    # ------------------------------------------------------------------
    # 快照辅助：单机透视 / 权重因子 / 内核级指标
    # ------------------------------------------------------------------
    def _uav_detail(self, running: List[dict], health: dict, online: dict,
                    station_of: Optional[Dict[int, int]] = None) -> Dict[str, dict]:
        """把"运行中任务"反查回每架无人机，得到线程五态 + 绑核 + 切换开销。"""
        by_uav = {int(r["uav_id"]): r for r in running}
        if station_of is None:
            station_of = self._station_of_uav()
        out: Dict[str, dict] = {}
        for uid in range(1, int(self.scenario.uav_count) + 1):
            r = by_uav.get(uid)
            is_online = bool(online.get(uid, True))
            if r is not None:
                state = "RUNNING"
            elif is_online:
                state = "READY"
            else:
                state = "OFFLINE"
            out[str(uid)] = {
                "health": round(float(health.get(uid, 100.0)), 2),
                "online": is_online,
                "busy": r is not None,
                "state": state,
                "task_id": r["task_id"] if r else None,
                "priority": r["priority"] if r else None,
                "task_type": r["task_type"] if r else None,
                "progress": r["progress"] if r else 0.0,
                "core": r["core"] if r else -1,
                "wait_ms": r["wait_ms"] if r else 0.0,
                "switch_ms": round(float(self._last_switch_ms.get(uid, 0.0)), 2),
                # 多基站：该机当前接入的站（3D 连线与"多基站"页签用）
                "station_id": int(station_of.get(uid, self.stations.default_id)),
            }
        return out

    def _weight_snapshot(self) -> dict:
        """动态权重的四因子与两个候选权重（供「权重天平」面板）。"""
        sched = self._scheduler
        if sched is None:
            return {}
        try:
            cands = getattr(sched, "last_candidates", None)
            if isinstance(cands, dict) and cands:
                return {
                    "model": getattr(sched, "name", self.scheduler_name),
                    "high": dict(cands.get("HIGH") or {}),
                    "low": dict(cands.get("LOW") or {}),
                    "link_aware": bool(getattr(sched, "link_aware", False)),
                }
            f = getattr(sched, "last_factors", None)
            if isinstance(f, dict) and f:
                return {"model": getattr(sched, "name", self.scheduler_name),
                        "factors": dict(f)}
        except Exception:  # pragma: no cover
            pass
        return {}

    def _kernel_snapshot(self, now: float) -> dict:
        """内核级指标：进程 user/kernel CPU 时间、上下文切换开销、死锁看门狗。"""
        try:
            user_s, kern_s = process_cpu_times()
        except Exception:  # pragma: no cover
            user_s, kern_s = 0.0, 0.0
        sw = [v for v in self._last_switch_ms.values() if v > 0]
        sched = self._scheduler
        cs = int(getattr(sched, "context_switches", 0)) if sched is not None else 0
        return {
            "cpu_user_s": round(float(user_s), 2),
            "cpu_kernel_s": round(float(kern_s), 2),
            "context_switches": cs,
            "switch_ms_avg": round(sum(sw) / len(sw), 2) if sw else 0.0,
            "switch_ms_max": round(max(sw), 2) if sw else 0.0,
            "deadlock_suspects": int(self._deadlock_suspects),
            "deadlock_recovered": int(self._deadlock_recovered),
            "trace_events": len(self._event_log),
            "threads_alive": int(threading.active_count()),
        }

    def snapshot(self) -> dict:
        """导出当前时刻的系统状态快照（JSON 友好）。

        用途：`run.py ui` 的网页可视化面板每 150 ms 轮询一次。
        设计约束：
          * **不做重活**（不遍历全部任务），只读计数器与队列深度；
          * 只取正在运行的 ≤8 个任务的明细；
          * 全部读操作要么走已有锁，要么是 GIL 下的原子读。
        """
        now = self.now()
        with self._lock:
            submitted = len(self._tasks)
            done = self._done_count
            running = [
                {
                    "task_id": t.task_id,
                    "uav_id": int(t.uav_id),
                    "priority": t.priority.name,
                    "task_type": t.task_type.value,
                    "remaining_s": round(t.remaining_s, 3),
                    "service_s": round(float(t.service_s), 3),
                    "progress": round(float(t.progress), 4),
                    "core": int(self._slot_of.get(t.task_id, -1)),
                    "wait_ms": round(float(t.wait_ms_total), 1),
                }
                for t, _ in self._running.values()
            ]
            blocked = self._blocked_count
            preempt = self._preempt_count
            dropped = self._dropped_in_queue
            retry_pending = len(self._retry_pending)
            online = dict(self._uav_online)

        limiter = self.limiter.get_status()
        # 先算"每机当前接入站"：它可能因某站宕机而**改派**，而站级快照的
        # uavs/uav_count 依赖改派结果，因此必须先于 stations 计算（否则同一份
        # 快照里 stations 与 station_of_uav 会自相矛盾）。
        station_of = self._station_of_uav()
        # 每机"到其**当前接入站**"的健康度。单站时 station_of 恒为默认站，
        # 于是 links_all 与旧 get_all_health() 逐位相同（旧口径零改动）；
        # 多站时 links 必须按接入站取，否则面板会显示"到别的站"的链路质量。
        links_all: Dict[int, float] = {
            int(uid): float(self.link_monitor.health_at(int(uid), now, int(sid)))
            for uid, sid in station_of.items()}
        sched_stats = {}
        if self._scheduler is not None and hasattr(self._scheduler, "get_stats"):
            try:
                sched_stats = self._scheduler.get_stats()
            except Exception:  # pragma: no cover
                sched_stats = {}

        return {
            "sim_time": round(now, 3),
            "duration_s": float(self.scenario.duration_s),
            "scenario": self.scenario.code,
            "scenario_label": self.scenario.label,
            "scheduler": self.scheduler_name,
            "uav_count": int(self.scenario.uav_count),
            "workers": int(self.workers),
            "queue": {
                "high": self.queue.high_depth,
                "low": self.queue.low_depth,
                "capacity": int(self.queue.capacity),
                "congestion": round(self.queue.congestion(), 4),
                "high_congestion": round(self.queue.congestion_of(TaskPriority.HIGH), 4),
                "low_congestion": round(self.queue.congestion_of(TaskPriority.LOW), 4),
            },
            "running_tasks": sorted(running, key=lambda r: -r["remaining_s"]),
            "running_count": len(running),
            # 每架无人机的合并状态（3D 面板直接按 uav_id 索引，免去前端自己 join）
            # 2026-11：补上"线程五态 / 绑定核号 / 当前任务 / 最近一次上下文切换开销"，
            # 供「OS 微观透视镜」点击单机时下钻。
            "uav_states": self._uav_detail(running, links_all, online, station_of),
            "counters": {
                "submitted": int(submitted),
                "done": int(done),
                "blocked": int(blocked),
                "preempted": int(preempt),
                "dropped": int(dropped),
                "retry_pending": int(retry_pending),
                "starvation_threshold_ms": float(self.starvation_threshold_ms),
            },
            "limiter": {
                "running": int(limiter.get("running", 0)),
                "max_concurrent": int(limiter.get("max_concurrent", self.workers)),
                "throttled": int(limiter.get("throttled", 0)),
                "rejected_large": int(limiter.get("rejected_large", 0)),
                "chunked": int(limiter.get("chunked", 0)),
            },
            "system_load": round(float(self.system_load()), 4),
            "overload_seconds": round(float(self._overload_seconds), 3),
            "links": {str(uid): round(float(links_all.get(uid, 100.0)), 2)
                      for uid in range(1, int(self.scenario.uav_count) + 1)},
            "uav_online": {str(uid): bool(v) for uid, v in online.items()},
            "scheduler_stats": {
                "high_time_slice_ms": round(float(sched_stats.get("high_time_slice_ms", 0.0)), 1),
                "low_time_slice_ms": round(float(sched_stats.get("low_time_slice_ms", 0.0)), 1),
                "context_switches": int(sched_stats.get("context_switches", 0)),
                "dispatched": int(sched_stats.get("dispatched", 0)),
            },
            "history": [
                {"ts": round(s.ts, 3), "high": s.high_depth, "low": s.low_depth,
                 "load": round(s.system_load, 4),
                 "hi_slice": round(float(s.high_time_slice_ms), 0),
                 "lo_slice": round(float(s.low_time_slice_ms), 0)}
                for s in self.queue_samples()[-160:]
            ],
            # ---- OS 微观透视：就绪队列"传送带" / 权重天平 / 内核指标 / 混沌状态 ----
            "queue_tasks": self.queue.snapshot_tasks(limit=40, now=now),
            "weight": self._weight_snapshot(),
            "pool": {
                "active": len(running),
                "idle": max(0, int(self.workers) - len(running)),
                "total": int(self.workers),
            },
            "kernel": self._kernel_snapshot(now),
            "chaos": {
                "weaknet_active": now < float(self._chaos.get("weaknet_until", 0.0)),
                "weaknet_until": round(float(self._chaos.get("weaknet_until", 0.0)), 2),
                "weaknet_uavs": len(self._chaos.get("weaknet_uavs") or ()),
                "burst": int(self._chaos.get("burst", 0)),
                "deadlock_task": self._chaos.get("deadlock_task"),
                "deadlock_until": round(float(self._chaos.get("deadlock_until", 0.0)), 2),
                # ---- 多基站混沌：节点宕机 / 网络分区 / 脑裂 ----
                "nodes_down": self.stations.nodes_down(),
                "partitions": self.stations.partitions_view(),
                "split_brain": bool(self.stations.split_brain),
            },
            # ---- 多基站 / MEC（改造建议第五章；契约键名见 docs/11） ----
            "stations": self.stations.snapshot(self._running_remaining_by_station(), now),
            "station_of_uav": {str(uid): int(sid)
                               for uid, sid in sorted(station_of.items())},
            # 全链路矩阵：**两层键都用字符串**，与 JSON 序列化后的键型一致，
            # 避免"直接读 snapshot() 是 int 键、经 HTTP 变成 str 键"的踩坑。
            # 覆盖**全部**无人机（含已返航的）：与 station_of_uav / links 口径一致，
            # 否则前端按 uav_id 索引时会出现"有的机没有链路记录"。
            "link_matrix": {str(uid): {str(sid): float(h) for sid, h in row.items()}
                            for uid, row in self.link_monitor.health_matrix(
                                [int(n.spec.id) for n in self.stations.nodes],
                                range(1, int(self.scenario.uav_count) + 1),
                                t=now).items()},
            "mec": self.mec.snapshot(),
            "trace_tail": list(self._event_log)[-60:],
            # ---- 天气快照（契约 §4.6.4：键名不可改，前端天气系统直接消费） ----
            # 运行中切换天气后这里立刻变化；未开始仿真时是 t=0 的曲线起点。
            "weather": self.weather.snapshot(now),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<Simulator {self.scenario.code}×{self.scheduler_name} "
                f"workers={self.workers} queue={self.queue.total_depth}>")
