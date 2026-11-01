"""
tests/test_statistics.py —— 统计汇总与重复实验单元测试
=============================================================================

覆盖：
    * 均值 / 样本标准差（n−1 分母）与退化情形（n=0/1）
    * t 分位数：查表值与已知精确值一致；df→∞ 收敛到 1.96
    * 95% 置信区间：半宽随样本量收缩（≈ 1/sqrt(n)）
    * 配对比较：纯噪声 → 不显著；真实偏移 → 显著（且方向判断正确）
    * ReplicationStudy 的种子生成遵循 CRN（所有策略共用同一份种子清单）
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from uav_sim import statistics as st  # noqa: E402


def test_basic_moments() -> None:
    assert st.mean([1, 2, 3, 4]) == 2.5
    assert st.mean([]) == 0.0
    # 样本标准差（n-1）：[2,4,4,4,5,5,7,9] -> 2.138...
    assert abs(st.sample_stdev([2, 4, 4, 4, 5, 5, 7, 9]) - 2.13809) < 1e-4
    assert st.sample_stdev([5.0]) == 0.0     # n=1 无法估计散布
    assert st.sample_stdev([]) == 0.0
    print("[OK] 均值 / 样本标准差（n-1 分母）与退化情形")


def test_t_critical_table_and_limit() -> None:
    assert st.t_critical_975(1) == 12.706
    assert st.t_critical_975(30) == 2.042
    assert st.t_critical_975(0) == float("inf")   # 样本不足 -> 诚实返回无穷宽
    # df 很大时应收敛到正态的 1.96 附近
    big = st.t_critical_975(100000)
    assert abs(big - 1.95996) < 1e-3, big
    assert st.t_critical_975(50) < st.t_critical_975(10)   # 单调下降
    print(f"[OK] t 分位数：表值精确，df→∞ 收敛到 {big:.5f}")


def test_ci_shrinks_with_n() -> None:
    data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    m, h8 = st.ci95(data)
    _, h2 = st.ci95(data[:2])
    assert m == 4.5
    assert h8 < h2, (h8, h2)
    # 半宽应大致按 1/sqrt(n) 收缩（这里 n 从 2→8，理应缩小）
    assert h8 / h2 < 0.6
    print(f"[OK] 置信区间随样本量收缩：n=2 半宽 {h2:.3f} -> n=8 半宽 {h8:.3f}")


def test_paired_significance() -> None:
    # 情形 1：两组完全相同（纯噪声被 CRN 抵消）-> 不显著
    a = [10.0, 12.0, 11.0, 13.0, 9.0, 14.0, 10.5, 12.5]
    d, half, sig = st.paired_ci95(a, list(a))
    assert d == 0.0 and not sig

    # 情形 2：目标组稳定高出 5（共同噪声由 CRN 消掉）-> 必须显著且方向为正
    noise = [3.0, -2.0, 5.0, -4.0, 1.0, 0.0, 2.0, -1.0]
    base = [50.0 + n for n in noise]
    targ = [55.0 + n for n in noise]
    d, half, sig = st.paired_ci95(targ, base)
    assert abs(d - 5.0) < 1e-9 and sig, (d, half, sig)
    assert st.paired_ci95(base, targ)[0] < 0     # 方向可翻转

    # 长度不一致必须报错（防止错位相减这种静默错误）
    try:
        st.paired_ci95([1.0, 2.0], [1.0])
        raise AssertionError("应当抛 ValueError")
    except ValueError:
        pass
    print("[OK] 配对比较：纯噪声不显著 / 真实偏移显著且方向正确")


def test_metric_aggregate() -> None:
    agg = st.aggregate("throughput", [25.0, 26.0, 24.0, 25.5])
    assert agg.n == 4
    assert abs(agg.mean - 25.125) < 1e-9
    assert agg.ci_low < agg.mean < agg.ci_high
    assert agg.format(2).startswith("25.12 ± ")
    print(f"[OK] 指标聚合：{agg.name} = {agg.format(3)}  (n={agg.n})")


def test_replication_seeds_are_crn() -> None:
    """同一次研究里所有策略必须拿到同一份种子清单（CRN 的前提）。"""
    from uav_sim.replication import ReplicationStudy

    cfg = {"simulation": {"random_seed": 1000, "repetitions": 4},
           "output": {"results_dir": "results"}}
    study = ReplicationStudy(cfg, reps=4)
    assert study.seeds() == [1000, 1001, 1002, 1003]
    # 显式传入基准种子时以参数为准
    assert ReplicationStudy(cfg, reps=2, seed_base=7).seeds() == [7, 8]
    print(f"[OK] CRN 种子清单：{study.seeds()}（所有策略共用）")


if __name__ == "__main__":  # pragma: no cover - 支持直接运行
    test_basic_moments()
    test_t_critical_table_and_limit()
    test_ci_shrinks_with_n()
    test_paired_significance()
    test_metric_aggregate()
    test_replication_seeds_are_crn()
    print("\n所有统计测试通过 [PASS]")
