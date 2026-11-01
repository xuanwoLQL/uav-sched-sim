"""
models.py —— 全局数据模型（接口契约冻结文件，由 Lead 维护，禁止其他模块擅自改动）

四层映射：
    企业业务层           ->  OS 原理层                ->  本文件中的载体
    无人机设备           ->  线程 / 进程             ->  UAVStatus
    单次数据上传业务     ->  CPU-IO 混合任务         ->  Task
    排队等待             ->  就绪队列                ->  QueueSnapshot
    通信链路             ->  IO 设备 / 阻塞          ->  LinkSample
    业务故障指标         ->  性能评价指标            ->  MetricRow / RunResult

时间约定（全项目统一，务必遵守）：
    * 所有 *_ts 字段一律是 **仿真时间**（单位：秒，float），从本次仿真启动时刻 0.0 起算。
    * 仿真时间 = 真实流逝时间 / config.runtime.time_scale。
      例：time_scale=0.02 时，真实 20 ms 记作仿真 1.0 s。
    * 任何模块都不得在时间戳里混入 wall-clock（time.time()）值。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 策略与场景的全局命名表（database / experiment / visualize 三方共享）
# --------------------------------------------------------------------------

#: 五种调度策略的注册名（顺序即报告中表格/图例的顺序）
SCHEDULER_NAMES: tuple = ("fcfs", "rr", "priority", "mlfq", "dynamic")

#: 策略中文显示名（图表与报告使用）
SCHEDULER_LABELS: Dict[str, str] = {
    "fcfs": "FCFS 先来先服务",
    "rr": "固定时间片轮转 RR",
    "priority": "静态优先级",
    "mlfq": "多级反馈队列 MLFQ",
    "dynamic": "动态权重自适应（本方案）",
}

#: 三种标准仿真工况
SCENARIO_CODES: tuple = ("LIGHT", "NORMAL", "PEAK")

SCENARIO_LABELS: Dict[str, str] = {
    "LIGHT": "轻载日常巡检",
    "NORMAL": "中载常规勘查",
    "PEAK": "高并发峰值勘查",
}


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


class TaskPriority(Enum):
    """任务优先级 —— 对应 OS 中的静态优先级级别。"""

    HIGH = 1  # 核心勘查任务（影像 / 多光谱 / LiDAR 点云）
    LOW = 2  # 高频轻量运维任务（GPS / 姿态 / 心跳 / 告警）


class TaskType(Enum):
    """异构业务任务类型 —— 数据体量与上报频次差异巨大。"""

    # 高负载核心勘查任务
    IMAGERY = "IMAGERY"  # 高清影像采集
    MULTISPECTRAL = "MULTISPECTRAL"  # 多光谱遥感探测
    LIDAR = "LIDAR"  # LiDAR 激光雷达点云扫描
    # 高频轻量运维任务
    GPS = "GPS"  # GPS 定位上报
    ATTITUDE = "ATTITUDE"  # 姿态数据回传
    HEARTBEAT = "HEARTBEAT"  # 设备心跳日志
    ALARM = "ALARM"  # 故障告警推送

    @property
    def priority(self) -> TaskPriority:
        """任务类型自带的默认优先级。"""
        return _TYPE_TO_PRIORITY[self]

    @property
    def label(self) -> str:
        return _TYPE_LABELS[self]


_TYPE_TO_PRIORITY: Dict[TaskType, TaskPriority] = {
    TaskType.IMAGERY: TaskPriority.HIGH,
    TaskType.MULTISPECTRAL: TaskPriority.HIGH,
    TaskType.LIDAR: TaskPriority.HIGH,
    TaskType.GPS: TaskPriority.LOW,
    TaskType.ATTITUDE: TaskPriority.LOW,
    TaskType.HEARTBEAT: TaskPriority.LOW,
    TaskType.ALARM: TaskPriority.LOW,
}

_TYPE_LABELS: Dict[TaskType, str] = {
    TaskType.IMAGERY: "高清影像",
    TaskType.MULTISPECTRAL: "多光谱",
    TaskType.LIDAR: "LiDAR点云",
    TaskType.GPS: "GPS定位",
    TaskType.ATTITUDE: "姿态回传",
    TaskType.HEARTBEAT: "心跳日志",
    TaskType.ALARM: "故障告警",
}


class TaskState(Enum):
    """任务状态机 —— 对应 OS 进程/线程五态模型。

    映射关系：NEW=新建  READY=就绪  RUNNING=运行  BLOCKED=阻塞
              CACHED=弱网本地缓存（新增态，对应 IO 阻塞降级）
              DONE=终止(正常)  DROPPED=终止(丢弃)
    """

    NEW = "NEW"
    READY = "READY"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    CACHED = "CACHED"
    DONE = "DONE"
    DROPPED = "DROPPED"


class UAVState(Enum):
    """无人机（= 线程）生命周期状态。"""

    OFFLINE = "OFFLINE"  # 未创建 / 已销毁
    ONLINE = "ONLINE"  # 线程运行中，正常生成任务
    RETURNING = "RETURNING"  # 返航下线中，不再生成新任务


# --------------------------------------------------------------------------
# 核心实体
# --------------------------------------------------------------------------


def new_task_id(prefix: str = "T") -> str:
    """生成全局唯一任务号（跨实验可追溯，写入 MySQL 主键）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Task:
    """单次数据上传任务 —— 映射为 OS 中的一个 CPU-IO 混合任务。

    生命周期：NEW -> READY(入队) -> RUNNING(被调度) -> DONE
              弱网时：RUNNING -> BLOCKED -> READY(重传)
              严重弱网：READY -> CACHED(本地缓存) -> READY(链路恢复)
    """

    # ---- 静态属性（生成后不变） ----
    task_id: str
    uav_id: int
    priority: TaskPriority
    task_type: TaskType
    data_size_mb: float
    submit_ts: float  # 仿真时间：任务产生时刻
    service_s: float  # 标定后的服务时长（仿真秒），由 Simulator 计算
    segment_index: int = 0  # 分片序号，从 0 开始
    segment_total: int = 1  # 分片总数；1 表示未分片
    parent_task_id: Optional[str] = None  # 分片所属的原始任务

    # ---- 动态属性（调度过程中变化，写入 DB 用于回溯） ----
    state: TaskState = TaskState.NEW
    first_start_ts: Optional[float] = None  # 首次被调度的时刻
    finish_ts: Optional[float] = None  # 完成/丢弃时刻
    enqueue_ts: float = 0.0  # 最近一次入队时刻（算等待时延用，阻塞重传会刷新）
    served_s: float = 0.0  # 已获得的服务时间（RR 时间片累加）
    wait_ms_total: float = 0.0  # 累计等待毫秒（不含运行时间，跨重传累加）
    requeue_count: int = 0  # 被放回队列次数（时间片耗尽 / 动态权重不达标）
    retry_count: int = 0  # 弱网重传次数
    preempt_count: int = 0  # 被抢占次数
    link_health: float = 100.0  # 提交时刻的链路健康度 (0-100)
    cached: bool = False  # 是否进入过本地缓存
    drop_reason: Optional[str] = None  # 丢弃原因（正常完成时为 None）

    # ---- 派生视图 ----

    @property
    def remaining_s(self) -> float:
        """还需要的服务时间（仿真秒）。"""
        return max(0.0, self.service_s - self.served_s)

    @property
    def progress(self) -> float:
        """完成进度 0.0 ~ 1.0。"""
        if self.service_s <= 0:
            return 1.0
        return min(1.0, self.served_s / self.service_s)

    def wait_ms(self, now: float) -> float:
        """当前累计等待时延（毫秒）—— 评价指标「平均等待时延」的口径。

        定义：任务从最近的入队时刻到 now 之间没有获得 CPU 的时间。
        已完成的累计值保存在 wait_ms_total。
        """
        if self.state in (TaskState.RUNNING, TaskState.DONE, TaskState.DROPPED):
            return self.wait_ms_total
        return self.wait_ms_total + max(0.0, now - self.enqueue_ts) * 1000.0

    def turnaround_ms(self, now: float) -> float:
        """周转时间（毫秒）= 从提交到完成。"""
        end = self.finish_ts if self.finish_ts is not None else now
        return max(0.0, end - self.submit_ts) * 1000.0

    def is_starved(self, now: float, threshold_ms: float) -> bool:
        """是否处于饥饿状态（等待时延超过阈值）。"""
        return self.wait_ms(now) > threshold_ms

    def to_row(self) -> Dict[str, Any]:
        """导出为可写入 MySQL task_record 的一行（键名与建表脚本一致）。"""
        return {
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "uav_id": self.uav_id,
            "priority": self.priority.name,
            "task_type": self.task_type.value,
            "data_size_mb": round(self.data_size_mb, 4),
            "segment_index": self.segment_index,
            "segment_total": self.segment_total,
            "service_s": round(self.service_s, 6),
            "submit_ts": round(self.submit_ts, 6),
            "first_start_ts": None if self.first_start_ts is None else round(self.first_start_ts, 6),
            "finish_ts": None if self.finish_ts is None else round(self.finish_ts, 6),
            "wait_ms": round(self.wait_ms_total, 3),
            "turnaround_ms": round(self.turnaround_ms(self.finish_ts or self.submit_ts), 3),
            "requeue_count": self.requeue_count,
            "retry_count": self.retry_count,
            "preempt_count": self.preempt_count,
            "link_health": round(self.link_health, 3),
            "cached": int(self.cached),
            "state": self.state.value,
            "drop_reason": self.drop_reason,
        }


@dataclass
class UAVStatus:
    """无人机节点状态 —— 映射为 OS 中的一个工作线程。"""

    uav_id: int
    state: UAVState = UAVState.OFFLINE
    online_ts: float = 0.0
    offline_ts: Optional[float] = None
    tasks_generated: int = 0
    tasks_done: int = 0
    tasks_dropped: int = 0
    online_seconds: float = 0.0  # 累计在线时长（仿真秒），算设备在线率用


@dataclass
class QueueSnapshot:
    """队列瞬时快照 —— 用于绘制堆积曲线与落库 queue_sample。"""

    ts: float
    high_depth: int
    low_depth: int
    running_count: int
    blocked_count: int
    system_load: float  # 0.0 ~ 1.0+
    high_time_slice_ms: float
    low_time_slice_ms: float
    high_weight: float
    low_weight: float

    def to_row(self) -> Dict[str, Any]:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class LinkSample:
    """链路质量采样 —— 落库 link_sample。"""

    ts: float
    uav_id: int
    delay_ms: float  # 时延
    loss_rate: float  # 丢包率 0.0 ~ 1.0
    rssi_dbm: float  # 信号强度
    health: float  # 健康度评分 0 ~ 100

    def to_row(self) -> Dict[str, Any]:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}


# --------------------------------------------------------------------------
# 指标与结果
# --------------------------------------------------------------------------

#: 全项目统一的指标口径（键名 -> (中文名, 单位, 越大越好?)）
METRIC_SPEC: Dict[str, tuple] = {
    "throughput": ("吞吐量", "个/秒", True),
    "avg_wait_high_ms": ("高优先级平均等待时延", "ms", False),
    "avg_wait_low_ms": ("低优先级平均等待时延", "ms", False),
    "avg_wait_all_ms": ("整体平均等待时延", "ms", False),
    "avg_turnaround_ms": ("平均周转时间", "ms", False),
    "p95_wait_high_ms": ("高优先级等待时延P95", "ms", False),
    "p95_wait_low_ms": ("低优先级等待时延P95", "ms", False),
    "starvation_rate": ("任务饥饿率", "%", False),
    "starvation_rate_low": ("低优先级饥饿率", "%", False),
    "resource_utilization": ("资源利用率", "%", True),
    "completion_rate": ("业务完整率", "%", True),
    "drop_rate": ("任务丢弃率", "%", False),
    "retry_rate": ("弱网重传率", "%", False),
    "avg_requeue": ("平均重新入队次数", "次", False),
    "max_high_depth": ("高优先级队列最大堆积", "个", False),
    "max_low_depth": ("低优先级队列最大堆积", "个", False),
    "peak_queue_depth": ("队列峰值堆积", "个", False),
    "context_switch": ("上下文切换次数", "次", False),
    "jitter_ms": ("时延抖动", "ms", False),
    "overload_seconds": ("过载持续时长", "秒", False),
    "throttle_count": ("限流触发次数", "次", False),
    "reject_large_count": ("超大任务拒绝次数", "次", False),
    "chunk_count": ("分片上传次数", "次", False),
    "runtime_wall_s": ("仿真实耗", "秒", False),
}


@dataclass
class MetricRow:
    """一条指标记录 —— 落库 exp_metric。"""

    metric_name: str
    metric_value: float
    unit: str = ""
    scope: str = "ALL"  # ALL / HIGH / LOW

    def to_row(self) -> Dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "metric_value": round(float(self.metric_value), 6),
            "unit": self.unit,
            "scope": self.scope,
        }


@dataclass
class RunResult:
    """一次「工况 × 调度策略」仿真的完整产出 —— ExperimentRunner 的返回值。"""

    exp_id: str
    scenario: str  # LIGHT / NORMAL / PEAK
    scheduler: str  # fcfs / rr / priority / mlfq / dynamic
    seed: int
    duration_s: float
    uav_count: int
    started_at: str  # ISO8601 wall-clock，仅用于落库审计
    finished_at: str
    metrics: Dict[str, float] = field(default_factory=dict)
    task_records: List[Dict[str, Any]] = field(default_factory=list)
    queue_samples: List[Dict[str, Any]] = field(default_factory=list)
    link_samples: List[Dict[str, Any]] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    error: Optional[str] = None
    #: 天气曲线快照（契约 §4.6.3 唯一授权的 RunResult 扩展字段：只加不改）。
    #: 每项 = WeatherSample.to_dict()（ts/preset/intensity/visibility_km/
    #: link_penalty/wind_speed_mps/wind_dir_deg），0.5 s 一个点。
    #: 默认空列表 -> 不跑天气实验时（如 run.py all）与改动前行为完全一致。
    weather_samples: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def wall_seconds(self) -> float:
        return float(self.metrics.get("runtime_wall_s", 0.0))
