"""
tests/test_queue.py —— 两级就绪队列单元测试
=============================================================================

覆盖的 OS 原理点（对应 PDF 实验 3.2 验收标准）：
    * 高/低优先级任务正确进入对应队列
    * 严格优先出队 / 跨队列全局 FIFO 的差异
    * 饥饿补偿通道能捞出等待最久的低优先级任务
    * **等待时间记账口径**：出队结账、错峰放回要回滚（否则等待时延被刷爆）
    * 队列满时返回 False（调用方须置 DROPPED）
    * `lock_enabled=False` 的演示模式可用（课堂反面教材）
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
from uav_sim.queue_manager import MultiLevelQueue  # noqa: E402


def make_task(tid: str, prio: TaskPriority, uav: int = 1, service_s: float = 1.0) -> Task:
    t = Task(task_id=tid, uav_id=uav, priority=prio,
             task_type=TaskType.IMAGERY if prio is TaskPriority.HIGH else TaskType.HEARTBEAT,
             data_size_mb=100.0, submit_ts=0.0, service_s=service_s)
    t.enqueue_ts = 0.0
    return t


def test_enqueue_by_priority() -> None:
    q = MultiLevelQueue(capacity=10)
    assert q.enqueue(make_task("H1", TaskPriority.HIGH), 0.0)
    assert q.enqueue(make_task("L1", TaskPriority.LOW), 0.0)
    assert q.high_depth == 1 and q.low_depth == 1 and q.total_depth == 2
    assert q.peek_high().task_id == "H1"
    assert q.peek_low().task_id == "L1"
    print("[OK] 按优先级分类入队")


def test_strict_priority_dequeue() -> None:
    """dequeue(prefer_high=True) 必须先取高优先级。"""
    q = MultiLevelQueue(capacity=10)
    q.enqueue(make_task("H1", TaskPriority.HIGH), 0.0)
    q.enqueue(make_task("L1", TaskPriority.LOW), 0.0)
    first = q.dequeue(prefer_high=True, now=0.0)
    assert first is not None and first.task_id == "H1", "严格优先应先取高优先级"
    second = q.dequeue(prefer_high=True, now=0.0)
    assert second is not None and second.task_id == "L1"
    print("[OK] 严格优先级出队")


def test_queue_full_returns_false() -> None:
    q = MultiLevelQueue(capacity=2)
    assert q.enqueue(make_task("A", TaskPriority.HIGH), 0.0)
    assert q.enqueue(make_task("B", TaskPriority.LOW), 0.0)
    assert not q.enqueue(make_task("C", TaskPriority.HIGH), 0.0), "队列满应返回 False"
    assert q.dropped_full == 1
    print("[OK] 队列满返回 False 并计数")


def test_wait_accounting_and_push_front_rollback() -> None:
    """出队结账、push_front 回滚 —— 错峰路径不能把等待时延刷爆。"""
    q = MultiLevelQueue(capacity=10)
    t = make_task("H1", TaskPriority.HIGH)
    q.enqueue(t, now=0.0)

    # 在队列里躺了 2 秒 -> 窥视时等待 2000 ms
    assert abs(t.wait_ms(2.0) - 2000.0) < 1e-6

    # 出队：结账 2000 ms
    popped = q.dequeue_high(now=2.0)
    assert popped is t
    assert abs(t.wait_ms_total - 2000.0) < 1e-6, f"出队应结账 2000ms，实际 {t.wait_ms_total}"
    assert t.state is TaskState.RUNNING

    # 错峰放回队首：必须把 2000 ms 扣回去，且排队位置不变
    q.push_front(t, now=2.0)
    assert t.state is TaskState.READY
    assert abs(t.wait_ms_total) < 1e-6, f"push_front 应回滚等待记账，实际 {t.wait_ms_total}"
    assert q.peek_high() is t, "push_front 应放回队首"
    assert abs(t.enqueue_ts - 0.0) < 1e-6, "push_front 应恢复原入队时刻"

    # 恢复后 enqueue_ts 仍是 0，所以到 t=5 时总等待 = 5s（2000 已被回滚、不重复计）
    assert abs(t.wait_ms(5.0) - 5000.0) < 1e-6, f"实际 {t.wait_ms(5.0)}"
    # 关键断言：如果 push_front 没回滚，就会变成 2000(已结) + 5000(重复计) = 7000ms
    assert t.wait_ms(5.0) < 7000.0, "push_front 未回滚等待记账，等待时间被重复累计"
    print("[OK] 等待时延记账 / 错峰回滚")


def test_starving_channel() -> None:
    """饥饿补偿通道应捞出等待超过阈值的低优先级任务（且只捞超阈值的）。"""
    q = MultiLevelQueue(capacity=10, starvation_threshold_ms=1000.0)
    fresh = make_task("L-new", TaskPriority.LOW)
    old = make_task("L-old", TaskPriority.LOW)
    q.enqueue(fresh, now=9.5)   # 到 t=10 只等了 0.5s
    q.enqueue(old, now=0.0)     # 到 t=10 等了 10s
    got = q.dequeue_starving(now=10.0, threshold_ms=1000.0)
    assert got is old, f"应捞出等待最久的 L-old，实际 {getattr(got, 'task_id', None)}"
    assert abs(old.wait_ms_total - 10000.0) < 1e-6
    assert q.dequeue_starving(now=10.0, threshold_ms=1000.0) is None, "其余任务未超阈值，不应被捞出"
    print("[OK] 饥饿补偿通道")


def test_requeue_counts() -> None:
    q = MultiLevelQueue(capacity=10)
    t = make_task("H1", TaskPriority.HIGH)
    q.enqueue(t, now=0.0)
    q.dequeue_high(now=1.0)
    q.requeue(t, now=1.0)
    assert t.requeue_count == 1
    assert t.state is TaskState.READY
    assert q.high_depth == 1
    print("[OK] requeue 计数与重新入队")


def test_congestion_normalization() -> None:
    """拥堵度按 congestion_ref 归一化（不能用 5000 的容量做分母，否则恒接近 0）。"""
    q = MultiLevelQueue(capacity=5000, congestion_ref=32.0)
    for i in range(16):
        q.enqueue(make_task(f"H{i}", TaskPriority.HIGH), 0.0)
    for i in range(16):
        q.enqueue(make_task(f"L{i}", TaskPriority.LOW), 0.0)
    assert abs(q.congestion_of(TaskPriority.HIGH) - 0.5) < 1e-6, q.congestion_of(TaskPriority.HIGH)
    assert abs(q.congestion_of(TaskPriority.LOW) - 0.5) < 1e-6
    assert abs(q.congestion() - 1.0) < 1e-6, "总深度 32 / ref 32 应为 1.0"
    print("[OK] 拥堵度归一化（按优先级分别计算）")


def test_unlocked_demo_mode() -> None:
    """教学演示模式：lock_enabled=False 时仍能正常工作（供课堂演示"去掉锁"的反面案例）。"""
    q = MultiLevelQueue(capacity=10, lock_enabled=False)
    for i in range(5):
        q.enqueue(make_task(f"T{i}", TaskPriority.HIGH), 0.0)
    got = q.dequeue_high(now=0.0)
    assert got is not None and got.task_id == "T0"
    assert q.high_depth == 4
    print("[OK] 无锁演示模式可用（真实竞态需多线程并发才能复现，见 PDF 思考题 2）")


if __name__ == "__main__":
    test_enqueue_by_priority()
    test_strict_priority_dequeue()
    test_queue_full_returns_false()
    test_wait_accounting_and_push_front_rollback()
    test_starving_channel()
    test_requeue_counts()
    test_congestion_normalization()
    test_unlocked_demo_mode()
    print("\n所有队列测试通过 [PASS]")
