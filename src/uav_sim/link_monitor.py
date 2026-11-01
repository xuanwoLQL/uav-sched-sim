"""
link_monitor.py —— 通信链路质量感知（对应契约 §4.4）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 野外矿区通信弱网：时延抖动、丢包、信号衰减、瞬时"弱网低谷"导致上传失败与业务失衡 |
| OS 原理层   | 等价于 **I/O 设备状态 / 阻塞率**：链路越差 -> 任务服务时间越长 + 失败阻塞（重传）概率越高，对应"IO 阻塞导致进程挂起" |
| 算法设计层 | 把 delay / loss / rssi 综合成 0~100 的**链路健康度**，供动态权重做弱网降权、供 Simulator 做失败判定 |
| 代码实现层 | `LinkState.health_score()` + `LinkMonitor.health_at()`（确定性预生成 + 线性插值） |

**确定性设计（控制变量的关键）**
    初始化时以 ``random.Random(seed * 100003 + uav_id)`` 为每台无人机**离线预生成**
    一条 0.5 s 网格的健康度曲线，``health_at(uav_id, t)`` 只做线性插值 ——
    纯函数式查询、无内部时钟依赖。因此：
      1. 同 ``(seed, uav_id)`` 任意次运行得到**逐点相同**的链路序列；
      2. 5 个调度策略回放同一条链路，实现严格的控制变量对照。

**天气是"外生衰减"（3D 渲染升级新增，契约 §4.6.2）**
    天气模型（``uav_sim.weather.WeatherModel``，可选）在每台无人机**种子噪声生成完之后**
    按同一 0.5 s 网格施加 ``h = weather.apply_health(h, weather.at(i*0.5))``：

        * 语义正确：先有"这台设备自身的链路随机波动"，再叠加"外部天气造成的整体恶化"；
        * 默认 ``weather=None``（或预设为 clear 时扣分恒 0）—— 数值与改动前**逐位相同**，
          既有 15 组实验结果不变（契约 §0 硬约束）；
        * 运行中切换天气调用 ``set_weather()`` 重建全部序列（确定性：等价于用新 preset
          重新构造，不依赖"何时切换"这一 wall-clock 事件）。

健康度 → 链路物理量的**反演映射**（保证 ``health_score()`` 恰好等于预设健康度 h）::

    delay_ms  = 10.0 * (100 - h)        # h=100 -> 0 ms ；h=0 -> 1000 ms
    loss_rate = 0.01 * (100 - h)        # h=100 -> 0    ；h=0 -> 1.0
    rssi_dbm  = -100 + 0.6 * h          # h=100 -> -40 dBm；h=0 -> -100 dBm

代入契约建议公式：``100×(0.4×delay_score + 0.4×(1-loss) + 0.2×rssi_score)``
恒等于 h（因 0.04×10 + 40×0.01 = 0.4 + 0.4 = 0.8，恰与 0.2 的 rssi 项配平）。
"""

from __future__ import annotations

import logging
import math
import random
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.config import cfg_get_float
from uav_sim.models import LinkSample
from uav_sim.weather import WeatherModel

logger = logging.getLogger(__name__)

#: 健康度曲线网格步长（仿真秒）—— 契约 §4.4 明确规定 0.5 s
HEALTH_GRID_S: float = 0.5

#: 弱网档位 -> (健康度均值 mu, 波动 sigma)
#: none：通畅野外链路；low：轻度弱网；high：重度弱网（含"弱网低谷段"）
WEAKNET_HEALTH_PROFILE: Dict[str, tuple] = {
    "none": (90.0, 3.0),
    "low": (72.0, 8.0),
    "high": (55.0, 12.0),
}

#: 重度弱网进入"低谷段"的概率与低谷持续网格数
_TROUGH_PROB: float = 0.10
_TROUGH_LEN_RANGE: tuple = (2, 5)
_TROUGH_HEALTH_RANGE: tuple = (15.0, 35.0)

#: 种子派生系数（保证不同 uav 的随机流互不相关又完全可复现）
_SEED_STRIDE: int = 100003


def health_profile(level: str) -> tuple:
    """返回弱网档位的 ``(mu, sigma)``；未知档位回退 ``none``。"""
    return WEAKNET_HEALTH_PROFILE.get(str(level), WEAKNET_HEALTH_PROFILE["none"])


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


@dataclass
class LinkState:
    """单条链路状态 —— 映射为 OS 中的一个 I/O 设备/通道。

    health 由 delay / loss / rssi 三者加权评分（契约 §4.4 建议公式）。
    """

    uav_id: int
    delay_ms: float = 0.0
    loss_rate: float = 0.0  # 0.0 ~ 1.0
    rssi_dbm: float = -100.0

    def apply_health(self, h: float) -> None:
        """把目标健康度 h 反演成 (delay, loss, rssi) 三个物理量。

        反演后 ``health_score() == h``（见模块 docstring 的恒等式）。
        """
        h = _clamp(float(h), 0.0, 100.0)
        self.delay_ms = 10.0 * (100.0 - h)
        self.loss_rate = _clamp(0.01 * (100.0 - h), 0.0, 1.0)
        self.rssi_dbm = -100.0 + 0.6 * h

    def update(self, weaknet_level: str, rng: random.Random) -> None:
        """链路状态随机游走（对应"野外弱网动态波动"）。

        OS 原理依据：设备状态随时间漂移 —— 用**均值回归 + 高斯噪声**建模，
        即 h <- h + 0.10×(mu - h) + N(0, 0.5σ)，既不会无限发散，也不会僵死。
        ``weaknet_level="high"`` 时额外有 10% 概率跌入"弱网低谷"（-15~-30）。
        """
        mu, sigma = health_profile(weaknet_level)
        h = self.health_score()
        h += 0.10 * (mu - h) + rng.gauss(0.0, 0.5 * sigma)
        if weaknet_level == "high" and rng.random() < _TROUGH_PROB:
            h -= rng.uniform(15.0, 30.0)
        self.apply_health(_clamp(h, 1.0, 100.0))

    def health_score(self) -> float:
        """链路健康度 0~100（契约 §4.4 建议公式，逐项注释 OS 依据）。

        ``health = 100 × (0.40×delay_score + 0.40×(1-loss_rate) + 0.20×rssi_score)``

        * ``delay_score = clamp(1 - delay_ms/1000, 0, 1)``
          —— 时延越高，任务在 I/O 上等待越久（阻塞概率↑）；
        * ``1 - loss_rate`` —— 丢包率越高，重传概率越高（本仿真里直接决定 p_fail）；
        * ``rssi_score = clamp((rssi_dbm + 100)/60, 0, 1)``
          —— 信号强度是链路是否可用的"硬底"，权重给 0.20。
        """
        delay_score = _clamp(1.0 - self.delay_ms / 1000.0, 0.0, 1.0)
        rssi_score = _clamp((self.rssi_dbm + 100.0) / 60.0, 0.0, 1.0)
        return 100.0 * (0.40 * delay_score + 0.40 * (1.0 - self.loss_rate) + 0.20 * rssi_score)


class LinkMonitor:
    """全局链路监控 —— 对应 OS 的"设备状态表"。

    线程安全说明：
        * ``_series``（预生成健康度曲线）在 ``register_uav`` 后**只读**，
          读取只依赖 GIL 下的 dict 查找，因此 ``health_at()`` 无锁（高频调用路径）；
        * ``_active`` / ``_samples`` 用 ``self._lock`` 保护（tick 可能被监控线程与主循环同时调用）。
    """

    def __init__(self, seed: int, weaknet_level: str, duration_s: float,
                 uav_ids: Iterable[int], cfg: dict,
                 weather: Optional[WeatherModel] = None) -> None:
        self.seed = int(seed)
        self.weaknet_level = str(weaknet_level)
        self.duration_s = float(duration_s)
        self.cfg = cfg
        self.health_poor: float = cfg_get_float(cfg, "weaknet.health_poor", 45.0)
        self.health_good: float = cfg_get_float(cfg, "weaknet.health_good", 70.0)
        self.sample_interval_s: float = cfg_get_float(cfg, "simulation.link_sample_interval_s", 1.0)
        self.time_scale: float = cfg_get_float(cfg, "runtime.time_scale", 0.1)

        # ---- 天气衰减模型（新增可选参数；None = 保持改动前的纯弱网行为） ----
        # 允许传 preset 字符串：等价于按本监控器的 (seed, duration) 现建一个 WeatherModel，
        # 方便单测/脚本直接 `LinkMonitor(..., weather="dust")` 做对比（契约 §4.6.2）。
        if isinstance(weather, str):
            weather = WeatherModel(seed=self.seed, preset=weather,
                                   duration_s=self.duration_s, cfg=cfg)
        self.weather: Optional[WeatherModel] = weather

        #: 网格点数 = ceil(duration/0.5) + 1（含 t=0 与 t>=duration 的收尾点）
        self._grid_len: int = int(math.ceil(self.duration_s / HEALTH_GRID_S)) + 1

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._series: Dict[int, List[float]] = {}
        self._active: set = set()
        self._states: Dict[int, LinkState] = {}
        self._samples: List[LinkSample] = []
        self._now: float = 0.0
        self._next_sample_ts: float = 0.0

        for uav_id in uav_ids:
            self.register_uav(int(uav_id))

    # ---------------------------------------------------------------- 注册
    def register_uav(self, uav_id: int) -> None:
        """注册新无人机链路（= 设备上线，创建其确定性健康度曲线）。

        曲线只由 ``(seed, uav_id)`` 决定，与注册顺序无关，故可离线复现。
        """
        uav_id = int(uav_id)
        if uav_id in self._series:
            with self._lock:
                self._active.add(uav_id)
            return
        self._series[uav_id] = self._build_series(uav_id)
        with self._lock:
            self._active.add(uav_id)
            self._states.setdefault(uav_id, LinkState(uav_id=uav_id))
            self._states[uav_id].apply_health(self._series[uav_id][0])

    def unregister_uav(self, uav_id: int) -> None:
        """注销无人机链路（= 设备下线，从活跃集合移除，曲线保留以供回放）。"""
        with self._lock:
            self._active.discard(int(uav_id))

    def _build_series(self, uav_id: int) -> List[float]:
        """离线生成 0.5 s 网格健康度序列（单 uav 一个独立随机流）。

        生成顺序（契约 §4.6.2 硬要求）：
            ① 先按 ``(seed, uav_id)`` 的独立随机流生成**基础健康度**（弱网噪声/低谷段）；
            ② **之后**再按同一网格时刻施加天气衰减 ``weather.apply_health``。
        顺序不能反：天气是外部环境，必须叠加在设备自身波动之上；反过来会让
        随机流抽样次数受天气影响，破坏"同 seed 同链路"的控制变量前提。
        """
        rng = random.Random(self.seed * _SEED_STRIDE + int(uav_id))
        mu, sigma = health_profile(self.weaknet_level)
        series: List[float] = []
        h = mu
        trough_left = 0
        for _ in range(self._grid_len):
            if self.weaknet_level == "high" and trough_left <= 0 and rng.random() < _TROUGH_PROB:
                # 重度弱网：10% 概率进入连续低谷段（模拟山区遮挡/电磁干扰）
                trough_left = rng.randint(*_TROUGH_LEN_RANGE)
            if trough_left > 0:
                target = rng.uniform(*_TROUGH_HEALTH_RANGE)
                h = h * 0.35 + target * 0.65
                trough_left -= 1
            else:
                # 均值回归 + 高斯抖动：既不发散也不僵死
                h += 0.25 * (mu - h) + rng.gauss(0.0, 0.45 * sigma)
            series.append(_clamp(h, 1.0, 100.0))

        # ---- ② 天气外生衰减（必须在种子噪声之后；clear 时为恒等变换） ----
        return self._apply_weather(series)

    def _apply_weather(self, series: List[float]) -> List[float]:
        """按网格时刻对整条健康度序列施加天气扣分。

        ``weather=None`` 时**原样返回同一个 list**（连一次浮点运算都不做），
        从实现层面保证改动前的数值逐位不变；clear 时 ``link_penalty`` 恒 0.0，
        ``apply_health`` 只是 clamp 到 [1,100]，而序列本就在该区间内，
        因此同样逐位不变（见 tests/test_weather.py 的回归用例）。
        """
        weather = self.weather
        if weather is None:
            return series
        return [weather.apply_health(h, weather.at(i * HEALTH_GRID_S))
                for i, h in enumerate(series)]

    def set_weather(self, weather: "str | WeatherModel | None") -> None:
        """运行中切换天气（契约 §4.6.2）：重建**全部** uav 的健康度序列。

        参数可以是天气名（按本监控器的 seed/duration 现建模型）、WeatherModel
        实例，或 None（退化为无天气的纯弱网基线，便于对照）。
        重建后立刻按当前时刻刷新 ``_states``，使 ``/api/state`` 的 links 字段下一秒
        就能反映新的链路健康度（无需等下一次采样）。
        """
        if weather is None:
            model: Optional[WeatherModel] = None
        elif isinstance(weather, str):
            model = WeatherModel(seed=self.seed, preset=weather,
                                 duration_s=self.duration_s, cfg=self.cfg)
        else:
            model = weather
        self.weather = model
        uids = list(self._series.keys())
        # 整体替换 dict（而不是原地改）：health_at 可能在其它线程并发读，
        # GIL 下"换引用"是原子的，读端要么看到旧表要么看到新表，不会读到半更新状态。
        rebuilt = {uid: self._build_series(uid) for uid in uids}
        self._series = rebuilt
        with self._lock:
            now = self._now
        for uid, values in rebuilt.items():
            with self._lock:
                st = self._states.get(uid)
            if st is not None and values:
                st.apply_health(self.health_at(uid, now))

    # ---------------------------------------------------------------- 查询
    def health_at(self, uav_id: int, t: float) -> float:
        """纯函数式查询：``t`` 时刻该链路的健康度（0~100，网格间线性插值）。

        未注册的 uav 返回 100.0（视为链路正常，避免"未知设备必然失败"的伪结论）。
        """
        series = self._series.get(int(uav_id))
        if not series:
            return 100.0
        idx = float(t) / HEALTH_GRID_S
        if idx <= 0.0:
            return series[0]
        i0 = int(math.floor(idx))
        if i0 >= len(series) - 1:
            return series[-1]
        frac = idx - i0
        return series[i0] + (series[i0 + 1] - series[i0]) * frac

    def get_health(self, uav_id: int) -> float:
        """当前时刻（``tick`` 推进到的 now）该链路健康度。"""
        with self._lock:
            now = self._now
        return self.health_at(uav_id, now)

    def get_all_health(self) -> dict:
        """当前时刻全部活跃链路健康度 ``{uav_id: health}``。"""
        with self._lock:
            now = self._now
            active = sorted(self._active)
        return {uid: self.health_at(uid, now) for uid in active}

    def is_poor(self, uav_id: int, t: float) -> bool:
        """是否劣质链路（``health < weaknet.health_poor``）。"""
        return self.health_at(uav_id, t) < self.health_poor

    # ---------------------------------------------------------------- 采样
    def tick(self, now: float) -> None:
        """推进仿真时刻并按 ``link_sample_interval_s`` 抽稀采集 ``LinkSample``。

        替代指导书里的阻塞 ``monitor_loop``：主循环需要一个**可停的步进式**监控，
        由 Simulator 每轮驱动（契约 §4.4）。
        """
        now = float(now)
        with self._lock:
            self._now = now
            if now + 1e-9 < self._next_sample_ts:
                return
            active = sorted(self._active)
            while self._next_sample_ts <= now + 1e-9:
                self._next_sample_ts += self.sample_interval_s
            samples = self._samples
        # 采样本身不持锁（health_at 无锁），减少主循环与工作线程的锁竞争
        for uid in active:
            health = self.health_at(uid, now)
            samples.append(self._make_sample(uid, now, health))

    @staticmethod
    def _make_sample(uav_id: int, ts: float, health: float) -> LinkSample:
        """由健康度反演物理量，构造落库用采样（与 health_score 公式自洽）。"""
        state = LinkState(uav_id=uav_id)
        state.apply_health(health)
        return LinkSample(
            ts=ts,
            uav_id=uav_id,
            delay_ms=state.delay_ms,
            loss_rate=state.loss_rate,
            rssi_dbm=state.rssi_dbm,
            health=health,
        )

    def samples(self) -> list:
        """已采集的链路采样列表（返回副本，调用方可安全遍历）。"""
        with self._lock:
            return list(self._samples)

    # ---------------------------------------------------------------- 监控线程
    def monitor_loop(self) -> None:
        """保留指导书签名的**阻塞式**监控循环（独立线程，交互式演示用）。

        正式仿真走 ``tick()``（主循环步进），本方法仅为兼容指导书
        ``LinkMonitor.monitor_loop()`` 接口。通过 ``stop()`` 或 ``stop_event`` 退出。
        """
        step_real = max(1e-3, self.sample_interval_s * self.time_scale)
        while not self._stop_event.is_set():
            self.tick(self._now + self.sample_interval_s)
            if self._stop_event.wait(step_real):
                break

    def stop(self) -> None:
        """请求 ``monitor_loop`` 退出（附加便捷方法，契约未列但演示需要）。"""
        self._stop_event.set()

    # ---------------------------------------------------------------- 统计辅助
    def mean_health(self, uav_id: Optional[int] = None) -> float:
        """整条曲线的健康度均值 —— 供标定/报告说明链路档位（附加方法）。"""
        if uav_id is not None:
            series = self._series.get(int(uav_id)) or [100.0]
            return sum(series) / len(series)
        all_points = [v for s in self._series.values() for v in s]
        return sum(all_points) / len(all_points) if all_points else 100.0


if __name__ == "__main__":  # pragma: no cover - 手工自检入口
    from uav_sim.config import load_config

    _cfg = load_config()
    for _level in ("none", "low", "high"):
        _mon = LinkMonitor(seed=20261031, weaknet_level=_level, duration_s=30.0,
                           uav_ids=[1, 2, 3], cfg=_cfg)
        _vals = [_mon.health_at(1, t / 10.0) for t in range(0, 300)]
        _poor = sum(1 for v in _vals if v < _cfg["weaknet"]["health_poor"])
        print(f"weaknet={_level:5s} 均值={sum(_vals) / len(_vals):6.2f} "
              f"最小={min(_vals):6.2f} 劣质占比={_poor / len(_vals) * 100:5.1f}%")
