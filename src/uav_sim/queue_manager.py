"""
queue_manager.py —— 两级就绪队列（多级反馈队列的隔离排队层）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 地面站的任务缓存池：高清影像 / LiDAR 点云等**核心勘查任务**与 GPS / 心跳 / 告警等**高频运维任务**混流涌入，必须有地方排队等待上传信道 |
| OS 原理层   | **多级就绪队列**：高、低两个独立就绪队列隔离排队，避免低优先级被高优先级洪流淹没；`enqueue/dequeue` 即 TCB 在就绪队列的入队/出队；队列长度即"就绪队列长度"，是判断系统是否过载的直接指标 |
| 算法设计层 | 双队列存储结构 + 分类入队 + 两级出队（严格优先 / 跨队列全局 FIFO）+ 饥饿检测与补偿通道 |
| 代码实现层 | `MultiLevelQueue`（`collections.deque` + `threading.Lock`） |

--------------------------------------------------------------------------
线程安全（教学重点，PDF 实验一思考题 2 的正面/反面教材）
--------------------------------------------------------------------------
* 正常模式 ``lock_enabled=True``：所有入队/出队/窥视都在 ``self._lock`` 内完成。
  deque 的 append/popleft 本身在 CPython 下是原子的，但"判空 -> 取头 -> 记等待"
  这三步是**复合操作**，不加锁就会出现"两个消费者取到同一个任务 / 丢失任务"。
* 演示模式 ``lock_enabled=False``：故意不加锁，用于课堂复现「去掉锁会数据错乱」。
  （见 ``tests/test_queue.py::test_unsafe_mode_can_lose_tasks``）

--------------------------------------------------------------------------
等待时延的记账口径（全项目统一，直接决定"平均等待时延 / 饥饿率"指标）
--------------------------------------------------------------------------
任务在队列里等待的时间必须在**出队时结账**，否则重复入队会重复计时::

    出队时： task.wait_ms_total += (now - task.enqueue_ts) × 1000
    重新入队： task.enqueue_ts = now
    窥视后放回（错峰）： push_front() 把上面加过的等待量**原样扣回**，避免反复
                        "取出-放回"把等待时间刷成天文数字
"""

from __future__ import annotations

import sys
import threading
from collections import deque
from contextlib import nullcontext
from itertools import islice
from pathlib import Path
from typing import Deque, List, Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.models import QueueSnapshot, Task, TaskPriority, TaskState

EPS = 1e-9


class MultiLevelQueue:
    """两级就绪队列管理器（契约 §4.3）。

    内部结构：
        self._high_queue: deque[Task]   核心勘查任务（HIGH）
        self._low_queue:  deque[Task]   运维任务（LOW）

    命名 ``_high_queue`` / ``_low_queue`` 是刻意与 `scheduler/base.py` 的
    非破坏性窥视（duck-typing）约定保持一致，避免调度器做破坏性出队。
    """

    def __init__(self, capacity: int = 5000,
                 starvation_threshold_ms: float = 3000.0,
                 lock_enabled: bool = True,
                 congestion_ref: float = 32.0) -> None:
        self.capacity = max(1, int(capacity))
        self.starvation_threshold_ms = float(starvation_threshold_ms)
        self.lock_enabled = bool(lock_enabled)
        #: 拥堵度归一化基准（"多少排队任务算拥堵"）。
        #: 为什么不用队列容量做分母：容量是 5000，实际峰值堆积只有几百，
        #: 用 5000 归一化会让 congestion() 恒在 0.05~0.1，动态权重的 Q 因子形同虚设。
        #: 经验取 ``4 × 工作线程数``：排队超过"4 轮工作量"即视为该队列已拥堵。
        self.congestion_ref: float = max(1.0, float(congestion_ref))

        self._high_queue: Deque[Task] = deque()
        self._low_queue: Deque[Task] = deque()
        self._lock = threading.Lock()

        # ---- 统计（供 report / 验收自检） ----
        self.enqueued_total = 0
        self.dequeued_total = 0
        self.dropped_full = 0
        self.max_depth_seen = 0
        self.max_high_seen = 0
        self.max_low_seen = 0

    # ------------------------------------------------------------------ 内部
    def _guard(self):
        """锁保护：演示模式（lock_enabled=False）下返回空上下文，故意不加锁。"""
        return self._lock if self.lock_enabled else nullcontext()

    def _queue_of(self, priority: TaskPriority) -> Deque[Task]:
        return self._high_queue if priority is TaskPriority.HIGH else self._low_queue

    def _track_depth(self) -> None:
        total = len(self._high_queue) + len(self._low_queue)
        if total > self.max_depth_seen:
            self.max_depth_seen = total
        if len(self._high_queue) > self.max_high_seen:
            self.max_high_seen = len(self._high_queue)
        if len(self._low_queue) > self.max_low_seen:
            self.max_low_seen = len(self._low_queue)

    # ------------------------------------------------------------ 只读快照
    def snapshot_tasks(self, limit: int = 40, now: float = 0.0) -> List[dict]:
        """按**出队顺序**列出前 ``limit`` 个就绪任务（供 3D「传送带」可视化）。

        严格只读：不记等待账、不改任何计数、不出队 —— 因此可以每 200 ms 调一次。
        高优队列在前、低优队列在后；每个元素带 data_size（方块大小）与 wait_ms
        （饥饿补偿倒计时的分子）。
        """
        out: List[dict] = []
        n = max(0, int(limit))
        if n == 0:
            return out
        with self._guard():
            for q, prio in ((self._high_queue, "HIGH"), (self._low_queue, "LOW")):
                for t in islice(q, n):
                    wait_ms = 0.0
                    if now > 0:
                        wait_ms = max(0.0, (now - float(getattr(t, "enqueue_ts", now))) * 1000.0)
                    out.append({
                        "task_id": t.task_id,
                        "uav_id": int(t.uav_id),
                        "priority": prio,
                        "task_type": t.task_type.value,
                        "data_size_mb": round(float(t.data_size_mb), 1),
                        "wait_ms": round(wait_ms, 1),
                        "retry": int(t.retry_count),
                    })
        return out

    # ------------------------------------------------------------ 入队 / 出队
    def enqueue(self, task: Task, now: float) -> bool:
        """按 ``task.priority`` 放入对应队列；队列已满返回 False（调用方须置 DROPPED）。

        对应 OS：进程被创建/唤醒后进入对应优先级的就绪队列尾。
        """
        if task is None:
            return False
        with self._guard():
            if len(self._high_queue) + len(self._low_queue) >= self.capacity:
                self.dropped_full += 1
                return False
            task.state = TaskState.READY
            task.enqueue_ts = float(now)
            self._queue_of(task.priority).append(task)
            self.enqueued_total += 1
            self._track_depth()
            return True

    def requeue(self, task: Task, now: float) -> bool:
        """未执行完 / 被抢占 / 弱网重传的任务放回**原优先级队列队尾**。

        ``task.requeue_count`` 自增 —— 该计数是「上下文切换」与「时间片轮转」
        教学量的直接证据。
        """
        if task is None:
            return False
        with self._guard():
            if len(self._high_queue) + len(self._low_queue) >= self.capacity:
                self.dropped_full += 1
                return False
            task.state = TaskState.READY
            task.enqueue_ts = float(now)
            task.requeue_count += 1
            self._queue_of(task.priority).append(task)
            self._track_depth()
            return True

    def push_front(self, task: Task, now: float) -> None:
        """错峰放回**队首**，并**扣回**出队时结账的那段等待（见模块 docstring）。

        只用于"窥视 -> 判限流 -> 放回"这条路径，避免把等待时间刷爆。
        """
        if task is None:
            return
        with self._guard():
            added = float(getattr(task, "_deq_added_ms", 0.0) or 0.0)
            # 注意：不能用 `or now` —— enqueue_ts 合法值可能就是 0.0（falsy），
            # 会把"恢复到 0.0"错写成 "恢复到当前时刻"，从而吃掉刚结的等待账。
            raw_prev = getattr(task, "_deq_prev_enq_ts", None)
            prev_enq = float(raw_prev) if raw_prev is not None else float(now)
            task.wait_ms_total = max(0.0, float(task.wait_ms_total) - added)
            task.enqueue_ts = prev_enq
            task.state = TaskState.READY
            self._queue_of(task.priority).appendleft(task)
            self._track_depth()

    def _pop_with_accounting(self, q: Deque[Task], now: float) -> Optional[Task]:
        """从队列头取出并结账等待时间（调用方负责持锁）。"""
        if not q:
            return None
        task = q.popleft()
        prev_enq = float(task.enqueue_ts)
        added_ms = max(0.0, float(now) - prev_enq) * 1000.0
        task.wait_ms_total = float(task.wait_ms_total) + added_ms
        # 供 push_front 回滚（错峰路径）
        task._deq_added_ms = added_ms
        task._deq_prev_enq_ts = prev_enq
        task.state = TaskState.RUNNING
        self.dequeued_total += 1
        return task

    def dequeue_high(self, now: float) -> Optional[Task]:
        """从高优先级队列取队首（就绪 -> 运行）。"""
        with self._guard():
            return self._pop_with_accounting(self._high_queue, now)

    def dequeue_low(self, now: float) -> Optional[Task]:
        """从低优先级队列取队首。"""
        with self._guard():
            return self._pop_with_accounting(self._low_queue, now)

    def dequeue(self, prefer_high: bool = True, now: float = 0.0) -> Optional[Task]:
        """``prefer_high=True`` 时严格优先高优先级队列（多级队列核心规则）。"""
        with self._guard():
            if prefer_high:
                task = self._pop_with_accounting(self._high_queue, now)
                if task is not None:
                    return task
                return self._pop_with_accounting(self._low_queue, now)
            task = self._pop_with_accounting(self._low_queue, now)
            if task is not None:
                return task
            return self._pop_with_accounting(self._high_queue, now)

    def dequeue_starving(self, now: float, threshold_ms: float) -> Optional[Task]:
        """**饥饿补偿通道**：从低优先级队列摘出第一个等待超阈值的任务。

        OS 原理：多级反馈队列的 aging（老化）机制 —— 长期得不到 CPU 的进程
        必须被人工"提权"，否则会被高优先级洪流永久饿死。本项目把它做成
        一条**独立于权重计算**的硬通道，保证低优先级任务有确定的出路。

        低优先级队列按到达顺序排列，因此队首就是等待最久的任务；
        仍然实现为顺序扫描，便于队列内存在乱序（重传放回队尾）时也成立。
        """
        with self._guard():
            if not self._low_queue:
                return None
            idx = -1
            for i, task in enumerate(self._low_queue):
                if task.wait_ms(now) > threshold_ms:
                    idx = i
                    break
            if idx < 0:
                return None
            task = self._low_queue[idx]
            del self._low_queue[idx]
            prev_enq = float(task.enqueue_ts)
            added_ms = max(0.0, float(now) - prev_enq) * 1000.0
            task.wait_ms_total = float(task.wait_ms_total) + added_ms
            task._deq_added_ms = added_ms
            task._deq_prev_enq_ts = prev_enq
            task.state = TaskState.RUNNING
            self.dequeued_total += 1
            return task

    def drain(self) -> List[Task]:
        """清空队列并返回全部任务（仿真收尾时统计未完成任务用）。"""
        with self._guard():
            tasks = list(self._high_queue) + list(self._low_queue)
            self._high_queue.clear()
            self._low_queue.clear()
            return tasks

    def tasks(self) -> List[Task]:
        """当前队列内全部任务的快照副本（按高->低顺序；调用方安全遍历）。"""
        with self._guard():
            return list(self._high_queue) + list(self._low_queue)

    # ------------------------------------------------------------ 非破坏性窥视
    def peek_high(self) -> Optional[Task]:
        """只读队首（供调度器比较权重 / 判断限流，不改变队列）。"""
        with self._guard():
            return self._high_queue[0] if self._high_queue else None

    def peek_low(self) -> Optional[Task]:
        with self._guard():
            return self._low_queue[0] if self._low_queue else None

    def peek(self, priority: TaskPriority) -> Optional[Task]:
        return self.peek_high() if priority is TaskPriority.HIGH else self.peek_low()

    # ---------------------------------------------------------------- 状态视图
    @property
    def high_depth(self) -> int:
        with self._guard():
            return len(self._high_queue)

    @property
    def low_depth(self) -> int:
        with self._guard():
            return len(self._low_queue)

    @property
    def total_depth(self) -> int:
        with self._guard():
            return len(self._high_queue) + len(self._low_queue)

    def depth_of(self, priority: TaskPriority) -> int:
        return self.high_depth if priority is TaskPriority.HIGH else self.low_depth

    def congestion(self) -> float:
        """**整体**队列拥堵度 0.0~1.0 = 总深度 / congestion_ref。

        注意：动态权重要用的是**按优先级分别计算**的拥堵度（见 `congestion_of`），
        因为"拥堵"是各队列各自的属性 —— 高优先级队列堆了 200 个而低优先级队列
        只有 3 个时，不该让两者共享同一个 Q 值，否则"向拥堵队列倾斜资源"就落空了。
        """
        return min(1.0, self.total_depth / self.congestion_ref)

    def congestion_of(self, priority: TaskPriority) -> float:
        """**指定优先级队列**的拥堵度 0.0~1.0 = 该队列深度 / congestion_ref。

        这是动态权重 Q 因子的正确口径：哪个队列堵，就给哪个队列的任务加权。
        """
        return min(1.0, self.depth_of(priority) / self.congestion_ref)

    def starving_count(self, now: float, threshold_ms: Optional[float] = None) -> int:
        """当前处于饥饿状态（等待超阈值）的任务数。"""
        thr = self.starvation_threshold_ms if threshold_ms is None else float(threshold_ms)
        with self._guard():
            items = list(self._high_queue) + list(self._low_queue)
        return sum(1 for t in items if t.wait_ms(now) > thr)

    def oldest_wait_ms(self, priority: TaskPriority, now: float) -> float:
        """指定优先级队列里等待最久任务的等待时延（毫秒）；空队列返回 0。"""
        with self._guard():
            q = self._queue_of(priority)
            if not q:
                return 0.0
            return max(t.wait_ms(now) for t in q)

    def total_remaining_s(self) -> float:
        """队列内所有任务的剩余服务需求之和（仿真秒）—— 系统负载的直接来源。"""
        with self._guard():
            items = list(self._high_queue) + list(self._low_queue)
        return sum(t.remaining_s for t in items)

    def snapshot(self, now: float, running_count: int, blocked_count: int,
                 system_load: float, high_slice_ms: float, low_slice_ms: float,
                 high_weight: float, low_weight: float) -> QueueSnapshot:
        """构造一条队列时序采样（落库 queue_sample / 出图 06 用）。"""
        with self._guard():
            snap = QueueSnapshot(
                ts=float(now),
                high_depth=len(self._high_queue),
                low_depth=len(self._low_queue),
                running_count=int(running_count),
                blocked_count=int(blocked_count),
                system_load=float(system_load),
                high_time_slice_ms=float(high_slice_ms),
                low_time_slice_ms=float(low_slice_ms),
                high_weight=float(high_weight),
                low_weight=float(low_weight),
            )
        return snap

    def __len__(self) -> int:
        return self.total_depth

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<MultiLevelQueue high={self.high_depth} low={self.low_depth} "
                f"cap={self.capacity} locked={self.lock_enabled}>")
