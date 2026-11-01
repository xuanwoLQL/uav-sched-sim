"""
tests/test_station_matrix.py —— 多站矩阵（`run.py stations`）结构守护
=============================================================================

这一组用例**不跑真仿真**（18 组要 3 分钟，测试里跑不起也不该跑），只守两条红线：

1. **矩阵口径**：`build_station_plan()` 必须是 triple × 6 策略 × 3 工况 = 18 组，
   且 MEC 关闭（隔离「拓扑 × 策略」效应）；CSV 表头必须含必要列；
2. **不动 15 组结论**：多站实验一律落在 `results/stations/` 下，
   绝不写到 `results/summary.csv`；并且 `ExperimentRunner.run_matrix` 的
   **默认策略集合仍是 5 个经典策略** —— 防止有人把 `cross_node` 塞进 15 组矩阵。

为什么要专门守第 2 条：15 组数字是既有报告（docs/01、docs/04、README）引用的口径，
一旦默认集合被改成 6 个，`results/summary.csv` 会变成 18 行，全书数字全部作废。
"""

from __future__ import annotations

import csv
import inspect
import sys
from pathlib import Path

try:  # Windows 控制台默认 cp936，遇到 GBK 不支持的符号会让测试假失败
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for _p in (_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import run as run_cli  # noqa: E402  (run.py 在项目根目录，不是包)

#: 只用于路径推导的最小配置（resolve_path 相对项目根解析）。
CFG = {"output": {"results_dir": "results", "charts_dir": "charts"}}


def _fake_rows() -> list:
    """按 build_station_plan() 造 18 行假数据（不跑仿真，只测 CSV 结构）。"""
    rows = []
    for i, item in enumerate(run_cli.build_station_plan()):
        row = {
            "mode": "multi",
            "scenario": item["scenario"],
            "scenario_label": item["scenario"],
            "scheduler": item["scheduler"],
            "scheduler_label": item["scheduler"],
            "topology": item["station_spec"],
            "mec": item["mec"],
        }
        for key, _ in run_cli.STATION_METRIC_COLUMNS:
            row[key] = float(i)
        rows.append(row)
    return rows


def test_station_plan_is_18_groups() -> None:
    """矩阵必须是 18 组 = triple × 6 策略 × 3 工况，且 MEC 关闭。"""
    plan = run_cli.build_station_plan()
    assert len(plan) == 18, f"多站矩阵应为 18 组，实际 {len(plan)}"
    assert len(plan) == 3 * 6

    scenarios = {p["scenario"] for p in plan}
    schedulers = {p["scheduler"] for p in plan}
    assert scenarios == set(run_cli.STATION_MATRIX_SCENARIOS)
    assert schedulers == set(run_cli.STATION_MATRIX_SCHEDULERS)
    assert len(schedulers) == 6 and "cross_node" in schedulers
    # 每个「工况 × 策略」恰好出现一次（防止漏组/重复）
    assert len({(p["scenario"], p["scheduler"]) for p in plan}) == 18
    # 拓扑固定 triple、MEC 一律关闭（否则拓扑效应与 MEC 角色约束会纠缠）
    assert {p["station_spec"] for p in plan} == {"triple"}
    assert all(p["mec"] is False for p in plan)
    print(f"[OK] 多站矩阵计划 = {len(plan)} 组（triple × {len(schedulers)} 策略 × "
          f"{len(scenarios)} 工况，MEC 关闭）")


def test_station_plan_respects_filters() -> None:
    """`--scenarios / --schedulers` 过滤后组数应随之收缩。"""
    plan = run_cli.build_station_plan(["PEAK"], ["dynamic", "cross_node"])
    assert len(plan) == 2
    assert {p["scenario"] for p in plan} == {"PEAK"}
    assert {p["scheduler"] for p in plan} == {"dynamic", "cross_node"}
    print("[OK] 过滤参数生效：PEAK × {dynamic, cross_node} = 2 组")


def test_station_csv_header_and_rows(tmp_path: Path) -> None:
    """CSV 结构：中文表头 + 英文键名 + 18 行数据，且关键列齐全。"""
    out = run_cli.write_station_csv(_fake_rows(), tmp_path / "stations_matrix.csv")
    lines = [ln for ln in out.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    assert len(lines) == 2 + 18, f"应为 表头 + 键名 + 18 行，实际 {len(lines)} 行"

    header = next(csv.reader([lines[0]]))
    keys = next(csv.reader([lines[1]]))
    assert len(keys) == len(header)
    for cn in ("工况", "策略", "拓扑", "MEC开关"):
        assert cn in header, f"中文表头缺列：{cn}"
    for key in ("scenario", "scheduler", "topology", "mec",
                "completion_rate", "avg_wait_all_ms",
                "station_load_imbalance", "migration_count"):
        assert key in keys, f"英文键名列缺列：{key}"
    # 数据行数 = 18，且每行列数一致
    widths = {len(next(csv.reader([ln]))) for ln in lines}
    assert widths == {len(keys)}
    assert out.read_text(encoding="utf-8-sig").startswith("工况"), "首列应为中文「工况」"
    print(f"[OK] CSV 表头 {len(keys)} 列、数据 18 行，关键列齐全 -> {out.name}")


def test_station_outputs_never_touch_summary_csv() -> None:
    """多站矩阵的产物路径必须与 15 组的 results/summary.csv 完全分离。"""
    paths = run_cli.station_output_paths(CFG)
    assert paths["csv"].name == "stations_matrix.csv"
    assert "stations" in paths["csv"].parts, paths["csv"]
    assert paths["csv"] != paths["results_dir"] / "summary.csv"
    assert paths["csv"].parent == paths["out_dir"]
    assert paths["raw_dir"].name == "raw"
    assert paths["overview"].name == "10-多站×多策略矩阵.png"

    probe = run_cli.station_probe_output_paths(CFG)
    assert probe["csv"].name == "station_probe.csv"
    assert "station_probe" in probe["csv"].parts
    assert probe["csv"] != paths["results_dir"] / "summary.csv"
    assert probe["png"].name == "09-站级与MEC指标.png"
    print(f"[OK] 产物隔离：{paths['csv'].name} / {probe['csv'].name} "
          f"都不在 summary.csv 路径上")


def test_run_matrix_default_is_still_five_classic_schedulers() -> None:
    """口径红线：15 组矩阵的默认策略集合仍是 5 个经典策略（不含 cross_node）。"""
    from uav_sim.experiment import ExperimentRunner
    from uav_sim.models import CLASSIC_SCHEDULER_NAMES, SCENARIO_CODES

    src = inspect.getsource(ExperimentRunner.run_matrix)
    assert "CLASSIC_SCHEDULER_NAMES" in src, "run_matrix 默认策略集不再是 CLASSIC_SCHEDULER_NAMES"
    assert len(CLASSIC_SCHEDULER_NAMES) == 5
    assert "cross_node" not in CLASSIC_SCHEDULER_NAMES
    assert len(SCENARIO_CODES) * len(CLASSIC_SCHEDULER_NAMES) == 15
    print(f"[OK] 15 组口径守护：默认策略 = {', '.join(CLASSIC_SCHEDULER_NAMES)}")


def test_existing_summary_csv_still_has_15_rows() -> None:
    """若工作区已有 results/summary.csv，它必须仍是 表头 + 15 行。"""
    path = _ROOT / "results" / "summary.csv"
    if not path.is_file():
        print("[SKIP] 尚无 results/summary.csv（先跑 run.py all 再检查）")
        return
    lines = [ln for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    assert len(lines) == 16, f"results/summary.csv 应为 表头 + 15 行，实际 {len(lines)} 行"
    print("[OK] results/summary.csv 仍是 表头 + 15 行（15 组口径未被污染）")


if __name__ == "__main__":  # pragma: no cover - 支持直接运行
    import tempfile

    test_station_plan_is_18_groups()
    test_station_plan_respects_filters()
    with tempfile.TemporaryDirectory() as td:
        test_station_csv_header_and_rows(Path(td))
    test_station_outputs_never_touch_summary_csv()
    test_run_matrix_default_is_still_five_classic_schedulers()
    test_existing_summary_csv_still_has_15_rows()
    print("\n所有多站矩阵守护测试通过 [PASS]")
