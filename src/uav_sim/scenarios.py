"""
scenarios.py —— 三类标准工况定义、可复现工作负载轨迹、系统容量标定
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 轻载日常巡检 / 中载常规勘查 / 高并发峰值勘查三种真实作业工况；以及「一趟架次一份大数据 vs 高频心跳」的异构任务构成 |
| OS 原理层   | 工况参数 = 系统的**到达率 λ 与服务率 μ**：三者共同决定 load_factor = λ/μ，也就决定了就绪队列是"极少堆积 / 偶发堆积 / 持续堆积" |
| 算法设计层 | `WorkloadTrace` 把到达序列**离线预生成**，实现「同一工况下 5 种策略面对完全相同输入」的控制变量法 |
| 代码实现层 | `Scenario` / `generate_trace()` / `calibrate()` |

--------------------------------------------------------------------------
为什么需要「标定」（本项目的关键工程设计，报告必写）
--------------------------------------------------------------------------
PDF 附件1 的参数表存在**量级不自洽**：

    勘查任务数据量 100~2000 MB，任务生成间隔 0.5~1.5 s（峰值 30 机）
    => 30 架 × 平均 1050 MB / 1 s ≈ 31.5 GB/s ≈ 252 Gbps 的持续上行需求

任何野外地面站都不可能承载（现有 5G 上行单站约百 Mbps 量级）。
若照抄，三种工况会**全部退化为"完全拥塞"**：队列无限增长、几乎没有任务能完成，
五种策略的差异被"全部崩掉"掩盖，对照实验失去意义。

因此本项目保留 PDF 中**物理合理**的两项参数（任务数据量区间、运维任务高频特性），
把「勘查任务生成间隔」改为 `运维间隔 × survey_gap_k`——现实中勘查任务本就是
"飞一个架次采一份大数据"，架次间隔远大于心跳周期；再用 `calibrate()`
把三工况的 load_factor 反解到 **0.22 / 0.85 / 1.38**，
从而与 PDF「预期队列堆积：极少 / 偶尔 / 频繁严重」严格对齐。

标定模型（三段式）::

    单任务服务时长(仿真秒) = data_size_mb / (worker_bandwidth_mbps × link_factor)
        link_factor = weaknet_bandwidth_floor + (1 - floor) × health/100
    系统有效容量(MB/s)     = worker_threads × worker_bandwidth_mbps × E[link_factor]
    提供负载(MB/s)         = Σ(任务数据量) / 仿真时长
    系统负载 load_factor   = 提供负载 / 有效容量      (>1 即过载)
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.config import cfg_get_float, cfg_get_int
from uav_sim.models import SCENARIO_CODES, SCENARIO_LABELS, TaskPriority, TaskType

#: 高优先级（勘查）任务类型池 —— 大流量、CPU/IO 重
HIGH_TYPES: tuple = (TaskType.IMAGERY, TaskType.MULTISPECTRAL, TaskType.LIDAR)

#: 低优先级（运维）任务类型池 —— 小包、高频
LOW_TYPES: tuple = (TaskType.GPS, TaskType.ATTITUDE, TaskType.HEARTBEAT, TaskType.ALARM)


# ===========================================================================
# 一、工况定义
# ===========================================================================
@dataclass(frozen=True)
class Scenario:
    """一类标准测试工况（契约 §4.2 冻结字段）。

    字段与 PDF 附件1「表3 测试场景参数」的对应关系写在 `note` 里，
    并由 `db.py` 同步进 MySQL 的 `scenario_def` 表，供报告直接引用。
    """

    code: str                       # LIGHT / NORMAL / PEAK
    label: str                      # 中文名
    duration_s: float               # 仿真时长（仿真秒）
    uav_count: int                  # 无人机数量 = 生产者线程数
    high_ratio: float               # 目标高优先级占比（仅作参考/落库，实际以轨迹为准）
    low_ratio: float
    ops_interval_s: tuple           # 运维（LOW）任务每机生成间隔区间（仿真秒）
    survey_gap_k: int               # 每 k 个运维周期，额外产生 1 个勘查（HIGH）任务
    high_size_range_mb: tuple       # 勘查任务数据量区间（MB）
    low_size_range_mb: tuple        # 运维任务数据量区间（MB）
    churn_prob: float               # 每 1s 采样周期内发生动态上下线的概率
    weaknet_level: str              # none / low / high
    target_load_factor: float       # 标定目标负载率
    health_mu: float                # 链路健康度分布中心
    health_sigma: float

    @property
    def survey_interval_s(self) -> tuple:
        """勘查任务每机生成间隔区间 = 运维间隔 × survey_gap_k（派生量）。"""
        return (self.ops_interval_s[0] * self.survey_gap_k,
                self.ops_interval_s[1] * self.survey_gap_k)

    @property
    def avg_high_size_mb(self) -> float:
        return (self.high_size_range_mb[0] + self.high_size_range_mb[1]) / 2.0

    @property
    def avg_low_size_mb(self) -> float:
        return (self.low_size_range_mb[0] + self.low_size_range_mb[1]) / 2.0

    @property
    def avg_ops_interval_s(self) -> float:
        return (self.ops_interval_s[0] + self.ops_interval_s[1]) / 2.0


#: 三工况字典（顺序即报告表格顺序）
SCENARIOS: Dict[str, Scenario] = {
    # ---- 轻载：企业日常设备自检 / 单机试飞，链路稳定、任务量小 ----
    "LIGHT": Scenario(
        code="LIGHT",
        label=SCENARIO_LABELS["LIGHT"],
        duration_s=30.0,
        uav_count=5,
        high_ratio=0.60,
        low_ratio=0.40,
        ops_interval_s=(2.0, 5.0),
        survey_gap_k=3,
        high_size_range_mb=(100.0, 1000.0),
        low_size_range_mb=(0.5, 3.0),
        churn_prob=0.0,
        weaknet_level="none",
        target_load_factor=0.22,
        health_mu=92.0,
        health_sigma=5.0,
    ),
    # ---- 常态：企业常规矿区勘查，多机协同、负载适中、轻度弱网波动 ----
    "NORMAL": Scenario(
        code="NORMAL",
        label=SCENARIO_LABELS["NORMAL"],
        duration_s=60.0,
        uav_count=15,
        high_ratio=0.70,
        low_ratio=0.30,
        ops_interval_s=(1.0, 3.0),
        survey_gap_k=8,
        high_size_range_mb=(100.0, 1500.0),
        low_size_range_mb=(0.5, 5.0),
        churn_prob=0.02,
        weaknet_level="low",
        target_load_factor=0.85,
        health_mu=74.0,
        health_sigma=9.0,
    ),
    # ---- 峰值：大规模集群同步勘查、瞬时流量冲击、重度弱网 + 频繁上下线 ----
    "PEAK": Scenario(
        code="PEAK",
        label=SCENARIO_LABELS["PEAK"],
        duration_s=90.0,
        uav_count=30,
        high_ratio=0.80,
        low_ratio=0.20,
        ops_interval_s=(0.5, 1.5),
        survey_gap_k=30,
        high_size_range_mb=(100.0, 2000.0),
        low_size_range_mb=(0.5, 5.0),
        churn_prob=0.03,
        weaknet_level="high",
        target_load_factor=1.38,
        health_mu=52.0,
        health_sigma=14.0,
    ),
}


def get_scenario(code: str) -> Scenario:
    """按编码取工况，未知编码抛出带提示的 KeyError。"""
    key = str(code).strip().upper()
    if key not in SCENARIOS:
        raise KeyError(f"未知工况编码 {code!r}，可选：{', '.join(SCENARIOS)}")
    return SCENARIOS[key]


def list_scenarios() -> List[Scenario]:
    """按 LIGHT / NORMAL / PEAK 顺序返回全部工况。"""
    return [SCENARIOS[c] for c in SCENARIO_CODES]


# ===========================================================================
# 二、可复现工作负载轨迹（控制变量法的物质基础）
# ===========================================================================
@dataclass(frozen=True)
class TraceEvent:
    """一条任务到达事件（离线预生成，永不在运行时随机）。"""

    t: float                # 仿真秒：任务到达时刻
    uav_id: int
    task_type: TaskType
    data_size_mb: float


@dataclass(frozen=True)
class ChurnEvent:
    """一条设备动态上下线事件（对应 OS 的线程创建 / 销毁）。"""

    t: float
    uav_id: int
    action: str             # "ONLINE" | "OFFLINE"


@dataclass
class WorkloadTrace:
    """一次工况的完整输入轨迹：任务到达 + 设备上下线。

    **确定性保证**：`generate_trace(scenario, seed)` 只用一个
    ``random.Random(seed)`` 顺序消费，因此同 ``(code, seed)`` 两次调用
    **逐元素相同**；5 种调度策略回放同一条轨迹 -> 严格的控制变量对照。
    """

    code: str
    seed: int
    duration_s: float
    events: List[TraceEvent] = field(default_factory=list)
    churn: List[ChurnEvent] = field(default_factory=list)

    # ---------------------------------------------------------------- 统计
    def counts(self) -> Dict[str, int]:
        """按优先级统计任务数（供标定报告与落库）。"""
        high = sum(1 for e in self.events if e.task_type.priority is TaskPriority.HIGH)
        return {"total": len(self.events), "high": high, "low": len(self.events) - high}

    def events_of(self, uav_id: int) -> List[TraceEvent]:
        """某台无人机的到达事件（按时间升序），供其生产者线程消费。"""
        return [e for e in self.events if e.uav_id == uav_id]

    def total_mb(self) -> float:
        return sum(e.data_size_mb for e in self.events)


def generate_trace(scenario: Scenario, seed: int, cfg: dict) -> WorkloadTrace:
    """离线生成工作负载轨迹（**确定性**，契约 §3）。

    生成规则（每台无人机独立节奏，但共用一个顺序 RNG 保证可复现）：

      * 运维（LOW）任务：按 ``Uniform(ops_interval_s)`` 逐次前进；
      * 勘查（HIGH）任务：每 ``survey_gap_k`` 个运维周期，在同一时刻额外产生 1 个；
      * 任务类型：分别从 ``LOW_TYPES`` / ``HIGH_TYPES`` 池中均匀抽样；
      * 数据量：分别在 ``low_size_range_mb`` / ``high_size_range_mb`` 上均匀抽样。

    上下线：按 ``churn_prob`` 在 1 s 网格上判定，成对产生 OFFLINE / ONLINE，
    离线持续 ``Uniform(2,6)`` 秒（模拟返航换电）。
    """
    rng = random.Random(int(seed))
    duration = float(scenario.duration_s)
    events: List[TraceEvent] = []

    for uav_id in range(1, scenario.uav_count + 1):
        t = rng.uniform(0.0, scenario.avg_ops_interval_s)   # 首包错峰，避免 0 时刻齐发
        ops_ticks = 0
        while t < duration:
            # --- 运维任务（高频轻量，MPU 上报类） ---
            events.append(TraceEvent(
                t=round(t, 6),
                uav_id=uav_id,
                task_type=LOW_TYPES[rng.randrange(len(LOW_TYPES))],
                data_size_mb=round(rng.uniform(*scenario.low_size_range_mb), 3),
            ))
            ops_ticks += 1

            # --- 勘查任务（低频大流量，按架次产生） ---
            if scenario.survey_gap_k > 0 and ops_ticks % scenario.survey_gap_k == 0:
                events.append(TraceEvent(
                    t=round(t + rng.uniform(0.0, 0.05), 6),
                    uav_id=uav_id,
                    task_type=HIGH_TYPES[rng.randrange(len(HIGH_TYPES))],
                    data_size_mb=round(rng.uniform(*scenario.high_size_range_mb), 3),
                ))

            t += rng.uniform(*scenario.ops_interval_s)

    events.sort(key=lambda e: (e.t, e.uav_id))

    # ---- 设备动态上下线（对应 OS 线程的创建与销毁） ----
    churn: List[ChurnEvent] = []
    if scenario.churn_prob > 0:
        grid = 1.0
        n_steps = int(duration / grid)
        offline_until = {uid: 0.0 for uid in range(1, scenario.uav_count + 1)}
        for step_i in range(n_steps):
            t = step_i * grid
            for uav_id in range(1, scenario.uav_count + 1):
                if t < offline_until[uav_id]:
                    continue
                if rng.random() < scenario.churn_prob:
                    off = rng.uniform(2.0, 6.0)
                    churn.append(ChurnEvent(t=round(t, 6), uav_id=uav_id, action="OFFLINE"))
                    churn.append(ChurnEvent(t=round(t + off, 6), uav_id=uav_id, action="ONLINE"))
                    offline_until[uav_id] = t + off
        churn.sort(key=lambda c: (c.t, c.uav_id))

    return WorkloadTrace(code=scenario.code, seed=int(seed),
                         duration_s=duration, events=events, churn=churn)


# ===========================================================================
# 三、容量标定
# ===========================================================================
def system_capacity_mbps(cfg: dict, health_mean: float = 100.0) -> float:
    """系统**有效**容量（MB/s）= 工作线程数 × 单线程带宽 × E[链路折损系数]。

    为什么要乘 E[link_factor]：弱网下同样的数据量要占用更久的服务时间，
    相当于把可用带宽按比例打了折 —— 这是"弱网失衡"痛点的量化体现。
    """
    workers = cfg_get_int(cfg, "runtime.worker_threads", 8)
    bw = cfg_get_float(cfg, "runtime.worker_bandwidth_mbps", 150.0)
    floor = cfg_get_float(cfg, "dynamic_weight.weaknet_bandwidth_floor", 0.25)
    factor = floor + (1.0 - floor) * max(0.0, min(100.0, health_mean)) / 100.0
    return float(workers) * float(bw) * factor


def calibrate(scenario: Scenario, cfg: dict, seed: Optional[int] = None) -> dict:
    """对单个工况做容量标定，返回报告用字典（契约 §4.2）。

    做法：**先真实生成一遍轨迹**，从轨迹里量出实际的到达率与平均数据量，
    再算 `offered_mbps / capacity_mbps = load_factor`。
    （比用理论均值更准，因为轨迹里上下线、采样都是随机的。）

    返回键：code / offered_mbps / capacity_mbps / load_factor /
            avg_high_service_s / avg_low_service_s / est_queue_depth /
            survey_interval_s / target_load_factor / verdict / n_tasks / n_high / n_low /
            retry_multiplier / p_fail

    **重传放大**：弱网上传会失败重传，同一份数据可能要传 1/(1−p_fail) 次才成功。
    因此真实负载必须乘上期望尝试次数 `retry_multiplier`，否则会严重低估 PEAK 工况
    的压力（踩过一次坑：retry_gain 取 1.2 时 p_fail≈0.58，需求被放大 2.4 倍，
    三种工况一起被打成"重传风暴"，饥饿率指标饱和、策略差异被掩盖）。
    """
    if seed is None:
        seed = cfg_get_int(cfg, "simulation.random_seed", 20261031)
    trace = generate_trace(scenario, int(seed), cfg)
    counts = trace.counts()

    floor = cfg_get_float(cfg, "dynamic_weight.weaknet_bandwidth_floor", 0.25)
    bw = cfg_get_float(cfg, "runtime.worker_bandwidth_mbps", 150.0)
    workers = cfg_get_int(cfg, "runtime.worker_threads", 8)
    retry_gain = cfg_get_float(cfg, "weaknet.retry_gain", 0.5)
    link_factor = floor + (1.0 - floor) * scenario.health_mu / 100.0
    effective_bw = bw * link_factor

    # 弱网失败概率与期望尝试次数（重传放大系数）
    p_fail = max(0.0, min(0.95, (1.0 - scenario.health_mu / 100.0) * retry_gain))
    retry_multiplier = 1.0 / max(1e-6, 1.0 - p_fail)

    duration = float(scenario.duration_s)
    offered_mbps = trace.total_mb() / duration if duration > 0 else 0.0
    capacity_mbps = float(workers) * effective_bw
    load_factor = (offered_mbps * retry_multiplier) / capacity_mbps if capacity_mbps > 0 else 0.0

    high_mb = sum(e.data_size_mb for e in trace.events
                  if e.task_type.priority is TaskPriority.HIGH)
    low_mb = trace.total_mb() - high_mb
    n_high = max(1, counts["high"])
    n_low = max(1, counts["low"])

    # 平均服务时长：用各自平均数据量 / 有效带宽（同一条链路折损假设）
    avg_high_service_s = (high_mb / n_high) / effective_bw
    avg_low_service_s = (low_mb / n_low) / effective_bw

    # 粗略队列深度估计（Little 定律直觉版）：过剩需求 × 平均服务时长
    over = max(0.0, offered_mbps - capacity_mbps)
    est_queue_depth = int(over / effective_bw * scenario.duration_s / 2.0) if over > 0 else 0

    target = float(scenario.target_load_factor)
    if abs(load_factor - target) <= 0.25 * target:
        verdict = "ok"
    elif load_factor < target:
        verdict = "too_light"
    else:
        verdict = "overloaded"

    return {
        "code": scenario.code,
        "label": scenario.label,
        "duration_s": duration,
        "uav_count": scenario.uav_count,
        "n_tasks": counts["total"],
        "n_high": counts["high"],
        "n_low": counts["low"],
        "actual_high_ratio": counts["high"] / max(1, counts["total"]),
        "survey_interval_s": round(scenario.ops_interval_s[0] * scenario.survey_gap_k, 2),
        "survey_interval_max_s": round(scenario.ops_interval_s[1] * scenario.survey_gap_k, 2),
        "offered_mbps": round(offered_mbps, 2),
        "capacity_mbps": round(capacity_mbps, 2),
        "load_factor": round(load_factor, 3),
        "retry_multiplier": round(retry_multiplier, 3),
        "p_fail": round(p_fail, 3),
        "target_load_factor": target,
        "avg_high_service_s": round(avg_high_service_s, 3),
        "avg_low_service_s": round(avg_low_service_s, 4),
        "est_queue_depth": est_queue_depth,
        "verdict": verdict,
    }


def calibrate_all(cfg: dict, seed: Optional[int] = None) -> List[dict]:
    """标定全部三工况（`run.py calibrate` 使用）。"""
    return [calibrate(sc, cfg, seed) for sc in list_scenarios()]


# ===========================================================================
# 自检入口：不依赖 run.py 也能打印标定表
# ===========================================================================
if __name__ == "__main__":  # pragma: no cover
    from uav_sim.config import load_config

    _cfg = load_config()
    _rows = calibrate_all(_cfg)
    print(f"{'工况':<8}{'时长':>6}{'机数':>5}{'任务数':>8}{'高优占比':>9}"
          f"{'勘查间隔':>10}{'提供负载':>11}{'有效容量':>11}{'负载率':>8}{'目标':>7}{'结论':>10}")
    print("-" * 96)
    for r in _rows:
        print(f"{r['code']:<8}{r['duration_s']:>6.0f}{r['uav_count']:>5}{r['n_tasks']:>8}"
              f"{r['actual_high_ratio'] * 100:>8.1f}%{r['survey_interval_s']:>10.1f}"
              f"{r['offered_mbps']:>11.1f}{r['capacity_mbps']:>11.1f}"
              f"{r['load_factor']:>8.2f}{r['target_load_factor']:>7.2f}{r['verdict']:>10}")
    print()
    print("说明：负载率 <0.5 轻载（极少堆积）；0.5~1.0 常态（偶发堆积）；>1.0 过载（持续堆积）。")

    # 确定性自检：同 seed 两次生成必须逐元素相同
    _t1 = generate_trace(get_scenario("PEAK"), 20261031, _cfg)
    _t2 = generate_trace(get_scenario("PEAK"), 20261031, _cfg)
    assert _t1.events == _t2.events, "WorkloadTrace 不可复现！"
    print(f"[OK] 确定性校验通过：PEAK 轨迹 {len(_t1.events)} 条事件，同 seed 两次生成完全一致。")
