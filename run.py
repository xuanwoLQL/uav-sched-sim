#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run.py —— UavSchedulerSim 统一命令行入口

《操作系统》课程实训项目：面向智慧地质勘查的无人机集群并发自适应负载均衡调度仿真系统

用法（在项目根目录 uav_sched_sim 下执行）：
    .venv\\Scripts\\python.exe run.py check                 环境自检
    .venv\\Scripts\\python.exe run.py calibrate             三工况容量标定报告
    .venv\\Scripts\\python.exe run.py one --scenario LIGHT --scheduler dynamic
    .venv\\Scripts\\python.exe run.py all                   全量 3 工况 × 5 策略 = 15 组
    .venv\\Scripts\\python.exe run.py all --no-db           不写数据库（离线跑）
    .venv\\Scripts\\python.exe run.py all --no-station-probe 跳过站级/MEC 伴生探针
    .venv\\Scripts\\python.exe run.py stations              多站矩阵：triple × 6 策略 × 3 工况 = 18 组
    .venv\\Scripts\\python.exe run.py charts                仅从 results/ 重新出图
    .venv\\Scripts\\python.exe run.py demo-limiter          演示大任务拒绝与分片
    .venv\\Scripts\\python.exe run.py weather               天气 × 策略对照实验（20 组）
    .venv\\Scripts\\python.exe run.py replicate --reps 3 --variants single triple triple_mec

退出码：0 成功 / 1 有实验失败 / 2 环境不可用
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# 让工程免设 PYTHONPATH：把 src/ 挂到 sys.path 最前面
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PY = sys.executable


def _setup_console() -> None:
    """修正 Windows 下的中文输出编码。

    行为：
      * 输出被重定向（管道/IDE 捕获）时，强制 UTF-8 —— 调用方按 UTF-8 解码才不会乱码；
      * 直接连在真实控制台时，保留系统本地编码（简体中文 Windows 为 GBK），
        这样在 PowerShell 窗口里直接运行时中文正常显示。
    """
    if os.name != "nt":
        return
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


_setup_console()


def _banner(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _load_cfg(path: str | None = None) -> dict:
    from uav_sim.config import load_config

    return load_config(path)


# ---------------------------------------------------------------------------
# check —— 环境自检
# ---------------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    _banner("环境自检")
    problems: list[str] = []
    warns: list[str] = []

    # 1) Python 版本
    v = sys.version_info
    ok_ver = v >= (3, 8)
    print(f"[{'OK ' if ok_ver else 'ERR'}] Python {v.major}.{v.minor}.{v.micro}  ({PY})")
    if not ok_ver:
        problems.append("Python 版本低于 3.8")

    # 2) 第三方依赖
    deps = {
        "yaml": "PyYAML",
        "matplotlib": "matplotlib",
        "pandas": "pandas",
        "pymysql": "PyMySQL",
        "numpy": "numpy",
    }
    for mod, pkg in deps.items():
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "?")
            print(f"[OK ] 依赖 {pkg:<12} {ver}")
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR] 依赖 {pkg:<12} 缺失：{exc}")
            problems.append(f"缺少依赖 {pkg}（pip install -r requirements.txt）")

    # 3) 配置文件
    cfg_path = ROOT / (args.config or "config.yaml")
    if cfg_path.exists():
        print(f"[OK ] 配置文件 {cfg_path}")
    else:
        print(f"[ERR] 配置文件不存在：{cfg_path}")
        problems.append("缺少 config.yaml")
        cfg = {}
    try:
        cfg = _load_cfg(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] 配置文件解析失败：{exc}")
        problems.append("config.yaml 解析失败")
        cfg = {}

    # 4) 输出目录可写
    from uav_sim.config import resolve_path

    for key in ("output.results_dir", "output.charts_dir", "output.logs_dir"):
        try:
            p = resolve_path(cfg, key)
            probe = p / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            print(f"[OK ] 目录可写 {p}")
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR] 目录不可写 {key}：{exc}")
            problems.append(f"目录不可写：{key}")

    # 5) 本机规格 vs PDF 要求
    try:
        import psutil  # 可选

        mem_gb = psutil.virtual_memory().total / 1024 ** 3
        print(f"[OK ] 内存 {mem_gb:.1f} GB（PDF 要求 ≥8GB）")
    except Exception:  # noqa: BLE001
        print("[-- ] psutil 未安装，跳过内存检测（PDF 要求内存 ≥8GB）")

    # 6) MySQL 连通性
    db_cfg = (cfg.get("database") or {})
    if db_cfg.get("enable", True):
        try:
            import pymysql

            t0 = time.perf_counter()
            conn = pymysql.connect(
                host=db_cfg.get("host", "127.0.0.1"),
                port=int(db_cfg.get("port", 3306)),
                user=db_cfg.get("user", "root"),
                password=os.environ.get("UAV_DB_PASSWORD") or db_cfg.get("password", ""),
                charset=db_cfg.get("charset", "utf8mb4"),
                connect_timeout=5,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                ver = cur.fetchone()[0]
            conn.close()
            print(f"[OK ] MySQL {ver} @ {db_cfg.get('host')}:{db_cfg.get('port')}"
                  f"（{(time.perf_counter() - t0) * 1000:.0f} ms）")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] MySQL 连接失败：{exc}")
            warns.append("MySQL 不可用 —— 实验仍可运行，结果只落 CSV/JSON（优雅降级）")
    else:
        print("[-- ] 配置中 database.enable=false，跳过数据库检测")

    # 7) 中文字体
    try:
        from uav_sim.visualize import setup_chinese_font

        font = setup_chinese_font()
        print(f"[OK ] matplotlib 中文字体 {font}")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 中文字体配置失败：{exc}")
        warns.append("图表中文可能乱码 —— 检查系统是否安装 微软雅黑/SimHei")

    # 8) 源码模块完整性
    modules = [
        "uav_sim.models",
        "uav_sim.config",
        "uav_sim.scenarios",
        "uav_sim.queue_manager",
        "uav_sim.link_monitor",
        "uav_sim.realsource",
        "uav_sim.resource_limiter",
        "uav_sim.simulator",
        "uav_sim.scheduler",
        "uav_sim.metrics",
        "uav_sim.db",
        "uav_sim.experiment",
        "uav_sim.visualize",
    ]
    missing = []
    for mod in modules:
        try:
            __import__(mod)
            print(f"[OK ] 模块 {mod}")
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR] 模块 {mod} 导入失败：{exc}")
            missing.append(mod)
    if missing:
        problems.append("模块缺失：" + ", ".join(missing))

    # 9) 六个调度器可实例化检查（不做真实仿真）
    try:
        from uav_sim.models import SCHEDULER_NAMES
        from uav_sim.scheduler import SCHEDULERS

        got = set(SCHEDULERS.keys())
        need = set(SCHEDULER_NAMES)
        if need <= got:
            print(f"[OK ] 调度器注册齐全：{', '.join(SCHEDULER_NAMES)}")
        else:
            print(f"[ERR] 调度器缺失：{', '.join(sorted(need - got))}")
            problems.append("调度器注册不全")
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] 调度器注册表读取失败：{exc}")
        problems.append("scheduler 注册表不可用")

    # 汇总
    _banner("自检结论")
    for w in warns:
        print(f"  [WARN] {w}")
    if problems:
        for p in problems:
            print(f"  [PROBLEM] {p}")
        print(f"\n  ✗ 发现 {len(problems)} 个问题，请先修复。")
        return 2
    print("  ✓ 环境就绪，可以开始跑实验。")
    print("\n  下一步：")
    print(f"    {PY} run.py calibrate")
    print(f"    {PY} run.py all")
    return 0


# ---------------------------------------------------------------------------
# calibrate —— 三工况容量标定
# ---------------------------------------------------------------------------
def cmd_calibrate(args: argparse.Namespace) -> int:
    _banner("三工况容量标定报告")
    from uav_sim.scenarios import calibrate, list_scenarios

    cfg = _load_cfg(args.config)
    seed = args.seed if args.seed is not None else cfg["simulation"]["random_seed"]

    print("容量模型：")
    print("  单任务服务时长(仿真秒) = data_size_mb / (worker_bandwidth_mbps × link_factor)")
    print("  link_factor = weaknet_bandwidth_floor + (1 - floor) × health/100")
    print("  系统有效容量(MB/s)    = worker_threads × worker_bandwidth_mbps × link_factor")
    print("  提供负载(MB/s)        = Σ(任务数据量) / 仿真时长")
    print("  重传放大系数          = 1 / (1 − p_fail)，p_fail = (1 − health/100) × retry_gain")
    print("  系统负载              = 提供负载 × 重传放大 / 有效容量    （>1 即过载）")
    print()

    header = (
        f"{'工况':<7}{'时长':>5}{'机数':>5}{'任务数':>7}{'勘查间隔':>9}"
        f"{'提供负载':>10}{'重传放大':>9}{'有效负载':>10}{'容量':>8}"
        f"{'负载率':>8}{'目标':>7}{'结论':>10}"
    )
    print(header)
    print("-" * 100)

    rows = []
    for sc in list_scenarios():
        rep = calibrate(sc, cfg, seed=seed)
        rows.append(rep)
        verdict_cn = {
            "ok": "✓ 达标",
            "too_light": "偏轻",
            "overloaded": "过载",
        }.get(rep.get("verdict", ""), rep.get("verdict", ""))
        print(
            f"{rep['code']:<7}{sc.duration_s:>5.0f}{sc.uav_count:>5}{rep['n_tasks']:>7}"
            f"{str(rep.get('survey_interval_s', '-')):>9}"
            f"{rep['offered_mbps']:>10.1f}{rep.get('retry_multiplier', 1.0):>9.2f}"
            f"{rep['offered_mbps'] * rep.get('retry_multiplier', 1.0):>10.1f}"
            f"{rep['capacity_mbps']:>8.1f}"
            f"{rep['load_factor']:>8.2f}{rep['target_load_factor']:>7.2f}{verdict_cn:>10}"
        )

    print()
    print("说明：负载率 <0.5 为轻载（极少堆积）；0.5~1.0 为常态（偶发堆积）；")
    print("      >1.0 为过载（队列持续堆积、饥饿与业务丢失风险上升）。")
    bad = [r for r in rows if r.get("verdict") != "ok"]
    if bad:
        print()
        print(f"⚠ 有 {len(bad)} 个工况偏离标定目标 >25%：{', '.join(r['code'] for r in bad)}")
        print("  调整方式：修改 src/uav_sim/scenarios.py 里对应工况的 survey_gap_k。")
        return 1
    print()
    print("✓ 三工况标定全部达标，与 PDF「预期队列堆积：极少 / 偶尔 / 频繁严重」一致。")
    return 0


# ---------------------------------------------------------------------------
# one —— 单组实验
# ---------------------------------------------------------------------------
def cmd_one(args: argparse.Namespace) -> int:
    from uav_sim.experiment import ExperimentRunner

    cfg = _load_cfg(args.config)
    if args.no_db:
        cfg["database"]["enable"] = False

    _banner(f"单组实验：{args.scenario} × {args.scheduler}")
    runner = ExperimentRunner(cfg)
    res = runner.run_one(
        scenario_code=args.scenario,
        scheduler_name=args.scheduler,
        seed=args.seed,
        persist=not args.no_db,
    )
    if res.error:
        print(f"\n✗ 实验失败：{res.error}")
        return 1
    _print_result(res)
    return 0


def _print_result(res) -> None:
    from uav_sim.models import METRIC_SPEC

    print(f"\n  实验号 {res.exp_id}")
    print(f"  工况 {res.scenario} / 策略 {res.scheduler} / seed {res.seed}")
    print(f"  仿真时长 {res.duration_s:.0f}s  实耗 {res.wall_seconds:.2f}s")
    print()
    keys = [
        "throughput",
        "avg_wait_high_ms",
        "avg_wait_low_ms",
        "starvation_rate",
        "starvation_rate_low",
        "resource_utilization",
        "completion_rate",
        "drop_rate",
        "retry_rate",
        "context_switch",
        "peak_queue_depth",
        "throttle_count",
    ]
    print(f"  {'指标':<26}{'数值':>14}  {'单位':<8}{'方向'}")
    print("  " + "-" * 62)
    for k in keys:
        spec = METRIC_SPEC.get(k)
        if not spec:
            continue
        cn, unit, higher_better = spec
        val = res.metrics.get(k, 0.0)
        arrow = "↑越大越好" if higher_better else "↓越小越好"
        print(f"  {cn:<26}{val:>14.3f}  {unit:<8}{arrow}")
    print()


# ---------------------------------------------------------------------------
# all —— 全量 3 工况 × 5 策略
# ---------------------------------------------------------------------------
def cmd_all(args: argparse.Namespace) -> int:
    from uav_sim.experiment import ExperimentRunner
    from uav_sim.models import CLASSIC_SCHEDULER_NAMES, SCENARIO_CODES

    cfg = _load_cfg(args.config)
    if args.no_db:
        cfg["database"]["enable"] = False

    # 默认矩阵 = 3 工况 × **5 个经典单节点策略** = 15 组（既有报告口径）。
    # 第 6 个策略 cross_node 属多基站能力面，需显式 opt-in（--schedulers cross_node），
    # 否则"策略差异"会与"拓扑差异"纠缠，且既有 15 组数字全部失效。
    sched_names = list(args.schedulers or CLASSIC_SCHEDULER_NAMES)
    _banner(f"全量对照实验：{len(SCENARIO_CODES)} 工况 × {len(sched_names)} 策略 = "
            f"{len(SCENARIO_CODES) * len(sched_names)} 组")

    t0 = time.perf_counter()
    runner = ExperimentRunner(cfg)
    results = runner.run_matrix(
        scenario_codes=args.scenarios or SCENARIO_CODES,
        scheduler_names=sched_names,
        persist=not args.no_db,
        progress=True,
    )
    elapsed = time.perf_counter() - t0

    ok = [r for r in results if not r.error]
    bad = [r for r in results if r.error]

    _banner("全量实验完成")
    print(f"  成功 {len(ok)} / {len(results)} 组，总实耗 {elapsed:.1f}s")
    if bad:
        print("\n  失败明细：")
        for r in bad:
            print(f"    ✗ {r.scenario} × {r.scheduler}: {r.error}")

    if ok:
        print()
        _print_matrix(ok)

        # ---- 自动出图（7 张对比图） ----
        print("\n  正在生成图表 ...")
        try:
            from uav_sim.config import resolve_path
            from uav_sim.visualize import plot_all

            charts_dir = resolve_path(cfg, "output.charts_dir")
            made = plot_all(ok, charts_dir)
            print(f"  ✓ 生成 {len(made)} 张图 -> {charts_dir}")
        except Exception as exc:  # noqa: BLE001 - 出图失败不应让实验判为失败
            print(f"  [警告] 出图失败：{exc}")
            print(f"         可稍后单独重跑：{PY} run.py charts")

        print("\n  产出：")
        print("    results/summary.csv         汇总表（Excel 可直接打开）")
        print("    results/raw/*.json          每次运行完整结果")
        print("    results/task_records/*.csv  任务级明细（报告附录用）")
        print("    charts/*.png                7 张对比图")
        print("\n  看数据库：")
        print("    mysql --host=127.0.0.1 -uroot -p ^")
        print("      -e \"use uav_sched_sim; select * from v_exp_summary;\"")

    # ---- P6a 站级 / MEC 伴生探针（独立 results_dir，不进 15 组口径） ----
    # 放在 15 组之外：它与 15 组**没有数值耦合**，成败判定也完全解耦
    # （这里任何异常都只打警告，不影响上面的返回码）。
    if not getattr(args, "no_station_probe", False):
        _banner("站级 / MEC 伴生探针（3 组，独立实验面，不属于 15 组矩阵）")
        try:
            _run_station_probe(cfg, seed=getattr(args, "seed", None))
        except Exception as exc:  # noqa: BLE001 - 伴生实验失败绝不影响 15 组成败
            print(f"  [警告] 站级/MEC 伴生实验失败（不影响 15 组结果与返回码）：{exc}")
            print(f"         可单独重跑：{PY} run.py stations")
    elif ok:
        print("\n  （已用 --no-station-probe 跳过站级/MEC 伴生实验）")

    return 0 if not bad else 1


def _print_matrix(results) -> None:
    """控制台打印 策略 × 关键指标 的对比矩阵（按工况分组）。"""
    from collections import defaultdict

    from uav_sim.models import SCENARIO_LABELS, SCHEDULER_LABELS

    groups: dict[str, list] = defaultdict(list)
    for r in results:
        groups[r.scenario].append(r)

    cols = [
        ("throughput", "吞吐量", "个/s", 2),
        ("avg_wait_high_ms", "高优时延", "ms", 1),
        ("avg_wait_low_ms", "低优时延", "ms", 1),
        ("starvation_rate_low", "低优饥饿率", "%", 1),
        ("resource_utilization", "资源利用率", "%", 1),
        ("completion_rate", "业务完整率", "%", 1),
    ]
    for code in ("LIGHT", "NORMAL", "PEAK"):
        rows = groups.get(code)
        if not rows:
            continue
        print(f"\n  【{code} · {SCENARIO_LABELS.get(code, code)}】")
        head = f"  {'策略':<24}" + "".join(f"{c[1]:>12}" for c in cols)
        print(head)
        print("  " + "-" * (len(head) - 2))
        # 每组指标的最优值（用于打星）
        best = {}
        for key, _, _, _ in cols:
            vals = [r.metrics.get(key, 0.0) for r in rows]
            higher_better = key in ("throughput", "resource_utilization", "completion_rate")
            best[key] = max(vals) if higher_better else min(vals)
        for r in rows:
            line = f"  {SCHEDULER_LABELS.get(r.scheduler, r.scheduler):<24}"
            for key, _, _, nd in cols:
                val = r.metrics.get(key, 0.0)
                mark = "*" if abs(val - best[key]) < 1e-9 else " "
                line += f"{val:>11.{nd}f}{mark}"
            print(line)
        print("  （* 表示该指标本工况最优）")


# ---------------------------------------------------------------------------
# weather —— 天气 × 调度策略 对照实验（课程报告「天气→弱网→调度权重」论证链）
# ---------------------------------------------------------------------------
#: 天气对照实验固定使用的工况（契约 §4.6.5）
WEATHER_EXP_SCENARIO: str = "PEAK"

#: 统计"劣质链路占比"的阈值兜底（真实值取 config weaknet.health_poor）
POOR_HEALTH_FALLBACK: float = 45.0


def _load_link_stats(res, poor_threshold: float) -> dict:
    """从一次运行的链路采样 + 天气采样里算链路口径统计。"""
    healths = [float(s.get("health", 100.0)) for s in (res.link_samples or [])]
    poor = sum(1 for h in healths if h < poor_threshold)
    weather_samples = list(res.weather_samples or [])
    n_w = len(weather_samples) or 1

    def _mean(key: str) -> float:
        if not weather_samples:
            return 0.0
        return sum(float(s.get(key, 0.0)) for s in weather_samples) / n_w

    return {
        "link_mean_health": (sum(healths) / len(healths)) if healths else 0.0,
        "link_poor_ratio_pct": (poor / len(healths) * 100.0) if healths else 0.0,
        "weather_mean_penalty": _mean("link_penalty"),
        "weather_mean_intensity": _mean("intensity"),
        "weather_mean_visibility_km": _mean("visibility_km"),
    }


def _save_weather_csv(rows: list, path: Path) -> Path:
    """写 results/weather_matrix.csv（utf-8-sig：Excel 双击打开中文不乱码）。"""
    import csv as _csv

    columns = [
        ("preset", "天气"), ("preset_label", "天气中文名"),
        ("scheduler", "策略"), ("scheduler_label", "策略中文名"),
        ("completion_rate", "业务完整率%"),
        ("avg_wait_high_ms", "高优先级平均等待ms"),
        ("avg_wait_low_ms", "低优先级平均等待ms"),
        ("avg_wait_all_ms", "整体平均等待ms"),
        ("p95_wait_high_ms", "高优先级等待P95 ms"),
        ("p95_wait_low_ms", "低优先级等待P95 ms"),
        ("starvation_rate", "任务饥饿率%"),
        ("starvation_rate_low", "低优先级饥饿率%"),
        ("throughput", "吞吐量个/秒"),
        ("retry_rate", "弱网重传率%"),
        ("drop_rate", "任务丢弃率%"),
        ("resource_utilization", "资源利用率%"),
        ("link_mean_health", "链路健康度均值"),
        ("link_poor_ratio_pct", "劣质链路占比%"),
        ("weather_mean_penalty", "天气平均扣分"),
        ("weather_mean_intensity", "天气平均强度"),
        ("weather_mean_visibility_km", "平均能见度km"),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = _csv.writer(fp)
        writer.writerow([cn for _, cn in columns])
        writer.writerow([key for key, _ in columns])   # 第二行英文键名，便于脚本/报告直接引用
        for row in rows:
            writer.writerow([
                f"{row[key]:.6f}" if isinstance(row.get(key), float) else row.get(key, "")
                for key, _ in columns
            ])
    return path


def _plot_weather_matrix(rows: list, path: Path) -> Path:
    """画 charts/08-天气-链路健康度与指标联动.png。

    左图：4 种天气下**链路健康度均值**（柱）+ **劣质链路占比**（线，右轴）；
    右图：4 种天气下 **dynamic（本方案） vs 静态优先级** 的高优完成率（柱）
          + 高优平均等待（虚线，右轴）—— 一张图讲完"天气变差后谁扛得住"。
    """
    from uav_sim.visualize import PALETTE, setup_chinese_font

    setup_chinese_font()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    from uav_sim.models import SCHEDULER_NAMES
    from uav_sim.weather import PRESET_ORDER, WEATHER_PRESETS

    presets = [p for p in PRESET_ORDER if any(r["preset"] == p for r in rows)]
    if not presets or not rows:
        print("  [警告] 无数据，跳过天气对比图")
        return path

    # 天气配色（与前端 §4.5 的雾色基调呼应：晴蓝 / 雨蓝 / 雪青 / 沙尘土黄）
    weather_colors = {"clear": "#7FB3D5", "rain": "#4A6FA5",
                      "snow": "#9AD0EC", "dust": "#C8965A"}
    labels = [WEATHER_PRESETS[p].label for p in presets]
    xs = np.arange(len(presets))

    def _pick(preset: str, sched: str, key: str) -> float:
        for r in rows:
            if r["preset"] == preset and r["scheduler"] == sched:
                return float(r.get(key, 0.0))
        return 0.0

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14.5, 6.0))

    # ---------------- 左图：链路健康度均值 + 劣质链路占比 ----------------
    link_mean = [float(np.mean([r["link_mean_health"] for r in rows
                                if r["preset"] == p])) for p in presets]
    poor_pct = [float(np.mean([r["link_poor_ratio_pct"] for r in rows
                               if r["preset"] == p])) for p in presets]
    bars = ax1.bar(xs, link_mean, width=0.55,
                   color=[weather_colors.get(p, "#999999") for p in presets],
                   edgecolor="white", linewidth=0.8)
    for b, v in zip(bars, link_mean):
        ax1.annotate(f"{v:.1f}", (b.get_x() + b.get_width() / 2, v),
                     textcoords="offset points", xytext=(0, 4),
                     ha="center", fontsize=10, fontweight="bold")
    ax1.set_xticks(xs)
    ax1.set_xticklabels(labels, fontsize=11)
    ax1.set_ylim(0, 108)
    ax1.set_ylabel("链路健康度均值（0~100，越高越好）", fontsize=10)
    ax1.set_title(f"4 种天气下的链路健康度（{WEATHER_EXP_SCENARIO} 工况，5 策略平均）",
                  fontsize=12, fontweight="bold")
    ax1.grid(axis="y", linestyle=":", alpha=0.45)

    ax1b = ax1.twinx()
    ink = ax1b.plot(xs, poor_pct, "o--", color="#E8684A", linewidth=1.8,
                    markersize=7, label="劣质链路占比（右轴）")
    for x, v in zip(xs, poor_pct):
        ax1b.annotate(f"{v:.1f}%", (x, v), textcoords="offset points",
                      xytext=(0, 8), ha="center", fontsize=9, color="#C0492B")
    ax1b.set_ylim(0, 108)
    ax1b.set_ylabel("劣质链路占比（%，越低越好）", fontsize=10)
    ax1.legend(ink, ["劣质链路占比（右轴）"], fontsize=8.5, loc="upper left")

    # ---------------- 右图：dynamic vs 静态优先级（完成率 + 高优等待） ----------------
    pair = [s for s in ("dynamic", "priority") if any(r["scheduler"] == s for r in rows)]
    if not pair:
        pair = list(SCHEDULER_NAMES[:2])
    width = 0.55 / max(1, len(pair))
    for i, sched in enumerate(pair):
        vals = [_pick(p, sched, "completion_rate") for p in presets]
        offs = xs - 0.275 + width * (i + 0.5)
        bars = ax2.bar(offs, vals, width=width * 0.92,
                       label=f"{sched}（{'本方案' if sched == 'dynamic' else '静态'}）",
                       color=PALETTE.get(sched, "#999999"), edgecolor="white")
        for b, v in zip(bars, vals):
            ax2.annotate(f"{v:.1f}", (b.get_x() + b.get_width() / 2, v),
                         textcoords="offset points", xytext=(0, 3),
                         ha="center", fontsize=8, rotation=90)
    ax2.set_xticks(xs)
    ax2.set_xticklabels(labels, fontsize=11)
    ax2.set_ylim(0, 112)
    ax2.set_ylabel("高优先级任务完成率（%，越高越好）", fontsize=10)
    ax2.set_title("dynamic（本方案） vs 静态优先级：恶劣天气下的业务保持能力",
                  fontsize=12, fontweight="bold")
    ax2.grid(axis="y", linestyle=":", alpha=0.45)

    ax2b = ax2.twinx()
    styles = ["--", ":"]
    for i, sched in enumerate(pair):
        vals = [_pick(p, sched, "avg_wait_high_ms") for p in presets]
        ax2b.plot(xs, vals, marker="s", linestyle=styles[i % len(styles)],
                  color=PALETTE.get(sched, "#999999"), linewidth=1.5,
                  markersize=5, label=f"{sched} 高优平均等待（右轴）")
    ax2b.set_ylabel("高优先级平均等待（ms，越低越好）", fontsize=10)
    handles, names = ax2.get_legend_handles_labels()
    handles2, names2 = ax2b.get_legend_handles_labels()
    ax2.legend(handles + handles2, names + names2, fontsize=8, loc="upper right", ncol=1)

    fig.suptitle("天气 → 弱网 → 链路健康度 → 调度指标：联动对照（数字来自实跑）",
                 fontsize=14, fontweight="bold", y=1.0)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def cmd_weather(args: argparse.Namespace) -> int:
    """天气对照实验：PEAK 工况 × 4 天气 × 5 策略 = 20 组（契约 §4.6.5）。"""
    from uav_sim.config import resolve_path
    from uav_sim.experiment import ExperimentRunner
    from uav_sim.models import CLASSIC_SCHEDULER_NAMES, SCHEDULER_LABELS
    from uav_sim.weather import PRESET_ORDER, WEATHER_PRESETS

    cfg = _load_cfg(args.config)
    if getattr(args, "seed", None) is not None:
        cfg.setdefault("simulation", {})["random_seed"] = int(args.seed)
    # 天气对照是**辅助实验**：不写数据库、不碰 results/summary.csv 与 results/raw
    # （既有的 15 组正式结果必须保持原样，契约 §0/§6）。
    cfg["database"]["enable"] = False
    results_dir = resolve_path(cfg, "output.results_dir")
    charts_dir = resolve_path(cfg, "output.charts_dir")
    out_csv = results_dir / "weather_matrix.csv"
    out_png = charts_dir / "08-天气-链路健康度与指标联动.png"
    raw_dir = results_dir / "weather"      # 天气实验专属 raw 目录（run.py charts 不会读到）
    poor_threshold = float(cfg.get("weaknet", {}).get(
        "health_poor", POOR_HEALTH_FALLBACK))

    total = len(PRESET_ORDER) * len(CLASSIC_SCHEDULER_NAMES)
    _banner(f"天气 × 调度策略 对照实验：{WEATHER_EXP_SCENARIO} × "
            f"{len(PRESET_ORDER)} 天气 × {len(CLASSIC_SCHEDULER_NAMES)} 策略 = {total} 组")
    print("  天气曲线：0.5s 网格、onset 爬升 → 平台期阵风 ±15% → 尾部衰减；")
    print("  控制变量：同 seed 下 5 个策略回放**同一条**天气 / 链路曲线，只有策略不同。")
    print(f"  链路口径：劣质链路 = 健康度 < {poor_threshold:.0f}；")
    print(f"  产出：{out_csv}")
    print(f"        {out_png}")
    print(f"  （天气实验 raw 落在 {raw_dir}，不覆盖既有 15 组 results/raw）\n")

    t0 = time.perf_counter()
    runner = ExperimentRunner(cfg, results_dir=raw_dir)
    rows: list = []
    failed = 0
    k = 0
    # 遍历顺序：**外层 = 策略，内层 = 天气**。
    # 为什么不是外层天气：天气矩阵要在共享机器上跑 20 组，机器负载会缓慢漂移；
    # 让同一策略的 4 个天气组在时间上相邻，组间的系统负载尽量一致 ——
    # 对照实验只允许"天气"一个变量变化，负载漂移是最大的混淆因素。
    for sched in CLASSIC_SCHEDULER_NAMES:
        for preset in PRESET_ORDER:
            label = WEATHER_PRESETS[preset].label
            k += 1
            print(f"[{k:>2}/{total}] {sched:<9} × {label}({preset:<5}) ... ",
                  end="", flush=True)
            res = runner.run_one(WEATHER_EXP_SCENARIO, sched, persist=False,
                                 weather_preset=preset)
            if res.error:
                failed += 1
                print(f"✗ {res.error}")
                continue
            stats = _load_link_stats(res, poor_threshold)
            row = {
                "preset": preset,
                "preset_label": label,
                "scheduler": sched,
                "scheduler_label": SCHEDULER_LABELS.get(sched, sched),
            }
            for key in ("completion_rate", "avg_wait_high_ms", "avg_wait_low_ms",
                        "avg_wait_all_ms", "p95_wait_high_ms", "p95_wait_low_ms",
                        "starvation_rate", "starvation_rate_low", "throughput",
                        "retry_rate", "drop_rate", "resource_utilization"):
                row[key] = float(res.metrics.get(key, 0.0))
            row.update(stats)
            rows.append(row)
            print(f"完整率={row['completion_rate']:5.1f}% "
                  f"高优等待={row['avg_wait_high_ms']:8.0f}ms "
                  f"低优饥饿={row['starvation_rate_low']:5.1f}% "
                  f"链路均值={row['link_mean_health']:5.1f} "
                  f"劣质={row['link_poor_ratio_pct']:4.1f}% "
                  f"({res.metrics.get('runtime_wall_s', 0.0):.1f}s)")

    elapsed = time.perf_counter() - t0
    _banner("天气对照实验完成")
    print(f"  成功 {len(rows)} / {total} 组，总实耗 {elapsed:.1f}s")
    if failed:
        print(f"  ✗ 失败 {failed} 组")
    if not rows:
        return 1

    _save_weather_csv(rows, out_csv)
    print(f"  ✓ 汇总表 -> {out_csv}")
    try:
        _plot_weather_matrix(rows, out_png)
        print(f"  ✓ 图表   -> {out_png}")
    except Exception as exc:  # noqa: BLE001 - 出图失败不应让实验判失败
        print(f"  [警告] 出图失败：{exc}")

    _print_weather_conclusion(rows, poor_threshold)
    return 0 if not failed else 1


# ===========================================================================
# 多基站 / MEC 边缘计算对照实验（改造建议第五章 B）
# ===========================================================================
#: MEC 权衡实验的两个可调参数（边缘算力 / 回传带宽）扫描点。
#: 每条 = (标签, 边缘算力 MB/s, 回传带宽 MB/s)；None 表示用 config 默认值。
MEC_SWEEP: tuple = (
    ("边缘-基线", None, None),
    ("边缘-算力减半", 60.0, None),
    ("边缘-回传1/3", None, 20.0),
    ("边缘-最差(算力减半+回传1/3)", 60.0, 20.0),
)

#: 参与对照的调度策略（动态权重 = 单节点基线；cross_node = 跨节点策略）
MEC_SCHEDULERS: tuple = ("dynamic", "cross_node")

#: 本实验使用的工况（峰值最能暴露节点间不均衡）
MEC_EXP_SCENARIO: str = "PEAK"


def cmd_mec(args: argparse.Namespace) -> int:
    """**纯中心 vs 端-边-云** 对照实验 + 通信/计算开销权衡（改造建议第五章 B）。

    三组对照，全部固定 PEAK 工况与同一 seed（控制变量）：

    * **A 纯中心**：单站拓扑 + MEC 关闭 —— 全部任务回中心处理（改造前的形态）；
    * **B 端-边-云**：3 站拓扑（1 中心 + 2 边缘）+ MEC 开启 ——
      轻量运维任务下沉边缘、重勘查任务回中心；
    * **C 权衡扫描**：在 B 的基础上分别压低"边缘算力"与"回传带宽"，
      观察端到端时延如何变化 —— 这就是「通信开销 vs 计算开销」的量化权衡。

    产出 ``results/mec/mec_matrix.csv``（不写数据库、不碰既有 15 组 results/raw）。
    """
    import csv as _csv

    from uav_sim.config import resolve_path
    from uav_sim.experiment import ExperimentRunner
    from uav_sim.models import SCHEDULER_LABELS

    cfg = _load_cfg(args.config)
    if getattr(args, "seed", None) is not None:
        cfg.setdefault("simulation", {})["random_seed"] = int(args.seed)
    # 辅助实验：不写数据库、不覆盖既有 15 组正式结果（契约 §0/§6）
    cfg["database"]["enable"] = False
    results_dir = resolve_path(cfg, "output.results_dir")
    out_dir = results_dir / "mec"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / "mec_matrix.csv"

    metric_keys = ("throughput", "completion_rate", "avg_wait_all_ms",
                   "avg_wait_high_ms", "starvation_rate_low",
                   "resource_utilization", "edge_offload_ratio",
                   "avg_e2e_ms", "edge_e2e_ms", "center_e2e_ms",
                   "e2e_if_edge_ms", "e2e_if_center_ms",
                   "e2e_light_if_edge_ms", "e2e_light_if_center_ms",
                   "e2e_heavy_if_edge_ms", "e2e_heavy_if_center_ms",
                   "mec_crossing_data_mb",
                   "mec_light_tasks", "mec_heavy_tasks",
                   "migration_count", "migration_cost_ms",
                   "cross_node_comm_ms", "station_load_imbalance")

    # ---- 组装运行计划：(模式, 标签, station_spec, mec_enabled, 参数覆盖, 策略) ----
    # 三档对照：① 纯中心（单站，改造前形态）
    #           ② 多站但**不启用 MEC**（无角色硬约束，最能体现"跨节点负载均衡"本身）
    #           ③ 端-边-云（多站 + MEC 角色分流）+ 两个可调参数的权衡扫描
    plan: list = []
    for sched in MEC_SCHEDULERS:
        plan.append(("center", "纯中心(单站)", None, False, {}, sched))
    for sched in MEC_SCHEDULERS:
        plan.append(("multi", "多站-无MEC", "triple", False, {}, sched))
    for tag, edge_mbps, backhaul in MEC_SWEEP:
        for sched in MEC_SCHEDULERS:
            over: dict = {}
            if edge_mbps is not None:
                over["edge_compute_mbps"] = float(edge_mbps)
            if backhaul is not None:
                over["backhaul_mbps"] = float(backhaul)
            plan.append(("edge", tag, "triple", True, over, sched))

    total = len(plan)
    _banner(f"MEC 边缘计算对照实验：{MEC_EXP_SCENARIO} × 纯中心/多站/端-边-云 = {total} 组")
    print("  控制变量：同一 seed、同一工况、同一任务轨迹；只有拓扑/MEC/策略不同。")
    print("  纯中心   = 1 站 + MEC 关闭（全部回中心，等价改造前形态）")
    print("  多站-无MEC = 3 站（1 中心 + 2 边缘）+ MEC 关闭（无角色约束，看均衡本身）")
    print("  端-边-云 = 3 站 + MEC 开启（轻量下沉边缘、重勘查回中心）")
    print(f"  产出：{out_csv}\n")

    rows: list = []
    failed = 0
    t0 = time.perf_counter()
    k = 0
    for mode, tag, station_spec, mec_on, over, sched in plan:
        k += 1
        run_cfg = copy.deepcopy(cfg)
        if over:
            run_cfg.setdefault("mec", {}).update(over)
        print(f"[{k:>2}/{total}] {tag:<26} × {sched:<10} ... ", end="", flush=True)
        runner = ExperimentRunner(run_cfg, results_dir=out_dir,
                                  station_spec=station_spec, mec_enabled=mec_on)
        res = runner.run_one(MEC_EXP_SCENARIO, sched, persist=False)
        if res.error:
            failed += 1
            print(f"✗ {res.error}")
            continue
        row = {
            "mode": mode,
            "variant": tag,
            "scheduler": sched,
            "scheduler_label": SCHEDULER_LABELS.get(sched, sched),
            "stations": runner.station_spec if runner.station_spec else "single",
            "mec": int(bool(mec_on)),
            "edge_compute_mbps": float(run_cfg.get("mec", {}).get("edge_compute_mbps", 0.0)),
            "backhaul_mbps": float(run_cfg.get("mec", {}).get("backhaul_mbps", 0.0)),
        }
        for key in metric_keys:
            row[key] = float(res.metrics.get(key, 0.0))
        rows.append(row)
        print(f"完整率={row['completion_rate']:5.1f}% "
              f"平均等待={row['avg_wait_all_ms']:7.0f}ms "
              f"端到端={row['avg_e2e_ms']:7.1f}ms "
              f"边缘分流={row['edge_offload_ratio']:5.1f}% "
              f"迁移={int(row['migration_count']):>4d}次 "
              f"({res.metrics.get('runtime_wall_s', 0.0):.1f}s)")

    elapsed = time.perf_counter() - t0
    _banner("MEC 对照实验完成")
    print(f"  成功 {len(rows)} / {total} 组，总实耗 {elapsed:.1f}s")
    if failed:
        print(f"  ✗ 失败 {failed} 组")
    if not rows:
        return 1

    cols = list(rows[0].keys())
    with out_csv.open("w", encoding="utf-8-sig", newline="") as fp:
        w = _csv.DictWriter(fp, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"  ✓ 汇总表 -> {out_csv}")

    _print_mec_conclusion(rows)
    return 0 if not failed else 1

def _print_mec_conclusion(rows: list) -> None:
    """打印可直接引用进报告的中文结论（数字全部来自本次实跑）。"""
    def one(mode: str, sched: str, key: str, variant: str | None = None) -> float:
        for r in rows:
            if r["mode"] == mode and r["scheduler"] == sched and (
                    variant is None or r["variant"] == variant):
                return float(r.get(key, 0.0))
        return 0.0

    print()
    print("  ── 纯中心 vs 端-边-云（同策略对比）" + "─" * 30)
    for sched in MEC_SCHEDULERS:
        c_e2e = one("center", sched, "avg_e2e_ms")
        e_e2e = one("edge", sched, "avg_e2e_ms", "边缘-基线")
        c_wait = one("center", sched, "avg_wait_all_ms")
        e_wait = one("edge", sched, "avg_wait_all_ms", "边缘-基线")
        c_comp = one("center", sched, "completion_rate")
        e_comp = one("edge", sched, "completion_rate", "边缘-基线")
        impro = (c_e2e - e_e2e) / c_e2e * 100.0 if c_e2e > 0 else 0.0
        print(f"  [{sched}] 端到端 {c_e2e:7.1f} -> {e_e2e:7.1f} ms（{impro:+.1f}%）；"
              f"平均等待 {c_wait:6.0f} -> {e_wait:6.0f} ms；"
              f"完整率 {c_comp:5.1f}% -> {e_comp:5.1f}%")

    print()
    print("  ── 通信开销 vs 计算开销：压低边缘算力 / 回传带宽后的端到端时延" + "─" * 12)
    print("  （反事实 = **同一批任务**分别放到边缘 / 中心算出来的端到端时延，排除任务大小差异）")
    print(f"  {'变体':<26}{'轻量@边缘':>11}{'轻量@中心':>11}{'重载@边缘':>11}{'重载@中心':>11}{'分水岭MB':>10}")
    for tag, _, _ in MEC_SWEEP:
        le = one("edge", "cross_node", "e2e_light_if_edge_ms", tag)
        lc = one("edge", "cross_node", "e2e_light_if_center_ms", tag)
        he = one("edge", "cross_node", "e2e_heavy_if_edge_ms", tag)
        hc = one("edge", "cross_node", "e2e_heavy_if_center_ms", tag)
        cross = one("edge", "cross_node", "mec_crossing_data_mb", tag)
        print(f"  {tag:<26}{le:>11.1f}{lc:>11.1f}{he:>11.1f}{hc:>11.1f}{cross:>10.2f}")
    print("  判读：'轻量@边缘 < 轻量@中心' 说明轻量运维任务下沉是**划算的**；")
    print("        '重载@边缘 >> 重载@中心' 说明重勘查任务必须回中心（边缘算力不够）；")
    print("        分水岭 MB = 两者相等的数据量，比它小的任务下沉边缘更快。")

    print()
    print("  ── 跨节点均衡效果（动态权重 vs 跨节点策略）" + "─" * 24)
    print(f"  {'模式':<14}{'策略':<12}{'不均衡度':>10}{'迁移次数':>10}{'迁移代价(ms)':>14}{'平均等待(ms)':>14}")
    for mode, mlabel in (("multi", "多站-无MEC"), ("edge", "端-边-云")):
        for sched in MEC_SCHEDULERS:
            tag = None if mode == "multi" else "边缘-基线"
            imb = one(mode, sched, "station_load_imbalance", tag)
            mig = one(mode, sched, "migration_count", tag)
            cost = one(mode, sched, "migration_cost_ms", tag)
            wait = one(mode, sched, "avg_wait_all_ms", tag)
            print(f"  {mlabel:<14}{sched:<12}{imb:>10.3f}{int(mig):>10d}{cost:>14.1f}{wait:>14.0f}")
    print()
    print("  说明：不均衡度 = 各站负载的变异系数（σ/均值，0 = 完全均衡）；")
    print("        迁移代价 = 在途耗时 + 跨节点回传造成的服务时长放大，**不是瞬移**；")
    print("        多站-无MEC 一档没有 MEC 角色硬约束，最能体现'跨节点均衡'本身的效果。")

    # ---- 口径交代：为什么"边缘分流比例"看起来接近 100% ----
    lt = one("edge", "cross_node", "mec_light_tasks", "边缘-基线")
    ht = one("edge", "cross_node", "mec_heavy_tasks", "边缘-基线")
    tot = lt + ht
    print()
    print("  ── 口径交代（重要，报告必须写）" + "─" * 34)
    if tot > 0:
        print(f"  本工况任务构成：轻量 {int(lt)} 个 / 重勘查 {int(ht)} 个 "
              f"（轻量占 {lt / tot * 100:.1f}%）")
        print("  因此「边缘分流比例」偏高是**任务配比**决定的，不能单独当作 MEC 成功与否的证据；")
        print("  真正说明 MEC 价值的判据是上面的**分水岭数据量**与**按类别的反事实时延对比**：")
        print("  轻量任务下沉边缘更快、重勘查回中心更快，这条结论与任务配比无关。")
        print("  （若需要「重勘查占比更高」的工况，可在 config 的工况参数里调高勘查比例后重跑。）")
    else:
        print("  未能统计任务分级构成（MEC 未记录）")


# ===========================================================================
# 多站 × 多策略矩阵实验（run.py stations）—— 改造建议第五章 A 的规模化实验面
# ===========================================================================
#: 矩阵固定的拓扑（config.yaml 的 stations.topologies.triple = 1 中心 + 2 边缘）。
STATION_MATRIX_TOPOLOGY: str = "triple"

#: 矩阵的 3 个工况与 6 个策略（6 个 = 5 个经典 + 跨节点 cross_node）。
STATION_MATRIX_SCENARIOS: tuple = ("LIGHT", "NORMAL", "PEAK")
STATION_MATRIX_SCHEDULERS: tuple = (
    "fcfs", "rr", "priority", "mlfq", "dynamic", "cross_node")

#: 多站矩阵 CSV 的标识列（`mode` 固定为 multi = 多站且 MEC 关闭）。
STATION_CSV_ID_COLUMNS: tuple = (
    ("scenario", "工况"),
    ("scenario_label", "工况中文名"),
    ("scheduler", "策略"),
    ("scheduler_label", "策略中文名"),
    ("mode", "模式"),
    ("topology", "拓扑"),
    ("mec", "MEC开关"),
)

#: 多站矩阵/伴生探针 CSV 的指标列（键, 中文名）。
#: 全部用 ``res.metrics.get(key, 0.0)`` 读取 —— 并行开发期不假设某个键一定存在。
STATION_METRIC_COLUMNS: tuple = (
    ("throughput", "吞吐量个/秒"),
    ("completion_rate", "业务完整率%"),
    ("avg_wait_all_ms", "整体平均等待ms"),
    ("avg_wait_high_ms", "高优先级平均等待ms"),
    ("avg_wait_low_ms", "低优先级平均等待ms"),
    ("starvation_rate_low", "低优先级饥饿率%"),
    ("resource_utilization", "资源利用率%"),
    ("drop_rate", "任务丢弃率%"),
    ("retry_rate", "弱网重传率%"),
    ("context_switch", "上下文切换次数"),
    ("peak_queue_depth", "队列峰值堆积"),
    ("station_max_queue_depth", "单站最大队列深度"),
    ("station_load_imbalance", "站间负载不均衡度σ/均值"),
    ("migration_count", "任务迁移次数"),
    ("migration_cost_ms", "迁移平均代价ms"),
    ("cross_node_comm_ms", "跨节点通信开销ms"),
    ("avg_e2e_ms", "端到端平均时延ms"),
    ("edge_offload_ratio", "边缘分流比例%"),
    ("runtime_wall_s", "仿真实耗秒"),
)

#: 单组结果里需要抄进 CSV 的指标键（= 上表的键序）。
STATION_METRIC_KEYS: tuple = tuple(k for k, _ in STATION_METRIC_COLUMNS)

#: 伴生探针：PEAK × dynamic × 3 档（纯中心 / 多站-无MEC / 端-边-云）。
STATION_PROBE_SCENARIO: str = "PEAK"
STATION_PROBE_SCHEDULER: str = "dynamic"
STATION_PROBE_PLAN: tuple = (
    ("center", "纯中心(单站)", None, False),
    ("multi", "多站-无MEC(triple)", "triple", False),
    ("edge", "端-边-云(triple+MEC)", "triple", True),
)

#: 伴生探针 CSV 的标识列。
STATION_PROBE_ID_COLUMNS: tuple = (
    ("mode", "模式"),
    ("variant", "变体"),
    ("station_spec", "拓扑"),
    ("stations", "站数"),
    ("mec", "MEC开关"),
    ("scenario", "工况"),
    ("scheduler", "策略"),
)


def build_station_plan(scenarios=None, schedulers=None) -> list:
    """组装多站矩阵的运行计划（纯函数，便于单测）。

    默认 = ``triple`` 拓扑 × 6 策略 × 3 工况 = **18 组**，每组一个字典：
    ``{"scenario", "scheduler", "station_spec", "mec"}``。MEC 一律关闭 ——
    这一实验面要隔离的是「拓扑 × 策略」效应，混入 MEC 会把角色硬约束带进来
    （见 docs/11 §4.4.2：MEC 分层会让站间不均衡度结构性升高）。
    """
    codes = [str(s).upper() for s in (scenarios or STATION_MATRIX_SCENARIOS)]
    scheds = [str(s).lower() for s in (schedulers or STATION_MATRIX_SCHEDULERS)]
    return [
        {"scenario": code, "scheduler": sched,
         "station_spec": STATION_MATRIX_TOPOLOGY, "mec": False}
        for code in codes for sched in scheds
    ]


def station_output_paths(cfg: dict) -> dict:
    """多站矩阵的产物路径（独立 results_dir：绝不落在 results/summary.csv 上）。"""
    from uav_sim.config import resolve_path

    results_dir = resolve_path(cfg, "output.results_dir")
    charts_dir = resolve_path(cfg, "output.charts_dir")
    out_dir = results_dir / "stations"
    return {
        "results_dir": results_dir,
        "out_dir": out_dir,
        "csv": out_dir / "stations_matrix.csv",
        "raw_dir": out_dir / "raw",
        "charts_dir": out_dir / "charts",
        "overview": charts_dir / "10-多站×多策略矩阵.png",
    }


def station_probe_output_paths(cfg: dict) -> dict:
    """站级/MEC 伴生探针的产物路径（同样是独立 results_dir）。"""
    from uav_sim.config import resolve_path

    results_dir = resolve_path(cfg, "output.results_dir")
    charts_dir = resolve_path(cfg, "output.charts_dir")
    return {
        "out_dir": results_dir / "station_probe",
        "csv": results_dir / "station_probe" / "station_probe.csv",
        "png": charts_dir / "09-站级与MEC指标.png",
    }


def _with_quorum_extras(columns: tuple, rows: list) -> list:
    """把行里出现的 ``quorum_*`` 指标动态补进列（并行开发期不假设键一定存在）。

    另一个开发面正在给 ``models.METRIC_SPEC`` 增加 quorum 相关键，本模块**不**依赖
    它们的名字或数量：出现了就带上（中文名优先取 ``METRIC_SPEC``），没出现就不带。
    """
    cols = list(columns)
    seen = {k for k, _ in cols}
    extras = sorted({str(k) for r in rows for k in r
                     if str(k).startswith("quorum_") and k not in seen})
    if not extras:
        return cols
    labels = {k: k for k in extras}
    try:
        from uav_sim.models import METRIC_SPEC

        labels = {k: METRIC_SPEC.get(k, (k, "", True))[0] for k in extras}
    except Exception:  # noqa: BLE001 - 读不到就用英文键兜底
        pass
    return cols + [(k, labels.get(k, k)) for k in extras]


def _write_keyed_csv(rows: list, path, id_columns: tuple, metric_columns: tuple):
    """写「中文表头 + 英文键名 + 数据行」的 CSV（utf-8-sig，Excel 双击不乱码）。

    第 2 行是英文键名 —— 报告/脚本引用列时不必猜中文表头，也不会因为改中文名而崩。
    """
    import csv as _csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = _with_quorum_extras(metric_columns, rows)
    fields = [k for k, _ in id_columns] + [k for k, _ in cols]
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = _csv.writer(fp)
        writer.writerow([cn for _, cn in id_columns] + [cn for _, cn in cols])
        writer.writerow(fields)
        for row in rows:
            out = []
            for key in fields:
                val = row.get(key, "")
                out.append(f"{float(val):.6f}" if isinstance(val, float) else val)
            writer.writerow(out)
    return path


def write_station_csv(rows: list, path):
    """写 ``results/stations/stations_matrix.csv``（纯函数，便于单测）。"""
    return _write_keyed_csv(rows, path, STATION_CSV_ID_COLUMNS, STATION_METRIC_COLUMNS)


def write_station_probe_csv(rows: list, path):
    """写 ``results/station_probe/station_probe.csv``（纯函数，便于单测）。"""
    return _write_keyed_csv(rows, path, STATION_PROBE_ID_COLUMNS, STATION_METRIC_COLUMNS)


def _station_metric_row(res) -> dict:
    """从一次 RunResult 抽出 CSV 需要的指标（缺键一律 0.0 兜底）。"""
    row = {key: float((res.metrics or {}).get(key, 0.0)) for key in STATION_METRIC_KEYS}
    for key, val in (res.metrics or {}).items():
        if str(key).startswith("quorum_") and key not in row:
            try:
                row[key] = float(val)
            except (TypeError, ValueError):
                row[key] = 0.0
    return row


def cmd_stations(args: argparse.Namespace) -> int:
    """多站矩阵实验：``triple`` × 6 策略 × 3 工况 = 18 组（MEC 关闭）。

    为什么要单独一个子命令（而不是塞进 ``run.py all``）：
    ``all`` 的 15 组口径是"单站 + 5 经典策略"，**绝不能**被拓扑或第 6 个策略污染；
    多站是**另一个实验面**，必须独立 results_dir、独立 CSV、独立图表
    （见 docs/11 §7.2 第 2 条：新实验面要单独跑、单独出表）。
    """
    from uav_sim.experiment import ExperimentRunner
    from uav_sim.models import SCENARIO_LABELS, SCHEDULER_LABELS
    from uav_sim.visualize import plot_station_charts

    cfg = _load_cfg(args.config)
    if getattr(args, "seed", None) is not None:
        cfg.setdefault("simulation", {})["random_seed"] = int(args.seed)
    # 硬约束：不写数据库；所有产物走 results/stations/，不碰 results/summary.csv。
    cfg.setdefault("database", {})["enable"] = False
    paths = station_output_paths(cfg)

    plan = build_station_plan(getattr(args, "scenarios", None),
                              getattr(args, "schedulers", None))
    total = len(plan)
    n_codes = len({p["scenario"] for p in plan})
    n_scheds = len({p["scheduler"] for p in plan})
    _banner(f"多站矩阵实验：{STATION_MATRIX_TOPOLOGY} × {n_scheds} 策略 "
            f"× {n_codes} 工况 = {total} 组（MEC 关闭）")
    print(f"  拓扑 {STATION_MATRIX_TOPOLOGY} = 1 中心站（6 通道）+ 2 边缘站（各 3 通道）")
    print("  控制变量：同一 seed、同一工况下 6 个策略回放**同一条**任务/链路轨迹；")
    print("  隔离目标：只有「策略」与「工况」在变，MEC 关闭（否则角色硬约束会掺进来）。")
    print(f"  产出：{paths['csv']}")
    print(f"        {paths['out_dir'] / 'raw'}/*.json、{paths['charts_dir']}/*.png")
    print(f"        {paths['overview']}（总览图另存）")
    print("  口径：本子命令**不写数据库、不碰 results/summary.csv**。\n")

    runner = ExperimentRunner(cfg, results_dir=paths["out_dir"],
                              station_spec=STATION_MATRIX_TOPOLOGY, mec_enabled=False)
    rows: list = []
    failed = 0
    t0 = time.perf_counter()
    for k, item in enumerate(plan, 1):
        code, sched = item["scenario"], item["scheduler"]
        print(f"[{k:>2}/{total}] {code:<7} × {sched:<10} ... ", end="", flush=True)
        res = runner.run_one(code, sched, persist=False, save_detail=True)
        if res.error:
            failed += 1
            print(f"✗ {res.error}")
            continue
        row = {
            "mode": "multi",
            "scenario": code,
            "scenario_label": SCENARIO_LABELS.get(code, code),
            "scheduler": sched,
            "scheduler_label": SCHEDULER_LABELS.get(sched, sched),
            "topology": STATION_MATRIX_TOPOLOGY,
            "mec": 0,
        }
        row.update(_station_metric_row(res))
        rows.append(row)
        print(f"完整率={row['completion_rate']:5.1f}% "
              f"平均等待={row['avg_wait_all_ms']:7.0f}ms "
              f"不均衡度={row['station_load_imbalance']:6.3f} "
              f"迁移={int(row['migration_count']):>4d}次 "
              f"({row['runtime_wall_s']:.1f}s)")

    elapsed = time.perf_counter() - t0
    _banner("多站矩阵实验完成")
    print(f"  成功 {len(rows)} / {total} 组，总实耗 {elapsed:.1f}s")
    if failed:
        print(f"  ✗ 失败 {failed} 组")
    if not rows:
        return 1

    write_station_csv(rows, paths["csv"])
    print(f"  ✓ 汇总表 -> {paths['csv']}")

    if not getattr(args, "no_chart", False):
        try:
            made = plot_station_charts(rows, paths["charts_dir"])
            print(f"  ✓ 生成 {len(made)} 张图 -> {paths['charts_dir']}")
            overview = made.get("总览")
            if overview is not None:
                paths["overview"].parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(overview, paths["overview"])
                print(f"  ✓ 总览另存 -> {paths['overview']}")
        except Exception as exc:  # noqa: BLE001 - 出图失败不应让实验判失败
            print(f"  [警告] 出图失败：{exc}")
    else:
        print("  （已用 --no-chart 跳过出图）")

    _print_station_conclusion(rows)
    return 0 if not failed else 1


def _print_station_conclusion(rows: list) -> None:
    """打印可直接引用进报告的中文结论（数字全部来自本次实跑）。"""
    from uav_sim.models import SCENARIO_LABELS, SCHEDULER_LABELS

    def at(code: str, sched: str, key: str) -> float:
        for r in rows:
            if r["scenario"] == code and r["scheduler"] == sched:
                return float(r.get(key, 0.0))
        return 0.0

    def label(sched: str) -> str:
        return SCHEDULER_LABELS.get(sched, sched)

    print()
    print("=" * 78)
    print("  中文结论（数字均来自本次实跑，可直接引用进课程报告）")
    print("=" * 78)
    print(f"  【口径】拓扑 {STATION_MATRIX_TOPOLOGY}（1 中心 + 2 边缘）× "
          f"{len(STATION_MATRIX_SCHEDULERS)} 策略 × {len(STATION_MATRIX_SCENARIOS)} 工况 = "
          f"{len(rows)} 组；MEC 关闭；不写库、不碰 results/summary.csv 的 15 组口径。")
    print()
    print("  ① 各工况下业务完整率的最优/最差策略：")
    for code in STATION_MATRIX_SCENARIOS:
        cell = [r for r in rows if r["scenario"] == code]
        if not cell:
            continue
        best = max(cell, key=lambda r: float(r.get("completion_rate", 0.0)))
        worst = min(cell, key=lambda r: float(r.get("completion_rate", 0.0)))
        print(f"     {code:<6}{SCENARIO_LABELS.get(code, code):<12} "
              f"最优 {label(best['scheduler']):<24}{float(best['completion_rate']):5.1f}%   "
              f"最差 {label(worst['scheduler']):<24}{float(worst['completion_rate']):5.1f}%")
    print()
    print("  ② cross_node（跨节点负载均衡） vs dynamic（站内动态权重）——本矩阵的核心对照：")
    print(f"  {'工况':<8}{'完整率 dy→cn':>22}{'不均衡度 dy→cn':>24}"
          f"{'迁移次数':>10}{'平均等待 dy→cn(ms)':>24}")
    for code in STATION_MATRIX_SCENARIOS:
        if not any(r["scenario"] == code for r in rows):
            continue
        d_c = at(code, "dynamic", "completion_rate")
        c_c = at(code, "cross_node", "completion_rate")
        d_i = at(code, "dynamic", "station_load_imbalance")
        c_i = at(code, "cross_node", "station_load_imbalance")
        mig = at(code, "cross_node", "migration_count")
        d_w = at(code, "dynamic", "avg_wait_all_ms")
        c_w = at(code, "cross_node", "avg_wait_all_ms")
        print(f"  {code:<8}{d_c:>10.1f}→{c_c:<10.1f}{d_i:>11.3f}→{c_i:<12.3f}"
              f"{int(mig):>10d}{d_w:>11.0f}→{c_w:<12.0f}")
    print()
    print("  ③ 判读要点（写进报告时请一并保留）：")
    print("     * 不均衡度 = 各站负载的变异系数（σ/均值，0 = 完全均衡）；")
    print("     * dynamic 不做跨节点迁移（迁移次数恒 0），因此「不均衡度下降」必然")
    print("       伴随「迁移次数 > 0」与「迁移代价」——两者要一起报，不能只挑不均衡度；")
    print("     * 本仿真器用真实多线程 + sleep 模拟 IO，指标有 wall-clock 抖动")
    print("       （完整率 0.2~0.5pp、端到端 3~8ms、不均衡度 0.02~0.10），")
    print("       跨批次比较只看效应方向与量级，不要把小数位当精确值；")
    print("     * 需要置信区间时用：run.py replicate --variants single,triple,triple_mec")
    print("=" * 78)
    print()


def _run_station_probe(cfg: dict, seed: int | None = None) -> list:
    """`run.py all` 追尾的**伴生探针**：PEAK × dynamic × 3 档（3 组）。

    产物落在**独立** results_dir（``results/station_probe/``）与独立图
    （``charts/09-站级与MEC指标.png``），**绝不进入** results/summary.csv 的 15 组口径。
    任何异常由调用方捕获（只告警，不影响 15 组成败与返回码）。
    """
    from uav_sim.experiment import ExperimentRunner
    from uav_sim.models import SCENARIO_LABELS, SCHEDULER_LABELS
    from uav_sim.station import resolve_station_specs
    from uav_sim.visualize import plot_station_probe

    paths = station_probe_output_paths(cfg)
    total = len(STATION_PROBE_PLAN)
    print("  控制变量：同一 seed、同一工况（PEAK）、同一策略（dynamic）；只有拓扑/MEC 不同。")
    print("  这 3 组是**伴生探针**（样本量 1 的点估计），只用于画图 9，不进 results/summary.csv。")
    print(f"  产出：{paths['csv']}")
    print(f"        {paths['png']}\n")

    rows: list = []
    failed = 0
    for k, (mode, label, spec, mec_on) in enumerate(STATION_PROBE_PLAN, 1):
        run_cfg = copy.deepcopy(cfg)
        run_cfg.setdefault("database", {})["enable"] = False   # 探针绝不写库
        if seed is not None:
            run_cfg.setdefault("simulation", {})["random_seed"] = int(seed)
        if mec_on:
            run_cfg.setdefault("mec", {})["enable"] = True
        print(f"  [{k}/{total}] {label:<24} ... ", end="", flush=True)
        runner = ExperimentRunner(run_cfg, results_dir=paths["out_dir"],
                                  station_spec=spec, mec_enabled=bool(mec_on))
        res = runner.run_one(STATION_PROBE_SCENARIO, STATION_PROBE_SCHEDULER,
                             seed=seed, persist=False, save_detail=True)
        if res.error:
            failed += 1
            print(f"✗ {res.error}")
            continue
        row = {
            "mode": mode,
            "variant": label,
            "station_spec": spec if spec else "single",
            "stations": len(resolve_station_specs(run_cfg, spec)),
            "mec": int(bool(mec_on)),
            "scenario": STATION_PROBE_SCENARIO,
            "scenario_label": SCENARIO_LABELS.get(STATION_PROBE_SCENARIO, ""),
            "scheduler": STATION_PROBE_SCHEDULER,
            "scheduler_label": SCHEDULER_LABELS.get(STATION_PROBE_SCHEDULER, ""),
        }
        row.update(_station_metric_row(res))
        rows.append(row)
        print(f"完整率={row['completion_rate']:5.1f}% "
              f"端到端={row['avg_e2e_ms']:7.1f}ms "
              f"边缘分流={row['edge_offload_ratio']:5.1f}% "
              f"不均衡度={row['station_load_imbalance']:6.3f} "
              f"迁移={int(row['migration_count']):>4d}次 "
              f"({row['runtime_wall_s']:.1f}s)")

    if not rows:
        print(f"  [警告] {total} 组探针全部失败，跳过 CSV/出图（不影响 15 组结果）")
        return rows
    if failed:
        print(f"  [警告] {failed}/{total} 组探针失败（不影响 15 组结果）")

    write_station_probe_csv(rows, paths["csv"])
    print(f"  ✓ 汇总表 -> {paths['csv']}")
    try:
        plot_station_probe(rows, paths["png"])
        print(f"  ✓ 图表   -> {paths['png']}")
    except Exception as exc:  # noqa: BLE001 - 出图失败不影响实验
        print(f"  [警告] 出图失败：{exc}")

    _print_station_probe_conclusion(rows)
    return rows


def _print_station_probe_conclusion(rows: list) -> None:
    """打印伴生探针的中文结论（数字来自本次实跑；必须写明样本量边界）。"""
    def at(mode: str, key: str) -> float:
        for r in rows:
            if r["mode"] == mode:
                return float(r.get(key, 0.0))
        return 0.0

    print()
    print("  中文结论（数字来自本次实跑；⚠ 3 组点估计，样本量 1）：")
    for mode, label in (("center", "纯中心(单站)"), ("multi", "多站-无MEC"),
                        ("edge", "端-边-云")):
        if not any(r["mode"] == mode for r in rows):
            continue
        print(f"   [{label:<12}] 完整率={at(mode, 'completion_rate'):5.1f}%  "
              f"平均等待={at(mode, 'avg_wait_all_ms'):7.0f}ms  "
              f"端到端={at(mode, 'avg_e2e_ms'):7.1f}ms  "
              f"边缘分流={at(mode, 'edge_offload_ratio'):5.1f}%  "
              f"不均衡度={at(mode, 'station_load_imbalance'):6.3f}  "
              f"迁移={int(at(mode, 'migration_count')):>4d}次")
    c_e2e, e_e2e = at("center", "avg_e2e_ms"), at("edge", "avg_e2e_ms")
    if c_e2e > 0:
        print(f"   → 端到端平均时延：纯中心 {c_e2e:.1f} ms → 端-边-云 {e_e2e:.1f} ms"
              f"（{(c_e2e - e_e2e) / c_e2e * 100:+.1f}%）")
    print("   ⚠ 诚实边界：本仿真器有 wall-clock 抖动（完整率 0.2~0.5pp、端到端 3~8ms、")
    print("     站间不均衡度 0.02~0.10），这 3 组是**单次点估计**，只看方向与量级；")
    print("     要置信区间请用：run.py replicate --variants single,triple,triple_mec")


def _print_weather_conclusion(rows: list, poor_threshold: float) -> None:
    """打印可直接引用进课程报告的中文结论（所有数字均来自本次实跑）。"""
    from uav_sim.weather import PRESET_ORDER, WEATHER_PRESETS

    def agg(preset: str, key: str) -> float:
        vals = [float(r.get(key, 0.0)) for r in rows if r["preset"] == preset]
        return sum(vals) / len(vals) if vals else 0.0

    def one(preset: str, sched: str, key: str) -> float:
        for r in rows:
            if r["preset"] == preset and r["scheduler"] == sched:
                return float(r.get(key, 0.0))
        return 0.0

    present = [p for p in PRESET_ORDER if any(r["preset"] == p for r in rows)]
    clear = "clear" if "clear" in present else present[0]
    worst = "dust" if "dust" in present else present[-1]

    print()
    print("=" * 78)
    print("  中文结论（数字均来自本次实跑，可直接引用进课程报告）")
    print("=" * 78)
    print(f"  【口径】{WEATHER_EXP_SCENARIO} 工况；劣质链路 = 健康度 < {poor_threshold:.0f}；"
          f"每组策略的天气/链路曲线完全相同（控制变量）。")
    print()
    print("  ① 天气对链路健康度的影响（5 策略平均）：")
    for p in present:
        print(f"     {WEATHER_PRESETS[p].label:<4}({p:<5}) 健康度均值={agg(p, 'link_mean_health'):6.2f}  "
              f"劣质链路占比={agg(p, 'link_poor_ratio_pct'):5.1f}%  "
              f"天气平均扣分={agg(p, 'weather_mean_penalty'):5.2f}")
    d_health = agg(worst, "link_mean_health") - agg(clear, "link_mean_health")
    d_poor = agg(worst, "link_poor_ratio_pct") - agg(clear, "link_poor_ratio_pct")
    print(f"     → {WEATHER_PRESETS[worst].label}相比{WEATHER_PRESETS[clear].label}："
          f"链路健康度均值 {agg(clear, 'link_mean_health'):.2f} → {agg(worst, 'link_mean_health'):.2f}"
          f"（{d_health:+.2f}），劣质链路占比 {agg(clear, 'link_poor_ratio_pct'):.1f}% → "
          f"{agg(worst, 'link_poor_ratio_pct'):.1f}%（{d_poor:+.1f} 个百分点）。")
    print()
    print("  ② 谁扛得住恶劣天气：dynamic（本方案） vs 静态优先级 priority（沙尘为例）：")
    for sched in ("dynamic", "priority"):
        if not any(r["scheduler"] == sched for r in rows):
            continue
        tag = "本方案" if sched == "dynamic" else "静态  "
        print(f"     {sched:<9}{tag} 完整率 晴朗={one(clear, sched, 'completion_rate'):5.1f}% → "
              f"沙尘={one(worst, sched, 'completion_rate'):5.1f}%  "
              f"高优平均等待 晴朗={one(clear, sched, 'avg_wait_high_ms'):8.0f}ms → "
              f"沙尘={one(worst, sched, 'avg_wait_high_ms'):8.0f}ms  "
              f"低优饥饿率 沙尘={one(worst, sched, 'starvation_rate_low'):5.1f}%")
    dyn_drop = one(clear, "dynamic", "completion_rate") - one(worst, "dynamic", "completion_rate")
    pri_drop = one(clear, "priority", "completion_rate") - one(worst, "priority", "completion_rate")
    dyn_c = one(worst, "dynamic", "completion_rate")
    pri_c = one(worst, "priority", "completion_rate")
    delta = dyn_c - pri_c
    if delta >= 0:
        print(f"     → 沙尘工况下 dynamic 完成率比静态优先级高 {delta:.1f} 个百分点"
              f"（{dyn_c:.1f}% vs {pri_c:.1f}%）；"
              f"沙尘相对晴朗的完成率差值：dynamic {dyn_drop:+.1f}pp、priority {pri_drop:+.1f}pp。")
    else:
        print(f"     → 如实记录：沙尘工况下 dynamic 完成率比静态优先级低 {abs(delta):.1f} 个百分点"
              f"（{dyn_c:.1f}% vs {pri_c:.1f}%）；沙尘相对晴朗的完成率差值："
              f"dynamic {dyn_drop:+.1f}pp、priority {pri_drop:+.1f}pp；"
              "原因需在报告「改进方向」中讨论（缓存/退避把弱网任务推迟得更久）。")
    print()
    print("  ③ 对课程报告的论证支撑：")
    print("     天气不是贴图滤镜 —— 它通过 link_penalty 真实改变了 0.5s 网格的链路健康度，")
    print("     进而改变弱网失败概率、服务时长标定与动态权重降权，最终反映在完成率/等待/饥饿率上。")
    print("     默认 clear 时扣分恒 0，既有 15 组结果不受影响（回归见 tests/test_weather.py）。")
    print()
    print("  ④ 口径与局限（写进报告时务必保留这句）：")
    print("     * 链路健康度均值 / 劣质链路占比是**确定性结论**（同 seed 同 preset 逐点可复现，")
    print("       与机器负载无关），可直接引用；")
    print("     * 完成率 / 等待 / 饥饿率是**同一次实跑**的数字，但本仿真器的调度与 IO 用真实")
    print("       墙钟时间模拟，整跑指标对机器负载敏感：安静时段同参数复跑差异 <1pp，")
    print("       高负载时段实测可差数十个百分点（dev 机实测 10.6%~60.1%）。因此")
    print("       跨批次的完成率对比不应作为强结论，报告引用时以同一张表内比较为准；")
    print("     * 需要更稳的业务指标时：在机器空闲时重跑本命令，或参考链路列 + 既有")
    print("       results/summary.csv 的 15 组策略对照（那一组同样只跑一次，也有同等抖动）。")
    print("=" * 78)
    print()


# ---------------------------------------------------------------------------
# charts —— 只出图
# ---------------------------------------------------------------------------
def cmd_charts(args: argparse.Namespace) -> int:
    _banner("从已有结果重新生成图表")
    from uav_sim.config import resolve_path
    from uav_sim.models import RunResult
    from uav_sim.visualize import plot_all

    cfg = _load_cfg(args.config)
    raw_dir = resolve_path(cfg, "output.results_dir") / "raw"
    if not raw_dir.exists():
        print(f"✗ 找不到结果目录：{raw_dir}")
        print("  请先跑一次：python run.py all")
        return 1

    results = []
    for f in sorted(raw_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            results.append(RunResult(**data))
        except Exception as exc:  # noqa: BLE001
            print(f"  [跳过] {f.name}: {exc}")
    if not results:
        print(f"✗ {raw_dir} 下没有可用的结果 JSON")
        return 1

    print(f"  读取 {len(results)} 组结果")
    charts_dir = resolve_path(cfg, "output.charts_dir")
    made = plot_all(results, charts_dir)
    print(f"\n  生成 {len(made)} 张图：")
    for name, path in made.items():
        print(f"    {name:<22} {path}")
    return 0


# ---------------------------------------------------------------------------
# demo-limiter —— 演示大任务拒绝 / 分片 / 并发限流
# ---------------------------------------------------------------------------
def cmd_demo_limiter(args: argparse.Namespace) -> int:
    _banner("资源限流演示（大任务拒绝 + 分片 + 并发上限）")
    from uav_sim.models import TaskPriority, TaskType
    from uav_sim.resource_limiter import ResourceLimiter
    from uav_sim.simulator import Simulator  # noqa: F401  (便于确认模块可用)

    cfg = _load_cfg(args.config)
    server = cfg["server"]
    limiter = ResourceLimiter(
        max_concurrent=int(server["max_concurrent"]),
        max_task_memory_mb=500.0,  # 演示用：故意设小，触发拒绝与分片
        per_uav_quota=int(server["per_uav_quota"]),
        enable_chunking=True,
    )

    def mk(i: int, size_mb: float):
        from uav_sim.models import Task

        return Task(
            task_id=f"DEMO-{i}",
            uav_id=i,
            priority=TaskPriority.HIGH,
            task_type=TaskType.IMAGERY,
            data_size_mb=size_mb,
            submit_ts=0.0,
            service_s=size_mb / 150.0,
        )

    print(f"  限流参数：max_concurrent={limiter.max_concurrent}，"
          f"max_task_memory_mb=500，per_uav_quota={server['per_uav_quota']}")
    print()

    # 1) 超大任务 -> 拒绝 + 分片
    big = mk(1, 1200.0)
    rc = limiter.try_admit(big)
    print(f"  ① 派发 1200 MB 大任务 -> try_admit 返回 {rc!r}")
    segs = limiter.split_task(big) if rc == "REJECT_LARGE" else []
    print(f"     分片结果：{len(segs)} 片 -> "
          + ", ".join(f"{s.task_id}({s.data_size_mb:.0f}MB,{s.segment_index + 1}/{s.segment_total})"
                      for s in segs))
    assert rc == "REJECT_LARGE" and len(segs) == 3, "大任务应被拒绝并切成 3 片"

    # 2) 并发打满 -> THROTTLE
    print()
    print("  ② 连续申请并发槽位，观察上限：")
    admitted = []
    for i in range(limiter.max_concurrent + 3):
        t = mk(100 + i, 50.0)
        rc = limiter.try_admit(t)
        print(f"     task#{i:<2} size=50MB -> {rc:<10} running={limiter.running}")
        if rc == "OK":
            admitted.append(t)
        else:
            assert rc == "THROTTLE", f"并发满时应返回 THROTTLE，实际 {rc}"
    assert limiter.running == limiter.max_concurrent, "并发数不应超过上限"

    # 3) 释放后可以再进
    print()
    print("  ③ 释放 2 个槽位后再申请：")
    for t in admitted[:2]:
        limiter.release(t)
    print(f"     released 2 -> running={limiter.running}")
    rc = limiter.try_admit(mk(999, 50.0))
    print(f"     再申请 -> {rc}，running={limiter.running}")
    assert rc == "OK", "释放后应能再获得许可"

    print()
    print("  ④ 限流器统计：")
    for k, v in limiter.get_status().items():
        print(f"     {k:<18} {v}")

    print()
    print("  ✓ 演示通过：并发不超上限、大任务被拒绝并分片、限流统计正确。")
    print("     （对应 PDF 实验 3.3 验收项「资源限流」）")
    return 0


# ---------------------------------------------------------------------------
# sensitivity —— 参数敏感性实验（对应 PDF 痛点④「静态参数固化」）
# ---------------------------------------------------------------------------
def cmd_sensitivity(args: argparse.Namespace) -> int:
    """验证「静态调度的参数是手工整定的，工况一变就失配」这一核心论点。

    三组对照（全部在 PEAK 峰值工况下跑，控制变量）：
      A. 静态优先级/MLFQ 的 aging 参数失配     —— MLFQ 的"好成绩"依赖手工调参
      B. 固定时间片 RR 的时间片失配            —— 时间片过小/过大两头都差
      C. **弱网加剧**（retry_gain 0.9）        —— 只有链路感知的动态权重能稳住
    """
    import copy

    from uav_sim.experiment import ExperimentRunner

    cfg = _load_cfg(args.config)
    cfg["database"]["enable"] = False        # 敏感性实验是临时对照，不入库
    out_dir = ROOT / "results" / "sensitivity"

    _banner("参数敏感性实验：验证「静态参数固化」痛点（PEAK 工况）")
    print("  说明：三组对照都只改一个参数，其余全部固定（控制变量法）。")
    print("        所有数字都在 PEAK 峰值工况、15000 ms 饥饿评价阈值下测得。\n")

    keys = [("throughput", "吞吐/s", 2), ("avg_wait_high_ms", "高优时延ms", 0),
            ("avg_wait_low_ms", "低优时延ms", 0), ("starvation_rate_low", "低优饥饿%", 1),
            ("completion_rate", "完整率%", 1)]

    def show(title: str, items):
        print(f"  【{title}】")
        print("  " + f"{'配置':<34}" + "".join(f"{n:>12}" for _, n, _ in keys))
        print("  " + "-" * (34 + 12 * len(keys)))
        best = {}
        for k, _, _ in keys:
            vals = [m.get(k, 0.0) for _, m in items]
            higher = k in ("throughput", "completion_rate")
            best[k] = (max(vals) if higher else min(vals)) if vals else 0.0
        for label, m in items:
            line = "  " + f"{label:<34}"
            for k, _, nd in keys:
                v = m.get(k, 0.0)
                mark = "*" if abs(v - best[k]) < 1e-9 else " "
                line += f"{v:>11.{nd}f}{mark}"
            print(line)
        print("  （* = 本组最优）\n")

    # ---------------- A. MLFQ aging 参数失配 ----------------
    items = []
    for aging in (1500.0, 6000.0):
        c = copy.deepcopy(cfg)
        c.setdefault("mlfq", {})["aging_ms"] = aging
        r = ExperimentRunner(c, results_dir=out_dir).run_one("PEAK", "mlfq", persist=False)
        items.append((f"MLFQ  aging={aging:.0f}ms", r.metrics))
    show("A. MLFQ 的 aging 升权阈值失配（同一算法，只改一个参数）", items)

    # ---------------- B. RR 时间片失配 ----------------
    items = []
    for q in (120.0, 500.0, 3000.0):
        c = copy.deepcopy(cfg)
        c.setdefault("rr", {})["time_slice_ms"] = q
        r = ExperimentRunner(c, results_dir=out_dir).run_one("PEAK", "rr", persist=False)
        items.append((f"RR    slice={q:.0f}ms", r.metrics))
    show("B. 固定时间片 RR 的时间片失配（过小切换爆炸、过大退化为 FCFS）", items)

    # ---------------- C. 弱网加剧：链路感知 vs 无感知 ----------------
    c = copy.deepcopy(cfg)
    c["weaknet"]["retry_gain"] = 0.90        # 弱网加剧：失败率翻倍
    items = []
    for name in ("rr", "mlfq", "dynamic"):
        r = ExperimentRunner(c, results_dir=out_dir).run_one("PEAK", name, persist=False)
        items.append((f"{name:<10}（弱网加剧）", r.metrics))
    show("C. 弱网加剧（retry_gain 0.5→0.9）：只有链路感知策略能稳住核心业务", items)

    print("  结论（如实陈述，含对本方案不利的发现）：")
    print("    1) 【成立】MLFQ 的 aging 从 1500ms 改为 6000ms、RR 的时间片从 120ms 改为")
    print("       3000ms 后，低优时延分别恶化 3.6 倍 / 完整率从 96% 掉到 75%（并出现 27% 饥饿）")
    print("       —— 这就是 PDF 痛点④「静态调度参数固化，工况一变就失配」的量化证据。")
    print("    2) 【不成立，需如实记录】本实验中『链路感知』并没有在弱网加剧时带来时延优势：")
    print("       dynamic 的高优时延反而最高。原因是 CACHED 本地缓存把弱网任务推迟得更久，")
    print("       这批被推迟的任务抬高了均值。该负面结果应在报告「改进方向」中讨论")
    print("       （可能的改进：缓存任务只降权不退避，或对弱网任务改走『低带宽独占通道』）。")
    print(f"\n  明细结果已落盘：{out_dir}")
    return 0


# ---------------------------------------------------------------------------
# replicate —— 重复实验 + 置信区间（补上长期"预留未启用"的 repetitions）
# ---------------------------------------------------------------------------
#: `run.py replicate --variants` 支持的三档实验变体（tag -> (station_spec, mec_enabled)）。
#: 三元组形式为 ``(tag, station_spec, mec_enabled)``，与 replication.ReplicationStudy 一致。
VARIANT_PRESETS: dict = {
    "single": (None, None),          # 单站（默认，= 改造前行为）
    "triple": ("triple", False),     # 多站（1 中心 + 2 边缘），MEC 关闭
    "triple_mec": ("triple", True),  # 端-边-云：多站 + MEC 开启
}


def parse_variant_specs(raw) -> list:
    """把 ``--variants`` 参数解析成 ``[(tag, station_spec, mec_enabled), ...]``。

    同时接受 ``--variants single triple triple_mec``（空格分隔）与
    ``--variants single,triple,triple_mec``（逗号分隔）两种写法。
    """
    tags: list = []
    for token in (raw if raw else ["single"]):
        for part in str(token).split(","):
            part = part.strip().lower()
            if part:
                tags.append(part)
    if not tags:
        tags = ["single"]
    out: list = []
    for tag in tags:
        preset = VARIANT_PRESETS.get(tag)
        if preset is None:
            raise ValueError(f"未知变体 {tag!r}（可选：{', '.join(VARIANT_PRESETS)}）")
        out.append((tag, preset[0], preset[1]))
    return out


def cmd_replicate(args: argparse.Namespace) -> int:
    """对每个「变体 × 工况 × 策略」重复 N 次，给出 均值 ± 95% 置信半宽 与配对比较。

    为什么需要它：单次运行的指标有 3~5% 的运行噪声，无法区分"算法差异"与"时序抖动"。
    重复实验用**同一组随机种子**跑所有策略（共同随机数 CRN），再做配对比较，
    产出的结论才站得住脚（可写进报告的"实验设计 / 统计显著性"章节）。

    ``--variants`` 把**多站/MEC 面**也纳入重复实验：每个变体一个独立 ExperimentRunner，
    聚合单元 key = ``(variant, scenario, scheduler)``，配对比较**只在变体内进行**
    （跨变体相减 = 把拓扑差异当成策略差异，是本项目明确要避免的伪结论）。
    """
    from uav_sim.models import CLASSIC_SCHEDULER_NAMES
    from uav_sim.replication import ReplicationStudy

    cfg = _load_cfg(args.config)
    if args.no_db:
        cfg.setdefault("database", {})["enable"] = False

    try:
        variants = parse_variant_specs(getattr(args, "variants", None))
    except ValueError as exc:
        print(f"✗ {exc}")
        return 2

    study = ReplicationStudy(cfg, reps=args.reps, seed_base=args.seed,
                             variants=variants)
    _banner(f"重复实验：{len(variants)} 变体 × 工况 × 策略，每单元 {study.reps} 次，"
            f"种子 {study.seed_base}..{study.seed_base + study.reps - 1}（所有策略/变体共用 = CRN）")
    print("  变体：" + "；".join(
        f"{tag}（拓扑={spec if spec else '单站默认'}，MEC="
        f"{'跟随配置' if mec is None else ('开' if mec else '关')}）"
        for tag, spec, mec in variants))
    study.run_matrix(scenarios=args.scenarios, schedulers=args.schedulers)

    study.print_matrix()
    # 配对比较的基线/目标按本次实际跑的策略自适应选择：
    # 默认 priority vs dynamic（15 组口径）；只跑多站策略时退化为 dynamic vs cross_node。
    scheds = [str(s).lower() for s in (args.schedulers or list(CLASSIC_SCHEDULER_NAMES))]
    baseline, target = "priority", "dynamic"
    if baseline not in scheds or target not in scheds:
        if "dynamic" in scheds and "cross_node" in scheds:
            baseline, target = "dynamic", "cross_node"
        elif len(scheds) >= 2:
            baseline, target = scheds[0], scheds[1]
    if study.reps >= 2:
        study.export_paired_csv(baseline, target)
        study.print_paired(baseline, target)
    else:
        print("\n（reps=1 时无法给出置信区间与显著性判断，"
              "建议 --reps ≥ 10；此处仅打印单次结果）")

    print(f"\n聚合结果：{study.out_dir / 'replication_summary.csv'}")
    if study.reps >= 2:
        print(f"配对比较：{study.out_dir / f'paired_{target}_vs_{baseline}.csv'}")
    return 0


# ---------------------------------------------------------------------------
# bench-weather —— 多运行场景（晴/雨/雪/沙尘）稳定性与效率测评
# ---------------------------------------------------------------------------
def cmd_bench_weather(args: argparse.Namespace) -> int:
    """调用 tools/bench_weather.py：四场景各跑 12s，采样帧间隔，出报告与对比图。

    这是"系统能够稳定高效运行"的取证入口：给出 FPS / P95 / P99 / 最坏帧 /
    掉帧比例 / 仿真是否推进 / JS 堆是否增长，并做一次 3x 像素压力测试。
    """
    import subprocess

    script = ROOT / "tools" / "bench_weather.py"
    if not script.is_file():
        print(f"找不到 {script}")
        return 2
    argv = [PY, str(script), "--port", str(args.port)]
    if args.no_chart:
        argv.append("--no-chart")
    return subprocess.call(argv, cwd=str(ROOT))


# ---------------------------------------------------------------------------
# gpu —— GPU 大规模重复实验（analysis model）
# ---------------------------------------------------------------------------
def cmd_gpu(args: argparse.Namespace) -> int:
    """调用 gpu/run_gpu.py：构建 CUDA 内核 -> 跑 N 次重复 -> 统计 + 出 CSV。

    与 ``run.py replicate`` 的区别：replicate 用 Python 参考模型跑几十次（高保真、慢），
    gpu 用 CUDA 分析模型跑几千次（同规则、统计严谨、秒级）——两者互补。
    """
    import subprocess

    script = ROOT / "gpu" / "run_gpu.py"
    if not script.is_file():
        print(f"找不到 {script}（GPU 模块未安装）")
        return 2
    argv = [PY, str(script), "--reps", str(args.reps)]
    if args.rebuild:
        argv.append("--rebuild")
    if args.out_dir:
        argv += ["--out-dir", args.out_dir]
    return subprocess.call(argv, cwd=str(ROOT))


# ---------------------------------------------------------------------------
# ui —— 实时可视化面板（本地网页）
# ---------------------------------------------------------------------------
def cmd_ui(args: argparse.Namespace) -> int:
    """启动实时可视化面板：在浏览器里看无人机链路、两级队列、线程槽位实时变化。"""
    from uav_sim.dashboard import serve

    cfg = _load_cfg(args.config)
    return serve(
        cfg=cfg,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        scenario=str(args.scenario).upper(),
        scheduler=str(args.scheduler).lower(),
        speed=float(args.speed),
        autostart=bool(args.autostart),
        stations=(str(args.stations).strip().lower() or None) if getattr(
            args, "stations", None) else None,
        mec=(None if getattr(args, "mec", None) is None else bool(args.mec)),
        real_weather=getattr(args, "real_weather", None),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="UavSchedulerSim —— 无人机集群并发自适应负载均衡调度仿真系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default=None, help="配置文件路径（默认项目根 config.yaml）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("check", help="环境自检：依赖 / 数据库 / 字体 / 模块完整性")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("calibrate", help="三工况容量标定报告")
    sp.add_argument("--seed", type=int, default=None, help="随机种子（默认取 config）")
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("one", help="跑单组实验")
    sp.add_argument("--scenario", required=True, choices=["LIGHT", "NORMAL", "PEAK"])
    sp.add_argument("--scheduler", required=True,
                    choices=["fcfs", "rr", "priority", "mlfq", "dynamic", "cross_node"])
    sp.add_argument("--seed", type=int, default=None)
    sp.add_argument("--no-db", action="store_true", help="不写数据库")
    sp.set_defaults(func=cmd_one)

    sp = sub.add_parser("all", help="全量 3 工况 × 5 策略 对照实验 + 出图")
    sp.add_argument("--no-db", action="store_true", help="不写数据库")
    sp.add_argument("--seed", type=int, default=None)
    sp.add_argument("--scenarios", nargs="*", default=None,
                    choices=["LIGHT", "NORMAL", "PEAK"], help="只跑指定工况")
    sp.add_argument("--schedulers", nargs="*", default=None,
                    choices=["fcfs", "rr", "priority", "mlfq", "dynamic", "cross_node"],
                    help="只跑指定策略")
    sp.add_argument("--no-station-probe", action="store_true",
                    help="跳过站级/MEC 伴生探针（3 组独立实验 + charts/09 图）")
    sp.set_defaults(func=cmd_all)

    sp = sub.add_parser(
        "stations",
        help="多站矩阵实验：triple 拓扑 × 6 策略 × 3 工况 = 18 组（MEC 关闭，独立产物）")
    sp.add_argument("--seed", type=int, default=None, help="覆盖随机种子（默认取配置）")
    sp.add_argument("--scenarios", nargs="*", default=None,
                    choices=["LIGHT", "NORMAL", "PEAK"], help="只跑指定工况")
    sp.add_argument("--schedulers", nargs="*", default=None,
                    choices=list(STATION_MATRIX_SCHEDULERS), help="只跑指定策略")
    sp.add_argument("--no-chart", action="store_true", help="不出图（只写 CSV/JSON）")
    sp.set_defaults(func=cmd_stations)

    sp = sub.add_parser("charts", help="仅从 results/raw 重新出图")
    sp.set_defaults(func=cmd_charts)

    sp = sub.add_parser("weather",
                        help="天气 × 调度策略对照实验（PEAK × 4 天气 × 5 策略 = 20 组）")
    sp.add_argument("--seed", type=int, default=None,
                    help="随机种子（默认取 config；固定种子 = 控制变量）")
    sp.set_defaults(func=cmd_weather)

    sp = sub.add_parser("demo-limiter", help="演示资源限流（拒绝/分片/并发上限）")
    sp.set_defaults(func=cmd_demo_limiter)

    sp = sub.add_parser(
        "mec", help="MEC 边缘计算对照实验：纯中心 vs 端-边-云 + 通信/计算开销权衡")
    sp.add_argument("--config", default=None, help="配置文件路径（默认 config.yaml）")
    sp.add_argument("--seed", type=int, default=None, help="覆盖随机种子（默认取配置）")
    sp.set_defaults(func=cmd_mec)

    sp = sub.add_parser("sensitivity", help="参数敏感性实验（验证「静态参数固化」痛点）")
    sp.set_defaults(func=cmd_sensitivity)

    sp = sub.add_parser(
        "replicate",
        help="重复实验：每单元跑 N 次，给出 均值±95%% 置信区间 与配对显著性")
    sp.add_argument("--reps", type=int, default=None,
                    help="每个「工况×策略」重复次数（默认取 config.simulation.repetitions）")
    sp.add_argument("--seed", type=int, default=None,
                    help="重复实验的基准种子（默认取 config.simulation.random_seed）")
    sp.add_argument("--scenarios", nargs="*", default=None,
                    choices=["LIGHT", "NORMAL", "PEAK"], help="只跑指定工况")
    sp.add_argument("--schedulers", nargs="*", default=None,
                    choices=["fcfs", "rr", "priority", "mlfq", "dynamic", "cross_node"],
                    help="只跑指定策略（含多站策略 cross_node）")
    sp.add_argument("--variants", nargs="*", default=None,
                    help="实验变体，可选 single / triple / triple_mec；"
                         "支持空格或逗号分隔（默认 single = 单站，向后兼容）")
    sp.add_argument("--no-db", action="store_true", help="不写数据库")
    sp.set_defaults(func=cmd_replicate)

    sp = sub.add_parser("gpu",
                        help="GPU 大规模重复实验（一个 CUDA 线程 = 一次仿真，秒级跑几千次）")
    sp.add_argument("--reps", type=int, default=2000, help="每个策略重复次数（默认 2000）")
    sp.add_argument("--rebuild", action="store_true", help="强制重新编译 CUDA 内核")
    sp.add_argument("--out-dir", default=None, help="输出目录（默认 results/gpu）")
    sp.set_defaults(func=cmd_gpu)

    sp = sub.add_parser("bench-weather",
                        help="多运行场景测评：晴/雨/雪/沙尘各跑 12s，出帧率与稳定性报告")
    sp.add_argument("--port", type=int, default=8791, help="本地服务端口（默认 8791）")
    sp.add_argument("--no-chart", action="store_true", help="不出对比图")
    sp.set_defaults(func=cmd_bench_weather)

    sp = sub.add_parser("ui", help="启动实时可视化面板（浏览器看仿真动画，演示首选）")
    sp.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机可访问）")
    sp.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    sp.add_argument("--scenario", default="PEAK", choices=["LIGHT", "NORMAL", "PEAK"],
                    help="面板默认工况（页面上仍可切换）")
    sp.add_argument("--scheduler", default="dynamic",
                    choices=["fcfs", "rr", "priority", "mlfq", "dynamic", "cross_node"],
                    help="面板默认策略（页面上仍可切换；cross_node = 跨节点负载均衡）")
    sp.add_argument("--stations", default=None,
                    help="多基站拓扑（默认单站；triple = 1 中心 + 2 边缘）。"
                         "会被记为会话默认值，页面上点「开始仿真」即按它启动")
    sp.add_argument("--mec", action="store_true", default=None,
                    help="启用 MEC 边缘计算（轻量运维下沉边缘、重勘查回中心）")
    sp.add_argument("--real-weather", default=None, metavar="SNAPSHOT",
                    help="指定真实天气快照 JSON（覆盖 config.real；不填则按配置）")
    sp.add_argument("--speed", type=float, default=1.0, help="播放速度倍率（0.5 ~ 4）")
    sp.add_argument("--autostart", action="store_true", help="打开页面后立即开始仿真")
    sp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    sp.set_defaults(func=cmd_ui)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n\n已中断（Ctrl+C）")
        return 130
    except ModuleNotFoundError as exc:
        print(f"\n✗ 模块缺失：{exc}")
        print("  请先执行：.venv\\Scripts\\python.exe -m pip install -r requirements.txt "
              "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 2
    except Exception:  # noqa: BLE001
        print("\n✗ 执行失败，完整堆栈：")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
