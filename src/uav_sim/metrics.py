"""
metrics.py —— 性能指标计算（实验评测层的度量核心）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 企业关心的五件事：**传得快不快（吞吐量）、等得久不久（时延）、有没有被饿死（饥饿率）、机器用没用满（资源利用率）、数据丢没丢（业务完整率）** |
| OS 原理层   | 系统性能评价体系：吞吐量 / 平均等待时间 / 响应比 / CPU 利用率 / 就绪队列长度 |
| 算法设计层 | 统一口径 + 分优先级统计 + P95 分位数（均值会掩盖长尾） |
| 代码实现层 | `compute_metrics()` / `percentile()` / `summarize()` |

--------------------------------------------------------------------------
指标口径（**报告里必须写死，否则数据无法复现**）
--------------------------------------------------------------------------
所有时延单位为**毫秒**，时间基准为**仿真时间**。

| 指标 | 口径 | 说明 |
|---|---|---|
| `throughput` | 完成任务数 / 仿真时长 | 单位 个/秒 |
| `avg_wait_high_ms` | 高优先级任务「入队→首次获得 CPU」累计等待的均值 | 已含重传/被抢占的等待累加 |
| `avg_wait_low_ms` | 低优先级同上 | 饥饿的主要观察对象 |
| `avg_turnaround_ms` | 提交→完成 的均值 | 含服务时间 |
| `p95_wait_*` | 等待时延的 95 分位 | 均值会被"大量短任务"稀释，P95 才暴露长尾 |
| `starvation_rate` | 等待 > `starvation_threshold_ms` 的任务占比 | 全任务 |
| `starvation_rate_low` | 仅低优先级 | **PDF 痛点②的核心量化指标** |
| `resource_utilization` | Σ已执行服务秒 / (仿真时长 × 并发上限) | 反映机器是否被用满 |
| `completion_rate` | 完成 root 任务数 / 生成 root 任务数 | 业务完整率（分片按 root 归并） |
| `drop_rate` | 被丢弃 root 任务 / 生成 root 任务 | 队列溢出丢弃 |
| `retry_rate` | 发生过弱网重传的任务占比 | 痛点①的量化指标 |
| `avg_requeue` | 平均重新入队次数 | 时间片轮转/抢占的开销 |
| `context_switch` | 调度器统计的上下文切换次数 | 切换开销 |
| `jitter_ms` | 等待时延标准差 | 时延抖动（PDF 提到"时延抖动"） |
| `overload_seconds` | 系统负载 > 阈值 的累计仿真秒 | 过载时长 |

**未完成任务的处理**：仿真结束仍未完成的任务，其等待时间按「到仿真结束时刻」结算 ——
它们本来就是"被饿着/被堵着"的任务，排除掉会系统性低估饥饿率。
"""

from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.models import METRIC_SPEC, QueueSnapshot, Task, TaskPriority, TaskState

EPS = 1e-9


# ===========================================================================
# 基础统计工具
# ===========================================================================
def percentile(values: List[float], p: float) -> float:
    """线性插值分位数（p ∈ [0,100]）。空列表返回 0.0。

    不用 numpy：本项目坚持"标准库可实现"，且数据量（万级）用纯 Python 足够。
    """
    if not values:
        return 0.0
    if p <= 0:
        return float(min(values))
    if p >= 100:
        return float(max(values))
    ordered = sorted(float(v) for v in values)
    idx = (len(ordered) - 1) * (float(p) / 100.0)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return ordered[lo]
    frac = idx - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _mean(values: List[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def _stdev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    try:
        return float(statistics.pstdev(values))
    except Exception:  # pragma: no cover
        return 0.0


# ===========================================================================
# 任务级辅助
# ===========================================================================
def is_root_task(task: Task) -> bool:
    """是否为 root 任务（未被切分的原始任务）。分片通过 parent_task_id 归并。"""
    return not getattr(task, "parent_task_id", None)


def final_wait_ms(task: Task, now_end: float) -> float:
    """任务最终等待时延（毫秒）。

    已完成/已丢弃 -> 用累计值；
    仍在队列/运行/缓存 -> 结算到 ``now_end``（未完成任务同样计入饥饿统计）。
    """
    if task is None:
        return 0.0
    if task.state in (TaskState.DONE, TaskState.DROPPED):
        return float(task.wait_ms_total)
    return float(task.wait_ms(now_end))


def final_turnaround_ms(task: Task, now_end: float) -> float:
    end = task.finish_ts if task.finish_ts is not None else now_end
    return max(0.0, (float(end) - float(task.submit_ts)) * 1000.0)


def executed_service_s(task: Task) -> float:
    """该任务实际占用过的工作线程时间（仿真秒）。"""
    if task.state is TaskState.DONE:
        return float(task.service_s)
    return float(min(task.service_s, max(0.0, task.served_s)))


# ===========================================================================
# 主计算入口
# ===========================================================================
def compute_metrics(tasks: List[Task],
                    duration_s: float,
                    uav_count: int,
                    capacity_mbps: float,
                    queue_samples: List[QueueSnapshot],
                    sched_stats: Optional[dict] = None,
                    limiter_status: Optional[dict] = None,
                    overload_seconds: float = 0.0,
                    starvation_threshold_ms: float = 3000.0,
                    end_ts: Optional[float] = None,
                    runtime_wall_s: float = 0.0,
                    preempt_count: int = 0,
                    station_metrics: Optional[dict] = None) -> Dict[str, float]:
    """计算一次仿真的全部指标（契约 §4.8）。

    返回扁平 dict，**键全部来自 ``models.METRIC_SPEC``**（缺项填 0.0），
    单位取 ``METRIC_SPEC[k][1]``，保证落库与出图的列头一致。
    """
    sched_stats = sched_stats or {}
    limiter_status = limiter_status or {}
    tasks = list(tasks or [])
    duration = max(EPS, float(duration_s))
    now_end = float(end_ts) if end_ts is not None else duration

    # ---- 按状态与优先级分组 ----
    done = [t for t in tasks if t.state is TaskState.DONE]
    high = [t for t in tasks if t.priority is TaskPriority.HIGH]
    low = [t for t in tasks if t.priority is TaskPriority.LOW]

    # ---- root 任务（未分片；分片按 parent 归并） ----
    roots = [t for t in tasks if is_root_task(t)]
    real_roots = [t for t in roots if t.drop_reason != "chunked_into_segments"]
    roots_done = [t for t in real_roots if t.state is TaskState.DONE]

    # ---- 时延 ----
    waits_all = [final_wait_ms(t, now_end) for t in tasks]
    waits_high = [final_wait_ms(t, now_end) for t in high]
    waits_low = [final_wait_ms(t, now_end) for t in low]
    turnarounds = [final_turnaround_ms(t, now_end) for t in done] or \
                  [final_turnaround_ms(t, now_end) for t in tasks]

    # ---- 饥饿 ----
    thr = float(starvation_threshold_ms)
    starved_all = sum(1 for w in waits_all if w > thr)
    starved_low = sum(1 for w in waits_low if w > thr)

    # ---- 资源利用率：实际占用的线程时间 / 系统可提供的线程时间 ----
    workers_cap = float((limiter_status or {}).get("max_concurrent", 0) or 0)
    if workers_cap <= 0:
        workers_cap = float(sched_stats.get("max_concurrent", 0) or 8.0)
    executed = sum(executed_service_s(t) for t in tasks)
    utilization = executed / max(EPS, duration * workers_cap) * 100.0

    # ---- 队列时序 ----
    high_depths = [int(s.high_depth) for s in queue_samples] or [0]
    low_depths = [int(s.low_depth) for s in queue_samples] or [0]
    peak_depth = max(high_depths) + max(low_depths)

    # ---- 汇总 ----
    metrics: Dict[str, float] = {
        "throughput": len(done) / duration,
        "avg_wait_high_ms": _mean(waits_high),
        "avg_wait_low_ms": _mean(waits_low),
        "avg_wait_all_ms": _mean(waits_all),
        "avg_turnaround_ms": _mean(turnarounds),
        "p95_wait_high_ms": percentile(waits_high, 95),
        "p95_wait_low_ms": percentile(waits_low, 95),
        "starvation_rate": starved_all / len(tasks) * 100.0 if tasks else 0.0,
        "starvation_rate_low": starved_low / len(low) * 100.0 if low else 0.0,
        "resource_utilization": utilization,
        "completion_rate": (len(roots_done) / len(real_roots) * 100.0) if real_roots else 0.0,
        "drop_rate": (len([t for t in real_roots if t.state is TaskState.DROPPED])
                      / len(real_roots) * 100.0) if real_roots else 0.0,
        "retry_rate": (sum(1 for t in tasks if t.retry_count > 0) / len(tasks) * 100.0)
                      if tasks else 0.0,
        "avg_requeue": _mean([float(t.requeue_count) for t in tasks]),
        "max_high_depth": float(max(high_depths)),
        "max_low_depth": float(max(low_depths)),
        "peak_queue_depth": float(peak_depth),
        "context_switch": float(sched_stats.get("context_switches", 0) or 0),
        "jitter_ms": _stdev(waits_all),
        "overload_seconds": float(overload_seconds),
        "throttle_count": float(limiter_status.get("throttled", 0) or 0),
        "reject_large_count": float(limiter_status.get("rejected_large", 0) or 0),
        "chunk_count": float(limiter_status.get("chunked", 0) or 0),
        "runtime_wall_s": float(runtime_wall_s),
    }

    # ---- 多基站 / MEC 扩展指标（改造建议第五章）----
    # 单站 + MEC 关闭时 station_metrics 全为 0，因此这些键恒为 0.0，
    # 与"改造前不存在这些指标"在数值上等价（既有结论不受影响）。
    # 注意例外（P4）：``quorum_commits`` 在单站下 = 完成数而**不是** 0
    # （majority=1，每次完成都算一次提交确认）—— 这是刻意口径，见 models.METRIC_SPEC。
    if station_metrics:
        for _k, _v in station_metrics.items():
            if _k in METRIC_SPEC:
                try:
                    metrics[_k] = float(_v)
                except (TypeError, ValueError):  # pragma: no cover - 容错
                    metrics[_k] = 0.0

    # ---- 契约要求：键必须来自 METRIC_SPEC；缺项补 0 ----
    for key in METRIC_SPEC:
        metrics.setdefault(key, 0.0)
    # 只保留契约内的键（额外统计放 extras，不进数据库的 exp_metric 主表）
    return {k: float(metrics.get(k, 0.0)) for k in METRIC_SPEC}


def extra_stats(tasks: List[Task]) -> Dict[str, float]:
    """附加诊断统计（不入 exp_metric，随 RunResult.metrics 之外的字段或 CSV 附录）。"""
    tasks = list(tasks or [])
    done = [t for t in tasks if t.state is TaskState.DONE]
    return {
        "n_tasks": float(len(tasks)),
        "n_done": float(len(done)),
        "n_high": float(sum(1 for t in tasks if t.priority is TaskPriority.HIGH)),
        "n_low": float(sum(1 for t in tasks if t.priority is TaskPriority.LOW)),
        "n_retried": float(sum(1 for t in tasks if t.retry_count > 0)),
        "n_preempted": float(sum(1 for t in tasks if t.preempt_count > 0)),
        "n_running_at_end": float(sum(1 for t in tasks if t.state is TaskState.RUNNING)),
        "n_queued_at_end": float(sum(1 for t in tasks
                                     if t.state in (TaskState.READY, TaskState.NEW))),
        "n_cached": float(sum(1 for t in tasks if t.cached)),
        "total_service_s": float(sum(t.service_s for t in tasks)),
        "total_high_service_s": float(sum(t.service_s for t in tasks
                                          if t.priority is TaskPriority.HIGH)),
    }


# ===========================================================================
# 汇总表
# ===========================================================================
def summarize(results: Iterable[Any]):
    """把多组 RunResult 汇总成 pandas.DataFrame（index = 工况+策略）。

    若环境没有 pandas，则退化为返回 list[dict]（调用方仍需能导出 CSV）。
    """
    rows: List[Dict[str, Any]] = []
    for r in results:
        row: Dict[str, Any] = {
            "exp_id": getattr(r, "exp_id", ""),
            "scenario": getattr(r, "scenario", ""),
            "scheduler": getattr(r, "scheduler", ""),
            "seed": getattr(r, "seed", 0),
            "duration_s": getattr(r, "duration_s", 0.0),
            "uav_count": getattr(r, "uav_count", 0),
        }
        row.update({k: v for k, v in (getattr(r, "metrics", {}) or {}).items()})
        rows.append(row)

    try:
        import pandas as pd  # noqa: WPS433 - 可选依赖

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(["scenario", "scheduler"]).reset_index(drop=True)
        return df
    except Exception:  # pragma: no cover - pandas 缺失时降级
        return rows
