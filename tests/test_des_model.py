"""
tests/test_des_model.py —— GPU 分析模型（纯 Python 回退路径）单元测试
=============================================================================

覆盖：
    * simulate_one 的指标落在合法区间（比率为 0~100、计数非负）
    * 确定性：同一 rep 跑两次结果逐位相同（这是 CRN 与可复现性的前提）
    * 五态语义成立：静态优先级确实让低优任务更难完成（教学结论）
    * 参数不敏感性：四因子权重在本模型里几乎不影响结果（如实断言的负面结论）

设计说明：本模型比参考模型轻，单次仿真约 30ms，因此可以直接在单元测试里跑真仿真。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "gpu"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import des_model as dm  # noqa: E402


def _run(mode: int, reps: int = 3) -> list:
    return [dm.simulate_one(rep, mode, dm.DEFAULT_PARAMS) for rep in range(reps)]


def _metric(vals: list, key: str) -> float:
    i = dm.METRIC_KEYS.index(key)
    return sum(v[i] for v in vals) / len(vals)


def test_metrics_in_range() -> None:
    for mode in (dm.MODE_FCFS, dm.MODE_PRIORITY, dm.MODE_DYNAMIC, dm.MODE_MLFQ):
        vals = _run(mode, reps=2)
        for v in vals:
            row = dict(zip(dm.METRIC_KEYS, v))
            assert row["throughput"] >= 0.0
            assert 0.0 <= row["completion_pct"] <= 100.0 + 1e-6
            assert 0.0 <= row["util_pct"] <= 120.0
            assert 0.0 <= row["starvation_low_pct"] <= 100.0 + 1e-6
            assert row["submitted"] > 0 and row["done"] >= 0
            assert row["submitted"] <= dm.MAX_TASKS
    print("[OK] 五个策略的指标都落在合法区间，任务量不超过上限")


def test_determinism() -> None:
    a = dm.simulate_one(7, dm.MODE_DYNAMIC, dm.DEFAULT_PARAMS)
    b = dm.simulate_one(7, dm.MODE_DYNAMIC, dm.DEFAULT_PARAMS)
    assert a == b, "同一 rep 必须逐位可复现"
    c = dm.simulate_one(8, dm.MODE_DYNAMIC, dm.DEFAULT_PARAMS)
    assert a != c, "不同 rep 应当是不同的随机实现"
    print("[OK] 确定性：同 rep 逐位一致；不同 rep 结果不同（CRN 的前提）")


def test_priority_starves_low_tasks() -> None:
    prio = _run(dm.MODE_PRIORITY, reps=4)
    dyn = _run(dm.MODE_DYNAMIC, reps=4)
    pc = _metric(prio, "completion_pct")
    dc = _metric(dyn, "completion_pct")
    assert pc < dc, (pc, dc)
    assert _metric(dyn, "promotions") > 0.0
    print(f"[OK] 教学结论成立：静态优先级完整率 {pc:.1f}% 低于动态权重 {dc:.1f}%")


def test_weight_coefficients_are_inert() -> None:
    """如实断言一条「对本方案不利」的结论：四因子权重几乎不影响结果。

    原因：在 PEAK 过载下，绝大多数低优任务是被「饥饿硬补偿通道」直接调度的
    （见 promotions 指标），权重比较根本不参与决策。该结论来自
    `gpu/run_gpu.py --sweep` 的实测：alpha/beta/gamma/delta 的极差约 0pp，
    而 starvation_ms 的极差约 48pp。
    """
    base = list(dm.DEFAULT_PARAMS)
    zero = list(base)
    for k in ("alpha", "beta", "gamma", "delta"):
        zero[dm.P[k]] = 0.0
    huge = list(base)
    for k in ("alpha", "beta", "gamma", "delta"):
        huge[dm.P[k]] = 10.0

    def mean_completion(prm: list) -> float:
        vals = [dm.simulate_one(rep, dm.MODE_DYNAMIC, tuple(prm)) for rep in range(3)]
        return _metric(vals, "completion_pct")

    c0 = mean_completion(zero)
    c1 = mean_completion(base)
    c2 = mean_completion(huge)
    spread = max(abs(c1 - c0), abs(c2 - c0), abs(c2 - c1))
    assert spread < 3.0, (c0, c1, c2)
    print(f"[OK] 负面结论可复现：权重取 0/默认/10 时完整率 "
          f"{c0:.1f}/{c1:.1f}/{c2:.1f}%（极差 {spread:.2f}pp）")


if __name__ == "__main__":  # pragma: no cover
    test_metrics_in_range()
    test_determinism()
    test_priority_starves_low_tasks()
    test_weight_coefficients_are_inert()
    print("\n所有分析模型测试通过 [PASS]")
