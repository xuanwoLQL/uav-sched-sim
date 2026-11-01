"""
tests/test_link.py —— 链路状态感知单元测试
=============================================================================

覆盖的 OS 原理点（对应 PDF 实验 3.3 验收标准「链路状态更新」「健康度计算」）：
    * 健康度评分在 0~100 区间，且随 delay / loss / rssi 变化
    * `apply_health()` 的反演自洽性：反演后再算 health_score 应等于原值
    * **确定性**：同 (seed, uav_id) 两次构建得到逐点相同的健康度曲线
        -> 这是"5 个策略面对同一份链路输入"（控制变量）的技术保证
    * 不同弱网档位下健康度均值单调下降（none > low > high）
    * tick() 能按采样间隔采集 LinkSample
"""

from __future__ import annotations

import sys

# Windows 控制台默认 cp936，遇到 GBK 不支持的符号会让测试假失败
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from uav_sim.config import load_config  # noqa: E402
from uav_sim.link_monitor import LinkMonitor, LinkState  # noqa: E402

CFG = load_config()


def test_health_score_range() -> None:
    st = LinkState(uav_id=1)
    st.apply_health(100.0)
    assert abs(st.health_score() - 100.0) < 1e-6, st.health_score()
    st.apply_health(0.0)
    assert st.health_score() < 5.0, "健康度 0 时评分应接近 0"
    st.apply_health(55.0)
    assert abs(st.health_score() - 55.0) < 1e-6, "反演应自洽"
    print("[OK] 健康度评分区间与反演自洽")


def test_health_monotonic_in_delay() -> None:
    good = LinkState(uav_id=1)
    good.apply_health(90.0)
    bad = LinkState(uav_id=1)
    bad.apply_health(20.0)
    assert good.delay_ms < bad.delay_ms, "健康度越低时延应越大"
    assert good.loss_rate < bad.loss_rate, "健康度越低丢包率应越大"
    assert good.rssi_dbm > bad.rssi_dbm, "健康度越低信号应越弱"
    assert good.health_score() > bad.health_score()
    print("[OK] 健康度 <-> 时延/丢包/信号 的单调关系")


def test_determinism() -> None:
    """同 seed 两次构建 -> 健康度曲线逐点相同（控制变量的技术保证）。"""
    a = LinkMonitor(seed=20261031, weaknet_level="high", duration_s=30.0,
                    uav_ids=[1, 2, 3], cfg=CFG)
    b = LinkMonitor(seed=20261031, weaknet_level="high", duration_s=30.0,
                    uav_ids=[1, 2, 3], cfg=CFG)
    pts = [i * 0.37 for i in range(200)]
    for uid in (1, 2, 3):
        va = [round(a.health_at(uid, t), 9) for t in pts]
        vb = [round(b.health_at(uid, t), 9) for t in pts]
        assert va == vb, f"uav {uid} 的健康度曲线不可复现"
    print("[OK] 链路轨迹确定性（同 seed 逐点一致）")


def test_different_uav_different_series() -> None:
    m = LinkMonitor(seed=7, weaknet_level="high", duration_s=30.0,
                    uav_ids=[1, 2], cfg=CFG)
    va = [m.health_at(1, t * 0.5) for t in range(60)]
    vb = [m.health_at(2, t * 0.5) for t in range(60)]
    assert va != vb, "不同无人机的随机流不应相同"
    print("[OK] 各机链路随机流互不相关")


def test_weaknet_levels() -> None:
    """弱网档位越差，平均健康度越低。"""
    means = {}
    for level in ("none", "low", "high"):
        m = LinkMonitor(seed=11, weaknet_level=level, duration_s=60.0,
                        uav_ids=list(range(1, 6)), cfg=CFG)
        vals = [m.health_at(1, t * 0.5) for t in range(120)]
        means[level] = sum(vals) / len(vals)
    assert means["none"] > means["low"] > means["high"], means
    print(f"[OK] 弱网档位健康度均值单调下降: "
          f"none={means['none']:.1f} low={means['low']:.1f} high={means['high']:.1f}")


def test_is_poor_and_bounds() -> None:
    m = LinkMonitor(seed=3, weaknet_level="high", duration_s=30.0, uav_ids=[1], cfg=CFG)
    poor = sum(1 for t in range(300) if m.is_poor(1, t * 0.1))
    assert 0 < poor < 300, "重度弱网下应有一部分时刻被判为劣质链路"
    for t in range(0, 300, 7):
        h = m.health_at(1, t * 0.1)
        assert 0.0 <= h <= 100.0, h
    print(f"[OK] 劣质链路判定与健康度上下界（劣质占比 {poor / 300 * 100:.0f}%）")


def test_tick_collects_samples() -> None:
    m = LinkMonitor(seed=5, weaknet_level="low", duration_s=10.0,
                    uav_ids=[1, 2], cfg=CFG)
    for i in range(0, 21):
        m.tick(i * 0.5)
    samples = m.samples()
    assert len(samples) >= 10, f"应采集到多条链路采样，实际 {len(samples)}"
    assert all(0.0 <= s.health <= 100.0 for s in samples)
    assert all(s.uav_id in (1, 2) for s in samples)
    print(f"[OK] tick() 采集链路采样 {len(samples)} 条")


if __name__ == "__main__":
    test_health_score_range()
    test_health_monotonic_in_delay()
    test_determinism()
    test_different_uav_different_series()
    test_weaknet_levels()
    test_is_poor_and_bounds()
    test_tick_collects_samples()
    print("\n所有链路测试通过 [PASS]")
