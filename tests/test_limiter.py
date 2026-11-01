"""
tests/test_limiter.py —— 系统资源限流器单元测试
=============================================================================

覆盖的 OS 原理点（对应 PDF 实验 3.3 验收标准「资源限流」）：
    * 并发数**永不超上限**（信号量语义）
    * 单任务超上限 -> 返回 REJECT_LARGE，并可被切分为 N 片（业务不丢）
    * 单机配额（per_uav_quota）能防止一台无人机霸占所有槽位
    * release 后槽位可复用；重复 release 不会把计数打成负数
    * 「占位-释放」严格配对（否则就是经典资源泄漏）
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

from uav_sim.models import Task, TaskPriority, TaskState, TaskType  # noqa: E402
from uav_sim.resource_limiter import ResourceLimiter  # noqa: E402


def make_task(tid: str, uav: int = 1, size_mb: float = 50.0,
              service_s: float = 1.0) -> Task:
    return Task(task_id=tid, uav_id=uav, priority=TaskPriority.HIGH,
                task_type=TaskType.IMAGERY, data_size_mb=size_mb,
                submit_ts=0.0, service_s=service_s)


def test_concurrency_cap() -> None:
    lim = ResourceLimiter(max_concurrent=5, max_task_memory_mb=2048,
                          per_uav_quota=99)
    admitted = []
    for i in range(8):
        rc = lim.try_admit(make_task(f"T{i}", uav=i))
        if rc == "OK":
            admitted.append(f"T{i}")
        else:
            assert rc == "THROTTLE", rc
    assert lim.running == 5, lim.running
    assert lim.peak_running <= 5
    assert lim.throttled == 3
    print(f"[OK] 并发上限 5 生效（admitted={len(admitted)}, throttled={lim.throttled}）")


def test_release_recycles_slot() -> None:
    lim = ResourceLimiter(max_concurrent=2, max_task_memory_mb=2048, per_uav_quota=99)
    t1, t2 = make_task("A"), make_task("B")
    assert lim.try_admit(t1) == "OK"
    assert lim.try_admit(t2) == "OK"
    assert lim.try_admit(make_task("C")) == "THROTTLE"
    lim.release(t1)
    assert lim.running == 1
    assert lim.try_admit(make_task("C")) == "OK", "释放后应能复用槽位"
    lim.release(t1)  # 重复释放
    assert lim.running == 2, "重复 release 不得把计数打成负数/多减"
    print("[OK] release 复用槽位；重复 release 幂等")


def test_reject_large_and_split() -> None:
    lim = ResourceLimiter(max_concurrent=8, max_task_memory_mb=500,
                          per_uav_quota=99, enable_chunking=True)
    big = make_task("BIG", size_mb=1200.0, service_s=8.0)
    rc = lim.try_admit(big)
    assert rc == "REJECT_LARGE", rc
    assert lim.rejected_large == 1

    segs = lim.split_task(big)
    assert len(segs) == 3, f"1200MB / 500MB 上限应切 3 片，实际 {len(segs)}"
    assert all(s.segment_total == 3 for s in segs)
    assert [s.segment_index for s in segs] == [0, 1, 2]
    assert all(s.parent_task_id == "BIG" for s in segs)
    assert abs(sum(s.data_size_mb for s in segs) - 1200.0) < 1e-6, "分片数据量之和应守恒"
    assert abs(sum(s.service_s for s in segs) - 8.0) < 1e-6, "分片服务时长之和应守恒"
    assert big.state is TaskState.DROPPED and big.drop_reason == "chunked_into_segments"
    assert lim.chunked == 1 and lim.segments_created == 3
    print("[OK] 大任务拒绝 + 分片（数据量与服务时长守恒）")


def test_no_chunking_when_disabled() -> None:
    lim = ResourceLimiter(max_concurrent=8, max_task_memory_mb=500,
                          per_uav_quota=99, enable_chunking=False)
    big = make_task("BIG", size_mb=1200.0)
    assert lim.try_admit(big) == "REJECT_LARGE"
    assert lim.split_task(big) == [], "关闭分片时应返回空列表"
    print("[OK] 关闭分片时返回空列表")


def test_per_uav_quota() -> None:
    """单机配额：一台无人机最多占用 per_uav_quota 个槽位，防止饿死其他节点。"""
    lim = ResourceLimiter(max_concurrent=8, max_task_memory_mb=2048, per_uav_quota=2)
    assert lim.try_admit(make_task("U1-a", uav=1)) == "OK"
    assert lim.try_admit(make_task("U1-b", uav=1)) == "OK"
    assert lim.try_admit(make_task("U1-c", uav=1)) == "QUOTA", "第 3 个应被单机配额挡住"
    assert lim.try_admit(make_task("U2-a", uav=2)) == "OK", "别的无人机仍可进入"
    assert lim.running == 3
    print(f"[OK] 单机配额 2 生效（quota_blocked={lim.quota_blocked}）")


def test_status_keys() -> None:
    lim = ResourceLimiter(max_concurrent=4, max_task_memory_mb=500, per_uav_quota=1)
    st = lim.get_status()
    for key in ("running", "available", "max_concurrent", "throttled",
                "rejected_large", "chunked", "released", "per_uav_running"):
        assert key in st, f"get_status 缺少契约要求的键 {key}"
    assert st["available"] == 4 and st["running"] == 0
    print("[OK] get_status 键齐全（契约 §4.5）")


def test_acquire_compat() -> None:
    """保留指导书签名的阻塞式 acquire（timeout=0 等价非阻塞）。"""
    lim = ResourceLimiter(max_concurrent=1, max_task_memory_mb=2048, per_uav_quota=99)
    assert lim.acquire(make_task("A"), timeout_s=0.0) is True
    assert lim.acquire(make_task("B"), timeout_s=0.0) is False
    lim.release(make_task("A"))
    assert lim.acquire(make_task("B"), timeout_s=0.1) is True
    print("[OK] acquire 兼容接口（阻塞/非阻塞两种语义）")


if __name__ == "__main__":
    test_concurrency_cap()
    test_release_recycles_slot()
    test_reject_large_and_split()
    test_no_chunking_when_disabled()
    test_per_uav_quota()
    test_status_keys()
    test_acquire_compat()
    print("\n所有限流器测试通过 [PASS]")
