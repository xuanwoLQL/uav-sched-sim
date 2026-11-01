"""
statistics.py —— 重复实验的统计汇总（均值 / 标准差 / 95% 置信区间 / 配对比较）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 运维报告不能只写"跑出来是 25.9 个/秒"——必须写"25.9 ± 0.4 个/秒（95% 置信，n=30）"，否则无法判断优化到底有没有效果 |
| OS 原理层   | 同一策略多次运行 = 同一进程反复调度；**调度时序抖动 + 弱网随机流**共同构成观测噪声，需要统计推断把它与"算法真实差异"分离 |
| 算法设计层 | 中心极限定理 + Student-t 置信区间 + 配对比较（共同随机数 CRN）方差缩减 |
| 代码实现层 | `mean` / `sample_stdev` / `t_critical_975` / `ci95` / `paired_ci95` |

为什么必须做重复实验（本项目 v1.1 的已知短板）
------------------------------------------------
`docs/01` 明确记录：仿真依赖真实时钟（2 ms tick + `time.sleep`），同一组实验重复运行
的指标波动约 3~5%。**单次运行的数字无法区分"算法差异"与"运行噪声"**。
`config.yaml` 里 `simulation.repetitions` 早就预留了这个开关，但 v1.1 的代码从未实现
聚合逻辑（全局搜索 `repetitions` 只能命中配置与文档，命中不到任何计算）。本模块补上。

为什么用「配对」而不是两独立样本比较
--------------------------------------
重复实验对不同调度策略使用**同一组随机种子**（共同随机数 CRN，Common Random
Numbers）：第 k 次重复里，5 个策略看到的是同一条任务到达轨迹、同一条弱网随机流。
于是策略差异可以用「逐次相减」得到配对的差值序列，噪声被大幅抵消，
同样的重复次数下置信区间更窄 —— 这是离散事件仿真方差缩减的标准做法
（Law & Kelton《Simulation Modeling and Analysis》第 11 章）。

无第三方依赖：t 分位数用查表 + 上限近似实现，避免为一个分位数引入 scipy。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

# --------------------------------------------------------------------------
# 双侧 95% Student-t 分位数 t_{0.975, df}
# --------------------------------------------------------------------------
# 常用自由度直接查表（精确值），>30 用 z + Cornish-Fisher 修正（误差 < 1%），
# df→inf 收敛到正态的 1.959964。这样既不依赖 scipy，又不会算出离谱的区间。
_T975_TABLE: Dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}
_Z975: float = 1.959963984540054  # 标准正态双侧 95% 分位数


def t_critical_975(df: int) -> float:
    """双侧 95% 置信区间使用的 t 临界值（``df`` = 自由度 = n − 1）。"""
    if df <= 0:
        return float("inf")          # n=1 时无法估计方差 -> 区间无限宽（诚实地说明"样本不足"）
    if df in _T975_TABLE:
        return _T975_TABLE[df]
    if df < 1:
        return float("inf")
    # Cornish-Fisher 展开：t ≈ z + (z^3+z)/(4df) + (5z^5+16z^3+3z)/(96df^2)
    z = _Z975
    z3, z5 = z ** 3, z ** 5
    return z + (z3 + z) / (4.0 * df) + (5.0 * z5 + 16.0 * z3 + 3.0 * z) / (96.0 * df * df)


# --------------------------------------------------------------------------
# 基础统计量（全部用样本口径，n−1 分母）
# --------------------------------------------------------------------------

def mean(xs: Sequence[float]) -> float:
    """算术平均；空序列返回 0.0（调用方负责判断样本是否充足）。"""
    xs = [float(x) for x in xs]
    return sum(xs) / len(xs) if xs else 0.0


def sample_stdev(xs: Sequence[float]) -> float:
    """样本标准差（n−1 分母）；n < 2 时返回 0.0。"""
    xs = [float(x) for x in xs]
    n = len(xs)
    if n < 2:
        return 0.0
    mu = sum(xs) / n
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))


def standard_error(xs: Sequence[float]) -> float:
    """均值的标准误 SEM = s / sqrt(n)。"""
    n = len(xs)
    return sample_stdev(xs) / math.sqrt(n) if n > 0 else 0.0


def ci95_half_width(xs: Sequence[float]) -> float:
    """均值 95% 置信区间的半宽 = t_{0.975,n−1} · SEM。"""
    n = len(xs)
    if n < 2:
        return float("nan") if n == 0 else 0.0
    return t_critical_975(n - 1) * standard_error(xs)


def ci95(xs: Sequence[float]) -> Tuple[float, float]:
    """返回 ``(均值, 半宽)``，即 均值 ± 半宽 构成 95% 置信区间。"""
    return mean(xs), ci95_half_width(xs)


def paired_ci95(a: Sequence[float], b: Sequence[float],
                alpha_sig: bool = True) -> Tuple[float, float, bool]:
    """配对差值 ``a − b`` 的 95% 置信区间，并判断是否显著。

    参数
    ----
    a, b : 长度相同的两组观测（同一次重复对应的两个策略指标）。

    返回
    ----
    ``(差值均值, 半宽, 是否显著)``。
    ``是否显著`` = 置信区间不跨 0（等价于双侧配对 t 检验 p < 0.05）。

    为什么配对：见模块 docstring —— 同一随机种子下两策略共享噪声，
    逐次相减把公共噪声消掉，只留下算法差异。
    """
    if len(a) != len(b):
        raise ValueError(f"配对比较要求样本等长：len(a)={len(a)} len(b)={len(b)}")
    diffs = [float(x) - float(y) for x, y in zip(a, b)]
    d_mean, d_half = ci95(diffs)
    if not alpha_sig or len(diffs) < 2 or math.isnan(d_half):
        return d_mean, d_half, False
    significant = (d_mean - d_half) > 0.0 or (d_mean + d_half) < 0.0
    return d_mean, d_half, significant


def welch_t(a: Sequence[float], b: Sequence[float]) -> float:
    """Welch t 统计量（不假设等方差）—— 报告里可选的显著性指标。"""
    a, b = [float(x) for x in a], [float(x) for x in b]
    if len(a) < 2 or len(b) < 2:
        return 0.0
    va, vb = sample_stdev(a) ** 2, sample_stdev(b) ** 2
    denom = math.sqrt(va / len(a) + vb / len(b))
    return (mean(a) - mean(b)) / denom if denom > 0 else 0.0


# --------------------------------------------------------------------------
# 汇总容器
# --------------------------------------------------------------------------

@dataclass
class MetricAggregate:
    """单个指标在 N 次重复上的汇总。"""

    name: str
    n: int
    mean: float
    std: float
    ci_half: float
    values: List[float] = field(default_factory=list, repr=False)

    @property
    def ci_low(self) -> float:
        return self.mean - self.ci_half

    @property
    def ci_high(self) -> float:
        return self.mean + self.ci_half

    @property
    def rel_ci_pct(self) -> float:
        """相对半宽（%）= 半宽 / |均值|；用于判断结果是否"够稳"。"""
        if abs(self.mean) < 1e-12:
            return 0.0
        return 100.0 * self.ci_half / abs(self.mean)

    def format(self, nd: int = 2) -> str:
        """``25.86 ± 0.42`` 形式（写进报告/CSV 的推荐口径）。"""
        if self.n < 2:
            return f"{self.mean:.{nd}f} (± n/a, n={self.n})"
        return f"{self.mean:.{nd}f} ± {self.ci_half:.{nd}f}"


def aggregate(name: str, values: Iterable[float]) -> MetricAggregate:
    """把一个指标的多轮观测汇总成 :class:`MetricAggregate`。"""
    xs = [float(v) for v in values]
    m, half = ci95(xs)
    return MetricAggregate(name=name, n=len(xs), mean=m,
                           std=sample_stdev(xs), ci_half=half, values=xs)
