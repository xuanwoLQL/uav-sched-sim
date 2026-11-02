"""
weather.py —— 天气 → 弱网 → 链路健康度衰减模型
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 野外地质勘查遇到降雨/降雪/沙尘，**能见度骤降、风力扰动天线**，无线链路质量被外部环境拖垮，上传反复失败/重传 |
| OS 原理层   | 等价于 **I/O 设备的外部故障注入**：设备本身没坏，但环境让通道的时延/丢包/信号强度恶化，表现为 IO 阻塞概率上升（与 link_monitor 的 health ↔ delay/loss/rssi 反演映射天然衔接） |
| 算法设计层 | 把"天气强度曲线"折算成**链路健康度扣分**（``link_penalty``），从而进入动态权重（弱网降权）、失败重传概率、服务时长标定 —— 天气不是贴图滤镜，而是真的改变业务指标 |
| 代码实现层 | ``WeatherPreset``（预设参数）+ ``WeatherModel``（0.5 s 网格确定性强度曲线 + 线性插值）+ ``WeatherSample``（单个网格样本） |

-------------------------------------------------------------------------
确定性设计（与 LinkMonitor 完全一致的"控制变量"哲学）
-------------------------------------------------------------------------
* 天气曲线由 ``random.Random(seed * 7919 + stable_hash(preset) % 100003)``
  **离线预生成**，绝不使用全局 ``random``，也不掺 wall-clock；
* 因此同 ``(seed, preset, duration_s)`` 任意次运行得到**逐点相同**的
  ``WeatherSample`` 序列 —— 5 个调度策略回放同一条天气曲线，对照实验里
  只有"调度策略"一个变量；
* ``preset="clear"`` 时 ``link_penalty`` 恒为 ``0.0``，``apply_health()``
  对已 clamp 在 [1,100] 内的健康度是**恒等变换**，故默认工况下既有 15 组
  实验结果逐位不变（硬约束，见 docs/07 §0）。

-------------------------------------------------------------------------
天气曲线形态（教学要点：为什么不是一条直线）
-------------------------------------------------------------------------
真实的天气过程不是"开关"：::

    intensity
      1.0 |        ～～～ 平台期（阵风抖动 ±15%）～～～
          |      /                                \\
      0.5 |     /                                  \\  尾部衰减
          |    /                                    \\
      0.0 +---+--------------------------------------+---> t
               onset 8~15%                      最后 12~22%

* **onset（爬升）**：云团移入/沙尘锋面到达，强度在 8%~15% 时长内 S 形爬升
  —— 对应"链路先抖动、再恶化"，而不是瞬间从 100 掉到 40；
* **平台期（阵风）**：用 AR(1) 低通随机过程生成 ±15% 的阵风抖动（``gust``），
  比纯白噪声更像真实阵风（有惯性、不会逐帧乱跳）；
* **尾部（衰减）**：天气过境，强度在最后 12%~22% 时长内平滑回落到 0。

三个随机相位（onset 长度、尾部起点、阵风序列）都由同一条随机流顺序抽取，
所以曲线形态"看起来自然"但完全可复现。
"""

from __future__ import annotations

import logging
import math
import random
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.config import get  # noqa: E402
from uav_sim.realsource import RealDataError, validate_weather_snapshot  # noqa: E402

logger = logging.getLogger(__name__)

#: 天气曲线网格步长（仿真秒）—— 与 ``link_monitor.HEALTH_GRID_S`` 对齐，
#: 这样天气扣分与基础健康度落在同一组时刻上，不需要二次插值。
WEATHER_GRID_S: float = 0.5

#: 预设顺序（报告/图表/前端下拉框的固定顺序）
PRESET_ORDER: tuple = ("clear", "rain", "snow", "dust")

#: 代码内置的预设兜底参数（config.yaml 缺 weather 段时也能跑）。
#: 键含义：
#:   label            —— 中文名（前端/报告直接引用，不写死在 HTML 里）
#:   visibility_km    —— 满强度时的能见度（km），前端据此调 fog 密度
#:   link_penalty_max —— 满强度时对链路健康度的**最大扣分**（0~100）
#:   intensity        —— 平台期目标强度 0~1（前端粒子浓度也用它）
#:   wind_speed_mps   —— 满强度时的风速（m/s，前端雨滴/雪花的倾斜方向）
#:   wind_dir_deg     —— 主风向（0~360°，0=北）
#:   fog_color_hex    —— 该天气的雾色（前端只做文案/兜底，真正雾色由 JS 决定）
_DEFAULT_PRESETS: Dict[str, Dict[str, Any]] = {
    "clear": dict(label="晴朗", visibility_km=12.0, link_penalty_max=0.0,
                  intensity=0.0, wind_speed_mps=1.5, wind_dir_deg=0.0,
                  fog_color_hex="#9fb6c9"),
    "rain": dict(label="中雨", visibility_km=3.0, link_penalty_max=18.0,
                 intensity=0.78, wind_speed_mps=8.0, wind_dir_deg=215.0,
                 fog_color_hex="#2a3340"),
    "snow": dict(label="降雪", visibility_km=1.2, link_penalty_max=24.0,
                 intensity=0.85, wind_speed_mps=4.0, wind_dir_deg=300.0,
                 fog_color_hex="#cfd8e2"),
    "dust": dict(label="沙尘", visibility_km=0.6, link_penalty_max=32.0,
                 intensity=0.92, wind_speed_mps=14.0, wind_dir_deg=135.0,
                 fog_color_hex="#8a6a48"),
}


def _clamp(value: float, low: float, high: float) -> float:
    """线性截断（不用 min/max 嵌套，保持与 link_monitor._clamp 同语义）。"""
    return low if value < low else (high if value > high else value)


def _smoothstep(k: float) -> float:
    """S 形插值曲线 3k²-2k³（k∈[0,1]）—— 天气爬升/衰减"两头缓、中间快"。"""
    k = _clamp(k, 0.0, 1.0)
    return k * k * (3.0 - 2.0 * k)


def _stable_hash(text: str) -> int:
    """**跨进程稳定**的字符串哈希（zlib.crc32）。

    为什么不用内置 ``hash()``：Python 3 对 str 的 hash 每个进程加盐
    （PYTHONHASHSEED 随机），用它派生随机种子会导致"同 seed 两次运行结果不同"，
    直接违反契约 §4.6.1 的确定性硬要求。crc32 是确定性的，完全满足需求。
    """
    return int(zlib.crc32(str(text).encode("utf-8")))


@dataclass(frozen=True)
class WeatherPreset:
    """一种天气的静态参数（对应 config.yaml ``weather.presets.<name>``）。

    ``frozen=True``：预设是只读常量，运行中切换天气应新建 ``WeatherModel``，
    而不是改预设对象（避免"改一个实例污染全局"的隐蔽 bug）。
    """

    preset: str
    label: str
    visibility_km: float
    link_penalty_max: float
    intensity: float
    wind_speed_mps: float
    wind_dir_deg: float
    fog_color_hex: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "preset": self.preset,
            "label": self.label,
            "visibility_km": round(float(self.visibility_km), 3),
            "link_penalty_max": round(float(self.link_penalty_max), 3),
            "intensity": round(float(self.intensity), 3),
            "wind_speed_mps": round(float(self.wind_speed_mps), 2),
            "wind_dir_deg": round(float(self.wind_dir_deg), 1),
            "fog_color_hex": self.fog_color_hex,
        }


@dataclass
class WeatherSample:
    """某一时刻的天气采样（= 契约 §4.6.1 的 WeatherSample）。

    该对象是**纯数据**：``link_penalty`` 由 ``WeatherModel`` 按强度曲线算好，
    消费方（LinkMonitor / 前端）只做 ``apply_health`` 或展示，不再自己造公式。
    """

    ts: float                  # 仿真时刻（秒）
    preset: str                # clear / rain / snow / dust
    intensity: float           # 强度 0~1（前端粒子浓度、报告里画曲线都用它）
    visibility_km: float       # 能见度（km）
    link_penalty: float        # 对链路健康度的扣分（0 ~ preset.link_penalty_max）
    wind_speed_mps: float      # 风速（m/s）
    wind_dir_deg: float = 0.0  # 风向（0~360°）

    def to_dict(self) -> Dict[str, Any]:
        """JSON 友好字典（保留 6 位小数，保证确定性序列可直接逐点比对）。"""
        return {
            "ts": round(float(self.ts), 6),
            "preset": self.preset,
            "intensity": round(float(self.intensity), 6),
            "visibility_km": round(float(self.visibility_km), 6),
            "link_penalty": round(float(self.link_penalty), 6),
            "wind_speed_mps": round(float(self.wind_speed_mps), 6),
            "wind_dir_deg": round(float(self.wind_dir_deg), 3),
        }


# ---------------------------------------------------------------------------
# 预设注册表（模块级常量：clear / rain / snow / dust）
# ---------------------------------------------------------------------------
WEATHER_PRESETS: Dict[str, WeatherPreset] = {
    name: WeatherPreset(
        preset=name,
        label=str(meta["label"]),
        visibility_km=float(meta["visibility_km"]),
        link_penalty_max=float(meta["link_penalty_max"]),
        intensity=float(meta["intensity"]),
        wind_speed_mps=float(meta["wind_speed_mps"]),
        wind_dir_deg=float(meta["wind_dir_deg"]),
        fog_color_hex=str(meta["fog_color_hex"]),
    )
    for name, meta in _DEFAULT_PRESETS.items()
}


def preset_names() -> List[str]:
    """全部合法天气名（固定顺序，供 CLI/前端下拉框/API 校验）。"""
    return list(PRESET_ORDER)


def is_valid_preset(preset: str) -> bool:
    """名字是否合法（API 层用它返回 400，而不是让非法值静默回退成 clear）。"""
    return str(preset).strip().lower() in WEATHER_PRESETS


def get_preset(preset: str) -> WeatherPreset:
    """取预设；未知名字回退 ``clear``（内部调用方的兜底，不会抛异常）。"""
    name = str(preset).strip().lower()
    return WEATHER_PRESETS.get(name, WEATHER_PRESETS["clear"])


def config_preset(cfg: Optional[dict], preset: Optional[str] = None) -> str:
    """解析最终生效的天气名：显式参数 > config ``weather.default_preset`` > clear。

    非法名字一律回退（配置容错；但 HTTP API 会先校验，非法请求返回 400）。
    """
    if preset is not None and is_valid_preset(preset):
        return str(preset).strip().lower()
    from_cfg = get(cfg or {}, "weather.default_preset", "clear")
    if is_valid_preset(str(from_cfg)):
        return str(from_cfg).strip().lower()
    return "clear"


def available_presets() -> List[Dict[str, str]]:
    """``/api/state.weather.available`` 的固定形态（键名不可改，契约 §4.6.4）。"""
    return [{"preset": p, "label": WEATHER_PRESETS[p].label} for p in PRESET_ORDER]


class WeatherModel:
    """天气模型：0.5 s 网格确定性强度曲线 + 线性插值 + 健康度扣分。

    Parameters
    ----------
    seed : int
        全局随机种子（与 ``Simulator.seed`` / ``LinkMonitor`` 同源，保证整条
        "天气 -> 链路 -> 指标"因果链可复现）。
    preset : str
        天气名（clear/rain/snow/dust）；非法值回退 clear。
    duration_s : float
        仿真总时长（秒）。曲线覆盖 ``[0, duration_s]``。
    cfg : dict | None
        全局配置；读取 ``weather.default_preset`` / ``weather.enable_dynamic`` /
        ``weather.grid_s`` / ``weather.presets.*`` 覆盖数值。

    使用方式（典型）::

        weather = WeatherModel(seed, "dust", duration_s=90.0, cfg=cfg)
        mon = LinkMonitor(..., weather=weather)   # 链路健康度自动扣分
        snap = weather.snapshot(sim.now())        # /api/state 的 weather 字段
    """

    def __init__(self, seed: int = 0, preset: str = "clear",
                 duration_s: float = 90.0, cfg: Optional[dict] = None,
                 real_snapshot: Optional[dict] = None) -> None:
        # 契约 §4.6.6 的示例里写作 WeatherModel("clear")（把预设放在第 1 个位置）。
        # 这里做一个无歧义的兼容：第一个位置参数是 str 时按 preset 处理，seed 取 0。
        if isinstance(seed, str):
            preset, seed = seed, 0
        self.seed: int = int(seed)
        self.cfg: dict = cfg or {}
        self.duration_s: float = max(0.0, float(duration_s))
        self.grid_s: float = max(0.05, float(
            get(self.cfg, "weather.grid_s", WEATHER_GRID_S) or WEATHER_GRID_S))
        self.enable_dynamic: bool = bool(
            get(self.cfg, "weather.enable_dynamic", True))

        self._grid_len: int = int(math.ceil(
            self.duration_s / self.grid_s - 1e-9)) + 1

        #: 真实数据来源元数据。合成模式为 synthetic / null；真实模式带采集时间。
        self.source: str = "synthetic"
        self.fetched_at: Optional[str] = None
        self.observed_at: Optional[str] = None
        self.real_snapshot: Optional[dict] = None
        self._real_derived: Optional[dict] = None

        if real_snapshot is not None:
            try:
                validate_weather_snapshot(real_snapshot)
            except RealDataError:
                # 调用方已经负责降级；这里再兜底一次，绝不让坏快照进入仿真。
                logger.warning("真实天气快照非法，WeatherModel 回退合成预设")
                real_snapshot = None

        if real_snapshot is not None:
            self.real_snapshot = validate_weather_snapshot(real_snapshot)
            self._real_derived = dict(self.real_snapshot["derived"])
            self.source = "real"
            self.fetched_at = self.real_snapshot.get("fetched_at")
            self.observed_at = self.real_snapshot.get("observed_at")
            self.preset = str(self._real_derived["preset"])
            base = get_preset(self.preset)
            self._preset_obj = WeatherPreset(
                preset=self.preset,
                label=str(self._real_derived.get("label") or base.label),
                visibility_km=max(0.05, float(self._real_derived["visibility_km"])),
                link_penalty_max=max(32.0, float(self._real_derived["link_penalty"])),
                intensity=_clamp(float(self._real_derived["intensity"]), 0.0, 1.0),
                wind_speed_mps=max(0.0, float(self._real_derived["wind_speed_mps"])),
                wind_dir_deg=float(self._real_derived["wind_dir_deg"]) % 360.0,
                fog_color_hex=base.fog_color_hex,
            )
            self._samples = self._build_real_curve()
            logger.info("WeatherModel 使用真实快照 source=%s observed_at=%s preset=%s",
                        self.real_snapshot.get("source"), self.observed_at, self.preset)
        else:
            self.preset = config_preset(self.cfg, preset)
            self._preset_obj = self._resolve_preset(self.preset)
            #: 预先算好的网格样本（0.5 s 一个，确定性；at() 只做插值）
            self._samples = self._build_curve()

        logger.debug("WeatherModel seed=%s preset=%s source=%s duration=%.1fs grid=%.1fs 点数=%d",
                     self.seed, self.preset, self.source, self.duration_s, self.grid_s,
                     len(self._samples))

    # ---------------------------------------------------------------- 构造辅助
    def _resolve_preset(self, name: str) -> WeatherPreset:
        """把 config.yaml ``weather.presets.<name>`` 的数值覆盖合并进代码预设。

        只允许覆盖**数值参数**（能见度/扣分上界/风速/风向/目标强度），
        中文名与雾色由代码常量统一维护，避免配置文件写错标签导致口径漂移。
        """
        base = get_preset(name)
        section = get(self.cfg, f"weather.presets.{name}", {}) or {}
        if not isinstance(section, dict):
            section = {}

        def num(key: str, default: float) -> float:
            try:
                return float(section.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        return WeatherPreset(
            preset=base.preset,
            label=base.label,
            visibility_km=max(0.05, num("visibility_km", base.visibility_km)),
            link_penalty_max=max(0.0, num("link_penalty_max", base.link_penalty_max)),
            intensity=_clamp(num("intensity", base.intensity), 0.0, 1.0),
            wind_speed_mps=max(0.0, num("wind_speed_mps", base.wind_speed_mps)),
            wind_dir_deg=num("wind_dir_deg", base.wind_dir_deg) % 360.0,
            fog_color_hex=base.fog_color_hex,
        )

    @property
    def preset_obj(self) -> WeatherPreset:
        return self._preset_obj

    @property
    def label(self) -> str:
        return self._preset_obj.label

    # ---------------------------------------------------------------- 曲线生成
    def _rng(self) -> random.Random:
        """派生**模块内独立**随机流：同 (seed, preset) 逐点可复现。

        系数 7919 与 ``Simulator._rng`` 的 ``seed*7919+13`` 同一族，方便排查；
        ``_stable_hash(preset) % 100003`` 让不同天气使用互不相关的随机流。
        """
        return random.Random(self.seed * 7919 + _stable_hash(self.preset) % 100003)

    def _penalty_visibility(self, intensity: float) -> tuple:
        """由强度推导 (link_penalty, visibility_km)。

        * ``link_penalty = link_penalty_max × intensity`` —— 强度越高，链路扣分越大；
          **clear 的 link_penalty_max=0，故扣分恒 0.0**（默认工况结果不变的技术保证）；
        * 能见度在"晴朗能见度 → 该天气满强度能见度"之间按强度线性插值 ——
          物理上能见度衰减与降水/沙尘强度近似成正比（教学简化，注释写明）。
        """
        p = self._preset_obj
        penalty = 0.0 if p.link_penalty_max <= 0.0 else p.link_penalty_max * intensity
        clear_vis = WEATHER_PRESETS["clear"].visibility_km
        vis = clear_vis - (clear_vis - p.visibility_km) * _clamp(intensity, 0.0, 1.0)
        return penalty, max(0.05, vis)

    def _wind(self, intensity: float, ts: float) -> tuple:
        """由强度/时刻推导 (wind_speed_mps, wind_dir_deg)。

        * 风速：静风基底 55% + 强度贡献 45%（天气越强风越大，阵风也更强）；
        * 风向：主风向叠加一个 **2 倍周期**的缓慢摆动（模拟风向渐变），
          clear 时固定为预设值（无风扰动的"教科书晴天"）。
        """
        p = self._preset_obj
        if p.intensity <= 0.0 and p.link_penalty_max <= 0.0:
            return max(0.0, p.wind_speed_mps), p.wind_dir_deg % 360.0
        speed = p.wind_speed_mps * (0.55 + 0.45 * _clamp(intensity, 0.0, 1.0))
        phase = 2.0 * math.pi * 2.0 * (ts / max(1e-6, self.duration_s))
        direction = (p.wind_dir_deg + 18.0 * math.sin(phase)) % 360.0
        return max(0.0, speed), direction

    def _build_curve(self) -> List[WeatherSample]:
        """离线生成整条 0.5 s 网格天气曲线（确定性；不许用全局 random）。"""
        n = max(1, self._grid_len)
        p = self._preset_obj
        peak = _clamp(p.intensity, 0.0, 1.0)
        samples: List[WeatherSample] = []

        # ---- 情况 A：clear / 恒定强度（enable_dynamic=false）→ 一条直线 ----
        # 物理意义：默认"晴朗"没有天气过程；敏感性对照里也用它做恒定强度基线。
        if (not self.enable_dynamic) or peak <= 0.0:
            intensity_const = peak
            penalty, vis = self._penalty_visibility(intensity_const)
            for i in range(n):
                ts = i * self.grid_s
                wind_speed, wind_dir = self._wind(intensity_const, ts)
                samples.append(WeatherSample(
                    ts=ts, preset=self.preset, intensity=intensity_const,
                    visibility_km=vis, link_penalty=penalty,
                    wind_speed_mps=wind_speed, wind_dir_deg=wind_dir))
            return samples

        # ---- 情况 B：动态天气（onset → 平台阵风 → 尾部衰减）----
        rng = self._rng()
        # 三个随机相位都由同一条随机流顺序抽取 → 形态自然但可复现
        onset_frac = rng.uniform(0.08, 0.15)          # 爬升段占比 8%~15%
        tail_start_frac = rng.uniform(0.78, 0.88)     # 尾部起点 → 尾部占 12%~22%
        onset_end = max(self.grid_s, self.duration_s * onset_frac)
        tail_start = max(onset_end + self.grid_s, self.duration_s * tail_start_frac)
        tail_span = max(self.grid_s, self.duration_s - tail_start)

        gust = 0.0            # AR(1) 阵风状态（有惯性，不会逐帧乱跳）
        tail_level = peak     # 尾部衰减的起始强度（取平台期最后一段）
        for i in range(n):
            ts = i * self.grid_s
            if ts <= onset_end:
                # onset：S 形爬升（云团移入 / 沙尘锋面到达）
                intensity = peak * _smoothstep(ts / max(1e-9, onset_end))
            elif ts < tail_start:
                # 平台期：AR(1) 低通随机过程 → ±15% 阵风抖动
                gust = 0.7 * gust + 0.3 * rng.uniform(-1.0, 1.0)
                gust = _clamp(gust, -1.0, 1.0)
                intensity = peak * (1.0 + 0.15 * gust)
                tail_level = intensity
            else:
                # 尾部：从平台期强度平滑衰减到 0（天气过境）
                k = (ts - tail_start) / tail_span
                intensity = tail_level * (1.0 - _smoothstep(k))
            intensity = _clamp(intensity, 0.0, 1.0)
            penalty, vis = self._penalty_visibility(intensity)
            wind_speed, wind_dir = self._wind(intensity, ts)
            samples.append(WeatherSample(
                ts=ts, preset=self.preset, intensity=intensity,
                visibility_km=vis, link_penalty=penalty,
                wind_speed_mps=wind_speed, wind_dir_deg=wind_dir))
        # 末尾样本强制归零：曲线必须在 duration 处收敛到 clear 状态，
        # 否则 run_matrix 收尾时仍按"天气未过境"扣分，报告口径说不清。
        last = samples[-1]
        if self.duration_s > 0 and last.ts + 1e-9 >= self.duration_s - self.grid_s:
            penalty0, vis0 = self._penalty_visibility(0.0)
            wind0, dir0 = self._wind(0.0, self.duration_s)
            samples[-1] = WeatherSample(
                ts=last.ts, preset=self.preset, intensity=0.0,
                visibility_km=vis0, link_penalty=penalty0,
                wind_speed_mps=wind0, wind_dir_deg=dir0)
        return samples

    def _build_real_curve(self) -> List[WeatherSample]:
        """真实快照 → 整轮常量曲线。

        为什么不做 onset/阵风/尾部：真实快照只有一个观测时刻，而仿真只有
        30~90 秒；把它包装成"真实秒级变化"是伪造。常量曲线让"同一快照回放
        两次逐位一致"成为可证明的事实。
        """
        d = self._real_derived or {}
        n = max(1, self._grid_len)
        intensity = _clamp(float(d.get("intensity", 0.0)), 0.0, 1.0)
        visibility = max(0.05, float(d.get("visibility_km", 12.0)))
        penalty = _clamp(float(d.get("link_penalty", 0.0)), 0.0, 100.0)
        wind_speed = max(0.0, float(d.get("wind_speed_mps", 0.0)))
        wind_dir = float(d.get("wind_dir_deg", 0.0)) % 360.0
        return [
            WeatherSample(
                ts=i * self.grid_s,
                preset=self.preset,
                intensity=intensity,
                visibility_km=visibility,
                link_penalty=penalty,
                wind_speed_mps=wind_speed,
                wind_dir_deg=wind_dir,
            )
            for i in range(n)
        ]

    # ---------------------------------------------------------------- 查询接口
    def at(self, t: float) -> WeatherSample:
        """``t`` 时刻的天气采样（0.5 s 网格线性插值；确定性纯函数）。"""
        t = float(t)
        if not self._samples:
            return WeatherSample(ts=t, preset=self.preset, intensity=0.0,
                                 visibility_km=WEATHER_PRESETS["clear"].visibility_km,
                                 link_penalty=0.0, wind_speed_mps=0.0,
                                 wind_dir_deg=0.0)
        if t <= 0.0:
            base = self._samples[0]
            return WeatherSample(ts=t, **{k: getattr(base, k) for k in (
                "preset", "intensity", "visibility_km", "link_penalty",
                "wind_speed_mps", "wind_dir_deg")})
        idx = t / self.grid_s
        i0 = int(math.floor(idx))
        if i0 >= len(self._samples) - 1:
            last = self._samples[-1]
            return WeatherSample(ts=t, **{k: getattr(last, k) for k in (
                "preset", "intensity", "visibility_km", "link_penalty",
                "wind_speed_mps", "wind_dir_deg")})
        frac = idx - i0
        a = self._samples[i0]
        b = self._samples[i0 + 1]

        def lerp(x: float, y: float) -> float:
            return x + (y - x) * frac

        # 风向跨 0/360 边界时走"最短弧"插值，否则会在 359→1 时插出 180° 的假转向
        d0, d1 = a.wind_dir_deg, b.wind_dir_deg
        delta = (d1 - d0 + 540.0) % 360.0 - 180.0
        direction = (d0 + delta * frac) % 360.0
        return WeatherSample(
            ts=t,
            preset=self.preset,
            intensity=lerp(a.intensity, b.intensity),
            visibility_km=lerp(a.visibility_km, b.visibility_km),
            link_penalty=lerp(a.link_penalty, b.link_penalty),
            wind_speed_mps=lerp(a.wind_speed_mps, b.wind_speed_mps),
            wind_dir_deg=direction,
        )

    def samples(self) -> List[WeatherSample]:
        """已采样的整条网格曲线（0.5 s 一个，返回副本）。"""
        return list(self._samples)

    def mean_penalty(self) -> float:
        """整条曲线的平均链路扣分 —— 报告里"该天气平均扣多少分"的直接口径。"""
        if not self._samples:
            return 0.0
        return sum(s.link_penalty for s in self._samples) / len(self._samples)

    def mean_intensity(self) -> float:
        """整条曲线的平均强度（附加诊断指标，前端/报告可用）。"""
        if not self._samples:
            return 0.0
        return sum(s.intensity for s in self._samples) / len(self._samples)

    # ---------------------------------------------------------------- 健康度联动
    @staticmethod
    def apply_health(base_health: float, sample: WeatherSample) -> float:
        """把天气扣分作用到基础健康度上：``clamp(base - penalty, 1, 100)``。

        为什么下限是 1 而不是 0：健康度 0 会让 ``link_factor`` 退化到带宽下限、
        ``health_score`` 反演完全失去区分度；保底 1 表示"链路极差但仍在线"，
        与 ``LinkMonitor`` 的原生 clamp 区间 [1, 100] 一致。
        """
        return _clamp(float(base_health) - float(sample.link_penalty), 1.0, 100.0)

    # ---------------------------------------------------------------- 运行中切换
    def set_preset(self, preset: str) -> None:
        """切换天气预设并**整条重建**曲线（确定性：等价于新建同参数模型）。

        为什么整条重建而不是"从现在开始换"：``LinkMonitor`` 的健康度序列是
        离线预生成的，5 个策略回放同一条序列是控制变量的前提；若只改 t>=now
        的后半段，序列就依赖"何时切换"这一 wall-clock 事件，无法复现。
        """
        name = config_preset(self.cfg, preset)
        if name == self.preset and self.source != "real":
            return
        self.source = "synthetic"
        self.fetched_at = None
        self.observed_at = None
        self.real_snapshot = None
        self._real_derived = None
        self.preset = name
        self._preset_obj = self._resolve_preset(name)
        self._samples = self._build_curve()
        logger.info("天气切换为 %s（%s），扣分上界=%.1f",
                    name, self._preset_obj.label, self._preset_obj.link_penalty_max)

    # ---------------------------------------------------------------- 快照
    def typical_snapshot(self) -> Dict[str, Any]:
        """"该天气的典型状态"快照：取曲线**平台期中段**（duration × 50%）的值。

        用途：面板在"未运行 / 刚结束"时没有可对齐的仿真时刻，需要给前端一组
        有意义的天气读数（preset=dust 就该显示强度≈0.8、能见度≈2km、扣分≈25），
        而不是 t=0（onset 起点，强度≈0）或 t=duration（尾部已衰减到 0）的边界值。
        为什么取 50%：曲线形态保证 onset 只占前 8%~15%、尾部只占后 12%~22%，
        50% 时长**一定**落在平台期，且与具体随机相位无关。
        """
        return self.snapshot(self.duration_s * 0.5)

    def snapshot(self, t: float) -> Dict[str, Any]:
        """``/api/state`` 的 ``weather`` 字段（键名严格按契约 §4.6.4，不可改名）。"""
        s = self.at(t)
        return {
            "preset": self.preset,
            "label": self._preset_obj.label,
            "intensity": round(float(s.intensity), 4),
            "visibility_km": round(float(s.visibility_km), 3),
            "link_penalty": round(float(s.link_penalty), 3),
            "wind_speed_mps": round(float(s.wind_speed_mps), 2),
            "wind_dir_deg": round(float(s.wind_dir_deg), 1),
            "source": self.source,
            "fetched_at": self.fetched_at,
            "observed_at": self.observed_at,
            "available": available_presets(),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"<WeatherModel seed={self.seed} preset={self.preset} "
                f"source={self.source} "
                f"duration={self.duration_s:.0f}s grid={self.grid_s}s "
                f"mean_penalty={self.mean_penalty():.2f}>")


if __name__ == "__main__":  # pragma: no cover - 手工自检入口
    from uav_sim.config import load_config

    _cfg = load_config()
    print("=== WeatherModel 自检（seed=20261031，90s）===")
    for _name in PRESET_ORDER:
        _m = WeatherModel(seed=20261031, preset=_name, duration_s=90.0, cfg=_cfg)
        _vals = [s.link_penalty for s in _m.samples()]
        _peaks = [s.intensity for s in _m.samples()]
        print(f"{_name:<6} {_m.label:<4} 扣分均值={_m.mean_penalty():6.2f} "
              f"扣分峰值={max(_vals):6.2f} 强度峰值={max(_peaks):.3f} "
              f"能见度均值={sum(s.visibility_km for s in _m.samples()) / len(_m.samples()):5.2f}km "
              f"采样点={len(_vals)}")
    _a = WeatherModel(seed=20261031, preset="rain", duration_s=90.0, cfg=_cfg)
    _b = WeatherModel(seed=20261031, preset="rain", duration_s=90.0, cfg=_cfg)
    print("确定性:", "OK" if [s.to_dict() for s in _a.samples()]
          == [s.to_dict() for s in _b.samples()] else "FAIL")
