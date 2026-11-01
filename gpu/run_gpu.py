"""
gpu/run_gpu.py —— GPU 大规模重复实验 / 参数扫描的驱动脚本
=============================================================================

职责：
  1. 确保分析模型已就绪（优先 CUDA 内核；无 CUDA 时回退 numba.cuda / 纯 Python）；
  2. 跑 N 次重复，dump **逐次**结果 CSV；
  3. 复用本项目的 ``uav_sim.statistics`` 做聚合：均值 ± 95% 置信区间 + 配对比较；
  4. ``--sweep``：对动态权重的 alpha/beta/gamma/delta 做网格扫描 + 边际敏感性分析。

用法（在项目根目录）：
    python gpu\\run_gpu.py --reps 3000                    # 5 个策略对照
    python gpu\\run_gpu.py --sweep --reps 300             # 参数扫描
    python gpu\\run_gpu.py --backend python --reps 200    # 强制纯 Python 回退
    python gpu\\run_gpu.py --compare                      # 与参考模型（results/summary.csv）对照
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from uav_sim import statistics as st  # noqa: E402

EXE = _HERE / "replication_kernel.exe"
SRC = _HERE / "replication_kernel.cu"
BUILD = _HERE / "build.ps1"

SCHED_LABELS = {
    "fcfs": "FCFS 先来先服务",
    "rr": "固定时间片轮转 RR",
    "priority": "静态优先级",
    "dynamic": "动态权重自适应（本方案）",
    "mlfq": "多级反馈队列 MLFQ",
}

#: 逐次 CSV 里的指标列 -> (中文名, 单位, 越大越好)
GPU_METRICS: Dict[str, tuple] = {
    "throughput": ("吞吐量", "个/秒", True),
    "wait_high_ms": ("高优平均等待时延", "ms", False),
    "wait_low_ms": ("低优平均等待时延", "ms", False),
    "starvation_low_pct": ("低优饥饿率", "%", False),
    "completion_pct": ("业务完整率", "%", True),
    "util_pct": ("资源利用率", "%", True),
    "retry_pct": ("弱网重传率", "%", False),
    "preempts": ("上下文切换次数", "次", False),
    "promotions": ("aging 升权次数", "次", False),
    "cached": ("本地缓存次数", "次", False),
}

MAIN_METRICS = ("throughput", "wait_low_ms", "starvation_low_pct", "completion_pct")


# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------
def ensure_cuda_built(force: bool = False) -> bool:
    """确保 CUDA exe 可用；失败返回 False（由调用方回退）。"""
    if not BUILD.exists():
        return False
    if force or not EXE.exists() or EXE.stat().st_mtime < SRC.stat().st_mtime:
        print("[build] 构建 CUDA 内核 ...")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BUILD)]
        if force:
            cmd.append("-Rebuild")
        try:
            proc = subprocess.run(cmd, cwd=str(_ROOT), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[build] 无法调用 PowerShell：{exc}")
            return False
        if proc.returncode != 0 or not EXE.exists():
            print("[build] CUDA 构建失败：")
            print(((proc.stdout or "") + (proc.stderr or ""))[-1500:])
            return False
    else:
        print(f"[build] 复用已构建的内核：{EXE.name}")
    return True


def pick_backend(requested: str) -> str:
    """返回实际使用的后端名：cuda / python。"""
    if requested == "python":
        return "python"
    if requested == "cuda":
        if not ensure_cuda_built():
            raise SystemExit("指定了 --backend cuda，但 CUDA 内核不可用。")
        return "cuda"
    # auto：优先 CUDA 内核，失败则回退纯 Python 分析模型
    if ensure_cuda_built():
        return "cuda"
    print("[backend] 无 CUDA 环境，回退到纯 Python 分析模型（较慢，但结果口径一致）")
    return "python"


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
def run_cuda(args, out_csv: Path, coeff_path: Path | None = None) -> str:
    cmd = [str(EXE), "--reps", str(args.reps), "--out", str(out_csv)]
    if args.sweep:
        cmd.append("--sweep")
        if coeff_path is not None:
            cmd += ["--coeffs", str(coeff_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SystemExit(f"内核运行失败（退出码 {proc.returncode}）：\n{out}")
    return out


def run_fallback(backend: str, args, out_csv: Path,
                 coeff_path: Path | None = None) -> str:
    """纯 Python 回退（调用 gpu/des_model.py 的同一套模型，多进程并行）。"""
    from des_model import run_study  # noqa: PLC0415

    modes = ["fcfs", "rr", "priority", "dynamic", "mlfq"]
    coeffs = None
    if args.sweep:
        coeffs = read_coeff_file(coeff_path) if coeff_path else default_grid()
    t0 = time.perf_counter()
    rows = run_study(reps=args.reps, modes=modes, coeffs=coeffs)
    wall = time.perf_counter() - t0
    write_rows(out_csv, rows)
    n = len(rows)
    return (f"[{backend}] {n} 次仿真完成，耗时 {wall:.1f}s"
            f"（{n / max(wall, 1e-9):.0f} 次/秒）\n")


def write_rows(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["scheduler", "coeff_index", "alpha", "beta", "gamma", "delta", "rep",
            "starve_ms", *GPU_METRICS.keys(), "max_low_depth", "submitted", "done"]
    with path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


# ---------------------------------------------------------------------------
# 参数扫描设计
# ---------------------------------------------------------------------------
def default_grid() -> List[Tuple[float, float, float, float]]:
    """默认扫描设计：**饥饿阈值(4 档) x 系数原型(6 组) = 24 组**。

    为什么不是无脑全因子？因为实测发现（见 gpu/README.md 第 5 节）：
    在 PEAK 过载下，四因子权重几乎不影响结果 —— 真正决定成败的是
    「饥饿硬补偿通道的触发阈值 `starvation_ms`」。所以默认设计把
    **真正敏感的那个参数**放进网格里，系数则用几个有代表性的原型对照。
    """
    starve_levels = [1000.0, 3000.0, 10000.0, 30000.0]
    archetypes = [
        ("默认", 0.45, 0.35, 0.15, 0.25),
        ("全零", 0.00, 0.00, 0.00, 0.00),
        ("高alpha", 0.75, 0.35, 0.15, 0.25),
        ("高beta", 0.45, 0.75, 0.15, 0.25),
        ("高gamma", 0.45, 0.35, 0.60, 0.25),
        ("高delta", 0.45, 0.35, 0.15, 0.75),
    ]
    return [(a, b, g, d, sm) for sm in starve_levels for _, a, b, g, d in archetypes]


def write_coeff_file(path: Path, coeffs: Sequence[Tuple[float, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii") as fp:
        fp.write("alpha,beta,gamma,delta,starve_ms\n")
        for c in coeffs:
            fp.write(",".join(str(x) for x in c) + "\n")


def read_coeff_file(path: Path) -> List[Tuple[float, ...]]:
    out: List[Tuple[float, ...]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        for row in csv.reader(fp):
            if len(row) >= 4:
                try:
                    out.append(tuple(float(x) for x in row[:5]))
                except ValueError:
                    continue
    return out


# ---------------------------------------------------------------------------
# 读取与聚合
# ---------------------------------------------------------------------------
def load_rows(csv_path: Path) -> Dict[str, Dict[str, List[float]]]:
    data: Dict[str, Dict[str, List[float]]] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            sched = row["scheduler"]
            bucket = data.setdefault(sched, {m: [] for m in GPU_METRICS})
            for m in GPU_METRICS:
                bucket[m].append(float(row[m]))
    return data


def load_sweep(csv_path: Path) -> List[dict]:
    """读扫描 CSV，按系数组合聚合（返回每组一行）。"""
    groups: Dict[int, dict] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            ci = int(row["coeff_index"])
            g = groups.setdefault(ci, {
                "coeff_index": ci,
                "alpha": float(row["alpha"]), "beta": float(row["beta"]),
                "gamma": float(row["gamma"]), "delta": float(row["delta"]),
                "starve_ms": float(row.get("starve_ms") or 0.0),
                "_vals": {m: [] for m in GPU_METRICS},
            })
            for m in GPU_METRICS:
                g["_vals"][m].append(float(row[m]))
    out = []
    for g in groups.values():
        for m in GPU_METRICS:
            g[m] = st.mean(g["_vals"][m])
            g[f"{m}_ci"] = st.ci95_half_width(g["_vals"][m])
        g["_vals"] = None
        out.append(g)
    out.sort(key=lambda x: x["coeff_index"])
    return out


def print_normal(data: Dict[str, Dict[str, List[float]]], reps: int) -> None:
    print("=" * 96)
    print(f"  聚合结果（每个策略 n={reps}，均值 ± 95% 置信半宽）")
    print("=" * 96)
    header = f"{'策略':<24}" + "".join(f"{GPU_METRICS[m][0][:8]:>18}" for m in MAIN_METRICS)
    print(header)
    print("-" * len(header))
    for sched in ("fcfs", "rr", "priority", "mlfq", "dynamic"):
        if sched not in data:
            continue
        line = f"{SCHED_LABELS.get(sched, sched):<24}"
        for m in MAIN_METRICS:
            agg = st.aggregate(m, data[sched][m])
            line += f"{agg.mean:>9.2f}±{agg.ci_half:<8.2f}"
        print(line)


def print_paired(data: Dict[str, Dict[str, List[float]]],
                 target: str, baseline: str) -> List[list]:
    if target not in data or baseline not in data:
        return []
    print(f"\n【配对比较】{SCHED_LABELS.get(target, target)} - "
          f"{SCHED_LABELS.get(baseline, baseline)}（同 rep 同随机种子 = CRN）")
    print(f"{'指标':<18}{'基线':>13}{'目标':>13}{'差值 ± 95%':>24}{'显著':>6}{'更优':>6}")
    print("-" * 82)
    rows = []
    for m, (label, unit, higher_better) in GPU_METRICS.items():
        a, b = data[target][m], data[baseline][m]
        diff, half, sig = st.paired_ci95(a, b)
        better = (diff > 0) if higher_better else (diff < 0)
        rows.append([m, label, unit, st.mean(b), st.mean(a), diff, half, sig, better])
        print(f"{label[:12]:<18}{st.mean(b):>13.3f}{st.mean(a):>13.3f}"
              f"{diff:>16.3f} ± {half:<6.3f}{'是' if sig else '否':>6}"
              f"{'✔' if better else '':>6}")
    return rows


def print_sweep(rows: List[dict], reps: int) -> None:
    print("=" * 96)
    print(f"  参数扫描结果（{len(rows)} 组系数 x {reps} 次重复）")
    print("=" * 96)
    best = sorted(rows, key=lambda r: (-r["completion_pct"], r["wait_low_ms"]))[:10]
    print("  按「业务完整率」排序的 Top-10（并列时取低优等待更小者）：")
    print(f"  {'#':>4} {'alpha':>6}{'beta':>7}{'gamma':>7}{'delta':>7}{'饥饿阈值ms':>12}"
          f"{'吞吐':>9}{'完整率':>9}{'低优等待':>11}{'低优饥饿':>10}")
    for r in best:
        print(f"  {r['coeff_index']:>4} {r['alpha']:>6.2f}{r['beta']:>7.2f}"
              f"{r['gamma']:>7.2f}{r['delta']:>7.2f}{r['starve_ms']:>12.0f}"
              f"{r['throughput']:>9.2f}{r['completion_pct']:>9.2f}"
              f"{r['wait_low_ms']:>11.1f}{r['starvation_low_pct']:>10.2f}")

    # ---- 边际敏感性：每个系数取不同值时，完整率的平均变化 ----
    print("\n  边际敏感性（该参数取不同值时，「业务完整率」的平均值）：")
    spans = {}
    for key in ("alpha", "beta", "gamma", "delta", "starve_ms"):
        levels = sorted({r[key] for r in rows})
        parts = []
        for lv in levels:
            vals = [r["completion_pct"] for r in rows if r[key] == lv]
            parts.append(f"{lv:.2f}->{st.mean(vals):5.2f}")
        spans[key] = max(
            st.mean([r["completion_pct"] for r in rows if r[key] == lv]) for lv in levels
        ) - min(
            st.mean([r["completion_pct"] for r in rows if r[key] == lv]) for lv in levels
        )
        print(f"    {key:<9} " + "   ".join(parts))

    # ---- 谁才是真正起作用的参数 ----
    ranked = sorted(spans.items(), key=lambda kv: -kv[1])
    print("\n  影响排序（完整率的极差，越大说明该参数越关键）：")
    for key, sp in ranked:
        print(f"    {key:<9} 极差 {sp:6.2f} 个百分点")
    main_key, main_span = ranked[0]
    if main_span < 1.0:
        print("    -> 所有参数都几乎不影响结果：本工况下没有可调空间。")
    elif main_span < 5.0:
        print(f"    -> 结果对参数不敏感（最大极差 {main_span:.2f}pp），"
              "说明该设计**不需要精调**，鲁棒性好。")
    else:
        print(f"    -> 真正起作用的是 `{main_key}`（极差 {main_span:.2f}pp），"
              "优先调它；其它系数在合理范围内任取即可。")


def compare_with_reference(gpu_csv: Path) -> None:
    """与 Python 参考模型的 PEAK 结果（results/summary.csv）逐行对照。"""
    ref_path = _ROOT / "results" / "summary.csv"
    if not ref_path.exists():
        print(f"\n[compare] 找不到 {ref_path}，先跑 `run.py all`。")
        return
    ref = {}
    with ref_path.open("r", encoding="utf-8-sig", newline="") as fp:
        for row in csv.DictReader(fp):
            if row["scenario"] == "PEAK":
                ref[row["scheduler"]] = row
    data = load_rows(gpu_csv)
    print("\n" + "=" * 88)
    print("  模型确认：分析模型(GPU) vs 参考模型(Python, PEAK 工况)")
    print("=" * 88)
    print(f"  {'策略':<12}{'参考吞吐':>10}{'GPU吞吐':>10}{'偏差':>9}"
          f"{'参考完整率':>12}{'GPU完整率':>11}{'偏差':>9}")
    for sched in ("fcfs", "rr", "priority", "mlfq", "dynamic"):
        if sched not in ref or sched not in data:
            continue
        r_th = float(ref[sched]["throughput"])
        r_cp = float(ref[sched]["completion_rate"])
        g_th = st.mean(data[sched]["throughput"])
        g_cp = st.mean(data[sched]["completion_pct"])
        d_th = 100.0 * (g_th - r_th) / r_th if r_th else 0.0
        d_cp = 100.0 * (g_cp - r_cp) / r_cp if r_cp else 0.0
        print(f"  {sched:<12}{r_th:>10.2f}{g_th:>10.2f}{d_th:>8.1f}%"
              f"{r_cp:>12.1f}{g_cp:>11.1f}{d_cp:>8.1f}%")
    print("\n  （偏差大的是 FCFS —— 分析模型里运维任务近乎免费，"
          "削弱了参考模型中大任务造成的护航效应，属已知差异。）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GPU 大规模重复实验 / 参数扫描")
    ap.add_argument("--reps", type=int, default=3000, help="每次重复次数")
    ap.add_argument("--sweep", action="store_true", help="参数扫描模式")
    ap.add_argument("--coeffs", default=None, help="系数网格 CSV（默认内置全因子网格）")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "cuda", "python"], help="计算后端")
    ap.add_argument("--rebuild", action="store_true", help="强制重新构建 CUDA 内核")
    ap.add_argument("--compare", action="store_true", help="与参考模型对照")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认 results/gpu）")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else (_ROOT / "results" / "gpu")
    out_dir.mkdir(parents=True, exist_ok=True)

    backend = pick_backend(args.backend)
    coeff_path = None
    if args.sweep:
        if args.coeffs:
            coeff_path = Path(args.coeffs)
        else:
            coeff_path = out_dir / "coeff_grid.csv"
            write_coeff_file(coeff_path, default_grid())
        n_coeffs = len(read_coeff_file(coeff_path))
        raw_csv = out_dir / "gpu_sweep_per_replication.csv"
        print(f"[run] 参数扫描：{n_coeffs} 组系数 x {args.reps} 次 = {n_coeffs * args.reps} 次仿真")
    else:
        raw_csv = out_dir / "gpu_per_replication.csv"
        print(f"[run] {args.reps} 次重复 x 5 策略 = {args.reps * 5} 次仿真")

    if backend == "cuda":
        if args.rebuild:
            ensure_cuda_built(force=True)
        print(run_cuda(args, raw_csv, coeff_path).rstrip())
    else:
        print(run_fallback(backend, args, raw_csv, coeff_path).rstrip())

    if args.sweep:
        rows = load_sweep(raw_csv)
        print_sweep(rows, args.reps)
        summary = out_dir / "gpu_sweep_summary.csv"
        with summary.open("w", newline="", encoding="utf-8-sig") as fp:
            cols = (["coeff_index", "alpha", "beta", "gamma", "delta"]
                    + [c for m in GPU_METRICS for c in (m, f"{m}_ci")])
            w = csv.DictWriter(fp, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"\n[out] 逐次数据：{raw_csv}")
        print(f"[out] 扫描汇总：{summary}")
        return 0

    data = load_rows(raw_csv)
    print_normal(data, args.reps)
    paired_rows = print_paired(data, "dynamic", "priority")
    print_paired(data, "dynamic", "mlfq")

    summary = out_dir / "gpu_replication_summary.csv"
    with summary.open("w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["scheduler", "scheduler_label", "metric", "metric_label", "unit",
                    "n", "mean", "std", "ci95_half", "ci95_low", "ci95_high", "rel_ci_pct"])
        for sched, cols in data.items():
            for m, vals in cols.items():
                label, unit, _ = GPU_METRICS[m]
                agg = st.aggregate(m, vals)
                w.writerow([sched, SCHED_LABELS.get(sched, sched), m, label, unit,
                            agg.n, f"{agg.mean:.6f}", f"{agg.std:.6f}", f"{agg.ci_half:.6f}",
                            f"{agg.ci_low:.6f}", f"{agg.ci_high:.6f}", f"{agg.rel_ci_pct:.3f}"])

    paired_csv = out_dir / "gpu_paired_dynamic_vs_priority.csv"
    with paired_csv.open("w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["metric", "metric_label", "unit", "priority_mean", "dynamic_mean",
                    "diff", "diff_ci95_half", "significant", "dynamic_better"])
        for m, label, unit, b, a, diff, half, sig, better in paired_rows:
            w.writerow([m, label, unit, f"{b:.6f}", f"{a:.6f}", f"{diff:.6f}",
                        f"{half:.6f}", int(sig), int(better)])

    print(f"\n[out] 逐次数据：{raw_csv}")
    print(f"[out] 聚合汇总：{summary}")
    print(f"[out] 配对比较：{paired_csv}")
    if args.compare:
        compare_with_reference(raw_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
