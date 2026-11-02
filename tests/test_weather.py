"""
tests/test_weather.py —— 天气 → 弱网 → 链路健康度 联动单元测试
=============================================================================

覆盖 docs/07-3D渲染升级接口契约.md §4.6.6 要求的 6 条：

    1. 确定性：同 (seed, preset, duration) 两次生成序列逐点相等；
    2. clear 零衰减：clear 下 link_penalty 恒 0，apply_health 是恒等变换；
    3. 衰减上界：任意 preset/intensity 下 1 <= apply_health(...) <= 100 且对 base 单调不增；
    4. 向后兼容：LinkMonitor(weather=None) 与改动前基线序列逐位一致
       （基线值在改动前用 build/weather_verify/link_series_baseline.py 实测录制）；
    5. 端到端：Simulator(scenario="LIGHT", weather_preset="dust") 能跑通，
       snapshot() 含契约 §4.6.4 的 weather 字段；
    6. 回归：clear 工况下 Simulator 的确定性输入（链路序列 + 任务标定）与
       不带 weather 参数时**逐项相等**。

为什么第 6 条比"整跑指标相等"更严格也更可信：
    本仿真器是"真实多线程 + time.sleep 模拟 IO"，整跑的指标受 wall-clock 线程调度
    抖动影响（实测：同一份代码连跑两次，PEAK 的 p95_wait_high 就能差 1.4 万 ms）。
    因此"clear 不影响结果"的正确断言层次是**确定性输入层**：链路序列、任务到达轨迹、
    任务标定（service_s / link_health）逐项相等 —— 这些相同，天气 clear 就不可能
    改变任何一次运行的行为分布，而整跑指标的差异属于既有噪声、与天气无关。

运行方式（与其它测试脚本一致）：
    .venv\\Scripts\\python.exe tests\\test_weather.py
退出码：0 = 全部通过；1 = 有断言失败。
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
from uav_sim.link_monitor import HEALTH_GRID_S, LinkMonitor  # noqa: E402
from uav_sim.scenarios import generate_trace, get_scenario  # noqa: E402
from uav_sim.scheduler import create_scheduler  # noqa: E402
from uav_sim.simulator import Simulator  # noqa: E402
from uav_sim.weather import (  # noqa: E402
    PRESET_ORDER,
    WEATHER_PRESETS,
    WeatherModel,
    WeatherSample,
    available_presets,
    is_valid_preset,
)

CFG = load_config()

#: 改动前（未引入 weather 参数时）用 link_series_baseline.py 录制的健康度均值基线：
#: seed=20261031, duration=90s, 30 架机、三个弱网档位、0.1s 间隔共 901 个查询点。
#: 只要 LinkMonitor(weather=None) 的数值有任何一个 bit 变化，这里就会失败。
BASELINE_MEAN_HEALTH = {
    "none": 90.100555327721,
    "low": 72.268147540589,
    "high": 42.661295296109,
}


def _model(preset: str, seed: int = 20261031, duration: float = 90.0) -> WeatherModel:
    return WeatherModel(seed=seed, preset=preset, duration_s=duration, cfg=CFG)


# ---------------------------------------------------------------------------
# 1. 确定性：同 seed/preset 两次生成序列逐点相等
# ---------------------------------------------------------------------------
def test_weather_determinism() -> None:
    a = _model("dust")
    b = _model("dust")
    da = [s.to_dict() for s in a.samples()]
    db = [s.to_dict() for s in b.samples()]
    assert da == db, "同 (seed, preset, duration) 两次生成的天气序列不一致"
    assert len(da) == 181, f"90s / 0.5s 网格应有 181 个点，实际 {len(da)}"

    # 插值查询也必须确定性
    for t in (0.0, 3.7, 45.0, 88.8, 90.0, 120.0):
        assert a.at(t).to_dict() == b.at(t).to_dict(), f"t={t} 的插值结果不确定"

    # 不同 preset / 不同 seed 应给出不同曲线（否则天气系统没有区分度）
    assert [s.link_penalty for s in _model("rain").samples()] != \
           [s.link_penalty for s in a.samples()], "不同天气的扣分曲线不应相同"
    assert [s.link_penalty for s in _model("dust", seed=7).samples()] != \
           [s.link_penalty for s in a.samples()], "不同 seed 的天气曲线不应相同"

    # 运行中 set_preset 等价于"新建同参数模型"（确定性：与切换时机无关）
    switched = _model("rain")
    switched.set_preset("dust")
    assert [s.to_dict() for s in switched.samples()] == da, \
        "set_preset(dust) 应等价于新建 dust 模型（整条重建、与切换时刻无关）"
    print("[OK] 天气曲线确定性（181 点逐点一致；不同 preset/seed 有区分度；"
          "set_preset 可复现）")


# ---------------------------------------------------------------------------
# 2. clear 零衰减：apply_health 恒等
# ---------------------------------------------------------------------------
def test_clear_zero_decay() -> None:
    # 契约 §4.6.6 原文示例写法 WeatherModel("clear") 也能用
    m = WeatherModel("clear")
    assert m.preset == "clear"
    for s in m.samples():
        assert s.link_penalty == 0.0, f"clear 的链路扣分必须恒 0，实际 {s.link_penalty}"
        assert s.intensity == 0.0
    for t in (0.0, 10.0, 45.0, 89.9):
        assert m.apply_health(88.0, m.at(t)) == 88.0, "clear 下 apply_health 必须是恒等变换"
        assert m.apply_health(1.0, m.at(t)) == 1.0
        assert m.apply_health(100.0, m.at(t)) == 100.0
    assert m.mean_penalty() == 0.0
    assert WEATHER_PRESETS["clear"].link_penalty_max == 0.0
    print("[OK] clear 零衰减：link_penalty 恒 0，apply_health 恒等")


# ---------------------------------------------------------------------------
# 3. 衰减上界与单调性：1 <= apply_health <= 100，且对 base 单调不增
# ---------------------------------------------------------------------------
def test_penalty_bounds_and_monotonic() -> None:
    bases = [0.0, 1.0, 25.0, 55.5, 88.0, 100.0, 120.0, -5.0]
    for preset in PRESET_ORDER:
        w = _model(preset)
        cap = WEATHER_PRESETS[preset].link_penalty_max
        for s in w.samples():
            assert 0.0 <= s.link_penalty <= cap + 1e-9, \
                f"{preset} 的扣分越界：{s.link_penalty} vs 上界 {cap}"
            assert 0.0 <= s.intensity <= 1.0, f"{preset} 强度越界：{s.intensity}"
            assert s.visibility_km > 0.0
            healths = [w.apply_health(b, s) for b in bases]
            assert all(1.0 <= h <= 100.0 for h in healths), \
                f"{preset} apply_health 越界：{healths}"
            # 对 base 单调不增：base 越大 -> 结果不减（等价于固定扣分的单调性）
            ordered = sorted(zip(bases, healths))
            ys = [h for _, h in ordered]
            assert ys == sorted(ys), f"{preset} apply_health 对 base 非单调：{list(zip(bases, healths))}"
            assert w.apply_health(88.0, s) <= 88.0 + 1e-9, "天气扣分不应抬高健康度"

    # 极端样本：扣分超过 100 时 clamp 到 1（下限保护），扣分 0 时上限不超 100
    extreme = WeatherSample(ts=0.0, preset="dust", intensity=1.0, visibility_km=0.1,
                            link_penalty=999.0, wind_speed_mps=20.0)
    assert _model("dust").apply_health(100.0, extreme) == 1.0
    none_penalty = WeatherSample(ts=0.0, preset="clear", intensity=0.0,
                                 visibility_km=12.0, link_penalty=0.0,
                                 wind_speed_mps=1.5)
    assert _model("clear").apply_health(100.0, none_penalty) == 100.0
    assert _model("clear").apply_health(-3.0, none_penalty) == 1.0
    print("[OK] 扣分上界与 apply_health 上下界/单调性（1~100，clamp 生效）")


# ---------------------------------------------------------------------------
# 4. 向后兼容：LinkMonitor(weather=None) 与改动前基线逐位一致
# ---------------------------------------------------------------------------
def test_link_monitor_backward_compat() -> None:
    for level, expected in BASELINE_MEAN_HEALTH.items():
        mon = LinkMonitor(seed=20261031, weaknet_level=level, duration_s=90.0,
                          uav_ids=list(range(1, 31)), cfg=CFG)
        got = mon.mean_health()
        assert abs(got - expected) < 1e-9, \
            f"weather=None 的健康度均值偏离改动前基线：{level} {got!r} vs {expected!r}"

    # clear 的 WeatherModel 对链路序列必须是**恒等变换**（逐位，不是近似）
    plain = LinkMonitor(seed=20261031, weaknet_level="high", duration_s=90.0,
                        uav_ids=list(range(1, 31)), cfg=CFG)
    clear = LinkMonitor(seed=20261031, weaknet_level="high", duration_s=90.0,
                        uav_ids=list(range(1, 31)), cfg=CFG,
                        weather=_model("clear"))
    for uid in (1, 2, 17, 30):
        for i in range(0, 181):
            t = i * HEALTH_GRID_S
            assert plain.health_at(uid, t) == clear.health_at(uid, t), \
                f"clear 天气改变了 uav{uid} t={t} 的健康度"
    # weather=None 与气象 clear 的均值也逐位相同
    assert plain.mean_health() == clear.mean_health()
    # 而 dust 必须真的把健康度压下去（否则联动是假的）
    dust = LinkMonitor(seed=20261031, weaknet_level="high", duration_s=90.0,
                       uav_ids=list(range(1, 31)), cfg=CFG, weather=_model("dust"))
    assert dust.mean_health() < plain.mean_health() - 10.0, \
        f"dust 应显著降低健康度均值：{dust.mean_health()} vs {plain.mean_health()}"

    # 运行中切换：set_weather(preset) 重建序列；切回 None 应恢复改动前逐位数值
    plain.set_weather("dust")
    assert abs(plain.mean_health() - dust.mean_health()) < 1e-9, \
        "运行中切 dust 应等价于用 dust 构造"
    plain.set_weather(None)
    assert abs(plain.mean_health() - BASELINE_MEAN_HEALTH["high"]) < 1e-9, \
        "set_weather(None) 应恢复无天气基线"
    print(f"[OK] 向后兼容：weather=None/clear 与改动前逐位一致；"
          f"dust 均值 {dust.mean_health():.2f} < 无天气 {plain.mean_health():.2f}；"
          f"运行中 set_weather 可重建/可还原")


# ---------------------------------------------------------------------------
# 5. 端到端：Simulator + dust 能跑通，snapshot 含 weather 字段
# ---------------------------------------------------------------------------
def test_simulator_end_to_end_dust() -> None:
    sc = get_scenario("LIGHT")
    seed = int(CFG["simulation"]["random_seed"])
    trace = generate_trace(sc, seed, CFG)
    sim = Simulator(sc, trace, CFG, scheduler_name="dynamic", seed=seed,
                    weather_preset="dust")
    assert sim.weather.preset == "dust"
    assert sim.weather.label == "沙尘"

    snap = sim.snapshot()
    assert "weather" in snap, "Simulator.snapshot() 必须包含 weather 字段（契约 §4.6.4）"
    weather = snap["weather"]
    need = {"preset", "label", "intensity", "visibility_km", "link_penalty",
            "wind_speed_mps", "wind_dir_deg", "source", "fetched_at",
            "observed_at", "available"}
    assert set(weather) == need, f"weather 字段键名不符契约：{sorted(weather)}"
    assert weather["preset"] == "dust"
    assert weather["source"] == "synthetic"
    assert weather["fetched_at"] is None
    assert weather["observed_at"] is None
    assert len(weather["available"]) == 4
    assert [a["preset"] for a in weather["available"]] == list(PRESET_ORDER)
    assert available_presets() == weather["available"]

    # 跑一小段真实仿真（4 仿真秒 ≈ 0.4 真实秒），确认多线程路径不炸
    sched = create_scheduler("dynamic", sim, CFG)
    raw = sim.run(sched, duration_s=4.0)
    assert "weather_samples" in raw, "Simulator.run() 结果应包含 weather_samples"
    # 天气曲线覆盖**整个工况时长**（LIGHT=30s，而非本次 run 的 4s）——
    # 链路健康度序列也是按工况时长预生成的，天气必须与之同网格。
    assert len(raw["weather_samples"]) == int(round(sc.duration_s / HEALTH_GRID_S)) + 1, \
        f"天气样本数应覆盖工况时长，实际 {len(raw['weather_samples'])}"
    assert raw["weather_samples"][0]["preset"] == "dust"
    snap2 = sim.snapshot()
    assert snap2["weather"]["preset"] == "dust"
    assert 0.0 <= snap2["weather"]["intensity"] <= 1.0
    # 无效天气名回退 clear（配置容错；HTTP 层才返回 400）
    assert not is_valid_preset("typhoon")
    fallback = Simulator(sc, trace, CFG, scheduler_name="dynamic", seed=seed,
                         weather_preset="typhoon")
    assert fallback.weather.preset == "clear"
    print(f"[OK] 端到端：dust 能跑通，snapshot/weather_samples 字段齐全"
          f"（weather_samples={len(raw['weather_samples'])}，"
          f"链路均值={sim.link_monitor.mean_health():.2f}）")


# ---------------------------------------------------------------------------
# 6. 回归：clear 与"不带 weather 参数"的确定性输入逐项相等
# ---------------------------------------------------------------------------
def test_clear_regression_equals_no_weather() -> None:
    sc = get_scenario("PEAK")
    seed = int(CFG["simulation"]["random_seed"])
    trace = generate_trace(sc, seed, CFG)
    uav_ids = list(range(1, int(sc.uav_count) + 1))

    no_weather = Simulator(sc, trace, CFG, scheduler_name="fcfs", seed=seed)
    clear = Simulator(sc, trace, CFG, scheduler_name="fcfs", seed=seed,
                      weather_preset="clear")
    assert no_weather.weather.preset == "clear"
    assert clear.weather.preset == "clear"

    # (a) 链路健康度序列逐位相等（含 1.0s 采样间隔的插值点）
    m0, m1 = no_weather.link_monitor, clear.link_monitor
    for uid in (1, 7, 15, 30):
        for i in range(0, 181):
            t = i * HEALTH_GRID_S
            assert m0.health_at(uid, t) == m1.health_at(uid, t), \
                f"clear 改变了 uav{uid} t={t} 的链路健康度"
        for t in (0.3, 12.7, 45.25, 77.7, 90.0):
            assert m0.health_at(uid, t) == m1.health_at(uid, t)
    assert m0.mean_health() == m1.mean_health()
    assert all(sample.health == sample.health for sample in m0.samples())  # 采样结构正常

    # (b) 任务标定逐项相等：同一条到达轨迹 -> 同一批 Task（service_s / link_health）
    checked = 0
    for uid in uav_ids:
        for ev in trace.events_of(uid):
            t0 = no_weather.make_task(uid, ev.task_type, ev.data_size_mb, ev.t)
            t1 = clear.make_task(uid, ev.task_type, ev.data_size_mb, ev.t)
            assert t0.service_s == t1.service_s, \
                f"clear 改变了 uav{uid} @t={ev.t} 的 service_s"
            assert t0.link_health == t1.link_health
            assert t0.priority is t1.priority and t0.task_type is t1.task_type
            checked += 1
    assert checked > 2000, f"PEAK 轨迹应有数千到达事件用于标定比对，实际 {checked}"

    # (c) 天气扣分恒 0 且样本全部在 [0,1] 强度区间
    assert no_weather.weather.mean_penalty() == 0.0
    assert clear.weather.mean_penalty() == 0.0
    assert all(s["link_penalty"] == 0.0 for s in clear.weather_samples())
    print(f"[OK] clear 回归：链路序列 + {checked} 条任务标定逐项相等（=不带 weather 参数）")


# ---------------------------------------------------------------------------
# 7. （补充证据）clear 的 health 序列与 **改动前实现文件** 逐点逐位相等
#    直接加载 _backup/link_monitor-*.py（Lead 在改动前留的备份）作为"旧实现"，
#    与当前 LinkMonitor(weather=None / clear) 在 30 机 × 3 弱网档 × 181 网格点上
#    做浮点 == 比较（不是近似），从而证明 weather 改动对 clear 路径零影响。
# ---------------------------------------------------------------------------
def _load_prechange_link_monitor():
    """把 _backup 下改动前的 link_monitor.py 当作独立模块加载；找不到返回 None。"""
    import glob
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    # 优先用随测试一起维护的 fixture（tests/fixtures/link_monitor_prechange.py）；
    # 兼容旧的 _backup/ 布局（历史备份）。
    fixture_dir = Path(__file__).resolve().parent / "fixtures"
    candidates = sorted(glob.glob(str(fixture_dir / "link_monitor_prechange*.py")))
    if not candidates:
        candidates = sorted(glob.glob(str(root / "_backup" / "link_monitor-*.py")))
    if not candidates:
        return None
    spec = importlib.util.spec_from_file_location("link_monitor_prechange", candidates[-1])
    module = importlib.util.module_from_spec(spec)
    # 必须先注册进 sys.modules：旧实现里的 @dataclass 会用 ``cls.__module__``
    # 反查 sys.modules 来解析字段类型，未注册会抛 AttributeError。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def test_clear_series_matches_prechange_backup() -> None:
    old = _load_prechange_link_monitor()
    if old is None:
        print("[SKIP] 未找到 _backup/link_monitor-*.py 改动前备份，跳过逐点对拍")
        return
    uav_ids = list(range(1, 31))
    checked = 0
    for level in ("none", "low", "high"):
        old_mon = old.LinkMonitor(seed=20261031, weaknet_level=level, duration_s=90.0,
                                  uav_ids=uav_ids, cfg=CFG)
        new_none = LinkMonitor(seed=20261031, weaknet_level=level, duration_s=90.0,
                               uav_ids=uav_ids, cfg=CFG, weather=None)
        new_clear = LinkMonitor(seed=20261031, weaknet_level=level, duration_s=90.0,
                                uav_ids=uav_ids, cfg=CFG, weather=_model("clear"))
        for uid in uav_ids:
            for i in range(0, int(round(90.0 / HEALTH_GRID_S)) + 1):
                t = i * HEALTH_GRID_S
                h_old = old_mon.health_at(uid, t)
                h_none = new_none.health_at(uid, t)
                h_clear = new_clear.health_at(uid, t)
                assert h_old == h_none, \
                    f"weather=None 与改动前实现不一致：{level} uav{uid} t={t} {h_old!r} != {h_none!r}"
                assert h_old == h_clear, \
                    f"weather=clear 与改动前实现不一致：{level} uav{uid} t={t} {h_old!r} != {h_clear!r}"
                checked += 1
            # 网格之间的线性插值点也逐位对拍
            for t in (0.37, 12.34, 45.67, 77.77, 89.99):
                assert old_mon.health_at(uid, t) == new_none.health_at(uid, t)
                assert old_mon.health_at(uid, t) == new_clear.health_at(uid, t)
                checked += 1
        assert old_mon.mean_health() == new_none.mean_health() == new_clear.mean_health()
    print(f"[OK] clear 健康度序列与改动前实现(fixture)逐位相等"
          f"（{checked} 个 (uav, 弱网档, 时刻) 点，浮点 == 全部成立）")


if __name__ == "__main__":
    test_weather_determinism()
    test_clear_zero_decay()
    test_penalty_bounds_and_monotonic()
    test_link_monitor_backward_compat()
    test_simulator_end_to_end_dust()
    test_clear_regression_equals_no_weather()
    test_clear_series_matches_prechange_backup()
    print("\n所有天气联动测试通过 [PASS]")
