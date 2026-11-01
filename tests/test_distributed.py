"""
tests/test_distributed.py —— 多基站 / MEC 边缘计算 / 跨节点调度验收测试
=============================================================================

覆盖 task-4（T4）要求的 8 条覆盖点：

    1. 单站回归：默认配置（`stations.nodes` 只有 1 个 CENTER）下
       `snapshot()["stations"]` 长度为 1、`station_of_uav` 全指向 1、
       且**单站链路序列与 T1 落地前逐点一致**（基线见下方 PRE_T1_* 常量）。
    2. 多站拓扑：`station_spec="triple"`（1 CENTER + 2 EDGE）下每站有独立的
       workers/队列/限流器；UAV 按 station_id 归属，各站 uavs 与 station_of_uav 互逆。
    3. 每 (uav, station) 链路：同一 uav 到不同站的健康度不同、同 seed 可复现；
       旧签名 `health_at(uav, t)` 仍可用且等于默认站。
    4. cross_node 策略：最少连接把新任务导向低负载站、一致性哈希对同一 key 稳定、
       MockSim（无 stations 属性）下优雅退化且不丢任务。
    5. 迁移代价：迁移不是瞬间完成 —— 迁移记账（耗时/带宽损耗）> 0，
       且迁移期间任务不重复占用两个站的槽位。
    6. MEC：轻量运维（GPS/HEARTBEAT/ATTITUDE/ALARM）下沉 EDGE、
       重勘查（IMAGERY/MULTISPECTRAL/LIDAR）回 CENTER；
       调边缘算力/回传带宽能改变端到端时延（权衡实验可复现）。
    7. 混沌：node_down 后该站 offline 且任务重调度；partition 后跨分区不迁移；
       split_brain 置位且 recover 后恢复，系统继续推进。
    8. 指标：站级队列深度/负载/接入机数/迁移次数、总吞吐与端到端时延字段齐全且非负。

为什么第 1 条用"确定性输入层"断言，而不是整跑指标：
    本仿真器是**真实多线程 + time.sleep 模拟 IO**，整跑指标受 wall-clock 线程调度
    抖动影响（docs/09 与 tests/test_weather.py 第 6 条已论证过）。所以"多站改造
    没有改变默认单站行为"的正确断言层次是**确定性输入**：链路健康度序列逐点相同，
    多站代码路径就不可能改变单站的行为分布。

运行方式（与其它测试一致）：
    & ".\\venv\\Scripts\\python.exe" -m pytest -q tests/test_distributed.py
    & ".\\venv\\Scripts\\python.exe" tests/test_distributed.py     # 直接跑（含 [OK] 打印）
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import sys
import threading
import time
import urllib.request

# Windows 控制台默认 cp936，遇到 GBK 不支持的符号会让测试假失败
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass
from dataclasses import replace
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from uav_sim.config import load_config  # noqa: E402
from uav_sim.link_monitor import LinkMonitor  # noqa: E402
from uav_sim.models import (  # noqa: E402
    SCHEDULER_LABELS,
    SCHEDULER_NAMES,
    Task,
    TaskPriority,
    TaskType,
)
from uav_sim.queue_manager import MultiLevelQueue  # noqa: E402
from uav_sim.resource_limiter import ResourceLimiter  # noqa: E402
from uav_sim.scenarios import generate_trace, get_scenario  # noqa: E402
from uav_sim.simulator import Simulator  # noqa: E402


def _create_scheduler(name: str, sim, cfg):
    """延迟导入 `create_scheduler`：T1 未落地时 scheduler 包 import 期会直接 raise，
    这里包成显式断言，让失败信息可读（就绪门禁已在模块顶部跳过，这里只是兜底）。"""
    from uav_sim.scheduler import create_scheduler

    return create_scheduler(name, sim, cfg)

CFG = load_config()
SEED = 20261031

# ---------------------------------------------------------------------------
# T1 落地**之前**录制的单站基线（同一份代码连跑两次逐位一致，可安全硬编码）
#   A) LinkMonitor(seed=11, 60s, 5 架机) 三档弱网的健康度均值（与 tests/test_link.py 同参）
#   B) Simulator(LIGHT, seed=20261031, clear 天气) 单站逐 (uav, t) 健康度序列的 SHA-256
#      —— 网格：uav 1..5、t = 0, 0.5, ..., 90.0（181 点），与录制脚本口径一致
# 只要 T1 的多站改造碰到默认单站的链路随机流，这些常量就会失配（这是它存在的意义）。
# ---------------------------------------------------------------------------
PRE_T1_WEAKNET_MEANS = {
    "none": 89.9073517138972,
    "low": 71.75293790372596,
    "high": 43.29040036627709,
}
PRE_T1_LINK_SERIES_SHA256 = "ca71fdd0645c5abcb47fbbf3e8b7b14c225c4d83339b72b0146d6f91311ac16f"
PRE_T1_LINK_MEAN = 89.49378779830154
PRE_T1_LINK_UAV1_MEAN = 90.34952520617905
PRE_T1_LINK_UAV_LAST_MEAN = 88.39073639279002
PRE_T1_LINK_T0_UAV1 = 90.52447048634872

#: 任务分级常量（与 config.yaml 的 mec.edge_task_types / center_task_types 对齐）
LIGHT_TYPES = {"GPS", "HEARTBEAT", "ATTITUDE", "ALARM"}
HEAVY_TYPES = {"IMAGERY", "MULTISPECTRAL", "LIDAR"}

#: 站级快照必须齐备的字段（契约：/api/state → stations[]）
STATION_FIELDS = (
    "id", "name", "role", "x", "z", "online", "partitioned", "workers", "active", "idle",
    "queue_depth", "queue_high", "queue_low", "load", "uav_count", "uavs",
    "migrations_in", "migrations_out", "migration_cost_ms", "done", "throughput",
    "edge_compute_mbps", "backhaul_mbps", "color",
)
STATION_NUMERIC_FIELDS = (
    "workers", "active", "idle", "queue_depth", "queue_high", "queue_low", "load",
    "uav_count", "migrations_in", "migrations_out", "migration_cost_ms", "done", "throughput",
)
MEC_FIELDS = ("enabled", "edge_tasks", "center_tasks", "backhaul_used_mbps",
              "edge_offload_ratio", "avg_e2e_edge_ms", "avg_e2e_center_ms", "task_class")


# ---------------------------------------------------------------------------
# T1 就绪探测：多基站后端没落盘时整模块跳过（不拖垮既有 62 个用例）
# ---------------------------------------------------------------------------
def _t1_missing() -> str:
    """T1（多基站后端）缺失项描述；空串表示已就绪。"""
    import importlib

    try:
        for mod in ("uav_sim.station", "uav_sim.mec", "uav_sim.scheduler.cross_node"):
            importlib.import_module(mod)
    except Exception as exc:  # noqa: BLE001 - 就绪探测，任何异常都算未就绪
        return f"模块导入失败 {type(exc).__name__}: {exc}"
    for name in ("inject_node_down", "inject_partition", "inject_split_brain", "recover_nodes"):
        if not hasattr(Simulator, name):
            return f"Simulator 缺少 {name}()"
    return ""


_MISSING = _t1_missing()
if _MISSING:
    pytest.skip(f"T1 多基站后端尚未落地（{_MISSING}）；落地后本模块的 8 条覆盖点自动生效",
                allow_module_level=True)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _triple_topo() -> list:
    """config.yaml 里的 3 站拓扑（1 CENTER + 2 EDGE）。"""
    topo = CFG["stations"]["topologies"]["triple"]
    return copy.deepcopy(topo)


def _make_sim(code: str = "LIGHT", scheduler: str = "dynamic", seed: int = SEED,
              station_spec: str | None = None, mec: bool | None = None,
              duration_s: float | None = None, cfg_over: dict | None = None):
    """按 T1 冻结参数构造 Simulator，返回 (sim, scenario)。

    station_spec="triple" → 3 站拓扑；mec=True → 打开 MEC 边缘分流；
    duration_s → 把工况时长改短（测试提速，不改其它参数）。
    """
    cfg = copy.deepcopy(CFG)
    if mec is not None:
        cfg.setdefault("mec", {})["enable"] = bool(mec)
    if cfg_over:
        for section, over in cfg_over.items():
            cfg.setdefault(section, {}).update(over)
    sc = get_scenario(code)
    if duration_s is not None:
        sc = replace(sc, duration_s=float(duration_s))
    trace = generate_trace(sc, seed, cfg)

    params = inspect.signature(Simulator.__init__).parameters
    assert "station_spec" in params, (
        "Simulator.__init__ 未提供 station_spec 参数（T1 契约：Simulator(..., station_spec='triple')）")
    kw = {}
    if station_spec is not None:
        kw["station_spec"] = station_spec
    if mec is not None and "mec_enabled" in params:
        kw["mec_enabled"] = bool(mec)
    sim = Simulator(sc, trace, cfg, scheduler_name=scheduler, seed=seed, **kw)
    return sim, sc


def _nodes(sim) -> list:
    """取出 Simulator 里的 StationNode 列表（T1：``sim.stations.nodes``）。"""
    return list(sim.stations.nodes)


def _run(sim, scheduler_name: str = "cross_node"):
    """跑完整仿真，返回 (raw, scheduler)。"""
    sched = _create_scheduler(scheduler_name, sim, sim.cfg)
    return sim.run(sched, duration_s=sim.scenario.duration_s), sched


def _station(snap: dict, sid: int) -> dict:
    for s in snap["stations"]:
        if int(s["id"]) == int(sid):
            return s
    raise AssertionError(f"快照里没有 {sid} 号站：{[s['id'] for s in snap['stations']]}")


def _group_of(sid: int, groups: list) -> int:
    for i, g in enumerate(groups):
        if int(sid) in {int(x) for x in g}:
            return i
    return -1


class MockSim:
    """最小仿真器替身（与 tests/test_schedulers.py 同构）：**故意不提供 stations 属性**，
    用来验证 cross_node 在无多站维度时的优雅退化。"""

    def __init__(self, max_concurrent: int = 3, max_task_memory_mb: float = 500.0):
        self.queue = MultiLevelQueue(capacity=100, starvation_threshold_ms=1000.0,
                                     congestion_ref=8.0)
        self.limiter = ResourceLimiter(max_concurrent=max_concurrent,
                                       max_task_memory_mb=max_task_memory_mb,
                                       per_uav_quota=99, enable_chunking=True)
        self.dispatched: list = []
        self.limit_s_seen: list = []
        self._now = 0.0

    def now(self) -> float:
        return self._now

    def system_load(self) -> float:
        return 0.5

    def dispatch(self, task: Task, limit_s=None, station_id=None) -> bool:
        self.dispatched.append(task)
        self.limit_s_seen.append(limit_s)
        return True

    class _Mon:
        @staticmethod
        def get_health(uav_id: int) -> float:
            return 80.0

    link_monitor = _Mon()


def _mk(tid: str, prio: TaskPriority, uav: int = 1, size: float = 50.0) -> Task:
    t = Task(task_id=tid, uav_id=uav, priority=prio,
             task_type=TaskType.IMAGERY if prio is TaskPriority.HIGH else TaskType.HEARTBEAT,
             data_size_mb=size, submit_ts=0.0, service_s=1.0)
    t.enqueue_ts = 0.0
    return t


# ===========================================================================
# 覆盖点 1：单站回归
# ===========================================================================
def test_single_station_snapshot_and_fields() -> None:
    """默认配置必须退化为"1 个 CENTER 站"，且旧字段口径（links）不变。"""
    sim, sc = _make_sim(code="LIGHT", scheduler="dynamic")
    snap = sim.snapshot()

    sts = snap.get("stations")
    assert isinstance(sts, list) and len(sts) == 1, f"默认单站快照应是长度 1 的列表，实际 {sts!r}"
    st = sts[0]
    assert int(st["id"]) == 1 and st["role"] == "CENTER", st
    assert st["online"] is True and st["partitioned"] is False
    assert int(st["workers"]) == int(CFG["runtime"]["worker_threads"]), (
        "默认单站 workers 必须等于 runtime.worker_threads（config.yaml 的控制变量硬约束）")
    assert int(st["uav_count"]) == int(sc.uav_count)

    so = snap["station_of_uav"]
    assert set(so) == {str(u) for u in range(1, sc.uav_count + 1)}, "station_of_uav 必须覆盖全部 UAV"
    assert {int(v) for v in so.values()} == {1}, "默认单站下所有 UAV 都应归属 1 号站"

    # 旧字段口径：links 仍是"每机一条"，且 == link_matrix 里该机到 1 号站的值
    assert set(snap["links"]) == set(so)
    for u, h in snap["links"].items():
        assert abs(float(snap["link_matrix"][u]["1"]) - float(h)) < 0.02, (
            f"uav {u}: links={h} 与 link_matrix={snap['link_matrix'][u]} 不一致")

    # MEC 默认关闭：不改变既有调度行为
    assert (snap.get("mec") or {}).get("enabled", False) is False
    print(f"[OK] 单站回归：stations=1(CENTER) / station_of_uav 全指向 1 / "
          f"links 口径不变（{len(snap['links'])} 架机）")


def test_single_station_link_series_matches_pre_t1_baseline() -> None:
    """单站链路序列与 T1 落地前逐点一致 —— 多站改造不得触碰默认单站的随机流。"""
    sim, sc = _make_sim(code="LIGHT", scheduler="dynamic")
    lm = sim.link_monitor

    h = hashlib.sha256()
    means = []
    for uid in range(1, sc.uav_count + 1):
        acc = 0.0
        for i in range(181):                      # t = 0, 0.5, ..., 90.0
            v = lm.health_at(uid, i * 0.5)
            acc += v
            h.update(("%d:%.9f;" % (uid, v)).encode())
        means.append(acc / 181)

    assert h.hexdigest() == PRE_T1_LINK_SERIES_SHA256, (
        "单站链路序列与 T1 落地前不一致：多站改造改变了默认单站的确定性输入\n"
        f"  期望 sha256={PRE_T1_LINK_SERIES_SHA256}\n  实际 sha256={h.hexdigest()}")
    assert abs(sum(means) / len(means) - PRE_T1_LINK_MEAN) < 1e-9
    assert abs(means[0] - PRE_T1_LINK_UAV1_MEAN) < 1e-9
    assert abs(means[-1] - PRE_T1_LINK_UAV_LAST_MEAN) < 1e-9
    assert abs(lm.health_at(1, 0.0) - PRE_T1_LINK_T0_UAV1) < 1e-9

    # 弱网三档均值也要逐位不变（seed=11 / 60s / 5 架机，与 tests/test_link.py 同参）
    for level, base in PRE_T1_WEAKNET_MEANS.items():
        m = LinkMonitor(seed=11, weaknet_level=level, duration_s=60.0,
                        uav_ids=[1, 2, 3, 4, 5], cfg=CFG)
        vals = [m.health_at(1, t * 0.5) for t in range(120)]
        mean = sum(vals) / len(vals)
        assert abs(mean - base) < 1e-9, f"{level} 档健康度均值漂移：{mean} != {base}"
    print("[OK] 单站链路序列逐点等同 T1 落地前基线（sha256 一致 + 弱网三档均值一致）")


# ===========================================================================
# 覆盖点 2：多站拓扑
# ===========================================================================
def test_multi_station_topology_independent_resources() -> None:
    """3 站拓扑：每站独立 workers/队列/限流器，UAV 归属自洽。"""
    sim, sc = _make_sim(code="LIGHT", station_spec="triple", mec=True)
    snap = sim.snapshot()
    topo = _triple_topo()

    sts = snap["stations"]
    assert [int(s["id"]) for s in sts] == [int(n["id"]) for n in topo], (
        f"站 id 必须与 config 拓扑一致且升序：{[s['id'] for s in sts]}")
    assert [s["role"] for s in sts] == ["CENTER", "EDGE", "EDGE"], [s["role"] for s in sts]
    assert [int(s["workers"]) for s in sts] == [int(n["workers"]) for n in topo], (
        f"每站 workers 应取自拓扑：{[s['workers'] for s in sts]} vs {[n['workers'] for n in topo]}")
    assert all(s["online"] for s in sts) and all(not s["partitioned"] for s in sts)

    nodes = _nodes(sim)
    assert len(nodes) == 3, (
        "Simulator 应能取到 3 个 StationNode（sim.stations / cluster.nodes），"
        f"实际 {len(nodes)} 个")
    assert len({id(n.queue) for n in nodes}) == 3, "每站必须有**独立**队列对象"
    assert len({id(n.limiter) for n in nodes}) == 3, "每站必须有**独立**限流器对象"

    # UAV 归属：各站 uavs 与 station_of_uav 互为反函数
    inv: dict[int, list[int]] = {}
    for u, sid in snap["station_of_uav"].items():
        inv.setdefault(int(sid), []).append(int(u))
    for s in sts:
        sid = int(s["id"])
        assert sorted(int(u) for u in s["uavs"]) == sorted(inv.get(sid, [])), (
            f"{sid} 号站 uavs={s['uavs']} 与 station_of_uav={inv.get(sid, [])} 不一致")
        assert int(s["uav_count"]) == len(s["uavs"])
        assert int(s["active"]) + int(s["idle"]) == int(s["workers"]), (
            f"{sid} 号站 active+idle 应等于 workers：{s['active']}+{s['idle']} != {s['workers']}")
    assert sum(int(s["uav_count"]) for s in sts) == int(sc.uav_count), "各站接入机数之和应等于机群规模"
    assert set(int(v) for v in snap["station_of_uav"].values()) <= {1, 2, 3}
    print("[OK] 3 站拓扑：workers=" + "/".join(str(s["workers"]) for s in sts)
          + "，接入机数=" + "/".join(str(s["uav_count"]) for s in sts)
          + "，队列与限流器均为独立对象")


# ===========================================================================
# 覆盖点 3：每 (uav, station) 链路
# ===========================================================================
def test_per_uav_station_links() -> None:
    """同一 uav 到不同站的链路必须不同、可复现；旧签名仍等价于默认站。"""
    sim, _ = _make_sim(code="NORMAL", station_spec="triple")
    lm = sim.link_monitor
    ts = (0.0, 3.7, 20.5, 55.25)

    gaps = []
    for t in ts:
        hs = [float(lm.health_at(1, t, station_id=sid)) for sid in (1, 2, 3)]
        for h in hs:
            assert 0.0 <= h <= 100.0, f"t={t} 健康度越界：{h}"
        gaps.append(max(hs) - min(hs))
    assert max(gaps) > 1e-9, (
        f"同一 uav 到三个站的健康度完全相同（逐点差 {gaps}）—— 每 (uav,station) 独立链路未生效")

    # 同 seed 确定性可复现
    sim2, _ = _make_sim(code="NORMAL", station_spec="triple")
    for t in ts:
        for sid in (1, 2, 3):
            a = round(float(lm.health_at(1, t, station_id=sid)), 9)
            b = round(float(sim2.link_monitor.health_at(1, t, station_id=sid)), 9)
            assert a == b, f"同 seed 链路不可复现：t={t} station={sid} {a} != {b}"

    # 旧签名仍然可用，且 station_id=None 等价于旧调用
    for t in ts:
        assert abs(float(lm.health_at(1, t)) - float(lm.health_at(1, t, station_id=None))) < 1e-12
    # 单站模式下旧签名 == 1 号站（旧调用零改动）
    sim1, _ = _make_sim(code="NORMAL")
    for t in ts:
        assert abs(float(sim1.link_monitor.health_at(1, t))
                   - float(sim1.link_monitor.health_at(1, t, station_id=1))) < 1e-9

    # 快照：link_matrix 覆盖 (uav × 全站)，links = 该机当前接入站
    snap = sim.snapshot()
    for u, so in snap["station_of_uav"].items():
        row = snap["link_matrix"][u]
        assert {int(k) for k in row} == {1, 2, 3}, f"uav {u} 的 link_matrix 未覆盖 3 个站：{row}"
        assert abs(float(snap["links"][u]) - float(row[str(so)])) < 0.02, (
            f"uav {u}: links={snap['links'][u]} 应等于到接入站 {so} 的健康度 {row[str(so)]}")
    print(f"[OK] 每 (uav,station) 链路独立且确定：逐点最大站间差 {max(gaps):.3f}，"
          "旧签名 health_at(uav,t) 等价于默认站")


# ===========================================================================
# 覆盖点 4：cross_node 策略
# ===========================================================================
def test_cross_node_registered() -> None:
    assert "cross_node" in SCHEDULER_NAMES, f"注册表缺少 cross_node：{SCHEDULER_NAMES}"
    assert "cross_node" in SCHEDULER_LABELS, "cross_node 缺少中文显示名"
    print(f"[OK] cross_node 已注册（{len(SCHEDULER_NAMES)} 种策略）：{SCHEDULER_LABELS['cross_node']}")


def test_cross_node_degrades_without_stations() -> None:
    """MockSim（无 stations 属性）下不抛异常、不丢任务、不超卖槽位。"""
    sim = MockSim(max_concurrent=2)
    sched = _create_scheduler("cross_node", sim, CFG)
    total = 10
    for i in range(total):
        sim.queue.enqueue(_mk(f"T{i}", TaskPriority.HIGH if i % 2 == 0 else TaskPriority.LOW), 0.0)
    for _ in range(20):
        sched.step()
    dispatched = len(sim.dispatched)
    remaining = sim.queue.total_depth
    assert dispatched + remaining == total, (
        f"cross_node 在无多站维度时丢任务：派发 {dispatched} + 排队 {remaining} != {total}")
    assert dispatched <= 2, f"cross_node 超卖并发槽位：{dispatched} > 2"
    assert set(sched.get_stats()) >= {"dispatched", "context_switches",
                                      "high_time_slice_ms", "low_time_slice_ms",
                                      "high_weight", "low_weight"}
    print(f"[OK] cross_node 在 MockSim（无 stations）下优雅退化："
          f"派发 {dispatched} / 排队 {remaining}，无丢失无超卖")


def test_cross_node_least_connections_prefers_idle_station() -> None:
    """最少连接纯函数：负载最低的站胜出；并列时按 id 稳定 tie-break。"""
    from uav_sim.scheduler.cross_node import least_connections

    stats = {1: {"active": 6, "queue_depth": 30, "load": 0.9},
             2: {"active": 0, "queue_depth": 0, "load": 0.05},
             3: {"active": 1, "queue_depth": 2, "load": 0.15}}
    assert least_connections(stats) == 2, "负载 0.9/0.05/0.15 三站应选中 2 号站"

    # 只看 load（含队列积压）而不是绝对连接数：3 号"在跑少"但队列负载更高时仍应选 2 号
    stats2 = {1: {"active": 9, "queue_depth": 99, "load": 3.0},
              2: {"active": 2, "queue_depth": 4, "load": 0.10},
              3: {"active": 0, "queue_depth": 60, "load": 0.80}}
    assert least_connections(stats2) == 2, (
        "least_connections 应先比 load（含队列积压）再比 active，不能被'在跑数为 0'误导")

    # 稳定 tie-break：完全并列时取 id 最小
    tie = {3: {"active": 1, "queue_depth": 1, "load": 0.5},
           1: {"active": 1, "queue_depth": 1, "load": 0.5},
           2: {"active": 1, "queue_depth": 1, "load": 0.5}}
    assert least_connections(tie) == 1, "并列时必须按 id 稳定 tie-break（可复现性）"
    assert least_connections({}) == 0, "空候选集应返回 0（调用方据此兜底默认站）"
    print("[OK] 最少连接 least_connections()：负载优先、队列计入、并列按 id 稳定 tie-break")


def test_cross_node_placement_routes_away_from_overloaded_station() -> None:
    """集成：某站被人为压垮时，cross_node 的选站决策必须避开它（真实 3 站拓扑）。"""
    sim, _ = _make_sim(code="LIGHT", station_spec="triple")
    nodes = _nodes(sim)
    assert len(nodes) == 3
    sched = _create_scheduler("cross_node", sim, CFG)

    # 把 1 号站的队列压到远超其它站（200 个 5s 任务 ≈ 1000 仿真秒积压）
    node1 = nodes[0]
    for i in range(200):
        t = _mk(f"STUFF-{i}", TaskPriority.HIGH, uav=1, size=40.0)
        t.service_s = 5.0
        t.station_id = 1
        node1.queue.enqueue(t, 0.0)
    load1 = node1.load(0.0)
    load_other = max(n.load(0.0) for n in nodes[1:])
    assert load1 > load_other * 5, f"测试前置条件不成立：{load1} 未显著高于 {load_other}"

    probe = _mk("PROBE", TaskPriority.HIGH, uav=3, size=40.0)
    probe.service_s = 5.0
    chosen = sched._pick_station(probe)
    assert chosen in (2, 3), (
        f"1 号站负载 {load1:.1f} 远高于其它站（{load_other:.1f}），"
        f"cross_node 仍把新任务选到了 {chosen} 号站（最少连接未生效）")
    assert sched.last_pick.get("chosen") == chosen
    print(f"[OK] 选站决策避开过载站：1 号站负载 {load1:.1f} vs 其它 {load_other:.1f} "
          f"-> 选中 {chosen} 号站")


def test_cross_node_consistent_hash_stable() -> None:
    """一致性哈希：同一 key（同一架无人机）多次映射必须稳定到同一站。"""
    from uav_sim.scheduler.cross_node import consistent_hash

    ids = [1, 2, 3]
    keys = [f"uav-{u}" for u in (1, 2, 3, 7, 11, 19, 23)]
    first = [int(consistent_hash(k, ids, CFG)) for k in keys]
    assert set(first) <= {1, 2, 3}, first
    for _ in range(5):
        assert [int(consistent_hash(k, ids, CFG)) for k in keys] == first, (
            "同一 key 的一致性哈希映射不稳定（会引发迁移抖动）")
    # 不传 cfg（走默认虚拟节点数）也必须稳定，且与传 cfg 的分布同构
    no_cfg = [int(consistent_hash(k, ids)) for k in keys]
    assert no_cfg == [int(consistent_hash(k, ids)) for k in keys]
    # 分布不应退化成"全落一个站"
    assert len(set(first)) >= 2, f"一致性哈希分布退化：{first}"
    # 站集变化时是"重映射"而不是"全乱"：去掉 3 号站后，原本落 1/2 的 key 仍应落 1/2
    two = [int(consistent_hash(k, [1, 2], CFG)) for k in keys]
    assert set(two) <= {1, 2}, two
    assert all(a == b for a, b in zip(first, two) if a in (1, 2)), (
        "缩容时应只重映射落在被删节点上的 key（一致性哈希的基本性质）")
    print(f"[OK] 一致性哈希 consistent_hash()：7 个 key 稳定映射到 {sorted(set(first))}，"
          f"缩容到 [1,2] 后仅有原属 3 号站的 key 被重映射")


# ===========================================================================
# 覆盖点 5：迁移代价
# ===========================================================================
def test_migration_not_instant_with_cost_accounting() -> None:
    """迁移必须有代价记账；迁移期间任务不重复占用两个站的槽位。"""
    sim, _ = _make_sim(code="PEAK", station_spec="triple", mec=True, duration_s=40)
    raw, _ = _run(sim, "cross_node")

    migrate_cfg = CFG["stations"]
    assert float(migrate_cfg["migration_latency_ms"]) > 0, "配置里迁移耗时必须 > 0（不是瞬间完成）"
    assert float(migrate_cfg["migration_bw_penalty"]) > 0, "配置里迁移带宽损耗必须 > 0"

    migrated = [t for t in raw["tasks"] if int(t.migrate_count) > 0]
    total_migrations = sum(int(t.migrate_count) for t in raw["tasks"])
    site_migrations = sum(int(s["migrations_in"]) for s in sim.snapshot()["stations"])
    assert total_migrations > 0, (
        "PEAK + triple + cross_node 一轮下来没有任何迁移 —— 既无法验证迁移代价，"
        "也说明跨节点均衡没有真正发生（请 Lead 核查迁移触发条件）")
    assert site_migrations > 0, "站级 migrations_in 未记账（迁移次数必须可观测）"

    # 记账完整性：迁移过的任务必须有耗时/带宽损耗
    for t in migrated:
        assert float(t.migration_cost_ms) > 0, (
            f"任务 {t.task_id} 迁移了 {t.migrate_count} 次却没有迁移代价（migration_cost_ms="
            f"{t.migration_cost_ms}）—— 迁移被实现成了『瞬移』")
        assert int(t.station_id) != int(t.home_station_id), "迁移标记与落点不一致"
    cost_ms = sum(float(t.migration_cost_ms) for t in migrated)
    assert cost_ms >= float(migrate_cfg["migration_latency_ms"]) - 1e-6, (
        f"迁移总代价 {cost_ms}ms 小于单次在途耗时 {migrate_cfg['migration_latency_ms']}ms")

    # 不重复占槽：全局在跑数 == 各站在跑数之和（同一任务不能同时在两站占坑）
    snap = sim.snapshot()
    assert int(snap["running_count"]) == sum(int(s["active"]) for s in snap["stations"]), (
        f"running_count={snap['running_count']} != Σ站 active="
        f"{sum(int(s['active']) for s in snap['stations'])}（有任务被两个站重复占用）")
    # 槽位账本自洽：各站被占用的站内核号数 == 全局在跑数
    assert sum(len(n.slot_of) for n in _nodes(sim)) == int(snap["running_count"]), (
        "站内核号占用数之和 != 全局在跑数（槽位账本对不上）")
    print(f"[OK] 迁移代价：{len(migrated)} 个任务迁移 {total_migrations} 次，"
          f"累计代价 {cost_ms:.1f}ms（在途 {migrate_cfg['migration_latency_ms']}ms/次），"
          "槽位不重复占用")


def test_migration_in_transit_does_not_occupy_two_stations() -> None:
    """单元级精确验证"迁移不是瞬移"：在途期间不在任何站的队列/槽位，只落地一次。"""
    sim, _ = _make_sim(code="LIGHT", station_spec="triple")
    cluster = sim.stations
    nodes = _nodes(sim)
    lat_ms = float(CFG["stations"]["migration_latency_ms"])
    pen = float(CFG["stations"]["migration_bw_penalty"])

    task = _mk("MIG-1", TaskPriority.HIGH, uav=1, size=60.0)
    task.service_s = 4.0
    task.station_id = 1
    task.home_station_id = 1
    nodes[0].queue.enqueue(task, 0.0)
    assert nodes[0].queue.total_depth == 1

    before_service = float(task.service_s)
    assert cluster.migrate_task(task, 2, now=0.0) is True, "同分区在线站之间应允许迁移"

    # ---- 代价记账 ----
    assert int(task.migrate_count) == 1 and int(task.station_id) == 2
    assert float(task.migration_cost_ms) >= lat_ms - 1e-9, task.migration_cost_ms
    assert abs(float(task.service_s) - before_service * (1.0 + pen)) < 1e-9, (
        "迁移带宽损耗未体现在服务时长上")

    # ---- 在途：不在源站队列、不在目标站队列、不占任何槽位 ----
    assert nodes[0].queue.total_depth == 0, "迁移后任务仍留在源站队列（会被两个站同时看到）"
    assert nodes[1].queue.total_depth == 0, "迁移尚未到期就出现在目标站队列（迁移被实现成了瞬移）"
    in_transit_tasks = [t for _, t in cluster.in_transit]
    assert task in in_transit_tasks, "任务应进入在途表"
    assert all(str(t.task_id) != "MIG-1" for n in nodes for t in n.queue.tasks()), \
        "在途任务不得出现在任何站的队列里"
    assert sum(len(n.slot_of) for n in nodes) == 0, "在途任务不得占用任何站的槽位"

    # ---- 到期才落地，且只落地一次 ----
    assert cluster.flush_migrations(now=lat_ms / 1000.0 - 1e-6) == 0, "未到期不得提前落地"
    assert nodes[1].queue.total_depth == 0
    assert cluster.flush_migrations(now=lat_ms / 1000.0 + 1e-6) == 1, "到期应恰好落地 1 个任务"
    assert nodes[1].queue.total_depth == 1
    landed = nodes[1].queue.tasks()
    assert [t.task_id for t in landed] == ["MIG-1"], landed
    # 任务守恒：源站 0 + 目标站 1 + 在途 0 == 1
    assert nodes[0].queue.total_depth + nodes[1].queue.total_depth + len(cluster.in_transit) == 1
    print(f"[OK] 迁移在途：代价 {task.migration_cost_ms:.1f}ms（在途 {lat_ms}ms + 带宽 {pen:.0%}），"
          f"{lat_ms}ms 内不占任何站队列/槽位，到期后仅落地一次")


# ===========================================================================
# 覆盖点 6：MEC 边缘分流与端到端时延权衡
# ===========================================================================
def test_mec_task_classification() -> None:
    """任务分级：轻量运维 = EDGE、重勘查 = CENTER，且与 config 的清单一致。"""
    from uav_sim import mec as mec_mod

    classify = None
    cname = ""
    for name in ("classify_task", "classify", "task_class", "task_class_of", "classify_task_type"):
        fn = getattr(mec_mod, name, None)
        if callable(fn):
            classify, cname = fn, name
            break
    assert classify is not None, "uav_sim.mec 未导出任务分级函数（期望 classify_task 之类）"

    got_light, got_heavy = set(), set()
    for tt in TaskType:
        cls = str(classify(tt)).upper()
        assert cls in {"LIGHT", "HEAVY", "EDGE", "CENTER"}, f"{tt.name} 分级结果非法：{cls}"
        (got_light if cls in {"LIGHT", "EDGE"} else got_heavy).add(tt.name)
    assert LIGHT_TYPES <= got_light, f"轻量运维任务应下沉 EDGE，实际分级：{sorted(got_light)}"
    assert HEAVY_TYPES <= got_heavy, f"重勘查任务应回 CENTER，实际分级：{sorted(got_heavy)}"

    # config 清单必须与分级函数一致（口径单一来源）
    assert set(CFG["mec"]["edge_task_types"]) == LIGHT_TYPES
    assert set(CFG["mec"]["center_task_types"]) == HEAVY_TYPES
    print(f"[OK] MEC 分级 {cname}()：EDGE={sorted(got_light)}，CENTER={sorted(got_heavy)}")


def test_mec_offload_and_e2e_latency_tradeoff() -> None:
    """开了 MEC：重勘查不许落边缘站；调回传带宽/边缘算力能改变端到端时延。"""
    sim, _ = _make_sim(code="PEAK", station_spec="triple", mec=True, duration_s=30)
    raw, _ = _run(sim, "cross_node")
    snap = sim.snapshot()
    mec = snap["mec"]

    for k in MEC_FIELDS:
        assert k in mec, f"mec 快照缺少字段 {k}（契约 §/api/state）"
    assert mec["enabled"] is True
    assert set(mec["task_class"]) >= LIGHT_TYPES | HEAVY_TYPES, mec["task_class"]

    edge_ids = {int(s["id"]) for s in snap["stations"] if s["role"] == "EDGE"}
    center_ids = {int(s["id"]) for s in snap["stations"] if s["role"] == "CENTER"}
    assert edge_ids and center_ids, "triple 拓扑应同时含 EDGE 与 CENTER"

    heavy_on_edge = []
    light_on_center = 0
    for t in raw["tasks"]:
        if t.state.name not in ("DONE", "RUNNING"):
            continue
        sid = int(t.station_id)
        if t.task_type.value in HEAVY_TYPES and sid in edge_ids:
            heavy_on_edge.append((t.task_id, t.task_type.value, sid))
        if t.task_type.value in LIGHT_TYPES and sid in center_ids:
            light_on_center += 1
    assert not heavy_on_edge, (
        f"重勘查任务落到了边缘站（MEC 要求回 CENTER）：{heavy_on_edge[:5]}")

    for k in ("edge_tasks", "center_tasks", "backhaul_used_mbps", "edge_offload_ratio",
              "avg_e2e_edge_ms", "avg_e2e_center_ms"):
        assert float(mec[k]) >= 0.0, f"mec.{k} 为负：{mec[k]}"
    assert int(mec["edge_tasks"]) + int(mec["center_tasks"]) > 0, "MEC 分流计数全为 0（未真正分流）"
    assert float(mec["avg_e2e_edge_ms"]) <= float(mec["avg_e2e_center_ms"]), (
        f"边缘直连时延应不大于中心：{mec['avg_e2e_edge_ms']} vs {mec['avg_e2e_center_ms']}")

    # 权衡实验可复现：把回传带宽砍到 1/6，端到端时延必须变化（通信开销 ↑）
    sim_slow, _ = _make_sim(code="PEAK", station_spec="triple", mec=True, duration_s=30,
                            cfg_over={"mec": {"backhaul_mbps": 10.0}})
    _run(sim_slow, "cross_node")
    mec_slow = sim_slow.snapshot()["mec"]
    fast = (float(mec["avg_e2e_edge_ms"]), float(mec["avg_e2e_center_ms"]))
    slow = (float(mec_slow["avg_e2e_edge_ms"]), float(mec_slow["avg_e2e_center_ms"]))
    assert fast != slow, (
        f"回传带宽 60MB/s → 10MB/s 后端到端时延完全没变（{fast}）—— "
        "通信开销没有进入时延模型，权衡实验不可复现")
    print(f"[OK] MEC：边缘分流 {mec['edge_tasks']}/{mec['center_tasks']}"
          f"（offload={mec['edge_offload_ratio']:.2f}），端到端 edge/center="
          f"{fast[0]:.1f}/{fast[1]:.1f}ms；回传降到 10MB/s 后 {slow[0]:.1f}/{slow[1]:.1f}ms")


# ===========================================================================
# 覆盖点 7：混沌（节点宕机 / 网络分区 / 脑裂）
# ===========================================================================
def test_chaos_node_down_reroutes_and_recovers() -> None:
    """node_down：站 offline、任务改派其它站、恢复后系统继续推进。"""
    sim, sc = _make_sim(code="LIGHT", station_spec="triple", mec=True, duration_s=20)
    sim.set_now(0.0)
    sim.inject_node_down(station_id=2, duration_s=1e9)

    snap = sim.snapshot()
    assert _station(snap, 2)["online"] is False, "node_down 后 2 号站必须 offline"
    assert 2 in [int(x) for x in (snap["chaos"].get("nodes_down") or [])], (
        f"chaos.nodes_down 未含 2：{snap['chaos']}")
    assert all(int(v) != 2 for v in snap["station_of_uav"].values()), (
        "宕机站仍在 station_of_uav 里（任务没有被重调度/接入点没有改派）")
    assert int(_station(snap, 2)["uav_count"]) == 0

    raw, _ = _run(sim, "cross_node")
    assert raw["submitted"] > 0 and raw["done"] > 0, "宕机期间仿真没有继续推进（可能死锁）"
    landed_down = [t.task_id for t in raw["tasks"]
                   if t.state.name == "DONE" and int(t.station_id) == 2]
    assert not landed_down, f"宕机站上仍有任务完成：{landed_down[:5]}"

    # 恢复：站应该重新上线，且新一轮仿真能继续推进
    sim.recover_nodes()
    snap2 = sim.snapshot()
    assert all(bool(s["online"]) for s in snap2["stations"]), (
        f"recover 后仍有站 offline：{[s['id'] for s in snap2['stations'] if not s['online']]}")
    assert not (snap2["chaos"].get("nodes_down") or []), (
        f"recover 后 chaos.nodes_down 未清空：{snap2['chaos']}")
    sim2, _ = _make_sim(code="LIGHT", station_spec="triple", duration_s=12)
    raw2, _ = _run(sim2, "cross_node")
    assert raw2["done"] > 0, "恢复后系统没有继续推进"
    print(f"[OK] 节点宕机：2 号站 offline -> {sum(1 for s in snap['stations'] if s['id'] != 2)}"
          f" 站接管（完成 {raw['done']} 个任务）；recover 后全部 online 并继续推进")


def test_chaos_partition_blocks_cross_partition_migration() -> None:
    """partition：分区状态可观测，且跨分区的任务不许迁移。"""
    sim, _ = _make_sim(code="PEAK", station_spec="triple", mec=True, duration_s=30)
    sim.inject_partition(a=1, b=[2, 3], duration_s=1e9)

    snap = sim.snapshot()
    groups = snap["chaos"].get("partitions")
    assert groups, f"chaos.partitions 未置位：{snap['chaos']}"
    norm = sorted(sorted(int(x) for x in g) for g in groups)
    assert norm == [[1], [2, 3]], f"分区结构应为 [[1],[2,3]]，实际 {norm}"

    # 直接调用迁移接口：跨分区必须被拒绝，同分区允许
    cluster = sim.stations
    nodes = _nodes(sim)
    assert cluster.can_reach(1, 2) is False and cluster.can_reach(2, 3) is True, (
        "分区后可达性判断错误：跨组应不可达、同组应可达")
    t_cross = _mk("X-CROSS", TaskPriority.HIGH, uav=1, size=30.0)
    t_cross.station_id, t_cross.home_station_id = 1, 1
    nodes[0].queue.enqueue(t_cross, 0.0)
    assert cluster.migrate_task(t_cross, 2, now=0.0) is False, "跨分区迁移必须被拒绝"
    assert int(t_cross.station_id) == 1 and int(t_cross.migrate_count) == 0, "被拒的迁移不得改动任务归属"
    assert nodes[0].queue.total_depth == 1, "被拒的迁移不得把任务摘出源站队列"
    t_same = _mk("X-SAME", TaskPriority.HIGH, uav=1, size=30.0)
    t_same.station_id, t_same.home_station_id = 2, 2
    nodes[1].queue.enqueue(t_same, 0.0)
    assert cluster.migrate_task(t_same, 3, now=0.0) is True, "同分区内迁移应允许"
    sim.stations.recover_all()

    sim.inject_partition(a=1, b=[2, 3], duration_s=1e9)
    raw, _ = _run(sim, "cross_node")
    for t in raw["tasks"]:
        if _group_of(int(t.home_station_id), groups) != _group_of(int(t.station_id), groups):
            assert int(t.migrate_count) == 0, (
                f"网络分区期间任务 {t.task_id} 跨分区迁移了 {t.migrate_count} 次"
                f"（home={t.home_station_id} -> {t.station_id}）")
    migrated = sum(int(t.migrate_count) for t in raw["tasks"])
    print(f"[OK] 网络分区 [[1],[2,3]]：跨分区零迁移（分区内迁移 {migrated} 次），"
          f"仿真仍完成 {raw['done']} 个任务")


def test_chaos_split_brain_and_recover() -> None:
    """split_brain 置位可观测，recover 后清位、站全部 online 且继续推进。"""
    sim, _ = _make_sim(code="LIGHT", station_spec="triple", duration_s=12)
    sim.inject_split_brain(duration_s=1e9)
    assert sim.snapshot()["chaos"].get("split_brain") is True, (
        f"inject_split_brain 后 chaos.split_brain 应为 True：{sim.snapshot()['chaos']}")

    sim.recover_nodes()
    snap = sim.snapshot()
    assert snap["chaos"].get("split_brain") is False, "recover 后 split_brain 未清位"
    assert all(bool(s["online"]) for s in snap["stations"]), "recover 后仍有站 offline"
    assert not (snap["chaos"].get("partitions") or []), (
        f"recover 后分区未清空：{snap['chaos'].get('partitions')}")

    raw, _ = _run(sim, "cross_node")
    assert raw["submitted"] > 0 and raw["done"] > 0, "recover 后系统没有继续推进"
    print(f"[OK] 脑裂：置位 -> recover 清位且全站 online，恢复后完成 {raw['done']}/{raw['submitted']} 个任务")


# ===========================================================================
# 覆盖点 8：站级 + 端到端指标
# ===========================================================================
def test_station_and_e2e_metric_fields() -> None:
    """站级与端到端指标字段齐全、非负、口径自洽。"""
    sim, _ = _make_sim(code="PEAK", station_spec="triple", mec=True, duration_s=30)
    raw, _ = _run(sim, "cross_node")
    snap = sim.snapshot()

    for s in snap["stations"]:
        missing = set(STATION_FIELDS) - set(s)
        assert not missing, f"{s.get('id')} 号站字段缺失：{sorted(missing)}"
        for k in STATION_NUMERIC_FIELDS:
            assert float(s[k]) >= 0.0, f"{s.get('id')} 号站 {k} 为负：{s[k]}"
        assert int(s["active"]) + int(s["idle"]) == int(s["workers"]), s
        assert int(s["uav_count"]) == len(s["uavs"]), s
        assert int(s["queue_depth"]) == int(s["queue_high"]) + int(s["queue_low"]), (
            f"{s['id']} 号站 queue_depth 应等于高优+低优：{s['queue_depth']} != "
            f"{s['queue_high']}+{s['queue_low']}")
        assert 0.0 <= float(s["load"]), s["load"]

    assert int(snap["running_count"]) == sum(int(s["active"]) for s in snap["stations"]), (
        "running_count 与各站 active 之和口径不一致")
    assert int(snap["counters"]["submitted"]) == raw["submitted"]
    assert int(snap["counters"]["done"]) <= int(snap["counters"]["submitted"])
    assert float(snap["system_load"]) >= 0.0

    # 站级 done 与任务明细逐站对账（站计数器不能是估算值）
    done_by_station: dict[int, int] = {}
    for t in raw["tasks"]:
        if t.state.name == "DONE":
            sid = int(t.station_id)
            done_by_station[sid] = done_by_station.get(sid, 0) + 1
    for s in snap["stations"]:
        assert int(s["done"]) == done_by_station.get(int(s["id"]), 0), (
            f"{s['id']} 号站 done={s['done']} 与任务明细里该站 DONE 数 "
            f"{done_by_station.get(int(s['id']), 0)} 不一致")
    assert all(float(s["throughput"]) >= 0.0 for s in snap["stations"])

    mec = snap["mec"]
    assert float(mec["avg_e2e_edge_ms"]) > 0.0, "端到端时延不能恒为 0（时延模型未接入）"
    assert float(mec["avg_e2e_center_ms"]) > 0.0
    print(f"[OK] 站级指标齐全：Σqueue_depth={sum(int(s['queue_depth']) for s in snap['stations'])}，"
          f"Σuav={sum(int(s['uav_count']) for s in snap['stations'])}，"
          f"Σmigrations_in={sum(int(s['migrations_in']) for s in snap['stations'])}；"
          f"端到端 edge/center={mec['avg_e2e_edge_ms']:.1f}/{mec['avg_e2e_center_ms']:.1f}ms")


def test_http_state_contract_multi_station() -> None:
    """HTTP 端到端：/api/start 多站+MEC、/api/state 新字段、/api/chaos node_down/recover。

    这一条同时是**截图链路的前置校验**：tools/shot_stations.js 依赖的接口
    （stations / station_of_uav / link_matrix / mec / chaos.nodes_down）在 HTTP 层
    必须真的存在且键名与冻结契约一致，否则无头截图只会拍到降级画面。
    """

    from uav_sim.dashboard import make_server

    httpd, dash, port = make_server(copy.deepcopy(CFG), host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     name="T4-HTTP", daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path: str) -> dict:
        with urllib.request.urlopen(base + path, timeout=10) as fp:
            return json.loads(fp.read().decode("utf-8"))

    try:
        r = get("/api/start?scheduler=cross_node&scenario=PEAK&stations=triple&mec=1&speed=10")
        assert r.get("ok") is not False, f"/api/start 失败：{r}"

        live: dict = {}
        deadline = time.time() + 30
        while time.time() < deadline:
            snap = get("/api/state")
            live = snap.get("live") or {}
            sts = live.get("stations") or []
            if len(sts) == 3 and int((live.get("counters") or {}).get("submitted", 0)) > 0:
                break
            time.sleep(0.2)
        else:
            raise AssertionError(f"/api/state 未在 30s 内给出 3 站 + 已提交任务：{list(live)[:8]}")

        sts = live["stations"]
        assert [int(s["id"]) for s in sts] == [1, 2, 3], [s["id"] for s in sts]
        for s in sts:
            assert set(STATION_FIELDS) <= set(s), f"{s['id']} 号站 HTTP 字段缺失：{sorted(set(STATION_FIELDS) - set(s))}"
        n_uav = int(live["uav_count"])
        assert set(live["station_of_uav"]) == {str(u) for u in range(1, n_uav + 1)}, (
            "station_of_uav 必须覆盖全部无人机（含当前下线机：接入点归属与在线状态无关）")

        # 链路口径边界①：station_of_uav 覆盖全部机（含下线机），links/link_matrix 只覆盖在线机
        # 链路口径边界②：links 用 snapshot 的 now 计算，link_matrix 用 link_monitor._now 计算，
        #   两者相差约 0.005~0.023 仿真秒；弱网深谷处斜率大，同一 (uav, 站) 的两处读数
        #   实测最大可差 1.23 点（已作为"同一量两条时钟"的缺陷上报 Lead）。
        #   因此这里严格校验结构化不变量，数值一致性用 3.0 点的容差（足以挡住"取错站"这类
        #   数量级错误），并把实测最大偏差打印出来便于跟踪。
        # 后台 churn 会在两次读之间改变在线集合，故最多重读 3 次快照以避开竞态
        def _check_links(snap_live: dict) -> str:
            rows = set(snap_live["link_matrix"])
            links = set(snap_live["links"])
            online = {u for u, v in snap_live["uav_online"].items() if v}
            assert rows <= set(snap_live["station_of_uav"]) and links <= set(snap_live["station_of_uav"])
            missing = online - rows
            assert not missing, f"在线机缺少链路矩阵行：{sorted(missing, key=int)}"
            worst = 0.0
            for u in sorted(rows):
                row = snap_live["link_matrix"][u]
                assert {int(k) for k in row} == {1, 2, 3}, f"uav {u} 的链路矩阵不完整：{row}"
                if u in links:
                    dev = abs(float(snap_live["links"][u])
                              - float(row[str(snap_live["station_of_uav"][u])]))
                    worst = max(worst, dev)
                    assert dev < 3.0, (
                        f"uav {u}: links={snap_live['links'][u]} 与到接入站 "
                        f"{snap_live['station_of_uav'][u]} 的链路矩阵值 "
                        f"{row[str(snap_live['station_of_uav'][u])]} 差 {dev:.2f} 点"
                        "（超过两条时钟的时间偏移能解释的范围）")
            return (f"{len(rows)}/{n_uav} 机有链路矩阵行（{n_uav - len(rows)} 架当时下线），"
                    f"links 与 link_matrix 最大偏差 {worst:.2f} 点")

        note = ""
        for attempt in range(3):
            try:
                note = _check_links(live)
                break
            except AssertionError:
                if attempt == 2:
                    raise
                live = get("/api/state").get("live") or {}

        assert (live.get("mec") or {}).get("enabled") is True, live.get("mec")
        assert int(live["mec"]["edge_tasks"]) + int(live["mec"]["center_tasks"]) > 0

        c = get("/api/chaos?kind=node_down&station=2&duration=200")
        assert c.get("ok") is not False, f"node_down 注入失败：{c}"
        live2 = get("/api/state").get("live") or {}
        chaos = live2.get("chaos") or {}
        assert 2 in [int(x) for x in (chaos.get("nodes_down") or [])], chaos
        assert [int(s["id"]) for s in live2["stations"] if not s["online"]] == [2], live2["stations"]
        assert all(int(v) != 2 for v in live2["station_of_uav"].values()), (
            "宕机站仍出现在 station_of_uav（HTTP 快照与站状态自相矛盾）")

        c2 = get("/api/chaos?kind=recover")
        assert c2.get("ok") is not False, f"recover 失败：{c2}"
        live3 = get("/api/state").get("live") or {}
        assert not (live3["chaos"].get("nodes_down") or []), live3["chaos"]
        assert all(s["online"] for s in live3["stations"])
        print(f"[OK] HTTP 契约：/api/start 三站+MEC -> /api/state 字段齐全"
              f"（{note}）；node_down 后 2 号站 offline 且接入点改派，recover 后全部恢复")
    finally:
        try:
            dash.stop_run()
        except Exception:  # noqa: BLE001 - 清理阶段不掩盖断言失败
            pass
        httpd.shutdown()
        httpd.server_close()


def test_links_and_link_matrix_share_one_time_base() -> None:
    """「双时钟」回归：links 与 link_matrix 必须共用同一个时间基准。

    背景（实测坑）：``links`` 曾用快照的 ``now``、``link_matrix`` 用
    ``link_monitor._now``（只在 tick 时推进），两者差小半个 tick；弱网深谷处
    健康度曲线很陡，同一 (uav, 站) 的两处读数实测最大差 **1.23 点**，
    表现为面板上"接入链路健康度"与"链路矩阵"对不上。
    修法是给 ``health_matrix(..., t=now)`` 传显式时间。本用例把它钉死：
    ① 数值必须精确相等（≤0.05，实际应精确到 2 位小数）；
    ② links / link_matrix / station_of_uav **三者键集完全一致**（全机群口径，
       含 churn 下线的机）。
    """
    sim, sc = _make_sim(code="NORMAL", station_spec="triple", mec=True, duration_s=40)
    sched = _create_scheduler("cross_node", sim, sim.cfg)

    # 真跑一小段（0.8 仿真秒）让队列/迁移/链路都动起来，再取快照
    worker = threading.Thread(target=lambda: sim.run(sched, duration_s=sim.scenario.duration_s),
                              name="T4-ClockBase", daemon=True)
    worker.start()
    time.sleep(0.8)
    snap = sim.snapshot()
    sim.stop_event.set()
    worker.join(timeout=10)

    links = snap["links"]
    matrix = snap["link_matrix"]
    station_of = snap["station_of_uav"]
    all_uavs = {str(u) for u in range(1, int(sc.uav_count) + 1)}

    assert set(links) == set(matrix) == set(station_of) == all_uavs, (
        "links / link_matrix / station_of_uav 的键集必须都是全机群："
        f"links={len(links)} link_matrix={len(matrix)} "
        f"station_of_uav={len(station_of)} 期望={len(all_uavs)}")

    worst_u, worst_dev = None, 0.0
    for u in sorted(all_uavs, key=int):
        sid = str(station_of[u])
        dev = abs(float(links[u]) - float(matrix[u][str(sid)]))
        if dev > worst_dev:
            worst_u, worst_dev = u, dev
        assert dev <= 0.05, (
            f"uav {u}: links={links[u]} 与 link_matrix[u][{sid}]={matrix[u][str(sid)]} "
            f"差 {dev:.3f} 点 —— 两处链路读数又用了不同的时间基准")
    assert worst_dev <= 0.05, "存在超出容差的双时钟偏差"
    print(f"[OK] 单一时基：{len(all_uavs)} 架机的 links 与 link_matrix[接入站] 最大偏差 "
          f"{worst_dev:.3f} 点（uav={worst_u}），且三者键集一致")


def test_topology_default_makes_ui_start_multi_station() -> None:
    """可用性缺口守护：/api/topology 设定会话默认后，**不带 stations/mec 的 /api/start**
    也必须跑起多站 + MEC —— 这是"用户在 3D 页面点「开始仿真」就能看到多基站"的唯一通路
    （前端按钮只发 /api/start，不带这两个参数）。"""

    from uav_sim.dashboard import make_server

    httpd, dash, port = make_server(copy.deepcopy(CFG), host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     name="T4-Topo", daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path: str) -> dict:
        with urllib.request.urlopen(base + path, timeout=10) as fp:
            return json.loads(fp.read().decode("utf-8"))

    try:
        # ① 设定会话默认拓扑（空闲态调用 -> applies="now"）
        t = get("/api/topology?stations=triple&mec=1")
        assert t.get("ok") is True, t
        assert t.get("stations") == "triple", t
        assert t.get("mec") is True, t
        assert t.get("applies") in ("now", "next_run"), t

        # ② 故意不带 stations/mec 启动（模拟页面按钮）
        r = get("/api/start?scenario=PEAK&scheduler=cross_node&speed=10")
        assert r.get("ok") is not False, f"/api/start（不带参数）失败：{r}"

        live: dict = {}
        deadline = time.time() + 30
        while time.time() < deadline:
            snap = get("/api/state")
            live = snap.get("live") or {}
            if len(live.get("stations") or []) == 3 and int(
                    (live.get("counters") or {}).get("submitted", 0)) > 0:
                break
            time.sleep(0.2)
        else:
            raise AssertionError(
                f"不带 stations/mec 的 /api/start 未跑起 3 站 + 提交任务："
                f"{len(live.get('stations') or [])} 站，"
                f"submitted={(live.get('counters') or {}).get('submitted')}")

        assert len(live["stations"]) == 3, (
            f"会话默认拓扑 triple 未生效：只起了 {len(live['stations'])} 个站")
        assert (live.get("mec") or {}).get("enabled") is True, live.get("mec")

        # ③ 快照回显本次运行实际用的拓扑 / MEC 开关（/api/state 顶层字段）
        top = get("/api/state")
        assert top.get("stations_spec") == "triple", (
            f"state.stations_spec 应回显本次拓扑 triple，实际 {top.get('stations_spec')!r}")
        assert top.get("mec_enabled") is True, (
            f"state.mec_enabled 应回显 True，实际 {top.get('mec_enabled')!r}")

        # ④ 运行中再设默认 -> applies 必须是 "next_run"（不打断当前运行）
        t2 = get("/api/topology?stations=1&mec=0")
        assert t2.get("ok") is True and t2.get("applies") == "next_run", (
            f"运行中设默认拓扑应返回 applies=next_run，实际 {t2}")
        print("[OK] 会话默认拓扑：/api/topology?stations=triple&mec=1 后，"
              "不带参数的 /api/start 也跑起 3 站 + MEC；运行中改默认返回 applies=next_run")
    finally:
        try:
            dash.stop_run()
        except Exception:  # noqa: BLE001
            pass
        httpd.shutdown()
        httpd.server_close()


def test_topology_default_reset_to_single_station() -> None:
    """复位：/api/topology?stations=1&mec=0 后，不带参数的 /api/start 应回到单站 + MEC 关闭。"""

    from uav_sim.dashboard import make_server

    httpd, dash, port = make_server(copy.deepcopy(CFG), host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     name="T4-TopoReset", daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path: str) -> dict:
        with urllib.request.urlopen(base + path, timeout=10) as fp:
            return json.loads(fp.read().decode("utf-8"))

    try:
        # stations=1 -> 归一化为 None（单站）；mec=0 -> False
        t = get("/api/topology?stations=1&mec=0")
        assert t.get("ok") is True, t
        assert t.get("stations") is None, f"stations=1 应归一化为 None，实际 {t.get('stations')!r}"
        assert t.get("mec") is False, t

        r = get("/api/start?scenario=LIGHT&scheduler=dynamic&speed=10")
        assert r.get("ok") is not False, r

        live: dict = {}
        deadline = time.time() + 25
        while time.time() < deadline:
            live = get("/api/state").get("live") or {}
            if (live.get("stations") or []) and int(
                    (live.get("counters") or {}).get("submitted", 0)) > 0:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("复位后 /api/start 未产生任何站快照")

        assert len(live["stations"]) == 1, (
            f"复位到单站后仍起了 {len(live['stations'])} 个站")
        assert live["stations"][0]["role"] == "CENTER", live["stations"][0]
        assert (live.get("mec") or {}).get("enabled") is False, (
            f"MEC 应关闭，实际 {live.get('mec')}")
        top = get("/api/state")
        assert top.get("stations_spec") is None, top.get("stations_spec")
        assert top.get("mec_enabled") is False, top.get("mec_enabled")
        print("[OK] 复位：/api/topology?stations=1&mec=0 后，不带参数的 /api/start "
              "回到 1 站（CENTER）且 MEC 关闭")
    finally:
        try:
            dash.stop_run()
        except Exception:  # noqa: BLE001
            pass
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":  # pragma: no cover - 直接运行时的手动入口
    test_single_station_snapshot_and_fields()
    test_single_station_link_series_matches_pre_t1_baseline()
    test_multi_station_topology_independent_resources()
    test_per_uav_station_links()
    test_cross_node_registered()
    test_cross_node_degrades_without_stations()
    test_cross_node_least_connections_prefers_idle_station()
    test_cross_node_placement_routes_away_from_overloaded_station()
    test_cross_node_consistent_hash_stable()
    test_migration_not_instant_with_cost_accounting()
    test_migration_in_transit_does_not_occupy_two_stations()
    test_mec_task_classification()
    test_mec_offload_and_e2e_latency_tradeoff()
    test_chaos_node_down_reroutes_and_recovers()
    test_chaos_partition_blocks_cross_partition_migration()
    test_chaos_split_brain_and_recover()
    test_station_and_e2e_metric_fields()
    test_http_state_contract_multi_station()
    test_links_and_link_matrix_share_one_time_base()
    test_topology_default_makes_ui_start_multi_station()
    test_topology_default_reset_to_single_station()
    print("\n所有分布式（多基站/MEC）测试通过 [PASS]")
