"""
visualize.py —— 实验数据可视化（matplotlib）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 给运维负责人看的"一张图看懂调度效果"：吞吐、时延、饥饿、利用率、完整率 |
| OS 原理层   | 性能评价的可视化：就绪队列长度曲线 = 系统是否过载的直观证据；上下文切换 = 调度开销 |
| 算法设计层 | 固定配色 + 分组柱状 + 时序曲线 + 机理散点，保证 5 策略横向可比 |
| 代码实现层 | `setup_chinese_font()` / `plot_all()` / `plot_queue_timeline()` |

共 7 张图（对应 PDF 实验 3.4 要求的四张图 + 本项目补充的三张机理图）：
    01 吞吐量对比 / 02 平均等待时延对比（高/低优先级分图）/ 03 饥饿率对比
    04 资源利用率对比 / 05 业务完整率对比
    06 队列堆积时序（PEAK：dynamic vs priority）/ 07 链路健康度与弱网降权机理

中文字体：Windows 下依次尝试 微软雅黑 / 黑体 / 宋体，并关闭 `axes.unicode_minus`，
避免负号显示成方块（matplotlib 中文环境的经典坑）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.models import SCENARIO_CODES, SCENARIO_LABELS, SCHEDULER_LABELS, SCHEDULER_NAMES

#: 五个策略的固定配色（所有图共用，保证读者"颜色即策略"的认知一致）
PALETTE: Dict[str, str] = {
    "fcfs": "#8C8C8C",       # 灰：最朴素基线
    "rr": "#5B8FF9",         # 蓝
    "priority": "#F6BD16",   # 黄
    "mlfq": "#5AD8A6",       # 绿
    "dynamic": "#E8684A",    # 橙红：本方案
}

_DPI = 150
_FIGSIZE = (11.0, 6.2)

_FONT_CANDIDATES: Sequence[str] = (
    "Microsoft YaHei", "微软雅黑", "SimHei", "黑体", "Source Han Sans CN",
    "Noto Sans CJK SC", "SimSun", "宋体", "DejaVu Sans",
)


def _mpl():
    """延迟导入 matplotlib 并强制 Agg 后端（无 GUI 环境也能出图）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: WPS433

    return plt


def setup_chinese_font() -> str:
    """配置 matplotlib 中文字体，返回实际生效的字体名；失败时返回空串（不抛异常）。"""
    try:
        plt = _mpl()
        from matplotlib import font_manager

        available = {f.name for f in font_manager.fontManager.ttflist}
        chosen = ""
        for name in _FONT_CANDIDATES:
            if name in available:
                chosen = name
                break
        if not chosen:
            print("[visualize] 警告：未找到中文字体，图表中的中文可能显示为方块。")
            print("            可安装 微软雅黑 / SimHei 后重试。")
            return ""
        plt.rcParams["font.sans-serif"] = [chosen] + [
            n for n in _FONT_CANDIDATES if n != chosen]
        plt.rcParams["axes.unicode_minus"] = False   # 负号正常显示
        plt.rcParams["figure.autolayout"] = True
        return chosen
    except Exception as exc:  # noqa: BLE001
        print(f"[visualize] 字体配置失败：{exc}")
        return ""


# ===========================================================================
# 内部小工具
# ===========================================================================
def _group(results: Iterable[Any], attr: str) -> Dict[str, list]:
    groups: Dict[str, list] = {}
    for r in results:
        groups.setdefault(str(getattr(r, attr, "")), []).append(r)
    return groups


def _pick(results: List[Any], scheduler: str) -> Optional[Any]:
    for r in results:
        if str(getattr(r, "scheduler", "")) == scheduler and not getattr(r, "error", None):
            return r
    return None


def _grouped_bar(ax, results: List[Any], metric: str, title: str, ylabel: str,
                 value_fmt: str = "{:.1f}") -> None:
    """按工况分组、按策略并列的柱状图（所有对比图共用的画法）。"""
    scenarios = [c for c in SCENARIO_CODES if any(r.scenario == c for r in results)]
    scheds = [s for s in SCHEDULER_NAMES if any(r.scheduler == s for r in results)]
    if not scenarios or not scheds:
        ax.text(0.5, 0.5, "无数据", ha="center", va="center", transform=ax.transAxes)
        return

    n = len(scheds)
    width = 0.8 / max(1, n)
    xs = list(range(len(scenarios)))

    for i, sched in enumerate(scheds):
        vals = []
        for sc in scenarios:
            hit = next((r for r in results if r.scenario == sc and r.scheduler == sched), None)
            vals.append(float((hit.metrics or {}).get(metric, 0.0)) if hit else 0.0)
        offs = [x - 0.4 + width * (i + 0.5) for x in xs]
        bars = ax.bar(offs, vals, width=width * 0.92,
                      label=SCHEDULER_LABELS.get(sched, sched),
                      color=PALETTE.get(sched, "#999999"), edgecolor="white", linewidth=0.5)
        # 柱顶数值标注（图小的时候隔一个标一个，避免糊成一团）
        for b, v in zip(bars, vals):
            if v == 0:
                continue
            ax.annotate(value_fmt.format(v), (b.get_x() + b.get_width() / 2, v),
                        textcoords="offset points", xytext=(0, 2),
                        ha="center", va="bottom", fontsize=6.5, rotation=90)

    ax.set_xticks(xs)
    ax.set_xticklabels([f"{SCENARIO_LABELS.get(s, s)}\n({s})" for s in scenarios], fontsize=9)
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_ylabel(ylabel, fontsize=10)
    ax.grid(axis="y", linestyle=":", alpha=0.45)
    ax.legend(fontsize=8, ncol=min(5, n), loc="best")
    ax.margins(y=0.16)


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# ===========================================================================
# 主入口
# ===========================================================================
def plot_all(results: List[Any], charts_dir: str | Path) -> Dict[str, Path]:
    """生成全部 7 张图，返回 {图名: 文件路径}（契约 §4.11）。"""
    plt = _mpl()
    setup_chinese_font()

    out_dir = Path(charts_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = [r for r in (results or []) if not getattr(r, "error", None)]
    made: Dict[str, Path] = {}
    if not results:
        print("[visualize] 没有可用结果，跳过出图。")
        return made

    # ------------------------------------------------------------------
    # 01 吞吐量
    # ------------------------------------------------------------------
    fig, ax = plt.subplots(figsize=_FIGSIZE)
    _grouped_bar(ax, results, "throughput",
                 "图1  各调度策略在不同工况下的吞吐量对比", "吞吐量（个/秒）", "{:.2f}")
    p = out_dir / "01-吞吐量对比.png"
    fig.savefig(p, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    made["吞吐量对比"] = p

    # ------------------------------------------------------------------
    # 02 平均等待时延（高 / 低优先级各一幅）
    # ------------------------------------------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(15.5, 6.2))
    _grouped_bar(axes[0], results, "avg_wait_high_ms",
                 "图2a  高优先级（核心勘查）平均等待时延", "等待时延（毫秒）", "{:.0f}")
    _grouped_bar(axes[1], results, "avg_wait_low_ms",
                 "图2b  低优先级（运维）平均等待时延", "等待时延（毫秒）", "{:.0f}")
    fig.suptitle("图2  平均等待时延对比：高优先级看效率，低优先级看公平",
                 fontsize=14, fontweight="bold")
    p = out_dir / "02-平均等待时延对比.png"
    fig.savefig(p, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    made["平均等待时延对比"] = p

    # ------------------------------------------------------------------
    # 03 饥饿率
    # ------------------------------------------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(15.5, 6.2))
    _grouped_bar(axes[0], results, "starvation_rate_low",
                 "图3a  低优先级任务饥饿率（痛点②核心指标）", "饥饿率（%）", "{:.1f}")
    _grouped_bar(axes[1], results, "starvation_rate",
                 "图3b  整体任务饥饿率", "饥饿率（%）", "{:.1f}")
    fig.suptitle("图3  任务饥饿率对比（等待超过饥饿阈值的任务占比）",
                 fontsize=14, fontweight="bold")
    p = out_dir / "03-饥饿率对比.png"
    fig.savefig(p, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    made["饥饿率对比"] = p

    # ------------------------------------------------------------------
    # 04 资源利用率
    # ------------------------------------------------------------------
    fig, ax = plt.subplots(figsize=_FIGSIZE)
    _grouped_bar(ax, results, "resource_utilization",
                 "图4  资源利用率对比（工作线程实际占用 / 系统可提供）",
                 "资源利用率（%）", "{:.1f}")
    p = out_dir / "04-资源利用率对比.png"
    fig.savefig(p, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    made["资源利用率对比"] = p

    # ------------------------------------------------------------------
    # 05 业务完整率
    # ------------------------------------------------------------------
    fig, ax = plt.subplots(figsize=_FIGSIZE)
    _grouped_bar(ax, results, "completion_rate",
                 "图5  业务完整率对比（成功完成 / 生成，体现峰值抗压能力）",
                 "业务完整率（%）", "{:.1f}")
    p = out_dir / "05-业务完整率对比.png"
    fig.savefig(p, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    made["业务完整率对比"] = p

    # ------------------------------------------------------------------
    # 06 队列堆积时序（PEAK：dynamic vs priority）
    # ------------------------------------------------------------------
    peak = [r for r in results if r.scenario == "PEAK"] or results
    dyn = _pick(peak, "dynamic") or _pick(peak, "mlfq")
    pri = _pick(peak, "priority") or _pick(peak, "fcfs")
    ref = dyn or (peak[0] if peak else None)
    if ref is not None:
        p = out_dir / "06-队列堆积时序.png"
        made["队列堆积时序"] = plot_queue_timeline(ref, p, against=pri)

    # ------------------------------------------------------------------
    # 07 链路健康度分布 + 弱网降权机理
    # ------------------------------------------------------------------
    p = out_dir / "07-链路健康度与权重曲线.png"
    made["链路健康度与权重"] = _plot_link_mechanism(results, p, plt)
    return made


def plot_queue_timeline(result: Any, path: str | Path,
                        against: Optional[Any] = None) -> Path:
    """就绪队列长度随时间变化曲线（图6）。

    OS 意义：就绪队列长度 > 0 且持续上升 = 系统过载；
    低优先级队列**单调上升** = 该策略正在饿死低优先级任务（静态优先级的典型形状）。
    """
    plt = _mpl()
    setup_chinese_font()
    path = Path(path)

    fig, axes = plt.subplots(2, 2, figsize=(15.0, 8.4))
    panels = [
        ("high_depth", "图6a  高优先级队列深度", "队列任务数"),
        ("low_depth", "图6b  低优先级队列深度", "队列任务数"),
        ("running_count", "图6c  并发运行任务数", "并发数"),
        ("system_load", "图6d  系统负载（>1 为过载）", "归一化负载"),
    ]
    series: List[tuple] = []
    if against is not None and against is not result:
        series.append((result, "dynamic", PALETTE["dynamic"]))
        series.append((against, "priority", PALETTE["priority"]))
    else:
        series.append((result, str(result.scheduler), PALETTE.get(str(result.scheduler), "#333")))

    for ax, (key, title, ylabel) in zip(axes.ravel(), panels):
        for res, name, color in series:
            samples = res.queue_samples or []
            if not samples:
                continue
            xs = [float(s.get("ts", 0.0)) for s in samples]
            ys = [float(s.get(key, 0.0)) for s in samples]
            ax.plot(xs, ys, label=SCHEDULER_LABELS.get(name, name),
                    color=color, linewidth=1.4, alpha=0.95)
        if key == "system_load":
            ax.axhline(1.0, color="#B37FEB", linestyle="--", linewidth=1.0,
                       label="过载阈值 = 1.0")
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.set_xlabel("仿真时间（秒）", fontsize=9)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(linestyle=":", alpha=0.45)
        ax.legend(fontsize=8, loc="best")

    fig.suptitle(f"图6  就绪队列时序对比（{result.scenario} 工况）", fontsize=14,
                 fontweight="bold")
    fig.savefig(path, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    return path


def _plot_link_mechanism(results: List[Any], path: Path, plt) -> Path:
    """图7：链路健康度分布 + 动态权重对健康度的降权曲线。

    左图：三种工况下链路健康度的分布（说明"峰值工况弱网更严重"）；
    右图：`dynamic` 策略的权重倍率随健康度变化的曲线，并标出
          `cache_health_threshold`（低于它权重×0.3 并转本地缓存）——
          这是"弱网任务不再长期抢占资源"的机理图。
    """
    import numpy as np

    fig, axes = plt.subplots(1, 2, figsize=(15.0, 6.0))

    # ---- 左：健康度分布 ----
    ax = axes[0]
    names = {"LIGHT": "轻载巡检", "NORMAL": "常态勘查", "PEAK": "峰值勘查"}
    colors = {"LIGHT": "#5AD8A6", "NORMAL": "#5B8FF9", "PEAK": "#E8684A"}
    plotted = False
    for sc in SCENARIO_CODES:
        hit = [r for r in results if r.scenario == sc]
        vals: List[float] = []
        for r in hit:
            vals.extend(float(s.get("health", 0.0)) for s in (r.link_samples or []))
        if not vals:
            continue
        ax.hist(vals, bins=40, range=(0, 100), alpha=0.55,
                label=f"{names.get(sc, sc)}（均值 {_mean(vals):.0f}）",
                color=colors.get(sc, "#999"), density=True)
        plotted = True
    if not plotted:
        ax.text(0.5, 0.5, "无链路采样数据", ha="center", va="center",
                transform=ax.transAxes)
    ax.set_xlabel("链路健康度（0~100）", fontsize=10)
    ax.set_ylabel("概率密度", fontsize=10)
    ax.set_title("图7a  三工况链路健康度分布（弱网强度递增）", fontsize=12, fontweight="bold")
    ax.grid(linestyle=":", alpha=0.4)
    ax.legend(fontsize=8)

    # ---- 右：弱网降权机理曲线 ----
    ax = axes[1]
    from uav_sim.config import get, load_config

    try:
        cfg = load_config()
        cache_th = float(get(cfg, "dynamic_weight.cache_health_threshold", 40.0))
        floor = float(get(cfg, "dynamic_weight.weaknet_bandwidth_floor", 0.25))
    except Exception:  # noqa: BLE001 - 配置读不到就用默认值
        cache_th, floor = 40.0, 0.25

    h = np.linspace(1.0, 100.0, 400)
    # 权重倍率：健康度低于缓存阈值时 ×0.3；否则线性随链路质量提升
    weight_mult = np.where(h < cache_th, 0.3, 0.3 + 0.7 * (h - cache_th) / max(1e-9, 100.0 - cache_th))
    ax.plot(h, weight_mult, color="#E8684A", linewidth=2.2, label="动态权重倍率")
    ax.axvspan(0, cache_th, color="#FFCCC7", alpha=0.55,
               label=f"本地缓存区（< {cache_th:.0f}）")
    ax.axhline(1.0, color="#8C8C8C", linestyle="--", linewidth=1.0, label="不降权基准")
    ax.set_xlabel("链路健康度（0~100）", fontsize=10)
    ax.set_ylabel("调度权重倍率", fontsize=10)
    ax.set_title("图7b  弱网自适应：健康度越低权重越低，触发本地缓存",
                 fontsize=12, fontweight="bold")
    ax.grid(linestyle=":", alpha=0.4)
    ax.legend(fontsize=8, loc="lower right")
    ax.text(0.5, 0.06,
            f"服务速率折损下限 = {floor:.2f}（health=0 时仍按 {floor * 100:.0f}% 带宽）",
            transform=ax.transAxes, ha="center", fontsize=8.5, color="#555555")

    fig.suptitle("图7  链路状态感知与弱网降权机理", fontsize=14, fontweight="bold")
    fig.savefig(path, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    return path
