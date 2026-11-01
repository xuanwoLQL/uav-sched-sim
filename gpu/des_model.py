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
MAX_STATIONS = 8              # 站点数上限（与 .cu 一致）
MAX_WORKERS_PER_STATION = 16  # 每站槽位上限（与 .cu 一致）

MODE_FCFS, MODE_RR, MODE_PRIORITY, MODE_DYNAMIC, MODE_MLFQ = 0, 1, 2, 3, 4
#: 第 6 种策略：跨节点负载均衡（站内复用 dynamic 规则 + 站间最少连接 + 带代价迁移）
MODE_CROSS_NODE = 5
MODE_NAMES = ["fcfs", "rr", "priority", "dynamic", "mlfq", "cross_node"]
MODE_INDEX = {n: i for i, n in enumerate(MODE_NAMES)}

# ---------------------------------------------------------------------------
# 多站 / MEC 的分析模型常量
# ---------------------------------------------------------------------------
#: 站点角色：0 号站 = CENTER（中心），其余 = EDGE（边缘）。
#: 与 station.py 的 `_synth_specs`（1×CENTER + 若干 EDGE）一致。
STATION_ROLE_CENTER = 0
STATION_ROLE_EDGE = 1

#: 任务数据量代理：``data_mb = 服务时长(s) × 该基准算力``。
#: 取 120 的意义：**边缘算力默认 120 Mbps 时，"站内计算时延"恰好等于任务的服务时长**，
#: 于是分析模型不需要额外的任务大小分布，也能保持与 docs/11 §4.3 同一套时延口径。
DATA_MB_PER_SVC_S = 120.0
#: 中心算力（docs/11 §4.2 默认 400）；边缘算力是参数 edge_compute_mbps（默认 120）。
CENTER_COMPUTE_MBPS = 400.0
#: 边缘需回传中心同步的数据比例（docs/11 §4.2 默认 0.15）—— 回传时延的来源。
SYNC_RATIO = 0.15
#: 中心接入时延与边缘接入时延之差（docs/11 §4.2：46 − 18 = 28）。
#: 参数 access_ms 表示**边缘接入时延基础值**，中心 = access_ms + 该差值。
CENTER_ACCESS_GAP_MS = 28.0
#: cross_node 的迁移参数（与 docs/11 §3.2 冻结值一致）
MIGRATION_LOAD_RATIO = 1.25   # 迁出水位：本站负载 > 全局平均 × 1.25
MIGRATION_BATCH = 8           # 单轮单站最多迁出任务数
MIGRATION_MAX_PER_TASK = 2    # 同一任务最多被迁 2 次（防迁移抖动）

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
    # ---- 多站 / MEC（尾部追加；顺序必须与 .cu 的 ModelParams 严格一致） ----
    "n_stations", "stations_workers", "mec_enable",
    "edge_compute_mbps", "backhaul_mbps", "access_ms",
    "migration_latency_ms", "migration_bw_penalty",
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
    # n_stations=1 / mec_enable=0 -> 单站默认行为与旧版完全一致（走 simulate_one 快速路径）
    1.0, 8.0, 0.0,                                            # n_stations / stations_workers / mec_enable
    120.0, 60.0, 18.0,                                        # edge_compute / backhaul / edge access
    120.0, 0.15,                                              # migration latency / bw penalty
)

# 指标输出顺序（与 CUDA Metrics 一致；后 4 个是整数计数，统一转 float）
METRIC_KEYS = [
    "throughput", "wait_high_ms", "wait_low_ms", "starvation_low_pct",
    "completion_pct", "util_pct", "retry_pct", "preempts",
    "promotions", "cached", "max_low_depth", "submitted", "done",
    # ---- 多站 / MEC：只在尾部追加（旧的 13 个键的下标不变） ----
    "station_imb", "e2e_ms", "edge_offload_pct", "migrations", "migration_cost_ms",
]

#: 仅多站 / MEC 路径才非零的指标键（单站快速路径统一补 0）
MULTI_METRIC_KEYS = ["station_imb", "e2e_ms", "edge_offload_pct",
                     "migrations", "migration_cost_ms"]

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
# 多站 + MEC（第 6 模式 cross_node）
# ---------------------------------------------------------------------------
def station_role(sid: int) -> int:
    """站点角色：0 号站 = CENTER（中心），其余 = EDGE（边缘）。

    与 ``station.py`` 的 ``_synth_specs``（1×CENTER + 若干 EDGE）一致。
    """
    return STATION_ROLE_CENTER if int(sid) == 0 else STATION_ROLE_EDGE


def station_home(uav: int, n_stations: int) -> int:
    """一致性哈希的简化替身：同一架无人机永远落到同一个「接入站」。

    真实实现（``station.py``）用 MD5 + 64 个虚拟节点的一致性哈希环；分析模型只需要
    「同一 key 稳定映射 + 分布大致均匀」这两条性质，故用一次乘法散列代替。**选址不消耗
    RNG**，这一点很关键：站维度不能扰动既有的随机数流。
    """
    h = (int(uav) * 2654435761 + 0x9E3779B9) & 0xFFFFFFFF
    return h % max(1, int(n_stations))


def _workers_per_station(prm: Sequence[float]) -> int:
    """每站槽位数：参数 ``stations_workers``（<=0 时回落到 ``workers``）。"""
    sw = int(prm[P["stations_workers"]])
    if sw < 1:
        sw = int(prm[P["workers"]])
    if sw < 1:
        sw = 1
    if sw > MAX_WORKERS_PER_STATION:
        sw = MAX_WORKERS_PER_STATION
    return sw


def simulate_one_multi(rep: int, mode: int, prm: Sequence[float]) -> List[float]:
    """多站 + MEC 的一次完整仿真，返回 ``METRIC_KEYS`` 对应的 18 个指标。

    与 ``simulate_one`` 的关系（**硬约束**）：

    * ``n_stations <= 1`` 且 MEC 关闭时，直接委托给 ``simulate_one`` 并在尾部补 0，
      因此**单站默认路径逐位不变**（旧结果的随机流与事件顺序都没有被动过）；
    * 其余情况走本函数：每站独立 workers/就绪队列，站间按「接入亲和（一致性哈希）→
      MEC 角色分流 → cross_node 最少连接 + 带代价迁移」三层决策；
    * RNG 的消耗顺序（播种 → 每机两条到达流 → 服务时长 → 弱网判定）与单站版一致，
      因此 ``stations=3`` 的结果同样是「同 rep 同随机流」。

    语义要点：

    * **station 维度**：``S`` 个站、每站 ``stations_workers`` 个槽位，各自两级就绪队列；
    * **cross_node**（模式 5）：站内沿用动态权重（含饥饿硬补偿 / 自适应时间片 / 链路感知），
      站间用「本站负载 > 全局平均 × 1.25」触发迁移，只做 ``hash_home → 其它站`` 的单向分流，
      每个任务最多迁 2 次；迁移要付两笔代价：``migration_latency_ms`` 在途（在途期间不占
      任何槽位）+ 剩余服务时长 ×(1 + ``migration_bw_penalty``)；
    * **MEC**：轻量任务（分析模型里用「低优/短服务」代表运维类）下沉 EDGE，重勘查（高优/
      长服务）回 CENTER；端到端时延 = 接入 + 计算（按站算力）+ 回传（边缘任务按
      ``SYNC_RATIO`` 回传中心入库的那部分），在**派发时记账、每个任务只计一次**。
    """
    n_stations_req = int(prm[P["n_stations"]])
    mec_on = prm[P["mec_enable"]] > 0.5
    if n_stations_req <= 1 and not mec_on:
        # 单站默认路径：逐位不变的旧行为 + 尾部补 0
        return list(simulate_one(rep, mode, prm)) + [0.0] * len(MULTI_METRIC_KEYS)

    n_stations = max(1, min(MAX_STATIONS, n_stations_req))
    n_uav = int(prm[P["n_uav"]])
    duration = prm[P["duration"]]
    qcap = prm[P["queue_capacity"]]
    sw = _workers_per_station(prm)
    n_slots = n_stations * sw
    cross = (mode == MODE_CROSS_NODE)
    link_aware = (mode == MODE_DYNAMIC or cross)

    edge_mbps = prm[P["edge_compute_mbps"]]
    if edge_mbps <= 0.0:
        edge_mbps = 1e-9
    backhaul_mbps = prm[P["backhaul_mbps"]]
    if backhaul_mbps <= 0.0:
        backhaul_mbps = 1e-9
    access_edge = prm[P["access_ms"]]
    if access_edge < 0.0:
        access_edge = 0.0
    access_center = access_edge + CENTER_ACCESS_GAP_MS
    mig_lat_ms = prm[P["migration_latency_ms"]]
    if mig_lat_ms < 0.0:
        mig_lat_ms = 0.0
    mig_penalty = prm[P["migration_bw_penalty"]]
    if mig_penalty < 0.0:
        mig_penalty = 0.0

    # 播种：与单站版逐位一致（同一 rep 同随机流 = CRN）
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
    st_of = [0] * MAX_TASKS          # 任务当前所属站
    mig_of = [0] * MAX_TASKS         # 该任务被迁移的次数
    counted = [False] * MAX_TASKS    # 端到端时延是否已记账（派发时只计一次）
    nxt = [-1] * MAX_TASKS
    n = 0

    next_high = [0.0] * n_uav
    next_low = [0.0] * n_uav
    for u in range(n_uav):
        next_high[u] = rnd_exp(prm[P["survey_gap_s"]])
        next_low[u] = rnd_exp(prm[P["ops_gap_s"]])

    # ---- 每站两级就绪队列（head/tail + 链表 next[]，O(1) 入队/出队） ----
    heads_h = [-1] * n_stations
    tails_h = [-1] * n_stations
    depth_h = [0] * n_stations
    heads_l = [-1] * n_stations
    tails_l = [-1] * n_stations
    depth_l = [0] * n_stations
    max_low_depth = 0

    # ---- 槽位（每站 sw 个，扁平数组） ----
    w_task = [-1] * n_slots
    w_finish = [0.0] * n_slots
    w_run = [0.0] * n_slots
    busy_by_station = [0.0] * n_stations

    # ---- 退避待办表（对应 simulator._retry_pending）----
    pend_idx = [0] * MAX_PENDING
    pend_due = [0.0] * MAX_PENDING
    pend_n = 0

    # ---- 迁移在途表：到期后投入目标站队列；在途期间不占任何站槽位 ----
    transit_idx: List[int] = []
    transit_due: List[float] = []

    sum_wait_high = sum_wait_low = 0.0
    starved_low = done_high = done_low = retries = preempts = 0
    promotions = cached_cnt = submitted_low = 0
    busy_service = 0.0
    e2e_sum = 0.0
    dispatched = edge_dispatched = 0
    migrations = 0
    migration_cost_total = 0.0
    t = 0.0
    guard = 0

    def enqueue(idx: int, sid: int, now: float) -> None:
        nonlocal max_low_depth
        enq[idx] = now
        nxt[idx] = -1
        if prio[idx] == 1:
            if tails_h[sid] >= 0:
                nxt[tails_h[sid]] = idx
            else:
                heads_h[sid] = idx
            tails_h[sid] = idx
            depth_h[sid] += 1
        else:
            if tails_l[sid] >= 0:
                nxt[tails_l[sid]] = idx
            else:
                heads_l[sid] = idx
            tails_l[sid] = idx
            depth_l[sid] += 1
            if depth_l[sid] > max_low_depth:
                max_low_depth = depth_l[sid]

    def active_of(sid: int) -> int:
        base = sid * sw
        c = 0
        for i in range(base, base + sw):
            if w_task[i] >= 0:
                c += 1
        return c

    def assign_station(uav: int, pr: int) -> int:
        """① 接入亲和（一致性哈希）② MEC 角色分流（轻量→EDGE / 重勘查→CENTER）。"""
        sid = station_home(uav, n_stations)
        if mec_on:
            want_edge = (pr == 0)
            if (station_role(sid) == STATION_ROLE_EDGE) != want_edge:
                pool = [k for k in range(n_stations)
                        if (station_role(k) == STATION_ROLE_EDGE) == want_edge]
                if not pool:
                    pool = list(range(n_stations))
                sid = min(pool, key=lambda k: (active_of(k) / float(sw),
                                               depth_h[k] + depth_l[k], k))
        return sid

    def detach_for_migration(sid: int) -> int:
        """从站 ``sid`` 的队列里摘出一个可迁任务（只是"最多迁 2 次"的限制）。

        注意方向：cross_node 做的是 ``hash_home → 其它站`` 的**单向分流**，
        因此"禁止迁回 home"约束落在**目标站**上（见 :func:`maybe_migrate`），
        而不是源站 —— 源站恰恰就是 home。这一点写反了会让迁移次数恒为 0
        （踩过的坑：MEC 关闭时 cross_node 与 dynamic 结果逐位相同）。
        """
        for is_low in (True, False):
            prev = -1
            i = heads_l[sid] if is_low else heads_h[sid]
            while i >= 0:
                if mig_of[i] < MIGRATION_MAX_PER_TASK:
                    j = nxt[i]
                    if prev < 0:
                        if is_low:
                            heads_l[sid] = j
                        else:
                            heads_h[sid] = j
                    else:
                        nxt[prev] = j
                    if j < 0:
                        if is_low:
                            tails_l[sid] = prev
                        else:
                            tails_h[sid] = prev
                    if is_low:
                        depth_l[sid] -= 1
                    else:
                        depth_h[sid] -= 1
                    return i
                prev = i
                i = nxt[i]
        return -1

    def maybe_migrate() -> None:
        """cross_node 的站间改判：负载高于水位的高站，把排队中的任务迁往最闲的站。

        触发条件（与 docs/11 §3.2 冻结参数一致）：``load(本站) > 全局平均 load × 1.25``；
        单轮单站最多迁 ``MIGRATION_BATCH = 8`` 个；每个任务最多迁 2 次。
        """
        nonlocal migrations, migration_cost_total
        loads = [(active_of(k) + depth_h[k] + depth_l[k]) / float(sw)
                 for k in range(n_stations)]
        mean = sum(loads) / n_stations
        if mean <= 1e-12:
            return
        for sid in range(n_stations):
            if loads[sid] <= mean * MIGRATION_LOAD_RATIO:
                continue
            budget = MIGRATION_BATCH
            while budget > 0 and loads[sid] > mean * MIGRATION_LOAD_RATIO:
                idx = detach_for_migration(sid)
                if idx < 0:
                    break
                cands = []
                for k in range(n_stations):
                    if k == sid or k == station_home(uav_of[idx], n_stations):
                        continue
                    if mec_on and ((k != 0) != (prio[idx] == 0)):
                        continue  # MEC 角色约束：重勘查不许迁到边缘
                    cands.append(k)
                if not cands:
                    enqueue(idx, sid, enq[idx])   # 没有合法目标 -> 原样放回
                    break
                dst = min(cands, key=lambda k: (loads[k], k))
                # 代价 ①：在途耗时（到期后由主循环投入目标站队列）
                # 代价 ②：跨节点回传的带宽损耗 -> 剩余服务时长被放大
                wait_acc[idx] += max(0.0, (t - enq[idx]) * 1000.0)
                extra_ms = remain[idx] * mig_penalty * 1000.0
                remain[idx] *= (1.0 + mig_penalty)
                mig_of[idx] += 1
                migrations += 1
                migration_cost_total += mig_lat_ms + extra_ms
                st_of[idx] = dst
                transit_idx.append(idx)
                transit_due.append(t + mig_lat_ms / 1000.0)
                loads[sid] -= 1.0 / sw
                loads[dst] += 1.0 / sw
                budget -= 1

    # ======================= 主循环：事件驱动 =======================
    while t < duration and guard < 5_000_000:
        guard += 1
        # ---------- 1) 最近事件（到达 / 完成 / 退避到期 / 迁移到站） ----------
        next_arrival = 1e30
        for u in range(n_uav):
            if next_high[u] < next_arrival:
                next_arrival = next_high[u]
            if next_low[u] < next_arrival:
                next_arrival = next_low[u]
        next_finish = 1e30
        for i in range(n_slots):
            if w_task[i] >= 0 and w_finish[i] < next_finish:
                next_finish = w_finish[i]
        next_pend = 1e30
        for i in range(pend_n):
            if pend_due[i] < next_pend:
                next_pend = pend_due[i]
        next_transit = 1e30
        for d in transit_due:
            if d < next_transit:
                next_transit = d
        t_next = min(next_arrival, next_finish, next_pend, next_transit, duration)
        if t_next > t:
            t = t_next

        # ---------- 2) 退避到期 -> 回到所属站的就绪队列 ----------
        i = 0
        while i < pend_n:
            if pend_due[i] <= t:
                idx = pend_idx[i]
                enqueue(idx, st_of[idx], pend_due[i])
                pend_n -= 1
                pend_idx[i] = pend_idx[pend_n]
                pend_due[i] = pend_due[pend_n]
            else:
                i += 1

        # ---------- 2b) 迁移在途到期 -> 投入目标站队列 ----------
        j = 0
        while j < len(transit_idx):
            if transit_due[j] <= t:
                idx = transit_idx[j]
                enqueue(idx, st_of[idx], transit_due[j])
                transit_idx[j] = transit_idx[-1]
                transit_due[j] = transit_due[-1]
                transit_idx.pop()
                transit_due.pop()
            else:
                j += 1

        # ---------- 3) 处理到达（生成任务 -> 选站 -> 入队） ----------
        for u in range(n_uav):
            while next_high[u] <= t and n < MAX_TASKS:
                idx = n
                n += 1
                prio[idx] = 1
                uav_of[idx] = u
                retry_of[idx] = 0
                mig_of[idx] = 0
                counted[idx] = False
                svc[idx] = rnd_exp(prm[P["svc_high_mu_s"]])
                remain[idx] = svc[idx]
                wait_acc[idx] = 0.0
                sid = assign_station(u, 1)
                st_of[idx] = sid
                enqueue(idx, sid, next_high[u])
                next_high[u] += rnd_exp(prm[P["survey_gap_s"]])
            while next_low[u] <= t and n < MAX_TASKS:
                idx = n
                n += 1
                prio[idx] = 0
                uav_of[idx] = u
                retry_of[idx] = 0
                mig_of[idx] = 0
                counted[idx] = False
                svc[idx] = rnd_exp(prm[P["svc_low_mu_s"]])
                remain[idx] = svc[idx]
                wait_acc[idx] = 0.0
                submitted_low += 1
                sid = assign_station(u, 0)
                st_of[idx] = sid
                enqueue(idx, sid, next_low[u])
                next_low[u] += rnd_exp(prm[P["ops_gap_s"]])
        if n >= MAX_TASKS:
            for u in range(n_uav):
                next_high[u] = 1e30
                next_low[u] = 1e30

        # ---------- 3b) cross_node：站间改判（带代价迁移） ----------
        if cross:
            maybe_migrate()

        # ---------- 4) 完成 / 时间片到期（遍历所有站的槽位） ----------
        for i in range(n_slots):
            if w_task[i] < 0 or w_finish[i] > t + 1e-6:
                continue
            idx = w_task[i]
            sid = i // sw
            w_task[i] = -1
            ran = w_run[i]
            remain[idx] -= ran
            busy_service += ran
            busy_by_station[sid] += ran
            if remain[idx] > 1e-6:
                preempts += 1
                enqueue(idx, st_of[idx], t)
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
                    enqueue(idx, st_of[idx], t + delay)
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

        # ---------- 5) 调度：每站各自把就绪队列里的任务填进本站空槽 ----------
        for sid in range(n_stations):
            while True:
                free_w = -1
                for i in range(sid * sw, sid * sw + sw):
                    if w_task[i] < 0:
                        free_w = i
                        break
                if free_w < 0 or (heads_h[sid] < 0 and heads_l[sid] < 0):
                    break
                cong = (depth_h[sid] + depth_l[sid]) / qcap
                cong = 0.0 if cong < 0.0 else (1.0 if cong > 1.0 else cong)
                pick = -1
                if mode in (MODE_FCFS, MODE_RR):
                    if heads_h[sid] >= 0 and heads_l[sid] >= 0:
                        pick = (heads_h[sid] if enq[heads_h[sid]] <= enq[heads_l[sid]]
                                else heads_l[sid])
                    else:
                        pick = heads_h[sid] if heads_h[sid] >= 0 else heads_l[sid]
                elif mode == MODE_PRIORITY:
                    pick = heads_h[sid] if heads_h[sid] >= 0 else heads_l[sid]
                elif mode == MODE_MLFQ:
                    if (heads_l[sid] >= 0
                            and (t - enq[heads_l[sid]]) * 1000.0 > prm[P["mlfq_aging_ms"]]):
                        pick = heads_l[sid]
                        promotions += 1
                    else:
                        pick = heads_h[sid] if heads_h[sid] >= 0 else heads_l[sid]
                else:
                    # 动态权重；cross_node 的站内规则与 dynamic 完全相同（docs/11 §3.3）
                    if (heads_l[sid] >= 0
                            and (t - enq[heads_l[sid]]) * 1000.0 > prm[P["starvation_ms"]]):
                        pick = heads_l[sid]
                        promotions += 1
                    else:
                        wh = wl = -1.0
                        if heads_h[sid] >= 0:
                            qh = min(1.0, depth_h[sid] / qcap)
                            wf = min(1.0, (t - enq[heads_h[sid]]) * 1000.0
                                     / prm[P["starvation_ms"]])
                            Lv = _link_health(uav_of[heads_h[sid]], t, prm, phase) / 100.0
                            wh = prm[P["base_high"]] * (
                                prm[P["w0"]] + prm[P["alpha"]] * qh
                                + prm[P["beta"]] * wf + prm[P["gamma"]] * Lv)
                            if (t - enq[heads_h[sid]]) * 1000.0 > prm[P["starvation_ms"]]:
                                wh *= min(prm[P["starve_boost_cap"]],
                                          1.0 + prm[P["starve_boost"]] * wf)
                            if Lv * 100.0 < prm[P["cache_health_threshold"]]:
                                wh *= prm[P["weaknet_downweight"]]
                        if heads_l[sid] >= 0:
                            ql = min(1.0, depth_l[sid] / qcap)
                            wf = min(1.0, (t - enq[heads_l[sid]]) * 1000.0
                                     / prm[P["starvation_ms"]])
                            Lv = _link_health(uav_of[heads_l[sid]], t, prm, phase) / 100.0
                            wl = prm[P["base_low"]] * (
                                prm[P["w0"]] + prm[P["alpha"]] * ql
                                + prm[P["beta"]] * wf + prm[P["gamma"]] * Lv)
                            if (t - enq[heads_l[sid]]) * 1000.0 > prm[P["starvation_ms"]]:
                                wl *= min(prm[P["starve_boost_cap"]],
                                          1.0 + prm[P["starve_boost"]] * wf)
                            if Lv * 100.0 < prm[P["cache_health_threshold"]]:
                                wl *= prm[P["weaknet_downweight"]]
                        pick = heads_h[sid] if wh >= wl else heads_l[sid]
                        if pick < 0:
                            pick = heads_h[sid] if heads_h[sid] >= 0 else heads_l[sid]
                if pick < 0:
                    break

                pp = prio[pick]
                if pp == 1:
                    heads_h[sid] = nxt[heads_h[sid]]
                    if heads_h[sid] < 0:
                        tails_h[sid] = -1
                    depth_h[sid] -= 1
                else:
                    heads_l[sid] = nxt[heads_l[sid]]
                    if heads_l[sid] < 0:
                        tails_l[sid] = -1
                    depth_l[sid] -= 1

                gap = (t - enq[pick]) * 1000.0
                if gap > 0.0:
                    wait_acc[pick] += gap
                enq[pick] = t

                # ---- MEC 端到端时延：在「派发时」记账，每个任务只计一次 ----
                if not counted[pick]:
                    counted[pick] = True
                    data_mb = svc[pick] * DATA_MB_PER_SVC_S
                    if station_role(sid) == STATION_ROLE_CENTER:
                        e2e = access_center + data_mb / CENTER_COMPUTE_MBPS * 1000.0
                    else:
                        e2e = (access_edge + data_mb / edge_mbps * 1000.0
                               + SYNC_RATIO * data_mb / backhaul_mbps * 1000.0)
                        edge_dispatched += 1
                    e2e_sum += e2e
                    dispatched += 1

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

        if (next_arrival > duration and next_finish > duration
                and next_pend > duration and next_transit > duration):
            break

    # ======================= 结算指标 =======================
    dur = duration if duration > 0 else 1.0
    done = done_high + done_low
    low_total = submitted_low if submitted_low > 0 else 1
    submitted = n if n > 0 else 1
    cap = n_slots if n_slots > 0 else 1

    # 站间不均衡度 = 各站负载的变异系数（σ/均值；0 = 完全均衡）。
    # 站负载 = （已消耗服务秒 + 队列里剩余服务秒）/ 本站槽位数 —— 与 station.py 的
    # ``load_imbalance``（变异系数）同口径，但分母换成"每槽位"，便于跨站数比较。
    loads = []
    for sid in range(n_stations):
        queued = 0.0
        k = heads_h[sid]
        while k >= 0:
            queued += remain[k]
            k = nxt[k]
        k = heads_l[sid]
        while k >= 0:
            queued += remain[k]
            k = nxt[k]
        loads.append((busy_by_station[sid] + queued) / float(sw))
    if n_stations < 2:
        station_imb = 0.0
    else:
        lmean = sum(loads) / n_stations
        if lmean <= 1e-12:
            station_imb = 0.0
        else:
            lvar = sum((x - lmean) ** 2 for x in loads) / n_stations
            station_imb = math.sqrt(lvar) / lmean

    e2e_ms = (e2e_sum / dispatched) if dispatched else 0.0
    edge_offload_pct = (100.0 * edge_dispatched / dispatched) if dispatched else 0.0
    migration_cost_ms = (migration_cost_total / migrations) if migrations else 0.0

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
        station_imb,
        e2e_ms,
        edge_offload_pct,
        float(migrations),
        migration_cost_ms,
    ]


# ---------------------------------------------------------------------------
# 批量执行（多进程并行）
# ---------------------------------------------------------------------------
def _task_worker(task) -> dict:
    """进程池 worker：跑一次仿真并打包成 CSV 行。"""
    mode, rep, prm, coeff_index, coeff, multi = task
    values = list(simulate_one_multi(rep, mode, prm) if multi
                  else simulate_one(rep, mode, prm))
    if len(values) < len(METRIC_KEYS):
        values += [0.0] * (len(METRIC_KEYS) - len(values))
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


def multi_params(stations: int = 1, mec: bool = False, station_workers: Optional[int] = None,
                 edge_mbps: Optional[float] = None,
                 backhaul_mbps: Optional[float] = None) -> Tuple[float, ...]:
    """按 ``DEFAULT_PARAMS`` 生成多站 / MEC 的参数元组（默认单站逐位不变）。"""
    prm = list(DEFAULT_PARAMS)
    n = max(1, min(MAX_STATIONS, int(stations)))
    prm[P["n_stations"]] = float(n)
    prm[P["mec_enable"]] = 1.0 if mec else 0.0
    if station_workers is None:
        # 总容量守恒（与 station.py 的 _synth_specs 同一控制变量思路）：
        # 多站不凭空增加总并发，只改变"任务落在哪一站"。
        station_workers = int(prm[P["workers"]]) // n
    prm[P["stations_workers"]] = float(max(1, min(MAX_WORKERS_PER_STATION,
                                                 int(station_workers))))
    if edge_mbps is not None and edge_mbps > 0:
        prm[P["edge_compute_mbps"]] = float(edge_mbps)
    if backhaul_mbps is not None and backhaul_mbps > 0:
        prm[P["backhaul_mbps"]] = float(backhaul_mbps)
    return tuple(prm)


def is_multi(prm: Sequence[float]) -> bool:
    """该参数组是否走多站 / MEC 路径（与 .cu 的 ``n_stations > 1 || mec_enable`` 一致）。"""
    return int(prm[P["n_stations"]]) > 1 or prm[P["mec_enable"]] > 0.5


def run_study(reps: int, modes: Sequence[str], coeffs=None,
              jobs: Optional[int] = None, stations: int = 1, mec: bool = False,
              station_workers: Optional[int] = None,
              edge_mbps: Optional[float] = None,
              backhaul_mbps: Optional[float] = None) -> List[dict]:
    """跑一批 replication，返回 CSV 行列表。

    ``coeffs`` 非空时只跑动态权重策略，并对每组 (alpha,beta,gamma,delta) 覆盖参数
    （对应 CUDA 的 sweep 模式）。

    ``stations`` / ``mec`` 非默认值时走 :func:`simulate_one_multi`（多站 + MEC）；
    默认 ``stations=1, mec=False`` 时**仍然调用** :func:`simulate_one`，保证旧结果逐位不变。
    """
    tasks = []
    prm = multi_params(stations=stations, mec=mec, station_workers=station_workers,
                       edge_mbps=edge_mbps, backhaul_mbps=backhaul_mbps)
    multi = is_multi(prm)
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
                tasks.append((MODE_DYNAMIC, rep, tuple(prm), ci, (a, b, g, d, sm), False))
    else:
        for name in modes:
            mode = MODE_INDEX[name]
            for rep in range(reps):
                tasks.append((mode, rep, prm, -1,
                              (0.0, 0.0, 0.0, 0.0, prm[P["starvation_ms"]]), multi))

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
