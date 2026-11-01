"""test_rebalance.py —— P2 一致性哈希**再平衡**专项测试
=============================================================================

覆盖点（对应 task-1 / docs/11 §8 第 4 条从"未做"升级为"已做"）：

1. **加站单调性**：3 -> 4 站后，所有映射发生变化的 key **全部迁往新站**，
   且 1/2/3 号站之间不发生任何 key 搬迁 —— 这就是一致性哈希的定义性质
   （不是"看起来像"，而是可断言的充分条件）。
2. **删站单调性**：3 -> 2 站后，所有变化的 key **全部来自被删站**，其余 key 原地不动。
3. **比例量级**：加站 ≈ 1/(N+1) = 25%，删站 ≈ 1/N = 33%（而不是朴素取模的 100%）。
4. **运行期 API**：``rebuild_ring()`` / ``add_node()`` / ``remove_node()`` 的
   前置校验（有排队/在跑任务时拒绝并给出原因；至少保留 1 个站；id 不可重复）。
5. **只对在线站选**：``pick_by_hash`` 跳过离线站；``hash_owner`` 是**纯映射**
   （不受在线状态影响，供上面的单调性断言使用）。
6. **仿真器侧再平衡**：``Simulator.rebalance_access()`` 把接入归属
   （``_uav_station`` / ``station_of_uav``）按新环重排，事件记 ``uav_rehome``；
   ``Simulator.add_station`` / ``remove_station`` 热插拔（含运行中热加站）。

-----------------------------------------------------------------------------
为什么比例断言只能给"宽容差"，不能写死 ±1%
-----------------------------------------------------------------------------
一致性哈希里某站占环的比例 = 该站 **V 个虚拟节点** 覆盖的弧长之和。V=64 时，
这个比例的标准差约 ``(1/N) / sqrt(V)``：N=3 时 ≈ 4%/√64... 具体地，
单站占比的波动 ≈ 4~8 个百分点（N=3、V=64 的实测：加站后 4 号站占环 28.0%，
理论 25%；删站时被删站占环 30.6%，理论 33.3%）。

因此把容差收到 ±1% 会得到一个**时红时绿**的测试（改一下 ``hash_virtual_nodes``
或换一批 key 就会失败），而它并没有更接近"再平衡是否正确"这个命题。
真正硬的断言是上面那条**单调性**（零容差、纯逻辑），比例只用来守"量级对了、
没有退化成全量重映射"这一层。容差取 ±13pp 左右已覆盖 >3σ，同时仍能把
"50% 或 100% 重映射"这类真错误挡在门外。
"""

from __future__ import annotations

import copy
import random
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

# Windows 控制台默认 cp936，遇到 GBK 不支持的符号会让测试假失败
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from uav_sim.config import load_config  # noqa: E402
from uav_sim.models import Task, TaskPriority, TaskType  # noqa: E402
from uav_sim.scenarios import generate_trace, get_scenario  # noqa: E402
from uav_sim.scheduler import create_scheduler  # noqa: E402
from uav_sim.simulator import Simulator  # noqa: E402
from uav_sim.station import StationCluster, StationSpec, resolve_station_specs  # noqa: E402

CFG = load_config()
SEED = 20261031
#: 一致性哈希虚拟节点数（config.yaml 的 stations.hash_virtual_nodes，默认 64）
VNODES = int(CFG["stations"]["hash_virtual_nodes"])
#: key 总数（要求 2000~5000：够多才能把"占环比例"测准，又不拖慢测试）
KEY_COUNT = 4000


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _spec4() -> StationSpec:
    """第 4 个站（边缘站 D）：容量与 triple 拓扑里的边缘站同档，便于横向对照。"""
    return StationSpec(id=4, name="边缘站D", role="EDGE", x=70.0, z=45.0,
                       workers=3, bandwidth_mbps=90.0, backhaul_mbps=60.0,
                       edge_compute_mbps=120.0)


def _cluster(n: int = 3) -> StationCluster:
    """按 config.yaml 的 triple 拓扑取前 n 个站构造簇（n<=3）。"""
    specs = resolve_station_specs(CFG, "triple")[:int(n)]
    return StationCluster(specs, copy.deepcopy(CFG))


def _keys() -> list:
    """两路 key：① 业务 key ``uav:{id}``（与 ``home_station_for`` 同构）；
    ② 定种随机字符串（模拟任意会话/缓存 key）。"""
    rnd = random.Random(SEED)
    half = KEY_COUNT // 2
    keys = [f"uav:{i}" for i in range(1, half + 1)]
    keys += [f"session-{i}-{rnd.randrange(1 << 30)}" for i in range(half)]
    return keys


def _make_sim(station_spec: str | None = "triple", duration_s: float = 6.0,
              code: str = "LIGHT"):
    """构造 Simulator（duration 改短以提速，其余参数不动）。"""
    cfg = copy.deepcopy(CFG)
    sc = replace(get_scenario(code), duration_s=float(duration_s))
    trace = generate_trace(sc, SEED, cfg)
    kw = {"station_spec": station_spec} if station_spec else {}
    sim = Simulator(sc, trace, cfg, scheduler_name="cross_node", seed=SEED, **kw)
    return sim, sc


def _mk_task(tid: str, sid: int) -> Task:
    t = Task(task_id=tid, uav_id=1, priority=TaskPriority.HIGH,
             task_type=TaskType.IMAGERY, data_size_mb=30.0,
             submit_ts=0.0, service_s=1.0, station_id=sid, home_station_id=sid)
    t.enqueue_ts = 0.0
    return t


# ===========================================================================
# 覆盖点 1：加站单调性 + 比例量级
# ===========================================================================
def test_add_node_only_migrates_keys_to_the_new_station() -> None:
    """3 -> 4 站：变化的 key 100% 迁往新站，其余站之间零搬迁。"""
    cl = _cluster(3)
    keys = _keys()
    before = {k: cl.hash_owner(k) for k in keys}
    assert len(cl._ring) == 3 * VNODES, "环规模应为 站数 × hash_virtual_nodes"

    node = cl.add_node(_spec4())
    assert int(node.spec.id) == 4
    assert [int(n.spec.id) for n in cl.nodes] == [1, 2, 3, 4], "节点表必须按 id 升序"
    # 显式重建幂等：add_node 已建过，再调一次结果必须一致
    assert cl.rebuild_ring() == 4 * VNODES

    after = {k: cl.hash_owner(k) for k in keys}
    changed = [k for k in keys if before[k] != after[k]]
    assert changed, "加站后竟无任何 key 映射变化 —— 环没有真正重建"
    bad = [k for k in changed if after[k] != 4][:5]
    assert not bad, f"违反一致性哈希单调性：变化的 key 必须全部迁往新站 4，实际 {bad}"
    drift = [k for k in keys if after[k] != 4 and before[k] != after[k]][:5]
    assert not drift, f"1/2/3 号站之间不该发生 key 搬迁（环只增不减），实际 {drift}"

    ratio = len(changed) / float(len(keys))
    assert 0.15 <= ratio <= 0.38, (
        f"加站重映射比例 {ratio:.3f} 偏离 1/(N+1)=0.25 太多"
        "（容差理由见模块 docstring：V=64 时单站占环比例有 ~±8pp 波动）")
    print(f"[OK] 加站 3->4：重映射 {len(changed)}/{len(keys)} = {ratio:.1%}（理论 25%），"
          f"全部迁往 4 号站、其余站零搬迁；环 {3 * VNODES}->{4 * VNODES} 个虚拟节点")


# ===========================================================================
# 覆盖点 2：删站单调性 + 比例量级
# ===========================================================================
def test_remove_node_only_migrates_keys_off_the_removed_station() -> None:
    """3 -> 2 站：变化的 key 100% 来自被删站，其余 key 原地不动。"""
    cl = _cluster(3)
    keys = _keys()
    before = {k: cl.hash_owner(k) for k in keys}

    ok, msg = cl.remove_node(3)
    assert ok, f"空载站应当允许删除，实际被拒：{msg}"
    assert cl.node(3) is None and sorted(cl.by_id) == [1, 2]
    assert cl.rebuild_ring() == 2 * VNODES

    after = {k: cl.hash_owner(k) for k in keys}
    changed = [k for k in keys if before[k] != after[k]]
    assert changed, "删站后竟无任何 key 映射变化 —— 环没有真正重建"
    bad = [k for k in changed if before[k] != 3][:5]
    assert not bad, f"违反单调性：变化的 key 必须全部来自被删的 3 号站，实际 {bad}"
    drift = [k for k in keys if before[k] != 3 and before[k] != after[k]][:5]
    assert not drift, f"与被删站无关的 key 不得改判归属，实际 {drift}"

    ratio = len(changed) / float(len(keys))
    assert 0.18 <= ratio <= 0.50, (
        f"删站重映射比例 {ratio:.3f} 偏离 1/N=0.333 太多"
        "（容差理由见模块 docstring：被删站占环比例本身就有 ~±8pp 波动）")
    print(f"[OK] 删站 3->2：重映射 {len(changed)}/{len(keys)} = {ratio:.1%}（理论 33.3%），"
          f"全部离开 3 号站、其余 key 原地不动；环 {3 * VNODES}->{2 * VNODES} 个虚拟节点")


# ===========================================================================
# 覆盖点 3：运行期 API 的前置校验
# ===========================================================================
def test_remove_node_refuses_when_busy_then_evacuates() -> None:
    """有排队任务的站不许直接删（给出原因且不改状态）；evacuate=True 可先疏散。"""
    cl = _cluster(3)
    node3 = cl.node(3)
    assert node3 is not None
    t = _mk_task("R-QUEUED", 3)
    node3.queue.enqueue(t, 0.0)
    probe = ["uav:1", "uav:9", "session-probe"]
    ring_before = {k: cl.hash_owner(k) for k in probe}

    ok, reason = cl.remove_node(3)
    assert ok is False and "排队" in reason, f"有排队任务时必须拒绝，实际 {ok}/{reason}"
    assert cl.node(3) is not None and len(cl.nodes) == 3, "被拒的删除不得改动节点表"
    assert {k: cl.hash_owner(k) for k in probe} == ring_before, "被拒的删除不得改动哈希环"
    assert node3.queue.total_depth == 1, "被拒的删除不得动该站队列"

    ok2, reason2 = cl.remove_node(3, now=0.0, evacuate=True)
    assert ok2 is True, f"疏散后应当允许删除，实际 {reason2}"
    assert cl.node(3) is None and sorted(cl.by_id) == [1, 2]
    assert int(t.station_id) in (1, 2), "疏散必须把排队任务改派到存活站"
    assert cl.flush_migrations(10.0) >= 1, "在途迁移到期后必须落到目标站"
    print(f"[OK] 删站前置校验：有排队任务被拒（{reason}）；evacuate=True 后删除成功，"
          f"任务改派到 {int(t.station_id)} 号站")


def test_add_duplicate_id_and_remove_last_station_are_rejected() -> None:
    """id 重复加站抛错；簇里最后一个站不许删。"""
    cl = _cluster(3)
    cl.add_node(_spec4())
    with pytest.raises(ValueError):
        cl.add_node(_spec4())

    single = StationCluster(resolve_station_specs(CFG, None), copy.deepcopy(CFG))
    assert len(single.nodes) == 1
    ok, reason = single.remove_node(1)
    assert ok is False and "至少保留" in reason, f"最后一个站必须拒删，实际 {ok}/{reason}"
    assert len(single.nodes) == 1
    print("[OK] 重复 id 加站抛 ValueError；单站簇拒删最后一个站")


# ===========================================================================
# 覆盖点 4：只对在线站选（hash_owner 是纯映射）
# ===========================================================================
def test_pick_by_hash_skips_offline_station_and_hash_owner_is_pure() -> None:
    """站宕机后：命中该站的 key 顺时针改判到别站，其余 key 一个都不动。"""
    cl = _cluster(3)
    keys = _keys()
    base = {k: cl.hash_owner(k) for k in keys}
    cl.set_online(2, False, now=0.0)

    moved = 0
    for k in keys:
        sid = int(cl.pick_by_hash(k))
        assert sid != 2, f"pick_by_hash 不得选中离线站：key={k}"
        if base[k] == 2:
            assert sid in (1, 3), f"命中离线站的 key 应顺时针改判到 1/3，实际 {sid}"
            moved += 1
        else:
            assert sid == base[k], f"未命中离线站的 key 不该改判：key={k}"
    assert moved > 0, "2000+ 个 key 里不可能没有一个命中 2 号站（环没生效？）"

    # hash_owner 是**纯映射**：在线状态不影响它（单调性断言依赖这一点）
    assert {k: cl.hash_owner(k) for k in keys} == base, "hash_owner 不应受在线状态影响"
    cl.set_online(2, True, now=0.0)
    assert {k: int(cl.pick_by_hash(k)) for k in keys} == base, "站恢复后映射应回到原样"
    print(f"[OK] 2 号站离线：{moved}/{len(keys)} 个 key 改判到 1/3 号站，其余零改动；"
          "站恢复后映射逐 key 复原（hash_owner 为纯映射）")


# ===========================================================================
# 覆盖点 5：仿真器侧再平衡（接入归属 _uav_station / station_of_uav）
# ===========================================================================
def test_simulator_rebalance_access_after_add_and_remove() -> None:
    """热加站 -> rebalance_access()：接入归属全部按新环重排，事件记 uav_rehome。

    用 PEAK 工况（30 架机）构造：机群越大，"接入归属再平衡"的读数越有代表性
    （LIGHT 只有 5 架机，新站占环 ~28% 时是否有机落到新站取决于哈希，样本太小）。
    """
    sim, sc = _make_sim(code="PEAK")
    before = dict(sim._uav_station)
    assert set(before.values()) <= {1, 2, 3}
    assert int(sc.uav_count) == 30

    info = sim.add_station(_spec4())
    assert info["ok"] is True and int(info["station"]) == 4
    reb = info["rebalance"]
    assert reb["total"] == int(sc.uav_count)
    changed_uavs = sorted(u for u in range(1, int(sc.uav_count) + 1)
                          if int(sim._uav_station[u]) != int(before[u]))
    assert reb["changed"] == len(changed_uavs), "changed 计数与逐机核对不一致"
    assert changed_uavs, "30 架机里不可能没有一架落到新站（环没生效？）"
    assert all(int(sim._uav_station[u]) == 4 for u in changed_uavs), (
        "加站后变化的接入归属必须全部落到新站 4")
    assert sim.stations.node(4).uavs == set(changed_uavs), "新站的接入集合必须与归属一致"

    snap = sim.snapshot()
    assert len(snap["stations"]) == 4
    assert {int(k): int(v) for k, v in snap["station_of_uav"].items()} == dict(sim._uav_station)
    assert sum(int(s["uav_count"]) for s in snap["stations"]) == int(sc.uav_count), (
        "各站 uav_count 之和必须等于机群规模（接入归属没有漏机/重机）")
    assert int(_station(snap, 4)["uav_count"]) == len(changed_uavs)
    # 事件留痕：改派的每一架机都记了一条 uav_rehome，且方向都是 -> 新站
    rehomes = [e for e in sim._event_log if e.get("k") == "uav_rehome"]
    assert {int(e["uav"]) for e in rehomes} == set(changed_uavs), (
        f"uav_rehome 事件与改派集合不一致：{sorted(int(e['uav']) for e in rehomes)[:5]} vs "
        f"{changed_uavs[:5]}")
    assert all(int(e["to"]) == 4 and int(e["from" if "from" in e else "frm"]) in (1, 2, 3)
               for e in rehomes), "uav_rehome 的 from/to 必须指向真实站号"

    # ---- 删站：此刻没有任何任务入队/在跑 -> 允许直接删，并把归属搬回 1/2/3 ----
    out = sim.remove_station(4)
    assert out["ok"] is True, out
    assert sim.stations.node(4) is None
    assert all(int(sim._uav_station[u]) in (1, 2, 3)
               for u in range(1, int(sc.uav_count) + 1)), "删站后仍有无人机归属被删站"
    assert set(int(sid) for sid in out["rebalance"]["to_new"]) <= {1, 2, 3}, (
        "删站后的再平衡只能落到存活的 1/2/3 号站")
    assert int(out["rebalance"]["changed"]) == len(changed_uavs), (
        "删站后应当恰好把原先落在新站的机搬回去")
    assert len(sim.snapshot()["stations"]) == 3
    print(f"[OK] 仿真器再平衡：加站 4 -> 接入变化 {reb['changed']}/{reb['total']} "
          f"= {reb['ratio']:.1%}（全部落到新站）；删站后归属全部回到 1/2/3 号站")


def _station(snap: dict, sid: int) -> dict:
    for s in snap["stations"]:
        if int(s["id"]) == int(sid):
            return s
    raise AssertionError(f"快照里没有 {sid} 号站：{[s['id'] for s in snap['stations']]}")


def test_hot_add_station_mid_run_keeps_progressing() -> None:
    """**运行中**热加第 4 站：仿真不崩、不死锁，且新站出现在收尾快照里。

    这条同时验证"新站按需补线程池"这件事真的接了：若没有补池，
    ``dispatch`` 会拒绝受理（留队重试）而不会派 AttributeError 把调度线程带崩。
    """
    sim, _ = _make_sim(duration_s=6.0)
    sched = create_scheduler("cross_node", sim, sim.cfg)
    box: dict = {}

    def _go() -> None:
        box["raw"] = sim.run(sched, duration_s=6.0)

    th = threading.Thread(target=_go, daemon=True)
    th.start()
    time.sleep(0.25)          # wall 0.25s ≈ 2.5 仿真秒（time_scale=0.1），确保"跑到一半"
    assert th.is_alive(), "仿真结束得太快，没测到'运行中热加站'"
    info = sim.add_station(_spec4())
    assert info["ok"] is True
    th.join(timeout=60.0)
    assert not th.is_alive(), "热加站后仿真没有结束（可能死锁 / 派发崩了）"

    raw = box.get("raw") or {}
    assert int(raw.get("submitted", 0)) > 0 and int(raw.get("done", 0)) > 0, (
        "热加站后仿真没有继续推进")
    assert any(int(s["id"]) == 4 for s in raw["stations"]), "新站未出现在收尾快照里"
    print(f"[OK] 运行中热加 4 号站：仿真照常推进（完成 {raw['done']}/{raw['submitted']}），"
          f"新站进入收尾快照（workers={raw['stations'][-1]['workers']}）")
