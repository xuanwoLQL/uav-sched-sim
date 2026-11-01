"""
tests/test_end_to_end.py —— 端到端集成测试
=============================================================================

覆盖点：
    1. **可复现性**：同一 (工况, seed) 两次生成的 WorkloadTrace 逐元素相同
       —— 这是"控制变量法"的前提。
    2. 单组实验全链路跑通：generate_trace -> Simulator -> Scheduler -> metrics
       -> RunResult -> JSON/CSV 落盘。
    3. **指标口径契约**：`compute_metrics()` 返回的键必须全部落在
       `models.METRIC_SPEC` 内，且数值有限（不能出现 NaN/Inf）。
    4. 指标合理性：吞吐量 > 0、完成率 ∈ [0,100]、饥饿率 ∈ [0,100]。
    5. MySQL 幂等建库 + 入库（数据库不可用时跳过，不影响其它断言）。
    6. 中文字体可用（图表不乱码的前提）。
"""

from __future__ import annotations

import json
import math
import sys

# Windows 控制台默认 cp936，遇到 GBK 不支持的符号会让测试假失败
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from uav_sim.config import load_config  # noqa: E402
from uav_sim.metrics import compute_metrics  # noqa: E402
from uav_sim.models import METRIC_SPEC  # noqa: E402
from uav_sim.scenarios import calibrate, generate_trace, get_scenario  # noqa: E402

CFG = load_config()


def test_trace_determinism() -> None:
    """同 (工况, seed) 的输入轨迹必须逐元素一致（控制变量法的物质基础）。"""
    for code in ("LIGHT", "NORMAL", "PEAK"):
        sc = get_scenario(code)
        t1 = generate_trace(sc, 20261031, CFG)
        t2 = generate_trace(sc, 20261031, CFG)
        assert t1.events == t2.events, f"{code} 任务到达轨迹不可复现"
        assert t1.churn == t2.churn, f"{code} 上下线轨迹不可复现"
        # 不同 seed 必须产生不同轨迹（否则 seed 没起作用）
        t3 = generate_trace(sc, 20261032, CFG)
        assert t3.events != t1.events, f"{code} 不同 seed 产出了相同轨迹"
        print(f"[OK] {code} 轨迹确定性：{len(t1.events)} 条到达事件，"
              f"{len(t1.churn)} 条上下线事件")


def test_trace_shape() -> None:
    """轨迹结构合理性：时间升序、优先级构成符合工况设定、数据量在区间内。"""
    for code in ("LIGHT", "NORMAL", "PEAK"):
        sc = get_scenario(code)
        tr = generate_trace(sc, 20261031, CFG)
        assert tr.events, f"{code} 无任务事件"
        ts = [e.t for e in tr.events]
        assert ts == sorted(ts), f"{code} 事件未按时间升序"
        assert max(ts) <= sc.duration_s + 1e-6, f"{code} 存在超出仿真时长的事件"
        for e in tr.events:
            if e.task_type.priority.name == "HIGH":
                lo, hi = sc.high_size_range_mb
            else:
                lo, hi = sc.low_size_range_mb
            assert lo - 1e-6 <= e.data_size_mb <= hi + 1e-6, (
                f"{code} 任务数据量 {e.data_size_mb} 超出区间 ({lo},{hi})")
        counts = tr.counts()
        print(f"[OK] {code} 轨迹结构：总 {counts['total']} "
              f"(高 {counts['high']} / 低 {counts['low']})")


def test_calibration_sane() -> None:
    """三工况标定结果必须落在目标 ±25% 内（与 PDF 预期堆积程度对齐）。"""
    for r in (calibrate(get_scenario(c), CFG) for c in ("LIGHT", "NORMAL", "PEAK")):
        assert r["verdict"] == "ok", f"{r['code']} 标定未达标：{r}"
        assert 0.05 < r["load_factor"] < 2.0, r
        assert r["n_tasks"] > 0
        print(f"[OK] {r['code']} 标定：负载率 {r['load_factor']:.2f} "
              f"(目标 {r['target_load_factor']:.2f})，"
              f"重传放大 {r['retry_multiplier']:.2f}x，任务 {r['n_tasks']} 个")


def test_single_run_end_to_end() -> None:
    """单组实验全链路：跑完、算指标、落 JSON/CSV。"""
    from uav_sim.experiment import ExperimentRunner

    with tempfile.TemporaryDirectory() as tmp:
        runner = ExperimentRunner(CFG, results_dir=tmp)
        res = runner.run_one("LIGHT", "dynamic", persist=False)
        assert res.error is None, f"实验失败：{res.error}"

        # ---- 指标口径契约 ----
        assert set(res.metrics) == set(METRIC_SPEC), (
            "指标键必须与 METRIC_SPEC 完全一致；"
            f"多出 {set(res.metrics) - set(METRIC_SPEC)}，"
            f"缺少 {set(METRIC_SPEC) - set(res.metrics)}")
        for k, v in res.metrics.items():
            assert isinstance(v, float) and math.isfinite(v), f"{k} 非有限数值: {v}"

        # ---- 指标合理性 ----
        assert res.metrics["throughput"] > 0, "吞吐量应 > 0"
        assert 0.0 <= res.metrics["completion_rate"] <= 100.0, res.metrics["completion_rate"]
        assert 0.0 <= res.metrics["starvation_rate"] <= 100.0
        assert 0.0 <= res.metrics["resource_utilization"] <= 100.0 + 1e-6
        assert res.metrics["runtime_wall_s"] > 0

        # ---- 落盘产物 ----
        assert len(res.task_records) > 0, "任务明细为空"
        assert len(res.queue_samples) > 0, "队列采样为空"
        assert len(res.link_samples) > 0, "链路采样为空"
        raw = Path(tmp) / "raw" / f"{res.exp_id}.json"
        assert raw.exists(), f"未生成结果 JSON：{raw}"
        data = json.loads(raw.read_text(encoding="utf-8"))
        assert data["exp_id"] == res.exp_id
        assert "throughput" in data["metrics"]

        print(f"[OK] 单组端到端：{res.exp_id}  吞吐={res.metrics['throughput']:.2f}/s  "
              f"任务 {len(res.task_records)} 条  队列采样 {len(res.queue_samples)} 条  "
              f"实耗 {res.metrics['runtime_wall_s']:.1f}s")


def test_metrics_keys_only_from_spec() -> None:
    """空输入也要返回完整键集（保证入库/出图列头稳定）。"""
    m = compute_metrics(tasks=[], duration_s=10.0, uav_count=5, capacity_mbps=1000.0,
                        queue_samples=[], sched_stats={}, limiter_status={},
                        overload_seconds=0.0, starvation_threshold_ms=15000.0)
    assert set(m) == set(METRIC_SPEC)
    assert all(v == 0.0 for v in m.values())
    print("[OK] 空输入返回完整零值指标集")


def test_db_roundtrip() -> None:
    """MySQL 幂等建库 + 一次真实入库（数据库不可用时跳过）。"""
    from uav_sim.db import DBWriter
    from uav_sim.experiment import ExperimentRunner

    writer = DBWriter(CFG)
    if not writer.connect():
        print("[SKIP] MySQL 不可用，跳过入库测试（这是预期的优雅降级路径）")
        writer.close()
        return
    try:
        assert writer.ensure_schema(), f"建库失败：{writer.last_error}"
        assert writer.ensure_schema(), "建库脚本必须幂等（第二次执行也要成功）"

        # 用临时目录，避免测试写出的 JSON/CSV 污染正式实验数据集
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(CFG, results_dir=tmp)
            res = runner.run_one("LIGHT", "fcfs", persist=True)
            assert res.error is None
            rows = writer.query(
                "SELECT COUNT(*) FROM exp_metric WHERE exp_id=%s", (res.exp_id,))
            assert rows and int(rows[0][0]) == len(METRIC_SPEC), (
                f"exp_metric 应有 {len(METRIC_SPEC)} 行，实际 {rows}")
            rows = writer.query(
                "SELECT COUNT(*) FROM task_record WHERE exp_id=%s", (res.exp_id,))
            assert rows and int(rows[0][0]) == len(res.task_records)
            print(f"[OK] MySQL 入库：exp_metric {len(METRIC_SPEC)} 行 / "
                  f"task_record {int(rows[0][0])} 行（exp_id={res.exp_id}）")
            # 自清理：测试写入的行不应污染正式实验数据集（外键 ON DELETE CASCADE）
            writer.query("DELETE FROM experiment WHERE exp_id=%s", (res.exp_id,))
            left = writer.query("SELECT COUNT(*) FROM experiment WHERE exp_id=%s", (res.exp_id,))
            assert not left or int(left[0][0]) == 0, "测试数据未能清理干净"
            print("[OK] 测试数据已自清理（不污染正式实验数据集）")
    finally:
        writer.close()


def test_chinese_font() -> None:
    """matplotlib 中文字体（图表中文不乱码的前提）。"""
    from uav_sim.visualize import setup_chinese_font

    font = setup_chinese_font()
    assert font, "未找到可用中文字体，图表中文会显示为方块"
    print(f"[OK] matplotlib 中文字体：{font}")


if __name__ == "__main__":
    test_trace_determinism()
    test_trace_shape()
    test_calibration_sane()
    test_metrics_keys_only_from_spec()
    test_chinese_font()
    test_single_run_end_to_end()
    test_db_roundtrip()
    print("\n所有端到端测试通过 [PASS]")
