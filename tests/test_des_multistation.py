"""
tests/test_des_multistation.py —— 多站 + MEC 分析模型（gpu/des_model.py）单元测试
=============================================================================

覆盖：
    * **单站回归红线**：``simulate_one`` 与「扩多站之前」逐位一致（随机流没被扰动）；
    * 单站 multi 与 ``simulate_one`` 逐位一致（默认行为不变）；
    * 多站指标合法（不均衡度 / 端到端时延 / 边缘分流 / 迁移次数）；
    * **加站 + 跨节点均衡后站间不均衡度显著下降**（对照"纯接入亲和"）；
    * ``cross_node`` 与 ``fcfs`` 有可测差异（且迁移确实发生）；
    * MEC 开启后边缘分流 > 0、端到端时延同向变化；
    * 端到端时延口径与 ``docs/11`` §4.4.1 的「纯中心」实测同量级（口径校准）。

设计说明：本模型比参考模型轻（单次仿真约 30ms），但多站 + 20 reps 的比较仍需
控制规模；用 3 个站 + 20 reps 的均值比较，并对"不均衡度下降"留出 5% 余量，
避免做成脆断言。整文件实测约 3.8 s（< 10 s）。

基线取自**改多站之前**的实测输出（5 个 rep × 5 个策略 × 3 个关键指标），
用 ``==`` 逐位比较 —— 这是"单站默认路径没被动过"的硬证据。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:  # pragma: no cover
    pass

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "gpu"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import des_model as dm  # noqa: E402

#: 改多站之前的单站基线：(策略, rep) -> (吞吐, 低优平均等待ms, 业务完整率%)
BASELINE_SINGLE = {
    ("fcfs", 0): (24.2, 1630.2679533133742, 77.31629392971246),
    ("fcfs", 1): (16.2, 7624.685627402742, 50.943396226415096),
    ("fcfs", 2): (18.533333333333335, 4780.889665958179, 59.528907922912204),
    ("fcfs", 3): (24.9, 6702.904550764948, 76.81233933161954),
    ("fcfs", 4): (27.566666666666666, 1606.1050267426251, 89.5021645021645),
    ("rr", 0): (30.283333333333335, 309.1568902780318, 97.63567974207416),
    ("rr", 1): (28.85, 1227.801647411792, 92.7652733118971),
    ("rr", 2): (30.766666666666666, 990.6101256612411, 95.35123966942149),
    ("rr", 3): (30.316666666666666, 431.58345081092114, 95.68648079957917),
    ("rr", 4): (30.083333333333332, 352.6108910134286, 96.06173496540713),
    ("priority", 0): (14.033333333333333, 440.01998589575385, 44.739638682252924),
    ("priority", 1): (6.75, 2.1420586341510703, 22.08287895310796),
    ("priority", 2): (10.183333333333334, 75.12223018185625, 33.81294964028777),
    ("priority", 3): (10.583333333333334, 8.652584464178393, 33.597883597883595),
    ("priority", 4): (17.616666666666667, 3243.884559565912, 55.74894514767932),
    ("dynamic", 0): (28.183333333333334, 2747.1618507695543, 90.57311194429566),
    ("dynamic", 1): (27.883333333333333, 3772.0256956723806, 89.5610278372591),
    ("dynamic", 2): (28.85, 3190.941657421095, 91.92777482740308),
    ("dynamic", 3): (27.933333333333334, 3614.072890190898, 90.10752688172043),
    ("dynamic", 4): (29.8, 713.4766558450918, 94.60317460317461),
    ("mlfq", 0): (30.083333333333332, 1229.0554256921205, 95.95959595959596),
    ("mlfq", 1): (28.416666666666668, 1614.05854534092, 92.41192411924119),
    ("mlfq", 2): (29.55, 1392.797156139466, 95.3225806451613),
    ("mlfq", 3): (29.716666666666665, 1357.3054370829068, 94.4385593220339),
    ("mlfq", 4): (29.316666666666666, 1357.7204538948577, 95.33875338753387),
}

IDX = {k: i for i, k in enumerate(dm.METRIC_KEYS)}
#: 单站路径（未扩多站前）的指标个数
SINGLE_METRIC_COUNT = 13


def _mean(vals: list, key: str) -> float:
    i = IDX[key]
    return sum(v[i] for v in vals) / len(vals)


def _run_multi(mode: int, prm, reps: int = 5) -> list:
    return [dm.simulate_one_multi(rep, mode, prm) for rep in range(reps)]


def test_frozen_single_station_baseline() -> None:
    """单站 ``simulate_one`` 与基线逐位一致 —— 多站改造没有扰动既有随机流。"""
    for (name, rep), (th, wl, cp) in BASELINE_SINGLE.items():
        v = dm.simulate_one(rep, dm.MODE_INDEX[name], dm.DEFAULT_PARAMS)
        assert v[IDX["throughput"]] == th, (name, rep, v[IDX["throughput"]], th)
        assert v[IDX["wait_low_ms"]] == wl, (name, rep, v[IDX["wait_low_ms"]], wl)
        assert v[IDX["completion_pct"]] == cp, (name, rep, v[IDX["completion_pct"]], cp)
    print(f"[OK] 单站基线逐位一致：{len(BASELINE_SINGLE)} 组（5 rep x 5 策略）")


def test_default_params_keep_single_station() -> None:
    """默认参数与「多站参数生成器」的单站默认值完全相同（尾部追加不改变旧语义）。"""
    assert dm.multi_params(stations=1, mec=False) == dm.DEFAULT_PARAMS
    assert dm.DEFAULT_PARAMS[dm.P["n_stations"]] == 1.0
    assert dm.DEFAULT_PARAMS[dm.P["mec_enable"]] == 0.0
    assert len(dm.DEFAULT_PARAMS) == len(dm.PARAM_ORDER)
    assert dm.METRIC_KEYS[:SINGLE_METRIC_COUNT] == [
        "throughput", "wait_high_ms", "wait_low_ms", "starvation_low_pct",
        "completion_pct", "util_pct", "retry_pct", "preempts",
        "promotions", "cached", "max_low_depth", "submitted", "done"]
    assert dm.MODE_NAMES[dm.MODE_CROSS_NODE] == "cross_node"
    print("[OK] 默认参数 = 单站 + MEC 关闭；METRIC_KEYS / 模式表只在尾部追加")


def test_single_station_multi_equals_simulate_one() -> None:
    """``stations=1`` 时 multi 与 ``simulate_one`` 逐位一致，新指标恒为 0。"""
    for mode in (dm.MODE_FCFS, dm.MODE_PRIORITY, dm.MODE_DYNAMIC, dm.MODE_CROSS_NODE):
        for rep in (0, 3):
            base = dm.simulate_one(rep, mode, dm.DEFAULT_PARAMS)
            multi = dm.simulate_one_multi(rep, mode, dm.DEFAULT_PARAMS)
            assert base == multi[:SINGLE_METRIC_COUNT], (mode, rep)
            assert all(x == 0.0 for x in multi[SINGLE_METRIC_COUNT:]), (mode, rep)
    print("[OK] stations=1 的 multi 路径与原单站逐位一致（差异 0，新指标为 0）")


def test_multi_metrics_in_range() -> None:
    """多站 / MEC 的新指标落在合法区间；单站的不均衡度退化为 0。"""
    prm_multi = dm.multi_params(stations=3, mec=True)
    for mode in (dm.MODE_FCFS, dm.MODE_DYNAMIC, dm.MODE_CROSS_NODE):
        for v in _run_multi(mode, prm_multi, reps=2):
            row = dict(zip(dm.METRIC_KEYS, v))
            assert 0.0 <= row["completion_pct"] <= 100.0 + 1e-6
            assert 0.0 <= row["edge_offload_pct"] <= 100.0 + 1e-6
            assert row["station_imb"] >= 0.0
            assert row["e2e_ms"] > 0.0
            assert row["migrations"] >= 0.0
            assert row["migration_cost_ms"] >= 0.0
    prm_one = dm.multi_params(stations=1, mec=True)      # 只有 1 个资源域 -> 不均衡度=0
    one = _run_multi(dm.MODE_DYNAMIC, prm_one, reps=2)
    assert all(v[IDX["station_imb"]] == 0.0 for v in one)
    assert all(v[IDX["e2e_ms"]] > 0.0 for v in one)
    print("[OK] 多站/MEC 指标区间合法；单站不均衡度退化为 0、端到端时延仍可测")


def test_more_stations_lower_imbalance_with_cross_node() -> None:
    """多站下启用「跨节点均衡」后，站间不均衡度显著低于纯接入亲和。

    说明口径：**单站的不均衡度恒为 0**（只有一个资源域，变异系数退化），
    所以"加站降不均衡"唯一可测的含义是：同一多站拓扑下，
    ``cross_node``（站点间最少连接 + 带代价迁移）把「按接入哈希落点」造成的
    站间差异压下去。实测（3 站、每站 2 槽位、300 reps）：
    dynamic 0.224 -> cross_node 0.172。
    这里用 20 reps 的均值、留 5% 余量，避免脆断言。
    """
    prm = dm.multi_params(stations=3, mec=False)
    dyn = _run_multi(dm.MODE_DYNAMIC, prm, reps=20)
    cross = _run_multi(dm.MODE_CROSS_NODE, prm, reps=20)
    imb_d, imb_c = _mean(dyn, "station_imb"), _mean(cross, "station_imb")
    mig = _mean(cross, "migrations")
    assert mig > 0.0, "cross_node 必须真的发生过迁移"
    assert _mean(dyn, "migrations") == 0.0, "dynamic 不做站间迁移"
    assert imb_c < imb_d * 0.95, (imb_d, imb_c)
    print(f"[OK] 站间不均衡度：dynamic {imb_d:.3f} -> cross_node {imb_c:.3f}"
          f"（迁移 {mig:.0f} 次/次仿真）")


def test_cross_node_differs_from_fcfs() -> None:
    """``cross_node`` 与 ``fcfs`` 有可测差异（完整率 + 迁移行为）。"""
    prm = dm.multi_params(stations=3, mec=False)
    fcfs = _run_multi(dm.MODE_FCFS, prm, reps=5)
    cross = _run_multi(dm.MODE_CROSS_NODE, prm, reps=5)
    cp_f, cp_c = _mean(fcfs, "completion_pct"), _mean(cross, "completion_pct")
    assert cp_f < cp_c - 5.0, (cp_f, cp_c)
    assert _mean(cross, "migrations") > 0.0 > _mean(fcfs, "migrations") - 1e-9
    print(f"[OK] cross_node 与 fcfs 差异可测：完整率 {cp_f:.1f}% vs {cp_c:.1f}%")


def test_mec_offloads_light_tasks_and_shortens_e2e() -> None:
    """MEC 开启：边缘分流 > 0，且端到端时延相对「多站无 MEC」下降。"""
    prm_on = dm.multi_params(stations=3, mec=True)
    prm_off = dm.multi_params(stations=3, mec=False)
    on = _run_multi(dm.MODE_DYNAMIC, prm_on, reps=5)
    off = _run_multi(dm.MODE_DYNAMIC, prm_off, reps=5)
    edge_on, edge_off = _mean(on, "edge_offload_pct"), _mean(off, "edge_offload_pct")
    e2e_on, e2e_off = _mean(on, "e2e_ms"), _mean(off, "e2e_ms")
    assert edge_on > 50.0, edge_on
    assert e2e_on < e2e_off, (e2e_on, e2e_off)
    print(f"[OK] MEC：边缘分流 {edge_off:.1f}% -> {edge_on:.1f}%；"
          f"端到端时延 {e2e_off:.1f}ms -> {e2e_on:.1f}ms")


def test_e2e_calibration_matches_reference_docs() -> None:
    """口径校准：纯中心（单站 + 记账开启）的端到端时延与 docs/11 §4.4.1 同量级。

    参考模型的纯中心实测是 122.71 ms（docs/11 §4.4.1）。本分析模型用
    「服务时长 × 120 = 数据量代理」标定，得到约 119~120 ms —— 同一个数量级，
    说明算的确实是「接入 + 计算 + 回传」这一套口径，而不是随手编的数。
    """
    prm = dm.multi_params(stations=1, mec=True)
    vals = _run_multi(dm.MODE_DYNAMIC, prm, reps=3)
    e2e = _mean(vals, "e2e_ms")
    assert 100.0 < e2e < 140.0, e2e
    print(f"[OK] 端到端时延口径校准：纯中心 {e2e:.1f}ms（docs/11 §4.4.1 实测 122.71ms）")


def test_station_count_is_clamped() -> None:
    """站数 / 每站槽位有上限（保护每线程本地内存），超限被夹紧而不是崩溃。"""
    assert dm.multi_params(stations=99)[dm.P["n_stations"]] == float(dm.MAX_STATIONS)
    prm = dm.multi_params(stations=3, station_workers=99)
    assert prm[dm.P["stations_workers"]] == float(dm.MAX_WORKERS_PER_STATION)
    v = dm.simulate_one_multi(0, dm.MODE_DYNAMIC, prm)
    assert len(v) == len(dm.METRIC_KEYS)
    print(f"[OK] 站数上限 {dm.MAX_STATIONS}、每站槽位上限 "
          f"{dm.MAX_WORKERS_PER_STATION} 生效")


if __name__ == "__main__":  # pragma: no cover
    test_frozen_single_station_baseline()
    test_default_params_keep_single_station()
    test_single_station_multi_equals_simulate_one()
    test_multi_metrics_in_range()
    test_more_stations_lower_imbalance_with_cross_node()
    test_cross_node_differs_from_fcfs()
    test_mec_offloads_light_tasks_and_shortens_e2e()
    test_e2e_calibration_matches_reference_docs()
    test_station_count_is_clamped()
    print("\n所有多站/MEC 分析模型测试通过 [PASS]")
