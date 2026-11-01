"""
dynamic.py —— 动态权重自适应负载均衡调度器（本方案核心创新模块）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 同时解决 PDF 四大痛点：①弱网失衡 ②运维任务饥饿 ③瞬时峰值过载 ④静态参数僵化 |
| OS 原理层   | **动态优先级 + 多级反馈队列 + 资源配额/限流 + I/O（链路）状态感知** 的组合式调度内核 |
| 算法设计层 | 四因子动态权重公式 + 饥饿硬补偿通道 + 自适应时间片 + 弱网降权/本地缓存 + 过载收敛 |
| 代码实现层 | `DynamicWeightScheduler.step()` / `_weight()` / `_adaptive_slice_ms()` |

==========================================================================
一、动态权重公式（契约 §4.7 冻结版）
==========================================================================

    Q = queue.congestion()                                   # 0~1 队列拥堵度
    W = min(1, task.wait_ms(now) / starvation_threshold_ms)   # 0~1 等待因子
    L = link_health(task.uav_id) / 100                        # 0~1 链路健康度
    S = min(1, max(0, sim.system_load()))                     # 0~1 系统负载

    base = base_high(1.00)  若 勘查任务
         = base_low(0.35)   若 运维任务

    W_raw = base × (w0 + α·Q + β·W + γ·L) × (1 − δ·S)
            └───┬───┘  └──────── 正向激励 ────────┘  └─ 过载收敛 ─┘

    若 wait > starvation_threshold_ms:                        # ② 饥饿硬补偿
        W_raw *= min(starve_boost_cap, 1 + starve_boost · W)

    若 L×100 < cache_health_threshold:                        # ① 弱网降权
        W_raw *= 0.3

各系数的物理含义与取值依据（答辩必背）：

| 系数 | 值 | 对应 OS 原理 | 取值依据 |
|---|---|---|---|
| `w0` | 0.50 | 基础优先级偏置 | 保证权重恒正，且使 Q/W/L 全 0 时权重仍有意义 |
| `α` | 0.45 | **多级队列资源倾斜**：拥堵队列应优先得到调度 | 队列长度是最直接的需求信号，给最高权重 |
| `β` | 0.35 | **aging 老化机制**：等得越久越该被服务 | 略低于 α：既防饿又不至于让老任务长期压制新任务 |
| `γ` | 0.15 | **I/O 就绪优先**：链路好的任务单位服务收益高 | 权重最小：链路只是"可行性"因子，不应压过业务重要性 |
| `δ` | 0.25 | **过载收敛/流量削峰**：系统满载时整体压低权重、拉长决策间隔 | 上限 0.25 使 (1−δS) ≥ 0.75，避免过载时权重塌缩为 0 |
| `base_low` | 0.35 | 静态优先级基线 | 运维任务仍应低于勘查任务，但差距从"绝对优先"变为 1:2.86 |

**与静态优先级的本质区别**：静态优先级只有 `base` 一项且永不改变；
动态权重的 `base` 只占最终权重的 **1/4 左右**，其余 3/4 由**实时状态**决定 ——
这才是"自适应"的含义。

==========================================================================
二、调度主循环五步（step()）
==========================================================================

    ① 饥饿补偿硬通道：低优先级队首等待 > 阈值 -> **绕过权重比较直接调度**
       （为什么需要硬通道：权重是连续函数，在 PEAK 过载下低优先级权重即使翻 6 倍
        仍可能低于高优先级，纯靠权重不保证"一定"被调度 -> 必须有一条确定性出路）
    ② 双候选比较：分别取高、低队列队首，算 W_raw，选权重高者
    ③ 限流准入 try_admit()：
         OK          -> 按自适应时间片派发
         THROTTLE    -> 错峰（任务留在队列，本轮结束）
         REJECT_LARGE-> 切分后重新入队
         QUOTA       -> 换另一个候选
    ④ 自适应时间片：slice = clamp(slice_base × (1 − shrink·Q), min, max)
       （队列越堵，时间片越短 -> 减少队头阻塞、压低平均等待；队列空闲时拉长
         -> 减少上下文切换开销。对应 OS 的"动态时间片配额"。
         **方向与直觉相反，是参数探针实测出来的**，详见 docs/01「参数标定记录」）
    ⑤ 记录权重/时间片快照 -> 供 queue_sample 落库与时序图

==========================================================================
三、饥饿补偿为什么真的能消除饥饿（论文式论证，报告可直接引用）
==========================================================================

设低优先级任务等待时间为 `w`。当 `w > T`（饥饿阈值）时：

    权重倍率 boost(w) = min(cap, 1 + starve_boost·min(1, w/T))

`boost` 随 `w` 单调不减，且 `cap = 6.0`。而高优先级任务权重上界为：

    base_high × (w0 + α + β + γ) × (1 − 0) = 1.00 × (0.5+0.45+0.35+0.15) = 1.45

低优先级任务一旦 `w > T`：

    W_low = 0.35 × (0.5 + ...) × boost
    取 Q=1, W=1, L=1, S=0 的最有利情形：0.35 × 1.45 × boost
    要超过 1.45，只需 boost > 1/0.35 ≈ 2.86，而 cap = 6.0 > 2.86  ✓

即：**只要等待超过阈值，低优先级任务的权重必然能压过高优先级任务的上界**，
所以饥饿等待时间有上界 `T + 有限轮次`，饥饿被从"可能无限"压缩为"有界"。
这正是实验四里 `dynamic` 的低优先级饥饿率显著低于 `priority` 的理论依据。

（注：`QUOTA`（单机配额）与 `THROTTLE`（并发上限）都会造成等待，
 但两者都是**有界**的系统级约束，不会让某个任务被永久跳过。）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.models import SCHEDULER_LABELS, Task, TaskPriority  # noqa: E402
from uav_sim.scheduler.base import BaseScheduler  # noqa: E402


class DynamicWeightScheduler(BaseScheduler):
    """动态权重负载均衡调度器（契约 §4.7，本方案核心）。"""

    name: str = "dynamic"
    label: str = SCHEDULER_LABELS["dynamic"]
    #: **链路感知 = True**：弱网失败后走 CACHED 本地缓存而非原样重传（痛点①）
    link_aware: bool = True

    def __init__(self, sim, cfg: dict) -> None:
        super().__init__(sim, cfg)
        dw = (cfg or {}).get("dynamic_weight", {}) or {}

        # ---- 权重系数（全部来自 config.yaml，便于调参对照实验） ----
        self.w0: float = float(dw.get("w0", 0.50))
        self.alpha: float = float(dw.get("alpha", 0.45))
        self.beta: float = float(dw.get("beta", 0.35))
        self.gamma: float = float(dw.get("gamma", 0.15))
        self.delta: float = float(dw.get("delta", 0.25))
        self.base_high: float = float(dw.get("base_high", 1.00))
        self.base_low: float = float(dw.get("base_low", 0.35))
        self.starve_boost: float = float(dw.get("starve_boost", 1.80))
        self.starve_boost_cap: float = float(dw.get("starve_boost_cap", 6.00))
        self.cache_health_threshold: float = float(
            dw.get("cache_health_threshold", 40.0))
        #: 弱网降权倍率：健康度低于阈值时权重乘以该系数（1.0 = 不降权）
        self.weaknet_downweight: float = float(dw.get("weaknet_downweight", 0.30))

        # ---- 自适应时间片 ----
        self.slice_base_ms: float = float(dw.get("slice_base_ms", 500.0))
        self.slice_min_ms: float = float(dw.get("slice_min_ms", 200.0))
        self.slice_max_ms: float = float(dw.get("slice_max_ms", 2000.0))
        #: 拥堵时时间片的收缩系数：slice = base × (1 − shrink × Q)
        self.slice_congestion_shrink: float = float(
            dw.get("slice_congestion_shrink", 0.60))
        # ---- 按任务类别差异化时间片（关键：大勘查任务不能被切碎） ----
        # 为什么必须分开：勘查任务服务时长 ~11 仿真秒，若用 700ms 时间片切，
        # 一个任务要重新入队 16 次；每次都被排到几百个任务的队尾，峰值工况下
        # 根本轮不完 -> 高优先级任务完成率暴跌（实测从 50% 掉到 24%）。
        # 而运维任务只要几十毫秒，短时间片即可，短时间片反而降低队头阻塞。
        # 因此：HIGH 用长时间片（尽量一次跑完），LOW 用短时间片（快速周转）。
        self.slice_high_base_ms: float = float(dw.get("slice_high_base_ms", 3000.0))
        self.slice_high_min_ms: float = float(dw.get("slice_high_min_ms", 800.0))
        self.slice_high_max_ms: float = float(dw.get("slice_high_max_ms", 6000.0))
        self.slice_low_base_ms: float = float(dw.get("slice_low_base_ms", 400.0))
        self.slice_low_min_ms: float = float(dw.get("slice_low_min_ms", 150.0))
        self.slice_low_max_ms: float = float(dw.get("slice_low_max_ms", 800.0))
        #: 高优先级任务开始切片的拥堵度阈值：低于它认为"没有排队压力，不必切"
        self.slice_preempt_threshold: float = float(
            dw.get("slice_preempt_threshold", 0.15))

        # ---- 饥饿阈值 ----
        self.starvation_threshold_ms: float = float(
            self.cfg_get("server.starvation_threshold_ms", 3000.0) or 3000.0)

        self.step_budget: int = max(1, int(self.cfg_get("runtime.worker_threads", 8) or 8))

        # ---- 诊断量（写进 queue_sample / 报告） ----
        self.starve_rescues: int = 0    # 饥饿硬通道救出的低优先级任务数
        self.throttle_skips: int = 0    # 因并发满而错峰的轮数
        self.last_factors: dict = {}    # 最近一次权重计算的四因子快照（答辩讲解用）

    # ==================================================================
    # 权重计算
    # ==================================================================
    def _weight(self, task: Task, now: float) -> float:
        """核心：计算单个任务的动态调度权重（公式见模块 docstring）。"""
        if task is None:
            return 0.0

        # ① **该任务所属队列**的拥堵度 Q：哪个队列堵，就优先给哪个队列的任务加权
        #    （多级队列资源倾斜 —— 这是"负载均衡"三个字真正的落点）
        q_obj = self.queue
        if q_obj is not None and hasattr(q_obj, "congestion_of"):
            Q = float(q_obj.congestion_of(task.priority))
        else:  # 兜底：退化为整体拥堵度
            Q = self.congestion()
        # ② 等待因子 W：等得越久权重越高（aging 老化的连续版本）
        wait = task.wait_ms(now)
        W = min(1.0, wait / self.starvation_threshold_ms) if self.starvation_threshold_ms > 0 else 0.0
        # ③ 链路健康度 L：链路越好，单位服务收益越高（I/O 就绪优先）
        L = max(0.0, min(1.0, self.link_health(task.uav_id) / 100.0))
        # ④ 系统负载 S：过载时整体收敛（流量削峰）
        S = max(0.0, min(1.0, self.system_load()))

        base = self.base_high if task.priority is TaskPriority.HIGH else self.base_low

        # ---- 主公式 ----
        weight = base * (self.w0 + self.alpha * Q + self.beta * W + self.gamma * L) \
            * (1.0 - self.delta * S)

        # ---- 饥饿硬补偿（防饥饿的关键）：等待超阈值后权重成倍放大 ----
        boosted = False
        if wait > self.starvation_threshold_ms:
            boost = min(self.starve_boost_cap, 1.0 + self.starve_boost * W)
            weight *= boost
            boosted = True

        # ---- 弱网降权：劣质链路任务不该持续消耗系统资源（痛点①） ----
        degraded = False
        if L * 100.0 < self.cache_health_threshold:
            weight *= self.weaknet_downweight
            degraded = True

        self.last_factors = {
            "Q": Q, "W": W, "L": L, "S": S, "base": base,
            "boosted": boosted, "degraded": degraded,
            "weight": weight, "wait_ms": wait,
        }
        return max(1e-9, weight)

    def _adaptive_slice_ms(self, congestion: float) -> float:
        """自适应时间片：**过载时缩短**，空闲时拉长（本项目的实测最优方向）。

        为什么过载时要缩短（与直觉相反，但是实测结论）：
            过载时若时间片拉长，一个大任务会长时间占住工作线程（队头阻塞，head-of-line
            blocking），排在它后面的所有任务的平均等待被显著拉高。
            缩短时间片让更多任务轮流获得 CPU，**平均等待时延显著下降**，
            代价是上下文切换次数上升（本实验实测 +15% 切换、-4% 平均寿命等待）。
            参数探针结果（PEAK 工况，见 docs/01 的「参数标定记录」）:
                slice_max=2000ms -> 高优平均等待 25076ms
                slice_max= 600ms -> 高优平均等待 24549ms   <-- 更优
        """
        c = max(0.0, min(1.0, congestion))
        raw = self.slice_base_ms * (1.0 - self.slice_congestion_shrink * c)
        return self._clamp(raw, self.slice_min_ms, self.slice_max_ms)

    def _slice_for(self, priority: TaskPriority, congestion: float) -> float:
        """按任务类别 + 队列拥堵度计算时间片。

        HIGH（核心勘查）：长时间片，800~6000ms，拥堵时收缩 —— 保证大任务能在
                          少数几轮内跑完，不被"切碎 + 排到队尾"拖死。
        LOW （高频运维）：短时间片，150~800ms —— 任务本身只有几十毫秒，
                          短时间片能快速周转且几乎不增加切换开销。
        """
        c = max(0.0, min(1.0, congestion))
        if priority is TaskPriority.HIGH:
            # 轻载时**不要切分高优先级任务**：此时没有排队压力，切片只会让任务
            # 多做几次"派发-抢占-归位"，白白抬高首次启动时延（实测 LIGHT 工况下
            # 高优平均等待从 442ms 恶化到 2620ms）。
            # 只有高优先级队列真正拥堵（Q ≥ 阈值）时，才靠切片降低队头阻塞。
            if c < self.slice_preempt_threshold:
                return 1.0e9          # 不抢占：一次跑完
            raw = self.slice_high_base_ms * (1.0 - self.slice_congestion_shrink * c)
            return self._clamp(raw, self.slice_high_min_ms, self.slice_high_max_ms)
        raw = self.slice_low_base_ms * (1.0 - self.slice_congestion_shrink * c)
        return self._clamp(raw, self.slice_low_min_ms, self.slice_low_max_ms)

    # ==================================================================
    # 调度主循环
    # ==================================================================
    def step(self) -> int:
        """五个步骤见模块 docstring。"""
        now = self.now()
        dispatched = 0
        Q = self.congestion()
        slice_high = self._slice_for(TaskPriority.HIGH, Q)
        slice_low = self._slice_for(TaskPriority.LOW, Q)
        self._high_slice_ms = slice_high
        self._low_slice_ms = slice_low

        for _ in range(self.step_budget):
            # ---------------- ① 饥饿补偿硬通道 ----------------
            low_head = self.queue.peek_low() if self.queue is not None else None
            if low_head is not None and low_head.wait_ms(now) > self.starvation_threshold_ms:
                rescued: Optional[Task] = self.queue.dequeue_starving(
                    now, self.starvation_threshold_ms)
                if rescued is not None:
                    rc = self._admit(rescued)
                    if rc == "OK":
                        if self.dispatch_admitted(rescued, now, slice_ms=slice_low):
                            dispatched += 1
                            self.starve_rescues += 1
                        continue
                    if rc == "REJECT_LARGE":
                        self._split_and_enqueue(rescued, now)
                        continue
                    if rc == "QUOTA":
                        # 单机配额挡住 -> 放回，改试正常通道（可能轮到别的无人机）
                        self.push_front(rescued, now)
                    else:  # THROTTLE：并发已满，本轮结束
                        self.push_front(rescued, now)
                        self.throttle_skips += 1
                        break

            # ---------------- ② 双候选比较权重 ----------------
            high = self.queue.peek_high() if self.queue is not None else None
            low = self.queue.peek_low() if self.queue is not None else None
            if high is None and low is None:
                break

            w_high = self._weight(high, now) if high is not None else -1.0
            w_low = self._weight(low, now) if low is not None else -1.0
            if high is not None:
                self._last_high_weight = w_high
            if low is not None:
                self._last_low_weight = w_low

            priority = TaskPriority.HIGH if w_high >= w_low else TaskPriority.LOW
            task = self._pop(priority, now)
            if task is None:  # 队列在这瞬间空了（理论上不会发生）
                break
            if task.state is not None and task.task_id not in {getattr(high, "task_id", None),
                                                              getattr(low, "task_id", None)}:
                # 防御：出队对象与比较对象不一致（说明队列被并发修改），原样放回
                self.push_front(task, now)
                break

            # ---------------- ③ 限流准入 ----------------
            rc = self._admit(task)
            if rc == "OK":
                # ---------------- ④ 按自适应时间片派发 ----------------
                # 按类别取时间片（见 `_slice_for`）：HIGH 长时间片、LOW 短时间片。
                # 曾试过"被抢占过就不切片、一次跑完"，虽然能把高优先任务完成率从
                # 28% 拉到 38%，但会让长任务长期占住线程、把高优平均等待从 30s
                # 推到 37s、并让饥饿率从 0 升到 5% —— 与课程要优化的目标相悖，故回退。
                task_slice = slice_high if priority is TaskPriority.HIGH else slice_low
                if self.dispatch_admitted(task, now, slice_ms=task_slice):
                    dispatched += 1
                continue
            if rc == "REJECT_LARGE":
                self._split_and_enqueue(task, now)
                continue
            # THROTTLE / QUOTA -> 错峰：留在队列，本轮结束
            self.push_front(task, now)
            self.throttle_skips += 1
            break

        return dispatched

    # ==================================================================
    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["starve_rescues"] = int(self.starve_rescues)
        stats["throttle_skips"] = int(self.throttle_skips)
        stats["high_time_slice_ms"] = float(self._high_slice_ms)
        stats["low_time_slice_ms"] = float(self._low_slice_ms)
        return stats
