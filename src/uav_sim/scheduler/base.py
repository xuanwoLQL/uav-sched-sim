"""
base.py —— 调度器基类（所有调度策略的公共能力集中在这里）

四层映射（企业业务层 ↔ OS 原理层）：
    企业业务层：地面站「调度中心」—— 决定下一个把哪架无人机的哪一份数据放上信道；
    它同时管着「任务完成台账」（业务统计）与「准入通行证」（机房容量约束）。
    OS 原理层：内核 scheduler 主循环 + TCB 生命周期管理。
      * `step()`        -> 调度器入口，等价于 schedule()，**绝不阻塞**（禁止 time.sleep）；
      * `_do_dispatch`  -> 上下文切换（context switch）：把 TCB 从就绪队列搬到 CPU；
      * `completed`     -> 终止态进程台账（供 wait/统计使用），工作线程并发写 -> 必须加锁；
      * `link_aware`    -> 该策略是否具备 I/O 设备（通信链路）状态感知能力；
      * 限流准入        -> 资源池 semaphore 的非阻塞 tryAcquire。

设计要点（契约 §4.7）
-------------------
1. `__init__(self, sim, cfg)`：只依赖 Simulator 暴露的公共组件
   （`queue` / `limiter` / `link_monitor` / `now()` / `system_load()` / `dispatch()`），
   不 import 任何 core-engine 的实现模块，避免并行开发期的耦合。
2. `self.completed` 被工作线程并发 append，用 `threading.Lock` 保护。
3. `context_switches`：一个**已经运行过**的任务再次被调度（被抢占/时间片耗尽/重传回来）
   时 +1；`dispatch_count` 统计总派发次数。
4. `link_aware = False`（静态策略）：弱网任务失败后由 Simulator 原样重传，反复抢占槽位，
   这就是痛点①「劣质链路长期抢占资源」的代码级复现。
5. 时间片抢占是**仿真简化**：`Simulator.dispatch()` 是异步的且任务跑满 `service_s`，
   因此本模块提供两条钩子：
     * `preempt_after_s(task)` -> float | None：告诉 Simulator「本轮最多跑这么久」；
       若 Simulator 的 `dispatch` 支持 `limit_s` 关键字（运行期探测签名），会自动传进去；
     * `_on_task_finished(task, exec_time)`：回调里判断「时间片耗尽未完成」，
       把剩余部分 `queue.requeue()` 放回队尾 —— 这样即使 Simulator 不支持抢占，
       时间片与重新入队次数这两个教学量仍然可被统计与验证。
"""

from __future__ import annotations

import inspect
import logging
import threading
from typing import TYPE_CHECKING, Any, Iterable, Optional

from ..models import Task, TaskPriority, TaskState

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，避免并行开发期的循环 import
    from ..simulator import Simulator

logger = logging.getLogger(__name__)

#: 浮点比较容差（仿真时间单位：秒）
EPS = 1e-9

#: 队列内部容器的候选属性名（用于「非破坏性取队首」的鸭子类型探测）
_HIGH_CONTAINER_ATTRS = ("_high_queue", "high_queue", "_high", "_high_q", "_q_high", "_high_deque")
_LOW_CONTAINER_ATTRS = ("_low_queue", "low_queue", "_low", "_low_q", "_q_low", "_low_deque")


class BaseScheduler:
    """调度器基类。子类只需实现 `step()`（可选覆写 `_on_task_finished`）。"""

    #: 注册名（必须与 models.SCHEDULER_NAMES 一致）
    name: str = "base"
    #: 中文显示名（图表/报告用）
    label: str = "基类"
    #: 是否具备链路感知能力：决定弱网失败后走 CACHED（本地缓存）还是原样重传
    link_aware: bool = False
    #: 时间片耗尽被抢占后如何归位：'tail' = 排到队尾（RR/MLFQ 轮转语义）;
    #: 'front' = 回到本优先级队首（保住排队位置，避免长任务被反复切碎后饿死）
    preempt_requeue: str = "front"

    # ------------------------------------------------------------------ 构造
    def __init__(self, sim: "Simulator", cfg: dict) -> None:
        self.sim = sim
        self.cfg = cfg or {}

        # ---- 完成台账：工作线程回调并发写入，必须加锁（契约 §4.7） ----
        self.completed: list[tuple[Task, float]] = []
        self._completed_lock = threading.Lock()

        # ---- 统计量 ----
        self.dispatch_count = 0      # 总派发次数（= 上下文装入次数）
        self.context_switches = 0    # 已运行过的任务被再次调度 -> 上下文切换
        self.requeue_count = 0       # 调度器主动放回队列的次数（不含队列内部计数）

        # ---- 契约 §4.7 get_stats() 需要的时间片/权重快照 ----
        default_slice = float(self.cfg_get("rr.time_slice_ms", 500.0) or 500.0)
        self._high_slice_ms: float = default_slice
        self._low_slice_ms: float = default_slice
        self._last_high_weight: float = 1.0
        self._last_low_weight: float = 1.0

        # ---- 内部记账（时间片累加，用于不支持抢占时的仿真简化） ----
        self._served_own: dict[str, float] = {}

        # ---- 运行期探测 Simulator.dispatch 是否支持 limit_s（只探测一次） ----
        self._limit_s_support: Optional[bool] = None

    # ------------------------------------------------------------ 配置与组件
    def cfg_get(self, dotted: str, default: Any = None) -> Any:
        """按 'dynamic_weight.alpha' 形式读取 cfg，缺项返回 default（不依赖 config.py）。"""
        node: Any = self.cfg
        for part in str(dotted).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    @property
    def queue(self):
        """就绪队列（MultiLevelQueue）。"""
        return getattr(self.sim, "queue", None)

    @property
    def limiter(self):
        """系统资源限流器（对所有策略一视同仁）。"""
        return getattr(self.sim, "limiter", None)

    @property
    def link_monitor(self):
        """链路监控器（只有 link_aware 策略会读它）。"""
        return getattr(self.sim, "link_monitor", None)

    def now(self) -> float:
        """当前仿真时刻（秒）。"""
        fn = getattr(self.sim, "now", None)
        return float(fn()) if callable(fn) else 0.0

    def system_load(self) -> float:
        """系统当前负载 0.0~1.0+（队列总需求 / 容量）。"""
        fn = getattr(self.sim, "system_load", None)
        try:
            return float(fn()) if callable(fn) else 0.0
        except Exception:  # pragma: no cover - 仿真器异常不得打断调度
            logger.exception("[%s] system_load() 调用失败，按 0 处理", self.name)
            return 0.0

    def link_health(self, uav_id: int) -> float:
        """取某台无人机的链路健康度 0~100；无监控器时返回 100（视为优质链路）。"""
        mon = self.link_monitor
        fn = getattr(mon, "get_health", None)
        if not callable(fn):
            return 100.0
        try:
            return float(fn(uav_id))
        except Exception:  # pragma: no cover
            return 100.0

    def congestion(self) -> float:
        """队列拥堵度 0~1（= total_depth / capacity）。"""
        q = self.queue
        fn = getattr(q, "congestion", None)
        try:
            return float(fn()) if callable(fn) else 0.0
        except Exception:  # pragma: no cover
            return 0.0

    # ------------------------------------------------------- 队列操作小工具
    def _requeue(self, task: Task, now: float) -> None:
        """把任务放回原优先级队列队尾（OS：抢占后重新进入就绪队列尾）。"""
        q = self.queue
        if q is None or task is None:
            return
        try:
            q.requeue(task, now)
            self.requeue_count += 1
        except Exception:  # pragma: no cover
            logger.exception("[%s] requeue 失败 task=%s", self.name, getattr(task, "task_id", "?"))

    def _enqueue(self, task: Task, now: float) -> bool:
        q = self.queue
        if q is None or task is None:
            return False
        try:
            ok = bool(q.enqueue(task, now))
        except Exception:  # pragma: no cover
            logger.exception("[%s] enqueue 失败 task=%s", self.name, getattr(task, "task_id", "?"))
            return False
        if not ok:
            # 队列已满 -> 丢弃（契约：调用方须置 DROPPED）
            task.state = TaskState.DROPPED
            task.drop_reason = "queue_full"
            task.finish_ts = now
        return ok

    @staticmethod
    def _arrival_key(task: Task) -> tuple:
        """全局 FIFO 排序键：先看入队时刻（重传/放回会刷新），再退化为提交时刻。

        对应 OS：先来先服务按「进入就绪队列的时刻」排序，而不是进程创建时刻。
        """
        enq = float(getattr(task, "enqueue_ts", 0.0) or 0.0)
        sub = float(getattr(task, "submit_ts", 0.0) or 0.0)
        return (enq, sub, str(getattr(task, "task_id", "")))

    # ---- 非破坏性取队首（FCFS/RR 的全局 FIFO、dynamic 的双候选比较都依赖它） ----
    @staticmethod
    def _container_head(container: Any) -> Optional[Task]:
        """从一个类 deque 容器里取队首，取不到返回 None。"""
        if container is None:
            return None
        if hasattr(container, "queue"):  # queue.Queue -> 内部 deque
            container = container.queue
        try:
            if len(container) == 0:
                return None
            return container[0]
        except Exception:
            return None

    def peek_head(self, priority: TaskPriority) -> Optional[Task]:
        """非破坏性读取指定优先级队列的队首；队列不支持窥视时返回 None。

        为什么要窥视：FCFS/RR 需要「跨两个物理队列比较到达顺序」以实现真正的单一 FIFO，
        破坏性出队再放回会污染 requeue_count 指标并打乱队列顺序。
        """
        q = self.queue
        if q is None:
            return None
        # 1) 队列自带公有 peek 接口（若 core-engine 提供）
        for meth in ("peek_high" if priority is TaskPriority.HIGH else "peek_low", "peek"):
            fn = getattr(q, meth, None)
            if not callable(fn):
                continue
            try:
                got = fn() if meth != "peek" else fn(priority)
            except TypeError:
                continue
            except Exception:
                break
            if got is not None:
                return got
        # 2) 内部容器（鸭子类型，仅用于非破坏性读取，不改动队列）
        attrs = _HIGH_CONTAINER_ATTRS if priority is TaskPriority.HIGH else _LOW_CONTAINER_ATTRS
        for attr in attrs:
            head = self._container_head(getattr(q, attr, None))
            if head is not None:
                return head
        return None

    def _pop(self, priority: TaskPriority, now: float) -> Optional[Task]:
        """按优先级取出队首。"""
        q = self.queue
        if q is None:
            return None
        try:
            if priority is TaskPriority.HIGH:
                return q.dequeue_high(now)
            return q.dequeue_low(now)
        except Exception:  # pragma: no cover
            logger.exception("[%s] dequeue 失败", self.name)
            return None

    def _take_global_fifo(self, now: float) -> Optional[Task]:
        """**跨优先级**取「到达最早」的任务 —— FCFS/RR「不区分优先级」的实现。

        先窥视两侧队首比较 `enqueue_ts`，只取出真正被选中的那一个；
        若队列不支持窥视，则退化为「高队列优先」（并在此处注释说明）。
        """
        high = self.peek_head(TaskPriority.HIGH)
        low = self.peek_head(TaskPriority.LOW)
        if high is not None and low is not None:
            if self._arrival_key(high) <= self._arrival_key(low):
                return self._pop(TaskPriority.HIGH, now)
            return self._pop(TaskPriority.LOW, now)
        if high is not None:
            return self._pop(TaskPriority.HIGH, now)
        if low is not None:
            return self._pop(TaskPriority.LOW, now)
        # 退化路径：队列不提供窥视能力
        h = self._pop(TaskPriority.HIGH, now)
        if h is not None:
            return h
        return self._pop(TaskPriority.LOW, now)

    # --------------------------------------------------------------- 限流准入
    def _admit(self, task: Task) -> str:
        """非阻塞准入判定（只判定，不改队列）：OK / THROTTLE / REJECT_LARGE / QUOTA。

        对应 OS：资源池 semaphore 的 tryAcquire —— 拿不到许可绝不阻塞，
        而是让任务留在就绪队列里错峰（保证调度线程不被拖死）。
        """
        limiter = self.limiter
        fn = getattr(limiter, "try_admit", None)
        if not callable(fn):
            return "OK"
        try:
            return str(fn(task))
        except Exception:  # pragma: no cover
            logger.exception("[%s] try_admit 异常，按 OK 处理", self.name)
            return "OK"

    def _split_and_enqueue(self, task: Task, now: float) -> int:
        """大任务分片：`limiter.split_task` 切开后逐片入队。

        对应 OS：大内存请求被内核切分为多个页框请求；保证业务完整率（不整单丢弃）。
        """
        limiter = self.limiter
        fn = getattr(limiter, "split_task", None)
        if not callable(fn):
            task.state = TaskState.DROPPED
            task.drop_reason = "reject_large_no_chunking"
            task.finish_ts = now
            return 0
        try:
            segs: Iterable[Task] = fn(task)
        except Exception:  # pragma: no cover
            logger.exception("[%s] split_task 异常 task=%s", self.name, task.task_id)
            return 0
        n = 0
        for seg in list(segs or []):
            if self._enqueue(seg, now):
                n += 1
        if n == 0:
            task.state = TaskState.DROPPED
            task.drop_reason = "reject_large_no_segment"
            task.finish_ts = now
        return n

    # ------------------------------------------------------------- 派发与抢占
    def _sim_supports_limit_s(self) -> bool:
        """探测 `Simulator.dispatch` 是否支持 `limit_s=`（支持则优先使用真实抢占）。"""
        if self._limit_s_support is None:
            fn = getattr(self.sim, "dispatch", None)
            try:
                params = inspect.signature(fn).parameters
                self._limit_s_support = ("limit_s" in params) or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
                )
            except (TypeError, ValueError):  # pragma: no cover - C 实现/不可反射
                self._limit_s_support = False
        return bool(self._limit_s_support)

    def _call_sim_dispatch(self, task: Task, limit_s: Optional[float]) -> bool:
        fn = getattr(self.sim, "dispatch", None)
        if not callable(fn):
            return False
        if limit_s is None or not self._sim_supports_limit_s():
            return bool(fn(task))
        return bool(fn(task, limit_s=limit_s))

    def _do_dispatch(self, task: Task, now: float, slice_ms: Optional[float] = None) -> bool:
        """把任务交给 Simulator 的工作线程池；返回是否被受理。

        * 时间片意图写进 `task._slice_ms`（供 Simulator / 测试读取）；
        * `first_start_ts` 为空 -> 首次获得 CPU；
          已有值 -> 本次属于「被抢占/时间片耗尽/重传后再次获得 CPU」= 一次上下文切换。
        """
        limit_s: Optional[float] = None
        if slice_ms is not None and slice_ms > 0:
            limit_s = float(slice_ms) / 1000.0
            try:
                task._slice_ms = float(slice_ms)  # 仿真时间片标记（教学/调试可见）
            except Exception:  # pragma: no cover
                pass
        accepted = self._call_sim_dispatch(task, limit_s)
        if not accepted:
            # 未被受理（线程池关闭 / 仿真停止）—— 放回队列，不计入派发统计
            self._requeue(task, now)
            return False
        if task.first_start_ts is None:
            task.first_start_ts = now
        else:
            self.context_switches += 1
        self.dispatch_count += 1
        return True

    def dispatch_admitted(self, task: Task, now: float,
                          slice_ms: Optional[float] = None) -> bool:
        """把**已获得限流许可**的任务派发出去；派发失败则归还许可。

        为什么需要这一步：`limiter.try_admit()` 返回 "OK" 时已经占掉一个并发槽位，
        若随后 `dispatch()` 因仿真停止 / 线程池关闭而失败，槽位就泄漏了
        （表现为 running 数量只增不减，后续任务永远 THROTTLE —— 经典资源泄漏）。
        这里统一兜底 `release()`，保证"占位-释放"严格配对。
        """
        ok = self._do_dispatch(task, now, slice_ms)
        if not ok:
            rel = getattr(self.limiter, "release", None)
            if callable(rel):
                try:
                    rel(task)
                except Exception:  # pragma: no cover
                    logger.exception("[%s] 归还限流许可失败 task=%s",
                                     self.name, getattr(task, "task_id", "?"))
        return ok

    def push_front(self, task: Task, now: float) -> None:
        """把任务放回**队首**并回滚出队时结的等待账（错峰路径专用）。

        对应 OS：资源不足时进程不该被踢出就绪队列，而应保持原有排队位置，
        等下一次调度再试 —— 这也是"错峰执行"的实现方式。
        """
        q = self.queue
        fn = getattr(q, "push_front", None)
        if callable(fn) and task is not None:
            try:
                fn(task, now)
                return
            except Exception:  # pragma: no cover
                logger.exception("[%s] push_front 失败 task=%s", self.name,
                                 getattr(task, "task_id", "?"))
        self._requeue(task, now)

    def preempt_after_s(self, task: Task) -> Optional[float]:
        """抢占钩子：返回「本轮最多允许运行多少仿真秒」，None 表示不抢占。

        基类不抢占（FCFS / 静态优先级）。RR 与 MLFQ 覆写本方法实现时间片。
        """
        return None

    # --------------------------------------------------------- 完成回调（线程）
    def add_task_finished(self, task: Task, exec_time: float) -> None:
        """工作线程完成任务后回调（**由 worker 线程调用**）。

        线程安全：`self.completed` 是共享列表 -> 用 `self._completed_lock` 保护；
        钩子 `_on_task_finished` 放在锁外执行，避免在持锁期间操作队列造成锁顺序问题。
        """
        try:
            exec_s = float(exec_time)
        except (TypeError, ValueError):  # pragma: no cover
            exec_s = 0.0
        with self._completed_lock:
            self.completed.append((task, exec_s))
        try:
            self._on_task_finished(task, exec_s)
        except Exception:  # pragma: no cover - 钩子异常不得影响 worker 收尾
            logger.exception("[%s] _on_task_finished 异常 task=%s", self.name,
                             getattr(task, "task_id", "?"))

    def _on_task_finished(self, task: Task, exec_time: float) -> None:
        """完成回调钩子（基类空实现）。RR / MLFQ 用它做时间片耗尽后的重新入队。"""
        return None

    def slice_exhausted(self, task: Task, exec_time: float) -> bool:
        """判断「时间片耗尽但任务未完成」。

        OS 原理：RR/MLFQ 中时间片到点即触发时钟中断，进程被剥夺 CPU 回到就绪队列；
        只有真正完成（state=DONE）才算终止态。
        """
        if task is None:
            return False
        if task.state in (TaskState.DONE, TaskState.DROPPED):
            return False
        if task.remaining_s <= EPS:
            return False
        return True

    def account_partial_service(self, task: Task, exec_time: float) -> None:
        """把本轮获得的 CPU 时间记回 TCB（`served_s`），并返回剩余量。

        仿真简化说明：若 Simulator 支持 `limit_s` 抢占，它可能已经自己累加了 `served_s`；
        这里用 max(Simulator 记账, 调度器自记账) 保证两种实现下都不重复累加。
        """
        if task is None:
            return
        tid = task.task_id
        own = self._served_own.get(tid, 0.0) + max(0.0, float(exec_time))
        if float(task.served_s) >= own - EPS:
            total = float(task.served_s)   # 信任 Simulator 的记账
        else:
            total = own                    # Simulator 未记账，用调度器自记账
        self._served_own[tid] = total
        task.served_s = min(float(task.service_s), max(float(task.served_s), total))

    # ------------------------------------------------------------- 统计与重置
    def get_stats(self) -> dict:
        """契约 §4.7：供 Simulator 写入 QueueSnapshot 与 metrics 使用。"""
        return {
            "dispatched": int(self.dispatch_count),
            "context_switches": int(self.context_switches),
            "high_time_slice_ms": float(self._high_slice_ms),
            "low_time_slice_ms": float(self._low_slice_ms),
            "high_weight": float(self._last_high_weight),
            "low_weight": float(self._last_low_weight),
        }

    def reset(self) -> None:
        """复位为初始状态（同一实例可复用跑多次实验）。"""
        with self._completed_lock:
            self.completed.clear()
        self.dispatch_count = 0
        self.context_switches = 0
        self.requeue_count = 0
        self._served_own.clear()

    # ------------------------------------------------------------------ 入口
    def step(self) -> int:
        """执行一次调度决策，返回本轮派发任务数（子类必须实现）。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ 小工具
    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def _defer(self, task: Task, now: float) -> None:
        """错峰：本轮不派发，原样放回队列（对应 OS：并发槽位已满 -> 任务留在就绪队列）。"""
        self._requeue(task, now)

    def completed_count(self) -> int:
        """已完成任务数（线程安全读取）。"""
        with self._completed_lock:
            return len(self.completed)

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"<{type(self).__name__} name={self.name} dispatched={self.dispatch_count}>"
