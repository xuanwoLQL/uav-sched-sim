"""test_quorum.py —— P4 最小 quorum 共识（教学用，**不做完整 Raft**）专项测试
=============================================================================

覆盖点（对应 task-1 / docs/11 §8 第 5 条从"未做"升级为"已做最小版"）：

1. **写入确认语义（N=3 脑裂）**：分区 ``{1} | {2,3}``、``majority = floor(3/2)+1 = 2``：
   * ``{1}`` 侧（size=1 < 2）任务跑完 -> **回滚**：``quorum_rejects`` +1、
     ``uncommitted_tasks`` +1、**done 不增**、任务不在队列里；
   * ``{2,3}`` 侧（size=2 >= 2）任务跑完 -> **提交**：``quorum_commits`` +1、done +1。
2. **恢复重放**：``recover_nodes()`` 后进入新任期（term +1）、未确认任务被
   **重放**回队列，再跑完即提交 —— done 总数"不少"（回滚不等于丢数据）。
3. **多数派按总站数算**：3 站挂掉 2 站（剩 1 站）时仍是"无多数派"，不因为
   "在线数变成了 1" 而自认多数（Raft 口径，不是按存活数）。
4. **单站退化**：单站配置下 majority=1、``has_majority=true``，每次完成都提交，
   ``quorum_rejects == 0`` —— 保证既有 15 组实验口径不被污染。
5. **指标与快照契约**：``models.METRIC_SPEC`` 有 4 个新键；``_station_metrics()``
   透出它们；``metrics.compute_metrics(..., station_metrics=...)`` 会把它们合并进
   返回的扁平指标；``live.quorum`` 的键名与 task-1 约定一致。
6. **真实多线程跑一遍**（无分区）：rejects 恒 0，commits == done —— 说明确认逻辑
   接在真实的完成路径上，而不是只在"手工构造"里成立。

-----------------------------------------------------------------------------
为什么用"直接调 _run_task"做确定性构造
-----------------------------------------------------------------------------
真实多线程仿真里"哪个任务在哪一侧跑完、什么时候跑完"是 wall-clock 相关的
（项目在 docs/09 已论证抖动），拿它去断言"某侧被拒 N 次"会得到一个时红时绿的
测试。因此本模块把"任务已跑完"这一步做成**确定性构造**：直接给任务设好
``station_id`` 与 ``served_s``，调用 ``Simulator._run_task``（完成路径本体），
并把弱网失败判定置 0 —— 这样考的就纯粹是"完成 -> 确认/回滚"这一段共识语义。
真实线程路径另由第 6 条覆盖。
"""

from __future__ import annotations

import copy
import sys
from dataclasses import replace
from pathlib import Path

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
from uav_sim.metrics import compute_metrics  # noqa: E402
from uav_sim.models import METRIC_SPEC, Task, TaskPriority, TaskState, TaskType  # noqa: E402
from uav_sim.scenarios import generate_trace, get_scenario  # noqa: E402
from uav_sim.scheduler import create_scheduler  # noqa: E402
from uav_sim.simulator import Simulator  # noqa: E402

CFG = load_config()
SEED = 20261031
#: P4 新增的指标键（键名冻结；方向见 models.METRIC_SPEC）
QUORUM_METRICS = ("quorum_commits", "quorum_rejects", "uncommitted_tasks", "quorum_term")
#: live.quorum 必须齐备的键（task-1 与 Lead 的 3D 面板消费代码共同约定）
QUORUM_FIELDS = ("term", "majority", "has_majority", "groups",
                 "commits", "rejects", "uncommitted")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _make_sim(station_spec: str | None = "triple", duration_s: float = 6.0,
              code: str = "LIGHT"):
    """构造 Simulator；``station_spec=None`` = 单站默认配置。"""
    cfg = copy.deepcopy(CFG)
    sc = replace(get_scenario(code), duration_s=float(duration_s))
    trace = generate_trace(sc, SEED, cfg)
    kw = {"station_spec": station_spec} if station_spec else {}
    sim = Simulator(sc, trace, cfg, scheduler_name="cross_node", seed=SEED, **kw)
    # 弱网失败判定置 0：本模块只考共识语义，不考重传随机流（否则失败重传会
    # 让"某次完成是否发生"变成随机事件，测试就不确定了）。
    sim._failure_probability = lambda *a, **k: 0.0
    return sim, sc


def _complete_at(sim: Simulator, sid: int, tid: str, uav: int = 1) -> Task:
    """**确定性**地把一个任务在该站"跑完"（走真实的完成路径 ``_run_task``）。

    任务只挂进 ``_by_id/_tasks``（不入队）—— 我们要测的是"跑完之后的确认/回滚"，
    不是调度；任务也没有真在跑的线程，所以不会与其它断言竞争。
    """
    t = Task(task_id=tid, uav_id=int(uav), priority=TaskPriority.HIGH,
             task_type=TaskType.IMAGERY, data_size_mb=50.0,
             submit_ts=0.0, service_s=0.001, station_id=int(sid),
             home_station_id=int(sid))
    t.enqueue_ts = 0.0
    t.served_s = 0.0
    with sim._lock:
        sim._by_id[t.task_id] = t
        sim._tasks.append(t)
    sim._run_task(t, t.service_s)
    return t


def _user_done(sim: Simulator) -> int:
    """用户可见的完成数（= /api/state.counters.done，回滚的任务不计）。"""
    return sum(1 for t in sim.all_tasks() if t.state is TaskState.DONE)


# ===========================================================================
# 覆盖点 1：N=3 脑裂 —— 少数派侧回滚、多数派侧提交
# ===========================================================================
def test_split_brain_minority_rolls_back_majority_commits() -> None:
    """{1} 侧写入被拒（回滚、不进 done）；{2,3} 侧写入提交。"""
    sim, _ = _make_sim()
    assert sim.stations.majority == 2, "N=3 的多数派门槛应为 floor(3/2)+1 = 2"

    groups = sim.stations.set_partition([[1], [2, 3]])
    assert groups == [[1], [2, 3]], f"分区结构应为 [[1],[2,3]]，实际 {groups}"
    assert sim.stations.term == 1, "分区注入应进入新任期（term=1）"
    assert sim.stations.group_has_quorum(1) is False, "1 号站所在组 size=1 < 2，不应有多数派"
    assert sim.stations.group_has_quorum(2) is True, "2 号站所在组 {2,3} size=2 >= 2，应有多数派"
    assert sim.stations.group_has_quorum(3) is True

    # ---- 少数派侧：跑完 -> 回滚 ----
    t_minor = _complete_at(sim, 1, "Q-MINOR", uav=1)
    assert sim._quorum_rejects == 1 and sim._quorum_commits == 0, (
        f"少数派侧应记 1 次拒绝、0 次提交，实际 {sim._quorum_rejects}/{sim._quorum_commits}")
    assert _user_done(sim) == 0 and sim._done_count == 0, "被回滚的任务不得计入 done"
    assert t_minor.state is not TaskState.DONE, "被回滚的任务不得处于 DONE"
    assert t_minor.finish_ts is None, "回滚必须清掉完成时刻（这次写入当作没发生）"
    assert len(sim._uncommitted) == 1 and "Q-MINOR" in sim._uncommitted, "未确认任务应进待重放表"
    assert sim._uncommitted["Q-MINOR"]["station"] == 1
    assert sim.stations.node(1).queue.total_depth == 0, "回滚的任务**不许**留在队列里（否则分区失效）"
    assert sim.stations.node(1).done == 0

    # ---- 多数派侧：跑完 -> 提交 ----
    _complete_at(sim, 2, "Q-MAJOR-2", uav=2)
    _complete_at(sim, 3, "Q-MAJOR-3", uav=3)
    assert sim._quorum_commits == 2, f"多数派侧应提交 2 次，实际 {sim._quorum_commits}"
    assert sim._quorum_rejects == 1, "多数派侧的提交不得被记成拒绝"
    assert _user_done(sim) == 2 and sim._done_count == 2
    assert sim.stations.node(2).done == 1 and sim.stations.node(3).done == 1

    # ---- 站卡片读数（脑裂截图靠这两个字段讲"两侧各自是否多数派"）----
    snap = sim.snapshot()
    by_id = {int(s["id"]): s for s in snap["stations"]}
    assert by_id[1]["partition_group"] == 1 and by_id[1]["is_majority"] is False, (
        f"1 号站应显示'分区组 1 · 无多数派'，实际 {by_id[1]['partition_group']}/"
        f"{by_id[1]['is_majority']}")
    for sid in (2, 3):
        assert by_id[sid]["partition_group"] == 2 and by_id[sid]["is_majority"] is True, (
            f"{sid} 号站应显示'分区组 2 · 多数派'，实际 {by_id[sid]['partition_group']}/"
            f"{by_id[sid]['is_majority']}")

    q = snap["quorum"]
    assert q["term"] == 1 and q["majority"] == 2 and q["has_majority"] is True
    assert q["commits"] == 2 and q["rejects"] == 1 and q["uncommitted"] == 1
    assert [g["size"] for g in q["groups"]] == [1, 2]
    assert [g["has_quorum"] for g in q["groups"]] == [False, True]
    print("[OK] N=3 脑裂 {1}|{2,3}：1 号站写 1 拒 1（done 不增、静置待重放），"
          "2/3 号站提交 2 次；站卡片 is_majority=False/True 可分辨")


# ===========================================================================
# 覆盖点 2：恢复 -> 重放 -> 最终提交（数据不丢）
# ===========================================================================
def test_recover_replays_uncommitted_and_no_data_loss() -> None:
    """恢复分区后：未确认任务重放回队列，再跑完即提交，done 总数不少。"""
    sim, _ = _make_sim()
    sim.stations.set_partition([[1], [2, 3]])
    t_minor = _complete_at(sim, 1, "Q-MINOR")
    _complete_at(sim, 2, "Q-MAJOR-2")
    _complete_at(sim, 3, "Q-MAJOR-3")
    assert _user_done(sim) == 2 and len(sim._uncommitted) == 1

    # ---- 恢复：新任期 + 多数派回来 ----
    sim.set_now(5.0)
    out = sim.recover_nodes()
    assert out["ok"] is True and int(out.get("replayed", 0)) == 1, (
        f"恢复时应重放 1 个未确认任务，实际 {out}")
    assert sim.stations.term == 2, "恢复应再进入一个新任期（term=2）"
    assert len(sim._uncommitted) == 0, "重放后待重放表应清空"
    assert sim._quorum_replayed == 1
    assert t_minor.state is TaskState.READY, f"重放的任务应回到就绪态，实际 {t_minor.state}"
    assert int(t_minor.station_id) == 1
    assert sim.stations.node(1).queue.total_depth == 1, "重放的任务必须回到队列里（不是丢弃）"
    replay_events = [e for e in sim._event_log if e.get("k") == "quorum_replay"]
    assert len(replay_events) == 1 and replay_events[0]["tid"] == "Q-MINOR"

    # ---- 重放的任务再跑完 -> 提交；done 总数 = 3（一个都没丢）----
    sim._run_task(t_minor, t_minor.service_s)
    assert t_minor.state is TaskState.DONE
    assert _user_done(sim) == 3, "重放并提交后 done 应为 3（2 个正常 + 1 个补交）"
    assert sim._quorum_commits == 3 and sim._quorum_rejects == 1
    assert len(sim._uncommitted) == 0
    snap = sim.snapshot()
    assert snap["quorum"]["uncommitted"] == 0 and snap["quorum"]["replayed"] == 1
    assert snap["quorum"]["has_majority"] is True and snap["quorum"]["term"] == 2
    print("[OK] 恢复重放：term 1->2、未确认 1 -> 重放 1 -> 提交 3/3（回滚不等于丢数据）")


# ===========================================================================
# 覆盖点 3：多数派门槛按**总站数**算，不随在线数下降
# ===========================================================================
def test_majority_uses_total_node_count_not_alive_count() -> None:
    """3 站挂掉 2 站：剩 1 站仍无多数派（Raft 口径），站恢复后自动重放。"""
    sim, _ = _make_sim()
    sim.stations.set_online(2, False, now=0.0)
    sim.stations.set_online(3, False, now=0.0)
    assert sim.stations.majority == 2, "多数派门槛不得因站宕机而下降"
    assert sim.stations.group_has_quorum(1) is False, (
        "只剩 1 站在线时 1 < 2，不得自认多数派（否则共识语义失效）")
    view = sim.stations.quorum_view()
    assert [g["ids"] for g in view["groups"]] == [[1]], "离线站不应出现在任何在线组里"
    assert view["has_majority_anywhere"] is False, "全簇无多数派"

    t = _complete_at(sim, 1, "Q-ALONE")
    assert sim._quorum_rejects == 1 and _user_done(sim) == 0
    assert len(sim._uncommitted) == 1

    # 站全部回来 -> 多数派恢复 -> 自动重放
    sim.recover_nodes()
    assert sim.stations.group_has_quorum(1) is True
    assert len(sim._uncommitted) == 0 and sim._quorum_replayed == 1, "站恢复后应自动重放"
    sim._run_task(t, t.service_s)
    assert _user_done(sim) == 1 and sim._quorum_commits == 1
    print("[OK] 多数派按总站数：3 站挂 2 站 -> 剩 1 站仍无多数派（1<2）；"
          "站恢复后自动重放并提交")


# ===========================================================================
# 覆盖点 4：单站完全退化（不得污染既有 15 组口径）
# ===========================================================================
def test_single_station_quorum_always_commits() -> None:
    """单站：majority=1、has_majority=true，每次完成都提交、零拒绝。"""
    sim, sc = _make_sim(station_spec=None)
    assert len(sim.stations.nodes) == 1
    assert int(sc.uav_count) > 0

    for i in range(1, 4):
        _complete_at(sim, 1, f"S-{i}", uav=i)
    assert sim._quorum_commits == 3 and sim._quorum_rejects == 0, (
        f"单站应全部提交、零拒绝，实际 {sim._quorum_commits}/{sim._quorum_rejects}")
    assert len(sim._uncommitted) == 0 and _user_done(sim) == 3
    assert sim.stations.term == 0, "单站不该有任何任期推进"

    snap = sim.snapshot()
    q = snap["quorum"]
    for key in QUORUM_FIELDS:
        assert key in q, f"live.quorum 缺少契约键 {key}：{sorted(q)}"
    assert q["term"] == 0 and q["majority"] == 1 and q["has_majority"] is True
    assert q["groups"] == [{"ids": [1], "size": 1, "has_quorum": True}], q["groups"]
    assert q["rejects"] == 0 and q["uncommitted"] == 0
    st = snap["stations"][0]
    assert st["partition_group"] == 0 and st["is_majority"] is True, (
        "单站站卡片应为 partition_group=0 / is_majority=true（旧字段一个不改）")
    assert st["partitioned"] is False and st["online"] is True
    print("[OK] 单站退化：majority=1 / has_majority=true / 3 次完成全部提交、零拒绝、"
          "term 恒为 0（既有 15 组口径不受影响）")


# ===========================================================================
# 覆盖点 5：指标键与 compute_metrics 合并路径
# ===========================================================================
def test_quorum_metrics_contract_and_merge_path() -> None:
    """4 个新指标在 METRIC_SPEC 里，且经 station_metrics 合并进 compute_metrics。"""
    for key in QUORUM_METRICS:
        assert key in METRIC_SPEC, f"models.METRIC_SPEC 缺少 P4 指标键 {key}"
    assert METRIC_SPEC["quorum_commits"][2] is True, "quorum_commits 方向应为'越高越好'"
    assert METRIC_SPEC["quorum_rejects"][2] is False
    assert METRIC_SPEC["uncommitted_tasks"][2] is False

    sim, sc = _make_sim()
    sim.stations.set_partition([[1], [2, 3]])
    _complete_at(sim, 1, "QM-1")
    _complete_at(sim, 2, "QM-2")

    sm = sim._station_metrics(3.0)
    for key in QUORUM_METRICS:
        assert key in sm, f"Simulator._station_metrics 未透出 {key}"
    assert sm["quorum_commits"] == 1.0 and sm["quorum_rejects"] == 1.0
    assert sm["uncommitted_tasks"] == 1.0 and sm["quorum_term"] == 1.0

    metrics = compute_metrics(
        sim.all_tasks(), duration_s=3.0, uav_count=int(sc.uav_count),
        capacity_mbps=150.0, queue_samples=[], station_metrics=sm, end_ts=3.0)
    assert set(metrics) == set(METRIC_SPEC), "指标键必须与 METRIC_SPEC 完全一致"
    assert metrics["quorum_commits"] == 1.0 and metrics["quorum_rejects"] == 1.0
    assert metrics["uncommitted_tasks"] == 1.0 and metrics["quorum_term"] == 1.0
    # 既有键名/口径一个都没动（抽样自查，防"顺手改名"）
    for legacy in ("throughput", "completion_rate", "avg_wait_all_ms", "resource_utilization"):
        assert legacy in metrics
    print(f"[OK] 指标契约：METRIC_SPEC 共 {len(METRIC_SPEC)} 键（含 4 个 P4 新键），"
          f"station_metrics 已合并进 compute_metrics（commits/rejects/uncommitted="
          f"{metrics['quorum_commits']}/{metrics['quorum_rejects']}/"
          f"{metrics['uncommitted_tasks']}）")


# ===========================================================================
# 覆盖点 6：真实多线程跑一遍（无分区 -> 全部提交）
# ===========================================================================
def test_real_run_without_partition_commits_everything() -> None:
    """无分区多站真跑：rejects 恒 0，commits == done（确认逻辑接在真实完成路径上）。"""
    sim, _ = _make_sim(duration_s=6.0)
    sched = create_scheduler("cross_node", sim, sim.cfg)
    raw = sim.run(sched, duration_s=sim.scenario.duration_s)

    assert int(raw["submitted"]) > 0 and int(raw["done"]) > 0, "仿真没有推进"
    assert sim._quorum_rejects == 0 and len(sim._uncommitted) == 0, (
        f"无分区时不该有回滚：rejects={sim._quorum_rejects} uncommitted={len(sim._uncommitted)}")
    assert sim._quorum_commits == int(raw["done"]), (
        f"提交次数应等于完成数：commits={sim._quorum_commits} done={raw['done']}")
    assert int(raw["station_metrics"]["quorum_commits"]) == int(raw["done"])
    assert int(raw["station_metrics"]["quorum_rejects"]) == 0
    assert len(raw["stations"]) == 3 and all(s["is_majority"] for s in raw["stations"])
    print(f"[OK] 真跑（triple/无分区）：完成 {raw['done']}/{raw['submitted']}，"
          f"提交 {sim._quorum_commits} 次、拒绝 0 次，3 站 is_majority 全 True")
