"""
resource_limiter.py —— 系统资源配额与并发限流（过载稳流保护）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 地面站服务器只有有限的上传处理通道与内存：瞬时峰值流量会把 CPU/IO 打满导致服务阻塞、勘查作业中断；超大单任务（>2 GB 点云）会挤爆单机处理能力 |
| OS 原理层   | **资源池 + 并发限流 + 资源配额**：`max_concurrent` = 可用资源单元数（信号量许可数）；`max_task_memory_mb` = 单次内存分配上限；`per_uav_quota` = 单进程资源配额（防止一个进程霸占全部资源） |
| 算法设计层 | 非阻塞准入判定（tryAcquire）+ 并发满时错峰缓存 + 超大任务切分（分片）+ 单机配额 + 限流统计 |
| 代码实现层 | `ResourceLimiter.try_admit / acquire / release / split_task / get_status` |

--------------------------------------------------------------------------
为什么用"非阻塞判定"而不是"阻塞等待"（重要设计决策）
--------------------------------------------------------------------------
指导书里 `acquire()` 写的是"并发满则阻塞等待"。但在本项目里，调度决策发生在
**调度器线程**上：如果调度器一阻塞，整个系统就没有人去派发其它任务，
5 个并发槽位都空着也无人使用 —— 直接死锁。

因此本实现把限流拆成两层：

    try_admit(task) -> "OK" / "THROTTLE" / "REJECT_LARGE" / "QUOTA"   # 非阻塞，调度器用
    acquire(task, timeout_s)                                          # 阻塞式，保留指导书签名

`try_admit` 返回 THROTTLE 时，调度器把任务**留在就绪队列里错峰**（不是丢包），
这正对应 OS 里"资源不足时进程继续留在就绪队列，等待下一次调度"。

--------------------------------------------------------------------------
为什么限流器对所有策略一视同仁
--------------------------------------------------------------------------
限流属于**系统级资源管控**，不属于调度策略。若只给动态权重策略配限流器，
实验就变成"策略差异 + 系统容量的混合差异"，违反控制变量法。因此
`Simulator` 统一创建一份 `ResourceLimiter` 注入给全部 5 个策略。

--------------------------------------------------------------------------
大任务：拒绝 + 分片，而不是整单丢弃
--------------------------------------------------------------------------
PDF 实验 3.3 验收项要求"大任务被拒绝，限流统计正确"。本实现同时保证
**业务完整率**不被牺牲：超过 `max_task_memory_mb` 的任务被计入
`reject_large`（验收统计），随后由 `split_task()` 按上限切成 N 片依次上传
（对应 OS：大段内存请求被拆成多个页框请求）。

    reject_large  —— "限流生效"的证据
    chunked       —— "业务不丢失"的证据
"""

from __future__ import annotations

import math
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.models import Task, TaskState, new_task_id

EPS = 1e-9


class ResourceLimiter:
    """系统资源限流器（契约 §4.5）。

    线程安全：``self._lock`` 保护 ``_running_ids`` / ``_per_uav`` 与全部计数器；
    ``try_admit`` 的"判定 + 占位"必须在同一临界区内完成（否则并发下会超卖槽位）。
    """

    def __init__(self, max_concurrent: int, max_task_memory_mb: float,
                 per_uav_quota: int, enable_chunking: bool = True) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self.max_task_memory_mb = float(max_task_memory_mb)
        self.per_uav_quota = max(1, int(per_uav_quota))
        self.enable_chunking = bool(enable_chunking)

        self._lock = threading.Lock()
        self._running_ids: set = set()
        self._per_uav: Counter = Counter()

        # ---- 限流统计（对应 PDF 验收项"限流统计正确"） ----
        self.throttled = 0          # 并发已满、任务被错峰缓存的次数
        self.rejected_large = 0     # 单任务超上限被拒绝的次数
        self.chunked = 0            # 被切分的大任务个数
        self.segments_created = 0   # 产生的分片总数
        self.quota_blocked = 0      # 单机配额挡住的次数
        self.released = 0           # 释放次数
        self.admitted = 0           # 成功获得许可次数
        self.peak_running = 0       # 并发峰值（应当 <= max_concurrent）

    # ------------------------------------------------------------------ 准入
    def try_admit(self, task: Task) -> str:
        """非阻塞准入判定（契约 §4.5）。

        返回：
            "OK"            已占到一个并发槽位，调用方必须 dispatch 并在任务结束后 release
            "THROTTLE"      并发已满 -> 任务留在就绪队列错峰（不占位、不丢包）
            "REJECT_LARGE"  单任务数据量超上限 -> 调用方应改走 split_task()
            "QUOTA"         该无人机的并发配额已用满 -> 换别的任务试

        **只有返回 "OK" 时才修改状态**（原子，避免并发超卖）。
        """
        if task is None:
            return "THROTTLE"
        with self._lock:
            # ① 单任务资源上限（内存/数据量配额）
            if float(task.data_size_mb) > self.max_task_memory_mb + EPS:
                self.rejected_large += 1
                return "REJECT_LARGE"
            # ② 系统并发上限（信号量许可数用尽）
            if len(self._running_ids) >= self.max_concurrent:
                self.throttled += 1
                return "THROTTLE"
            # ③ 单机资源配额（防止一台无人机霸占所有通道）
            if self._per_uav[task.uav_id] >= self.per_uav_quota:
                self.quota_blocked += 1
                return "QUOTA"
            # ④ 真正占位
            self._running_ids.add(task.task_id)
            self._per_uav[task.uav_id] += 1
            self.admitted += 1
            if len(self._running_ids) > self.peak_running:
                self.peak_running = len(self._running_ids)
            task.state = TaskState.RUNNING
            return "OK"

    def acquire(self, task: Task, timeout_s: float = 0.0) -> bool:
        """阻塞式申请许可（保留指导书签名，交互式演示用）。

        ``timeout_s <= 0`` 等价于「只试一次」，即 ``try_admit() == "OK"``。
        """
        if timeout_s is None or float(timeout_s) <= 0:
            return self.try_admit(task) == "OK"
        deadline = time.perf_counter() + float(timeout_s)
        while True:
            if self.try_admit(task) == "OK":
                return True
            if time.perf_counter() >= deadline:
                return False
            time.sleep(0.001)

    def release(self, task: Task) -> None:
        """释放执行许可（任务结束/失败/被抢占时必须调用，含异常路径的 finally）。"""
        if task is None:
            return
        with self._lock:
            if task.task_id in self._running_ids:
                self._running_ids.discard(task.task_id)
                self._per_uav[task.uav_id] -= 1
                if self._per_uav[task.uav_id] <= 0:
                    del self._per_uav[task.uav_id]
                self.released += 1

    def is_admitted(self, task: Task) -> bool:
        """该任务当前是否持有许可（调试/自检用）。"""
        with self._lock:
            return task is not None and task.task_id in self._running_ids

    # ------------------------------------------------------------------ 分片
    def split_task(self, task: Task) -> List[Task]:
        """把超上限的大任务切成 N 个分片任务（契约 §4.5）。

        切分规则：
            * N = ceil(data_size / max_task_memory_mb)
            * 每片数据量与 ``service_s`` 按 1/N 等比拆分（总服务量不变）
            * 继承 ``parent_task_id`` / ``priority`` / ``uav_id`` / ``submit_ts``
            * ``segment_index`` 从 0 开始，``segment_total = N``

        未启用分片或任务本身不超限时返回 ``[]``。
        """
        if task is None or not self.enable_chunking:
            return []
        size = float(task.data_size_mb)
        if size <= self.max_task_memory_mb + EPS:
            return []

        n = int(math.ceil(size / self.max_task_memory_mb))
        n = max(2, n)
        root_id = task.parent_task_id or task.task_id
        seg_size = size / n
        seg_service = float(task.service_s) / n

        segs: List[Task] = []
        for i in range(n):
            seg = Task(
                task_id=new_task_id("SEG"),
                uav_id=task.uav_id,
                priority=task.priority,
                task_type=task.task_type,
                data_size_mb=seg_size,
                submit_ts=task.submit_ts,
                service_s=seg_service,
                segment_index=i,
                segment_total=n,
                parent_task_id=root_id,
            )
            seg.enqueue_ts = task.enqueue_ts
            seg.link_health = task.link_health
            segs.append(seg)

        with self._lock:
            self.chunked += 1
            self.segments_created += n
        # 原任务不再参与调度（它的"业务"由分片承载），保留痕迹便于回溯
        task.state = TaskState.DROPPED
        task.drop_reason = "chunked_into_segments"
        task.finish_ts = None
        return segs

    # ------------------------------------------------------------------ 状态
    @property
    def running(self) -> int:
        with self._lock:
            return len(self._running_ids)

    def is_saturated(self) -> bool:
        """并发是否已打满（系统过载的直接判据）。"""
        return self.running >= self.max_concurrent

    def available(self) -> int:
        with self._lock:
            return max(0, self.max_concurrent - len(self._running_ids))

    def get_status(self) -> Dict[str, object]:
        """限流器状态快照（契约 §4.5 的 7 个键 + 若干附加统计）。"""
        with self._lock:
            return {
                "running": len(self._running_ids),
                "available": max(0, self.max_concurrent - len(self._running_ids)),
                "max_concurrent": self.max_concurrent,
                "throttled": self.throttled,
                "rejected_large": self.rejected_large,
                "chunked": self.chunked,
                "released": self.released,
                "per_uav_running": dict(self._per_uav),
                # ---- 附加：便于报告与自检 ----
                "admitted": self.admitted,
                "quota_blocked": self.quota_blocked,
                "segments_created": self.segments_created,
                "peak_running": self.peak_running,
                "max_task_memory_mb": self.max_task_memory_mb,
                "per_uav_quota": self.per_uav_quota,
            }

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<ResourceLimiter running={self.running}/{self.max_concurrent} "
                f"throttled={self.throttled} rejected_large={self.rejected_large}>")
