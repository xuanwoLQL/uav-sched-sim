"""
tests/test_replication_variants.py —— 重复实验「变体（variant）」管线单测
=============================================================================

覆盖 `run.py replicate --variants` 这条新增路径，全部用 **monkeypatch 替身**
（不跑真仿真，秒级完成）：

1. 默认只跑单站时，CSV 里仍有一列 `variant`（值为 `single`）——向后兼容；
2. 三变体时聚合单元 key = `(variant, scenario, scheduler)`，CSV 行数 =
   变体数 × 工况数 × 策略数 × 指标数；
3. **配对比较不跨变体混算**：替身让每个变体给出 10/20/30 三档基线值，
   若实现把 `single` 与 `triple_mec` 的单元混起来相减，差值会变成 15/25 而不是 5；
4. `--variants` 参数解析同时接受空格与逗号分隔。

为什么要专门验证第 3 条：跨变体相减 = 把"拓扑差异"当成"策略差异"，
是本项目最需要避免的伪结论（docs/11 §7.2 第 2 条）。
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
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

from uav_sim.experiment import ExperimentRunner  # noqa: E402
from uav_sim.models import METRIC_SPEC, RunResult  # noqa: E402
from uav_sim.replication import ReplicationStudy  # noqa: E402

#: 三档变体 = (tag, station_spec, mec_enabled)
VARIANTS = [("single", None, None), ("triple", "triple", False),
            ("triple_mec", "triple", True)]

#: 每个变体的基线值（dynamic 用基线，cross_node = 基线 + 5）
VARIANT_BASE = {"single": 10.0, "triple": 20.0, "triple_mec": 30.0}


def _fake_run_one(self, scenario_code, scheduler_name, seed=None, persist=True,
                  weather_preset=None, save_detail=True) -> RunResult:
    """替身：不跑仿真，按「执行器上的拓扑/MEC」+「策略名」给确定性数值。

    注意这里读的是 ``self.station_spec`` / ``self.mec_enabled`` —— 也就是
    每个变体各自的 ExperimentRunner，因此替身能验证"变体确实各跑各的执行器"。
    """
    tag = "single" if self.station_spec is None else (
        "triple" if not self.mec_enabled else "triple_mec")
    base = VARIANT_BASE[tag]
    val = base if str(scheduler_name).strip().lower() == "dynamic" else base + 5.0
    return RunResult(
        exp_id=f"fake-{tag}-{scenario_code}-{scheduler_name}-{seed}",
        scenario=str(scenario_code), scheduler=str(scheduler_name),
        seed=int(seed or 0), duration_s=1.0, uav_count=1,
        started_at="", finished_at="",
        metrics={k: val for k in METRIC_SPEC},
    )


def _study(tmp_path: Path, monkeypatch, variants) -> ReplicationStudy:
    monkeypatch.setattr(ExperimentRunner, "run_one", _fake_run_one)
    cfg = {"simulation": {"random_seed": 1000, "repetitions": 1},
           "output": {"results_dir": str(tmp_path)}}
    return ReplicationStudy(cfg, reps=3, results_dir=tmp_path, variants=variants)


def _read_csv(path: Path) -> list:
    with path.open(encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def test_default_variant_is_single_and_csv_has_variant_column(tmp_path: Path,
                                                              monkeypatch) -> None:
    """默认（不传 variants）= 单站，输出仍是一份合理的表，且多出 variant 列。"""
    study = _study(tmp_path, monkeypatch, None)
    assert study.variant_tags() == ["single"]

    study.run_matrix(scenarios=["PEAK"], schedulers=["dynamic"], progress=False)
    assert set(study.cells) == {("single", "PEAK", "dynamic")}

    rows = _read_csv(study.export_csv())
    assert rows, "聚合 CSV 不应为空"
    assert {r["variant"] for r in rows} == {"single"}
    assert len(rows) == 1 * 1 * 1 * len(METRIC_SPEC)
    # 单站基线 = 10.0（说明确实只跑了 single 变体）
    assert all(abs(float(r["mean"]) - 10.0) < 1e-9 for r in rows)
    print(f"[OK] 默认 variant=single：CSV {len(rows)} 行 = 1变体×1工况×1策略×"
          f"{len(METRIC_SPEC)}指标，含 variant 列")


def test_three_variants_cells_and_row_count(tmp_path: Path, monkeypatch) -> None:
    """三变体：cell key 正确、行数 = 变体×工况×策略×指标，且各变体数值不串档。"""
    study = _study(tmp_path, monkeypatch, VARIANTS)
    assert study.variant_tags() == ["single", "triple", "triple_mec"]

    scheds = ["dynamic", "cross_node"]
    study.run_matrix(scenarios=["PEAK"], schedulers=scheds, progress=False)
    expect = {(tag, "PEAK", s) for tag, _, _ in VARIANTS for s in scheds}
    assert set(study.cells) == expect, set(study.cells) ^ expect

    rows = _read_csv(study.export_csv())
    assert len(rows) == len(VARIANTS) * 1 * len(scheds) * len(METRIC_SPEC)
    assert Counter(r["variant"] for r in rows) == Counter(
        {tag: len(scheds) * len(METRIC_SPEC) for tag, _, _ in VARIANTS})
    for r in rows:
        want = VARIANT_BASE[r["variant"]] + (0.0 if r["scheduler"] == "dynamic" else 5.0)
        assert abs(float(r["mean"]) - want) < 1e-9, (r["variant"], r["scheduler"], r["mean"])
    print(f"[OK] 三变体：cells={len(study.cells)}，CSV {len(rows)} 行 = "
          f"{len(VARIANTS)}×1×{len(scheds)}×{len(METRIC_SPEC)}")


def test_paired_report_never_mixes_variants(tmp_path: Path, monkeypatch) -> None:
    """配对比较只在同一变体内进行（否则会把拓扑差异当成策略差异）。"""
    study = _study(tmp_path, monkeypatch, VARIANTS)
    study.run_matrix(scenarios=["PEAK"], schedulers=["dynamic", "cross_node"],
                     progress=False)

    rows = study.paired_report(baseline="dynamic", target="cross_node")
    assert {r["variant"] for r in rows} == {tag for tag, _, _ in VARIANTS}
    assert all(r["scenario"] == "PEAK" for r in rows)
    assert len(rows) == len(VARIANTS) * len(METRIC_SPEC)
    # 每个变体内的配对差值恒为 +5；跨变体混算会得到 15/25 之类的错值
    assert all(abs(r["diff"] - 5.0) < 1e-9 for r in rows), sorted({r["diff"] for r in rows})

    out = study.export_paired_csv(baseline="dynamic", target="cross_node")
    assert out.name == "paired_cross_node_vs_dynamic.csv"
    with out.open(encoding="utf-8-sig", newline="") as fp:
        head = next(csv.reader(fp))
    assert head[0] == "variant"
    print(f"[OK] 配对比较按变体分组：{len(rows)} 行，每变体差值恒为 +5.0（未跨变体混算）")


def test_parse_variant_specs_accepts_space_and_comma() -> None:
    """`--variants` 同时支持空格分隔与逗号分隔，且不认识的名字要报错。"""
    import run as run_cli  # noqa: E402  (run.py 在项目根目录，不是包)

    assert run_cli.parse_variant_specs(None) == [("single", None, None)]
    assert run_cli.parse_variant_specs([]) == [("single", None, None)]
    assert run_cli.parse_variant_specs(["single", "triple", "triple_mec"]) == VARIANTS
    assert run_cli.parse_variant_specs(["single,triple_mec"]) == [
        ("single", None, None), ("triple_mec", "triple", True)]
    try:
        run_cli.parse_variant_specs(["nope"])
    except ValueError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover - 只有实现出错才会走到
        raise AssertionError("未知变体名应抛出 ValueError")
    print("[OK] --variants 解析：空格/逗号两种写法均可，未知变体名报错")


if __name__ == "__main__":  # pragma: no cover - 支持直接运行
    import tempfile

    class _MP:  # 极简 monkeypatch（直接运行本文件时用）
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        test_default_variant_is_single_and_csv_has_variant_column(p / "a", _MP())
        test_three_variants_cells_and_row_count(p / "b", _MP())
        test_paired_report_never_mixes_variants(p / "c", _MP())
    test_parse_variant_specs_accepts_space_and_comma()
    print("\n所有变体管线测试通过 [PASS]")
