"""
tests/test_telemetry.py —— OS 微观透视遥测（快照字段 / 混沌注入 / 死锁看门狗）
=============================================================================

覆盖老师要求的"消除黑盒感"三件事：
    * 快照里必须有：线程五态 / 绑核 / 当前任务 / 切换间隔 / 队列明细 /
      权重四因子 / 线程池 / 内核指标 / 事件日志；
    * 混沌注入必须真的改变系统状态（并发洪峰 -> submitted 增加；
      弱网风暴 -> chaos.weaknet_active）；
    * 死锁注入必须被看门狗在 3 仿真秒后强制回收（deadlock_recovered +1）。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from uav_sim.config import load_config  # noqa: E402
from uav_sim.scenarios import generate_trace, get_scenario  # noqa: E402
from uav_sim.scheduler import create_scheduler  # noqa: E402
from uav_sim.simulator import Simulator  # noqa: E402


def _make(scenario: str = "LIGHT"):
    cfg = load_config()
    cfg["runtime"]["time_scale"] = 0.02          # 加速：1 真实秒 = 50 仿真秒
    sc = get_scenario(scenario)
    tr = generate_trace(sc, 20261031, cfg)
    sim = Simulator(sc, tr, cfg, "dynamic", 20261031)
    sched = create_scheduler("dynamic", sim, cfg)
    return sim, sched, sc


def _start(sim, sched, sc):
    th = threading.Thread(target=lambda: sim.run(sched, duration_s=sc.duration_s), daemon=True)
    th.start()
    time.sleep(0.8)
    return th


def _stop(sim, th):
    sim.stop_event.set()
    th.join(timeout=8)


def test_snapshot_has_inspection_fields() -> None:
    sim, sched, sc = _make("LIGHT")
    th = _start(sim, sched, sc)
    try:
        snap = sim.snapshot()
        for k in ("queue_tasks", "weight", "pool", "kernel", "chaos",
                  "trace_tail", "uav_states", "history"):
            assert k in snap, f"快照缺字段 {k}"
        u = snap["uav_states"]["1"]
        for k in ("state", "core", "task_id", "switch_ms", "wait_ms"):
            assert k in u, f"uav_states 缺字段 {k}"
        assert snap["pool"]["total"] == sim.workers
        assert snap["kernel"]["trace_events"] > 0
        assert snap["kernel"]["cpu_user_s"] >= 0.0
        assert len(snap["trace_tail"]) > 0
        kinds = {e["k"] for e in snap["trace_tail"]}
        assert "enqueue" in kinds and "dispatch" in kinds
        print(f"[OK] 快照字段齐全：事件 {snap['kernel']['trace_events']} 条，"
              f"池 {snap['pool']['active']}/{snap['pool']['total']}，"
              f"CPU 用户 {snap['kernel']['cpu_user_s']:.2f}s / 内核 {snap['kernel']['cpu_kernel_s']:.2f}s")
    finally:
        _stop(sim, th)


def test_weight_candidates_present() -> None:
    sim, sched, sc = _make("LIGHT")
    th = _start(sim, sched, sc)
    try:
        w = sim.snapshot()["weight"]
        assert w.get("model") == "dynamic"
        assert "high" in w and "low" in w
        for side in ("high", "low"):
            for k in ("Q", "W", "L", "S", "weight"):
                assert k in w[side], f"{side} 缺因子 {k}"
        print(f"[OK] 权重天平：高优 {w['high']['weight']:.3f} / 低优 {w['low']['weight']:.3f}")
    finally:
        _stop(sim, th)


def test_chaos_burst_and_weaknet() -> None:
    sim, sched, sc = _make("LIGHT")
    th = _start(sim, sched, sc)
    try:
        before = sim.snapshot()["counters"]["submitted"]
        r = sim.inject_burst(20)
        assert r["ok"] and r["injected"] > 0
        time.sleep(0.25)
        after = sim.snapshot()["counters"]["submitted"]
        assert after > before, (before, after)

        w = sim.inject_weaknet(duration_s=30.0, frac=0.5)
        assert w["ok"] and w["uav_count"] >= 1
        snap = sim.snapshot()
        assert snap["chaos"]["weaknet_active"] is True
        print(f"[OK] 混沌注入：洪峰 +{r['injected']}（提交 {before}->{after}），"
              f"弱网风暴 {w['uav_count']} 架生效")
    finally:
        _stop(sim, th)


def test_deadlock_watchdog_recovers() -> None:
    sim, sched, sc = _make("PEAK")
    th = _start(sim, sched, sc)
    try:
        # 等到确实有任务在跑（轻载可能一个都没有）
        ok = False
        for _ in range(60):
            if sim.snapshot()["running_count"] > 0:
                ok = True
                break
            time.sleep(0.1)
        assert ok, "10 秒内没有出现运行中的任务，无法测试死锁"

        r = sim.inject_deadlock(hold_s=30.0)
        assert r["ok"], r
        # 看门狗阈值是 3 仿真秒；time_scale=0.02 -> 0.06 真实秒，留足余量
        recovered = 0
        for _ in range(40):
            k = sim.snapshot()["kernel"]
            recovered = int(k["deadlock_recovered"])
            if recovered > 0:
                break
            time.sleep(0.1)
        assert recovered > 0, "看门狗没有回收死锁任务"
        print(f"[OK] 死锁看门狗：注入后强制回收 {recovered} 次（可疑 "
              f"{sim.snapshot()['kernel']['deadlock_suspects']}）")
    finally:
        _stop(sim, th)


if __name__ == "__main__":  # pragma: no cover
    test_snapshot_has_inspection_fields()
    test_weight_candidates_present()
    test_chaos_burst_and_weaknet()
    test_deadlock_watchdog_recovers()
    print("\n所有遥测测试通过 [PASS]")
