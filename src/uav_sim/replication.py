"""
replication.py —— 重复实验（replication）执行与聚合
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 一次实测不足以支撑"上不上这套调度"，需要重复作业、给出置信区间 |
| OS 原理层   | 单次仿真 = 一次运行；重复仿真 = 同进程调度反复跑，用统计把噪声和真差异分开 |
| 算法设计层 | **共同随机数（CRN）+ 配对比较**：同一次重复里所有策略共享同一条到达/弱网随机流 |
| 代码实现层 | `ReplicationStudy.run_cell()` / `run_matrix()` / `export_csv()` / `paired_report()` |

--------------------------------------------------------------------------
为什么要"同种子跨策略"（CRN）而不是"每策略各自随机"
--------------------------------------------------------------------------
朴素做法：策略 A 用种子 1..30，策略 B 也用种子 1..30 —— 这已经是 CRN（就是本项目做法）。
反面做法：两个策略各用 100..130 / 200..230 —— 那样两组的噪声不同源，
配对相减消不掉噪声，置信区间会明显变宽，还可能出现"结论翻转"。

**本模块生成唯一的种子清单 ``seed_base .. seed_base+n-1``，所有策略共用**，
于是第 k 次重复构成一组配对观测，可直接做配对比较（见 :func:`uav_sim.statistics.paired_ci95`）。

--------------------------------------------------------------------------
为什么重复实验不该落明细
--------------------------------------------------------------------------
15 个单元 × 30 次 = 450 次运行，若每次写 raw JSON + 任务级 CSV 会产出数百 MB 垃圾。
本模块对每次运行传 ``persist=False, save_detail=False``，**只保留聚合结果**；
明细仍可由 ``run.py one`` / ``run.py all`` 单独生成（那是"抽样取证"，不是统计推断）。

----------------------------------------------------------------------------
变体（variant）：把"多站 / MEC"也纳入重复实验（对应 docs/11 §8 诚实清单第 7 条）
----------------------------------------------------------------------------
长期缺口是"重复实验只覆盖单站 5 策略"，于是**多站/MEC 面的差异无法与运行噪声分开**。
本模块用 ``variants`` 解决：每个变体 = ``(tag, station_spec, mec_enabled)``，各自持有
**独立的 ExperimentRunner**（拓扑与 MEC 开关不同），聚合单元的 key 从
``(scenario, scheduler)`` 升级为 ``(variant, scenario, scheduler)``。

关键纪律：**配对比较只在同一变体内进行** —— 跨变体相减等于把"拓扑差异"当成
"策略差异"，那是最典型的伪结论来源。默认 ``variants=[("single", None, None)]``
时输出与改造前逐列兼容（只是多了一列恒为 ``single`` 的 variant）。
"""

from __future__ import annotations

import csv
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.config import cfg_get_int, load_config, resolve_path
from uav_sim.experiment import ExperimentRunner
from uav_sim.models import (  # noqa: E402
    CLASSIC_SCHEDULER_NAMES,
    METRIC_SPEC,
    SCENARIO_CODES,
    SCENARIO_LABELS,
    SCHEDULER_LABELS,
    SCHEDULER_NAMES,
)
from uav_sim.statistics import MetricAggregate, aggregate, paired_ci95

#: 实验变体 = ``(标签, station_spec, mec_enabled)``。
#: ``station_spec=None`` = 单站默认；``mec_enabled=None`` = 取 config.mec.enable。
VariantSpec = Tuple[str, Optional[object], Optional[bool]]

#: 默认只跑单站（行为与"重复实验上线时"完全一致，向后兼容）。
DEFAULT_VARIANTS: Tuple[VariantSpec, ...] = (("single", None, None),)


@dataclass
class CellSummary:
    """一个「变体 × 工况 × 策略」单元在 N 次重复上的聚合结果。"""

    scenario: str
    scheduler: str
    n: int
    seed_base: int
    seeds: List[int]
    wall_s: float
    variant: str = "single"
    metrics: Dict[str, MetricAggregate] = field(default_factory=dict)

    def mean_of(self, metric: str) -> float:
        agg = self.metrics.get(metric)
        return agg.mean if agg is not None else 0.0

    def ci_of(self, metric: str) -> float:
        agg = self.metrics.get(metric)
        return agg.ci_half if agg is not None else 0.0

    def values_of(self, metric: str) -> List[float]:
        agg = self.metrics.get(metric)
        return list(agg.values) if agg is not None else []


class ReplicationStudy:
    """重复实验编排器（对应 docs 里长期"预留未启用"的 ``simulation.repetitions``）。"""

    def __init__(self, cfg: Optional[dict] = None, reps: Optional[int] = None,
                 seed_base: Optional[int] = None,
                 results_dir: Optional[str | Path] = None,
                 variants: Optional[Sequence[VariantSpec]] = None) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        cfg_reps = cfg_get_int(self.cfg, "simulation.repetitions", 1)
        self.reps = max(1, int(reps if reps is not None else cfg_reps))
        self.seed_base = int(
            seed_base if seed_base is not None
            else cfg_get_int(self.cfg, "simulation.random_seed", 20261031)
        )
        self.results_dir = Path(results_dir) if results_dir is not None \
            else resolve_path(self.cfg, "output.results_dir")
        self.out_dir = self.results_dir / "replications"
        self.out_dir.mkdir(parents=True, exist_ok=True)

        #: 实验变体列表（默认只有单站）。每个变体一个独立的 ExperimentRunner。
        self.variants: List[VariantSpec] = [
            (str(tag), spec, mec) for tag, spec, mec in
            (list(variants) if variants else list(DEFAULT_VARIANTS))
        ]
        if not self.variants:
            self.variants = list(DEFAULT_VARIANTS)

        #: 聚合单元：``(variant, scenario, scheduler) -> CellSummary``
        self.cells: Dict[Tuple[str, str, str], CellSummary] = {}
        self._runners: Dict[str, ExperimentRunner] = {}

    # ------------------------------------------------------------------ 变体
    def variant_tags(self) -> List[str]:
        """已注册的变体标签（按传入顺序）。"""
        return [tag for tag, _, _ in self.variants]

    def _runner_for(self, variant: str) -> ExperimentRunner:
        """取（必要时惰性创建）某个变体的执行器。

        每个变体**各持一个 ExperimentRunner**：`station_spec` / `mec_enabled` 是
        执行器上的构造参数，共用一个实例就没法在同一份研究里比较不同拓扑。
        """
        tag = str(variant).strip().lower()
        runner = self._runners.get(tag)
        if runner is not None:
            return runner
        spec = next(((s, m) for t, s, m in self.variants
                     if t == tag), None)
        if spec is None:
            raise KeyError(f"未注册的 variant：{variant}（已注册：{self.variant_tags()}）")
        runner = ExperimentRunner(self.cfg, results_dir=self.results_dir,
                                  station_spec=spec[0], mec_enabled=spec[1])
        self._runners[tag] = runner
        return runner

    @property
    def _runner(self) -> ExperimentRunner:
        """默认（第一个）变体的执行器 —— 兼容改造前的单变体调用点。"""
        return self._runner_for(self.variants[0][0])

    # ------------------------------------------------------------------ 种子
    def seeds(self) -> List[int]:
        """本研究的共同随机数种子清单（所有策略/所有变体共用 = CRN）。"""
        return [self.seed_base + i for i in range(self.reps)]

    # ------------------------------------------------------------------ 单单元
    def run_cell(self, scenario: str, scheduler: str, variant: str = "single",
                 progress: bool = True) -> CellSummary:
        """对一个「变体 × 工况 × 策略」跑 ``reps`` 次并聚合。"""
        code = str(scenario).strip().upper()
        sched = str(scheduler).strip().lower()
        tag = str(variant).strip().lower()
        runner = self._runner_for(tag)
        seed_list = self.seeds()

        per_metric_values: Dict[str, List[float]] = {k: [] for k in METRIC_SPEC}
        wall = 0.0
        t0 = time.perf_counter()
        for k, sd in enumerate(seed_list, 1):
            if progress:
                print(f"    rep {k:>2}/{self.reps} (seed={sd}) ... ", end="", flush=True)
            res = runner.run_one(code, sched, seed=sd,
                                 persist=False, save_detail=False)
            if res.error:
                if progress:
                    print(f"✗ {res.error}")
                continue
            for name in METRIC_SPEC:
                per_metric_values[name].append(float(res.metrics.get(name, 0.0)))
            if progress:
                m = res.metrics
                print(f"吞吐={m['throughput']:.2f}/s "
                      f"低优饥饿={m['starvation_rate_low']:.1f}%")
        wall = time.perf_counter() - t0

        summary = CellSummary(
            scenario=code, scheduler=sched, n=len(per_metric_values["throughput"]),
            seed_base=self.seed_base, seeds=seed_list, wall_s=wall, variant=tag,
        )
        for name, vals in per_metric_values.items():
            summary.metrics[name] = aggregate(name, vals)
        self.cells[(tag, code, sched)] = summary
        return summary

    # ------------------------------------------------------------------ 全矩阵
    def run_matrix(self, scenarios: Optional[Iterable[str]] = None,
                   schedulers: Optional[Iterable[str]] = None,
                   variants: Optional[Iterable[str]] = None,
                   progress: bool = True) -> Dict[Tuple[str, str, str], CellSummary]:
        """跑完整矩阵，返回 ``{(variant, scenario, scheduler): CellSummary}``。

        默认：已注册的全部变体 × 3 工况 × 5 个经典单节点策略。
        """
        codes = [str(c).upper() for c in (scenarios or SCENARIO_CODES)]
        # 默认 5 个经典单节点策略（= 3×5 矩阵）；多基站策略 cross_node 需显式指定，
        # 理由同 experiment.run_matrix：不让拓扑差异污染策略对照。
        scheds = [str(s).lower() for s in (schedulers or CLASSIC_SCHEDULER_NAMES)]
        tags = [str(v).strip().lower() for v in (variants or self.variant_tags())]
        total = len(tags) * len(codes) * len(scheds)

        if progress:
            print("=" * 74)
            print("  重复实验（replication study）")
            print(f"  变体 = {len(tags)} 个（{', '.join(tags)}）")
            print(f"  单元 = {len(tags)} 变体 × {len(codes)} 工况 × {len(scheds)} 策略 = {total}；"
                  f"每单元重复 {self.reps} 次；种子 = {self.seed_base}..{self.seed_base + self.reps - 1}（CRN）")
            print(f"  合计仿真次数 = {total * self.reps}")
            print("=" * 74)

        k = 0
        t0 = time.perf_counter()
        for tag in tags:
            for code in codes:
                for sched in scheds:
                    k += 1
                    if progress:
                        print(f"\n[{k}/{total}] {tag} | {code} × {sched}")
                    self.run_cell(code, sched, variant=tag, progress=progress)

        if progress:
            print(f"\n全部完成，实耗 {time.perf_counter() - t0:.1f}s")
        self.export_csv()
        return self.cells

    # ------------------------------------------------------------------ 导出
    def export_csv(self, path: Optional[str | Path] = None) -> Path:
        """长表导出：一行 = (变体, 工况, 策略, 指标) 的聚合结果，方便 pandas 直接透视。"""
        out = Path(path) if path is not None else self.out_dir / "replication_summary.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as fp:
            w = csv.writer(fp)
            w.writerow([
                "variant", "scenario", "scenario_label", "scheduler", "scheduler_label",
                "metric", "metric_label", "unit", "n",
                "mean", "std", "ci95_half", "ci95_low", "ci95_high", "rel_ci_pct",
                "seed_base", "wall_s",
            ])
            for (tag, code, sched), cell in self.cells.items():
                for name, agg in cell.metrics.items():
                    label, unit, _ = METRIC_SPEC.get(name, (name, "", True))
                    w.writerow([
                        tag, code, SCENARIO_LABELS.get(code, code),
                        sched, SCHEDULER_LABELS.get(sched, sched),
                        name, label, unit, agg.n,
                        f"{agg.mean:.6f}", f"{agg.std:.6f}", f"{agg.ci_half:.6f}",
                        f"{agg.ci_low:.6f}", f"{agg.ci_high:.6f}", f"{agg.rel_ci_pct:.3f}",
                        cell.seed_base, f"{cell.wall_s:.3f}",
                    ])
        return out

    # ------------------------------------------------------------------ 配对比较
    def paired_report(self, baseline: str = "priority",
                      target: str = "dynamic") -> List[dict]:
        """对照基线策略，给出目标策略在各指标上的**配对**差异与显著性。

        返回行：``{variant, scenario, metric, baseline_mean, target_mean, diff,
        diff_ci_half, significant, better}``，``better`` 依据 ``METRIC_SPEC`` 的方向判断。

        **按变体分组**：只在同一变体内配对（跨变体相减会把拓扑差异当成策略差异）。
        """
        rows: List[dict] = []
        for tag in self.variant_tags():
            scenarios = sorted({code for (v, code, _) in self.cells if v == tag})
            for code in scenarios:
                base = self.cells.get((tag, code, baseline))
                targ = self.cells.get((tag, code, target))
                if base is None or targ is None:
                    continue
                for name, (label, unit, higher_better) in METRIC_SPEC.items():
                    a, b = targ.values_of(name), base.values_of(name)
                    if len(a) < 2 or len(a) != len(b):
                        continue
                    diff, half, sig = paired_ci95(a, b)
                    better = (diff > 0) if higher_better else (diff < 0)
                    rows.append({
                        "variant": tag,
                        "scenario": code,
                        "metric": name,
                        "metric_label": label,
                        "unit": unit,
                        "baseline": baseline,
                        "target": target,
                        "baseline_mean": sum(b) / len(b),
                        "target_mean": sum(a) / len(a),
                        "diff": diff,
                        "diff_ci_half": half,
                        "significant": sig,
                        "better": better,
                    })
        return rows

    def export_paired_csv(self, baseline: str = "priority", target: str = "dynamic",
                          path: Optional[str | Path] = None) -> Path:
        """把配对比较结果写成 CSV（报告"统计显著性"章节直接用）。"""
        out = Path(path) if path is not None else \
            self.out_dir / f"paired_{target}_vs_{baseline}.csv"
        rows = self.paired_report(baseline, target)
        with out.open("w", newline="", encoding="utf-8-sig") as fp:
            w = csv.DictWriter(fp, fieldnames=[
                "variant", "scenario", "metric", "metric_label", "unit",
                "baseline", "target", "baseline_mean", "target_mean",
                "diff", "diff_ci_half", "significant", "better",
            ])
            w.writeheader()
            for r in rows:
                w.writerow({
                    **r,
                    "baseline_mean": f"{r['baseline_mean']:.6f}",
                    "target_mean": f"{r['target_mean']:.6f}",
                    "diff": f"{r['diff']:.6f}",
                    "diff_ci_half": f"{r['diff_ci_half']:.6f}",
                })
        return out

    # ------------------------------------------------------------------ 打印
    def print_matrix(self, metrics: Optional[Sequence[str]] = None) -> None:
        """控制台打印「策略 × 指标（均值 ± 95% 半宽）」，按变体分段。"""
        metrics = list(metrics or [
            "throughput", "avg_wait_high_ms", "starvation_rate_low", "completion_rate",
        ])
        labels = {tag: (spec, mec) for tag, spec, mec in self.variants}
        shown = False
        for tag in self.variant_tags():
            codes = [c for c in SCENARIO_CODES if any(v == tag and s == c for v, s, _ in self.cells)]
            if not codes:
                continue
            shown = True
            spec, mec = labels.get(tag, (None, None))
            topo = "单站(默认)" if spec is None else str(spec)
            mec_cn = "跟随配置" if mec is None else ("开" if mec else "关")
            print(f"\n{'=' * 74}")
            print(f"  变体 {tag}：拓扑 = {topo}，MEC = {mec_cn}")
            print(f"{'=' * 74}")
            for code in codes:
                print(f"\n【{code} · {SCENARIO_LABELS.get(code, code)}】  "
                      f"n={self.reps}（均值 ± 95% 置信半宽）")
                header = f"{'策略':<26}" + "".join(
                    f"{METRIC_SPEC[m][0][:10]:>22}" for m in metrics)
                print(header)
                print("-" * len(header))
                for sched in SCHEDULER_NAMES:
                    cell = self.cells.get((tag, code, sched))
                    if cell is None:
                        continue
                    line = f"{SCHEDULER_LABELS.get(sched, sched):<26}"
                    for m in metrics:
                        agg = cell.metrics.get(m)
                        line += f"{agg.format(2) if agg else '-':>22}"
                    print(line)
        if not shown:
            print("（没有可打印的聚合结果）")

    def print_paired(self, baseline: str = "priority", target: str = "dynamic") -> None:
        """打印配对比较摘要（只列显著项），按变体分段。"""
        rows = self.paired_report(baseline, target)
        if not rows:
            return
        print(f"\n【配对比较】{SCHEDULER_LABELS.get(target, target)} "
              f"− {SCHEDULER_LABELS.get(baseline, baseline)}（同种子 CRN，差值为正表示目标更大）")
        print("  纪律：只在**同一变体**内配对 —— 跨变体相减等于把拓扑差异当成策略差异。")
        print(f"{'变体':<12}{'工况':<10}{'指标':<12}{'基线':>14}{'目标':>14}{'差值±95%':>22}{'显著':>6}{'更优':>6}")
        print("-" * 96)
        for r in rows:
            print(f"{r['variant']:<12}{r['scenario']:<10}{r['metric_label'][:10]:<12}"
                  f"{r['baseline_mean']:>14.3f}{r['target_mean']:>14.3f}"
                  f"{r['diff']:>16.3f} ± {r['diff_ci_half']:<5.3f}"
                  f"{'是' if r['significant'] else '否':>6}"
                  f"{'✔' if r['better'] else '':>6}")
