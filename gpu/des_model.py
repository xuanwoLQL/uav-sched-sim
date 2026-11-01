"""
gpu/des_model.py —— 分析模型的纯 Python 实现（回退路径 + 交叉验证基准）
=============================================================================

为什么需要它：
  * **回退**：没有 CUDA 工具链 / 没有 N 卡时，`run.py gpu` 不该直接报错失败，
    而应退化成"跑得慢但口径一致"的实现；
  * **交叉验证**：它和 `replication_kernel.cu` 是**同一套规则的两份实现**。
    两者结果接近，说明 CUDA 内核没有在移植过程中写错（比"看起来合理"强得多的证据）。

实现原则：
  * 与内核使用**同一个 RNG**（xorshift64*）与同一套事件顺序，因此同样 rep 的随机流一致；
  * 只用标准库（可选 numpy/numby 加速），不引入新依赖；
  * 多进程并行（每个进程跑一批 replication），把纯 Python 的损失补回来一部分。

用法：
    python gpu\\des_model.py --reps 200            # 自检 + 跑一遍
    from des_model import run_study                # 被 run_gpu.py 调用
"""

from __future__ import annotations

import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

MAX_TASKS = 2048
MAX_PENDING = 1024

MODE_FCFS, MODE_RR, MODE_PRIORITY, MODE_DYNAMIC, MODE_MLFQ = 0, 1, 2, 3, 4
MODE_NAMES = ["fcfs", "rr", "priority", "dynamic", "mlfq"]
MODE_INDEX = {n: i for i, n in enumerate(MODE_NAMES)}

# ---------------------------------------------------------------------------
# 参数元组的下标（与 CUDA 版 ModelParams 字段一一对应，顺序必须一致）
# ---------------------------------------------------------------------------
PARAM_ORDER = [
    "n_uav", "duration", "workers", "queue_capacity",
    "w0", "alpha", "beta", "gamma", "delta",
    "base_high", "base_low", "starve_boost", "starve_boost_cap",
    "weaknet_downweight", "cache_health_threshold",
    "slice_high_base_ms", "slice_high_min_ms", "slice_high_max_ms",
    "slice_low_base_ms", "slice_low_min_ms", "slice_low_max_ms",
    "slice_shrink", "slice_preempt_threshold",
    "rr_slice_ms", "mlfq_slice0_ms", "mlfq_slice1_ms", "mlfq_aging_ms",
    "starvation_ms", "metric_starve_ms",
    "retry_gain", "retry_backoff_base_s", "cache_retry_backoff_s",
    "survey_gap_s", "ops_gap_s", "health_mu", "health_sigma",
    "svc_high_mu_s", "svc_low_mu_s",
]
P = {name: i for i, name in enumerate(PARAM_ORDER)}

#: 默认参数（与 replication_kernel.cu 的 default_params() 保持一致）
DEFAULT_PARAMS: Tuple[float, ...] = (
    30.0, 60.0, 8.0, 5000.0,                                  # n_uav duration workers qcap
    0.50, 0.45, 0.35, 0.15, 0.25,                             # w0 alpha beta gamma delta
    1.00, 0.35, 1.80, 6.00,                                   # base_high base_low starve boost cap
    0.30, 40.0,                                               # weaknet_downweight cache threshold
    3000.0, 800.0, 6000.0,                                    # dynamic high slice
    400.0, 150.0, 800.0,                                      # dynamic low slice
    0.60, 0.15,                                               # slice shrink / preempt threshold
    500.0,                                                    # rr slice
    300.0, 800.0, 1500.0,                                     # mlfq slice0 / slice1 / aging
    3000.0, 15000.0,                                          # starvation / metric starve
    0.5, 0.2, 2.0,                                            # retry gain / backoff base / cache backoff
    24.0, 1.0, 52.0, 18.0,                                    # survey gap / ops gap / health mu / sigma
    7.5, 0.01,                                                # svc high / svc low
)

# 指标输出顺序（与 CUDA Metrics 一致；后 4 个是整数计数，统一转 float）
METRIC_KEYS = [
    "throughput", "wait_high_ms", "wait_low_ms", "starvation_low_pct",
    "completion_pct", "util_pct", "retry_pct", "preempts",
    "promotions", "cached", "max_low_depth", "submitted", "done",
]

MASK64 = 0xFFFFFFFFFFFFFFFF


def _rng_uniform(state: int) -> Tuple[int, float]:
    """xorshift64* —— 与 CUDA 内核 rng_next/rng_uniform 完全一致。"""
    s = state
    s ^= (s >> 12) & MASK64
    s ^= (s << 25) & MASK64
    s ^= (s >> 27) & MASK64
    s &= MASK64
    v = (s * 2685821657736338717) & MASK64
    return s, (v >> 11) * (1.0 / 9007199254740992.0)


def _link_health(uav: int, t: float, prm: Sequence[float], phase: float) -> float:
    w = 0.11 * uav + phase
    h = prm[P["health_mu"]] + prm[P["health_sigma"]] * (
        0.6 * math.sin(0.08 * t + w) + 0.4 * math.sin(0.31 * t + 2.1 * w))
    return 0.0 if h < 0.0 else (100.0 if h > 100.0 else h)


def simulate_one(rep: int, mode: int, prm: Sequence[float]) -> List[float]:
    """跑一次完整仿真，返回 METRIC_KEYS 对应的指标列表（语义与 CUDA 内核一致）。"""
    n_uav = int(prm[P["n_uav"]])
    duration = prm[P["duration"]]
    workers = int(prm[P["workers"]])
    qcap = prm[P["queue_capacity"]]
    link_aware = (mode == MODE_DYNAMIC)

    # 播种：与 CUDA 一致
    state = 0x9E3779B97F4A7C15 ^ (((rep + 1) * 0xBF58476D1CE4E5B9) & MASK64)
    state, u0 = _rng_uniform(state)
    phase = u0 * 6.2831853

    def rnd_exp(mu: float) -> float:
        nonlocal state
        state, u = _rng_uniform(state)
        if u < 1e-7:
            u = 1e-7
        return -mu * math.log(u)

    svc = [0.0] * MAX_TASKS
    remain = [0.0] * MAX_TASKS
    enq = [0.0] * MAX_TASKS
    wait_acc = [0.0] * MAX_TASKS
    prio = [0] * MAX_TASKS
    uav_of = [0] * MAX_TASKS
    retry_of = [0] * MAX_TASKS
    nxt = [-1] * MAX_TASKS
    n = 0

    next_high = [0.0] * n_uav
    next_low = [0.0] * n_uav
    for u in range(n_uav):
        next_high[u] = rnd_exp(prm[P["survey_gap_s"]])
        next_low[u] = rnd_exp(prm[P["ops_gap_s"]])

    head_h = tail_h = -1
    head_l = tail_l = -1
    depth_h = depth_l = 0
    max_low_depth = 0

    w_task = [-1] * workers
    w_finish = [0.0] * workers
    w_run = [0.0] * workers

    pend_idx = [0] * MAX_PENDING
    pend_due = [0.0] * MAX_PENDING
    pend_n = 0

    sum_wait_high = sum_wait_low = 0.0
    starved_low = done_high = done_low = retries = preempts = 0
    promotions = cached_cnt = submitted_low = 0
    busy_service = 0.0
    t = 0.0
    guard = 0

    def enqueue(idx: int, now: float) -> None:
        nonlocal head_h, tail_h, head_l, tail_l, depth_h, depth_l, max_low_depth
        enq[idx] = now
        nxt[idx] = -1
        if prio[idx] == 1:
            if tail_h >= 0:
                nxt[tail_h] = idx
            else:
                head_h = idx
            tail_h = idx
            depth_h += 1
        else:
            if tail_l >= 0:
                nxt[tail_l] = idx
            else:
                head_l = idx
            tail_l = idx
            depth_l += 1
            if depth_l > max_low_depth:
                max_low_depth = depth_l

    while t < duration and guard < 5_000_000:
        guard += 1
        # 1) 最近事件
        next_arrival = 1e30
        for u in range(n_uav):
            if next_high[u] < next_arrival:
                next_arrival = next_high[u]
            if next_low[u] < next_arrival:
                next_arrival = next_low[u]
        next_finish = 1e30
        for i in range(workers):
            if w_task[i] >= 0 and w_finish[i] < next_finish:
                next_finish = w_finish[i]
        next_pend = 1e30
        for i in range(pend_n):
            if pend_due[i] < next_pend:
                next_pend = pend_due[i]
        t_next = min(next_arrival, next_finish, next_pend, duration)
        if t_next > t:
            t = t_next

        # 2) 退避到期
        i = 0
        while i < pend_n:
            if pend_due[i] <= t:
                enqueue(pend_idx[i], pend_due[i])
                pend_n -= 1
                pend_idx[i] = pend_idx[pend_n]
                pend_due[i] = pend_due[pend_n]
            else:
                i += 1

        # 3) 到达
        for u in range(n_uav):
            while next_high[u] <= t and n < MAX_TASKS:
                idx = n
                n += 1
                prio[idx] = 1
                uav_of[idx] = u
                retry_of[idx] = 0
                svc[idx] = rnd_exp(prm[P["svc_high_mu_s"]])
                remain[idx] = svc[idx]
                wait_acc[idx] = 0.0
                enqueue(idx, next_high[u])
                next_high[u] += rnd_exp(prm[P["survey_gap_s"]])
            while next_low[u] <= t and n < MAX_TASKS:
                idx = n
                n += 1
                prio[idx] = 0
                uav_of[idx] = u
                retry_of[idx] = 0
                svc[idx] = rnd_exp(prm[P["svc_low_mu_s"]])
                remain[idx] = svc[idx]
                wait_acc[idx] = 0.0
                submitted_low += 1
                enqueue(idx, next_low[u])
                next_low[u] += rnd_exp(prm[P["ops_gap_s"]])
        if n >= MAX_TASKS:
            for u in range(n_uav):
                next_high[u] = 1e30
                next_low[u] = 1e30

        # 4) 完成 / 时间片到期
        for i in range(workers):
            if w_task[i] < 0 or w_finish[i] > t + 1e-6:
                continue
            idx = w_task[i]
            w_task[i] = -1
            ran = w_run[i]
            remain[idx] -= ran
            busy_service += ran
            if remain[idx] > 1e-6:
                preempts += 1
                enqueue(idx, t)
                continue
            h = _link_health(uav_of[idx], t, prm, phase)
            p_fail = (1.0 - h / 100.0) * prm[P["retry_gain"]]
            p_fail = 0.0 if p_fail < 0.0 else (1.0 if p_fail > 1.0 else p_fail)
            state, u_roll = _rng_uniform(state)
            if u_roll < p_fail:
                retries += 1
                rk = retry_of[idx]
                if rk < 255:
                    retry_of[idx] = rk + 1
                if link_aware and h < prm[P["cache_health_threshold"]]:
                    cached_cnt += 1
                    delay = prm[P["cache_retry_backoff_s"]]
                else:
                    e = rk if rk < 3 else 3
                    delay = prm[P["retry_backoff_base_s"]] * (1 << e)
                remain[idx] = svc[idx]
                if pend_n < MAX_PENDING:
                    pend_idx[pend_n] = idx
                    pend_due[pend_n] = t + delay
                    pend_n += 1
                else:
                    enqueue(idx, t + delay)
                continue
            wait = wait_acc[idx]
            if prio[idx]:
                sum_wait_high += wait
                done_high += 1
            else:
                sum_wait_low += wait
                done_low += 1
                if wait > prm[P["metric_starve_ms"]]:
                    starved_low += 1

        # 5) 调度
        while True:
            free_w = -1
            for i in range(workers):
                if w_task[i] < 0:
                    free_w = i
                    break
            if free_w < 0 or (head_h < 0 and head_l < 0):
                break
            cong = (depth_h + depth_l) / qcap
            cong = 0.0 if cong < 0.0 else (1.0 if cong > 1.0 else cong)
            pick = -1
            if mode in (MODE_FCFS, MODE_RR):
                if head_h >= 0 and head_l >= 0:
                    pick = head_h if enq[head_h] <= enq[head_l] else head_l
                else:
                    pick = head_h if head_h >= 0 else head_l
            elif mode == MODE_PRIORITY:
                pick = head_h if head_h >= 0 else head_l
            elif mode == MODE_MLFQ:
                if head_l >= 0 and (t - enq[head_l]) * 1000.0 > prm[P["mlfq_aging_ms"]]:
                    pick = head_l
                    promotions += 1
                else:
                    pick = head_h if head_h >= 0 else head_l
            else:
                if head_l >= 0 and (t - enq[head_l]) * 1000.0 > prm[P["starvation_ms"]]:
                    pick = head_l
                    promotions += 1
                else:
                    wh = wl = -1.0
                    if head_h >= 0:
                        qh = min(1.0, depth_h / qcap)
                        wf = min(1.0, (t - enq[head_h]) * 1000.0 / prm[P["starvation_ms"]])
                        Lv = _link_health(uav_of[head_h], t, prm, phase) / 100.0
                        wh = prm[P["base_high"]] * (
                            prm[P["w0"]] + prm[P["alpha"]] * qh
                            + prm[P["beta"]] * wf + prm[P["gamma"]] * Lv)
                        if (t - enq[head_h]) * 1000.0 > prm[P["starvation_ms"]]:
                            wh *= min(prm[P["starve_boost_cap"]],
                                      1.0 + prm[P["starve_boost"]] * wf)
                        if Lv * 100.0 < prm[P["cache_health_threshold"]]:
                            wh *= prm[P["weaknet_downweight"]]
                    if head_l >= 0:
                        ql = min(1.0, depth_l / qcap)
                        wf = min(1.0, (t - enq[head_l]) * 1000.0 / prm[P["starvation_ms"]])
                        Lv = _link_health(uav_of[head_l], t, prm, phase) / 100.0
                        wl = prm[P["base_low"]] * (
                            prm[P["w0"]] + prm[P["alpha"]] * ql
                            + prm[P["beta"]] * wf + prm[P["gamma"]] * Lv)
                        if (t - enq[head_l]) * 1000.0 > prm[P["starvation_ms"]]:
                            wl *= min(prm[P["starve_boost_cap"]],
                                      1.0 + prm[P["starve_boost"]] * wf)
                        if Lv * 100.0 < prm[P["cache_health_threshold"]]:
                            wl *= prm[P["weaknet_downweight"]]
                    pick = head_h if wh >= wl else head_l
                    if pick < 0:
                        pick = head_h if head_h >= 0 else head_l
            if pick < 0:
                break

            pp = prio[pick]
            if pp == 1:
                head_h = nxt[head_h]
                if head_h < 0:
                    tail_h = -1
                depth_h -= 1
            else:
                head_l = nxt[head_l]
                if head_l < 0:
                    tail_l = -1
                depth_l -= 1

            gap = (t - enq[pick]) * 1000.0
            if gap > 0.0:
                wait_acc[pick] += gap
            enq[pick] = t

            if mode in (MODE_FCFS, MODE_PRIORITY):
                slice_s = 1e7
            elif mode == MODE_RR:
                slice_s = prm[P["rr_slice_ms"]] / 1000.0
            elif mode == MODE_MLFQ:
                slice_s = (prm[P["mlfq_slice0_ms"]] if pp == 1
                           else prm[P["mlfq_slice1_ms"]]) / 1000.0
            elif pp == 1:
                if cong < prm[P["slice_preempt_threshold"]]:
                    slice_s = 1e7
                else:
                    raw = prm[P["slice_high_base_ms"]] * (1.0 - prm[P["slice_shrink"]] * cong)
                    slice_s = min(prm[P["slice_high_max_ms"]],
                                  max(prm[P["slice_high_min_ms"]], raw)) / 1000.0
            else:
                raw = prm[P["slice_low_base_ms"]] * (1.0 - prm[P["slice_shrink"]] * cong)
                slice_s = min(prm[P["slice_low_max_ms"]],
                              max(prm[P["slice_low_min_ms"]], raw)) / 1000.0

            run = slice_s if slice_s < remain[pick] else remain[pick]
            if run < 1e-9:
                run = remain[pick]
            w_task[free_w] = pick
            w_finish[free_w] = t + run
            w_run[free_w] = run

        if next_arrival > duration and next_finish > duration and next_pend > duration:
            break

    dur = duration if duration > 0 else 1.0
    cap = workers if workers > 0 else 1
    done = done_high + done_low
    low_total = submitted_low if submitted_low > 0 else 1
    submitted = n if n > 0 else 1
    return [
        done / dur,
        (sum_wait_high / done_high) if done_high else 0.0,
        (sum_wait_low / done_low) if done_low else 0.0,
        100.0 * starved_low / low_total,
        100.0 * done / submitted,
        100.0 * busy_service / (dur * cap),
        100.0 * retries / submitted,
        float(preempts),
        float(promotions),
        float(cached_cnt),
        float(max_low_depth),
        float(n),
        float(done),
    ]


# ---------------------------------------------------------------------------
# 批量执行（多进程并行）
# ---------------------------------------------------------------------------
def _task_worker(task) -> dict:
    """进程池 worker：跑一次仿真并打包成 CSV 行。"""
    mode, rep, prm, coeff_index, coeff = task
    values = simulate_one(rep, mode, prm)
    sm = coeff[4] if len(coeff) >= 5 and coeff[4] > 0 else prm[P["starvation_ms"]]
    row = {
        "scheduler": MODE_NAMES[mode],
        "coeff_index": coeff_index,
        "alpha": coeff[0], "beta": coeff[1], "gamma": coeff[2], "delta": coeff[3],
        "starve_ms": sm,
        "rep": rep,
    }
    row.update(dict(zip(METRIC_KEYS, values)))
    return row


def run_study(reps: int, modes: Sequence[str], coeffs=None,
              jobs: Optional[int] = None) -> List[dict]:
    """跑一批 replication，返回 CSV 行列表。

    ``coeffs`` 非空时只跑动态权重策略，并对每组 (alpha,beta,gamma,delta) 覆盖参数
    （对应 CUDA 的 sweep 模式）。
    """
    tasks = []
    if coeffs:
        base = list(DEFAULT_PARAMS)
        for ci, coeff in enumerate(coeffs):
            a, b, g, d = coeff[0], coeff[1], coeff[2], coeff[3]
            sm = coeff[4] if len(coeff) >= 5 else 0.0
            prm = list(base)
            prm[P["alpha"]] = a
            prm[P["beta"]] = b
            prm[P["gamma"]] = g
            prm[P["delta"]] = d
            if sm and sm > 0:
                prm[P["starvation_ms"]] = sm
            for rep in range(reps):
                tasks.append((MODE_DYNAMIC, rep, tuple(prm), ci, (a, b, g, d, sm)))
    else:
        for name in modes:
            mode = MODE_INDEX[name]
            for rep in range(reps):
                tasks.append((mode, rep, DEFAULT_PARAMS, -1,
                              (0.0, 0.0, 0.0, 0.0, DEFAULT_PARAMS[P["starvation_ms"]])))

    if jobs is None:
        import os
        jobs = max(1, min(os.cpu_count() or 1, 8))
    if jobs <= 1 or len(tasks) < 4:
        return [_task_worker(t) for t in tasks]

    rows: List[dict] = []
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for row in pool.map(_task_worker, tasks, chunksize=max(1, len(tasks) // (jobs * 8))):
            rows.append(row)
    rows.sort(key=lambda r: (r["coeff_index"], r["scheduler"], r["rep"]))
    return rows


def _selftest(reps: int) -> int:
    """自检：跑一遍并打印关键指标（同时给 CUDA 内核留一个对照基准）。"""
    print("=" * 78)
    print("  纯 Python 分析模型（GPU 回退路径）自检")
    print("=" * 78)
    for name in MODE_NAMES:
        rows = run_study(reps=reps, modes=[name], jobs=1)
        th = sum(r["throughput"] for r in rows) / len(rows)
        cp = sum(r["completion_pct"] for r in rows) / len(rows)
        wl = sum(r["wait_low_ms"] for r in rows) / len(rows)
        st_ = sum(r["starvation_low_pct"] for r in rows) / len(rows)
        print(f"  {name:<9} n={len(rows):<4} 吞吐={th:6.2f}/s  低优时延={wl:8.1f}ms  "
              f"低优饥饿={st_:5.1f}%  完整率={cp:5.1f}%")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="纯 Python 分析模型（回退路径）")
    ap.add_argument("--reps", type=int, default=100)
    raise SystemExit(_selftest(ap.parse_args().reps))
