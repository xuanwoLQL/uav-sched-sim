"""
realsource.py —— 真实环境数据源（采集 → 录制 → 回放）
=============================================================================

本轮先只接**真实气象**这一条线：

    Open-Meteo current/hourly
        -> 规范化 raw 字段
        -> 映射为 intensity / visibility_km / link_penalty
        -> 落 JSON 快照
        -> WeatherModel 只读快照回放

设计纪律（与 docs/11 §7.1 一致）
--------------------------------------------------------------------------
* 采集与回放分离：仿真运行期不访问网络；网络只出现在采集脚本或显式
  ``source=open_meteo`` 的启动阶段。
* 快照是唯一事实来源：同一份快照回放两次，派生天气值和链路输入逐位一致。
* 默认关闭：``real.enable=false`` 时所有既有实验仍走内置合成天气。
* 失败降级：缺文件、坏 JSON、字段非法、网络超时、mapping 版本不符，都返回
  ``None`` 让调用方回退合成模型，而不是让仿真崩溃。

注意时间尺度：仿真时长只有 30/60/90 仿真秒，而 Open-Meteo current 是 15 分钟
级、hourly 是 1 小时级。本轮把一次快照当作整轮常量外部条件，不把小时数据
伪造成秒级动态。
"""

from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from uav_sim.config import get, project_root

#: 快照结构版本。结构变更时递增，旧文件会被明确拒绝并触发降级。
WEATHER_SNAPSHOT_SCHEMA: int = 1

#: 映射规则版本。映射表变化时递增，避免旧快照被静默解释成新口径。
WEATHER_MAPPING_VERSION: str = "wmo-openmeteo-v1"

_OPEN_METEO_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
_CURRENT_FIELDS = (
    "temperature_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "precipitation",
    "visibility",
    "weather_code",
)
_HOURLY_FIELDS = (
    "temperature_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "precipitation",
    "visibility",
    "weather_code",
)


class RealDataError(RuntimeError):
    """真实数据不可用（采集失败、快照非法、版本不符等）。"""


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def _num(raw: Dict[str, Any], key: str, default: Optional[float] = None) -> float:
    value = raw.get(key, default)
    if value is None:
        raise RealDataError(f"缺少数值字段 {key}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RealDataError(f"字段 {key} 不是数值：{value!r}") from exc
    if not math.isfinite(number):
        raise RealDataError(f"字段 {key} 不是有限数值：{value!r}")
    return number


def _now_iso() -> str:
    """当前本地时间（带 UTC offset），用于快照的 fetched_at。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _observed_at_with_offset(value: str, tz_name: str) -> str:
    """给 Open-Meteo 的无 offset 本地时间补一个可读时区后缀。

    Open-Meteo 的 current.time 形如 ``2026-11-01T22:30``。我们只对常见的
    Asia/Shanghai 补 ``+08:00``；其它时区保留原字符串，时区名仍放在 location。
    """
    text = str(value or "").strip()
    if not text:
        return text
    if text.endswith("Z") or "+" in text[10:] or text.count("-") > 2:
        return text
    if str(tz_name) == "Asia/Shanghai":
        return text + "+08:00"
    return text


def _raw_from_current(current: Dict[str, Any]) -> Dict[str, Any]:
    """把 Open-Meteo current 对象规范化为快照 raw。"""
    return {
        "temperature_2m_c": _num(current, "temperature_2m"),
        "wind_speed_10m_kmh": _num(current, "wind_speed_10m"),
        "wind_direction_10m_deg": _num(current, "wind_direction_10m"),
        "precipitation_mm": _num(current, "precipitation"),
        "visibility_m": _num(current, "visibility"),
        "weather_code": int(round(_num(current, "weather_code"))),
    }


def _wmo_label(code: int) -> str:
    table = {
        0: "晴",
        1: "大部晴朗",
        2: "多云",
        3: "阴",
        30: "浮尘",
        31: "扬沙",
        32: "沙尘",
        33: "强沙尘",
        34: "严重沙尘",
        35: "严重沙尘",
        45: "雾",
        48: "雾凇",
        51: "小毛毛雨",
        53: "毛毛雨",
        55: "大毛毛雨",
        56: "冻毛毛雨",
        57: "强冻毛毛雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        66: "冻雨",
        67: "强冻雨",
        71: "小雪",
        73: "中雪",
        75: "大雪",
        77: "雪粒",
        80: "小阵雨",
        81: "中阵雨",
        82: "强阵雨",
        85: "小阵雪",
        86: "大阵雪",
        95: "雷暴",
        96: "雷暴伴小冰雹",
        99: "雷暴伴大冰雹",
    }
    return table.get(int(code), "未知天气")


def _wmo_preset(code: int) -> str:
    code = int(code)
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if 30 <= code <= 35:
        return "dust"
    if code in (45, 48) or 51 <= code <= 67 or 80 <= code <= 82 or 95 <= code <= 99:
        return "rain"
    return "clear"


def _wmo_code_intensity(code: int) -> float:
    code = int(code)
    if code in (0,):
        return 0.0
    if code in (1, 2, 3):
        return 0.15
    if code in (45, 48):
        return 0.35
    if 51 <= code <= 57:
        return 0.35
    if 61 <= code <= 67:
        return 0.60
    if 71 <= code <= 77:
        return 0.70
    if 80 <= code <= 82:
        return 0.60
    if code in (85, 86):
        return 0.75
    if 30 <= code <= 35:
        return 0.85
    if 95 <= code <= 99:
        return 0.90
    return 0.0


def _wmo_code_penalty(code: int) -> float:
    code = int(code)
    if code in (0,):
        return 0.0
    if code in (1, 2, 3):
        return 1.0
    if code in (45, 48):
        return 8.0
    if 51 <= code <= 57:
        return 6.0
    if 61 <= code <= 67:
        return 12.0
    if 71 <= code <= 77:
        return 15.0
    if 80 <= code <= 82:
        return 14.0
    if code in (85, 86):
        return 18.0
    if 30 <= code <= 35:
        return 22.0
    if 95 <= code <= 99:
        return 22.0
    return 0.0


def map_open_meteo_raw(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把规范化 raw 映射为仿真可直接消费的天气派生值。

    单调性（测试守护）：
      * 固定其它输入，降水↑ -> ``penalty_precip`` 与 ``intensity`` 不降；
      * 固定其它输入，能见度↓ -> ``penalty_visibility`` 不降；
      * 固定其它输入，风速↑ -> ``penalty_wind`` 不降；
      * 最终 ``link_penalty = max(...)``，因此以上任一恶化都不会抬高健康度。
    """
    temp_c = _num(raw, "temperature_2m_c")
    wind_kmh = max(0.0, _num(raw, "wind_speed_10m_kmh"))
    wind_dir = _num(raw, "wind_direction_10m_deg") % 360.0
    precip_mm = max(0.0, _num(raw, "precipitation_mm"))
    visibility_m = max(0.0, _num(raw, "visibility_m"))
    code = int(round(_num(raw, "weather_code")))

    vis_km = _clamp(visibility_m / 1000.0, 0.05, 12.0)
    precip_intensity = _clamp(precip_mm / 10.0, 0.0, 1.0)
    visibility_intensity = _clamp((12.0 - vis_km) / 11.95, 0.0, 1.0)
    wind_intensity = _clamp((wind_kmh - 20.0) / 80.0, 0.0, 1.0)
    code_intensity = _wmo_code_intensity(code)

    precip_penalty = min(30.0, 6.0 * math.sqrt(precip_mm))
    visibility_penalty = min(30.0, (12.0 - vis_km) / 12.0 * 30.0)
    wind_penalty = _clamp((wind_kmh - 20.0) / 8.0, 0.0, 10.0)
    code_penalty = _wmo_code_penalty(code)

    return {
        "temperature_2m_c": round(temp_c, 3),
        "preset": _wmo_preset(code),
        "label": _wmo_label(code),
        "intensity": round(max(precip_intensity, visibility_intensity,
                               wind_intensity, code_intensity), 6),
        "visibility_km": round(vis_km, 6),
        "link_penalty": round(_clamp(max(precip_penalty, visibility_penalty,
                                         wind_penalty, code_penalty), 0.0, 32.0), 6),
        "wind_speed_mps": round(wind_kmh / 3.6, 6),
        "wind_dir_deg": round(wind_dir, 3),
        "weather_code": code,
    }


def build_weather_snapshot(raw: Dict[str, Any], *,
                           source: str = "open-meteo",
                           fetched_at: Optional[str] = None,
                           observed_at: Optional[str] = None,
                           location: Optional[Dict[str, Any]] = None,
                           request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造并校验一份天气快照。"""
    loc = dict(location or {})
    lat = _num(loc, "latitude", 41.8455)
    lon = _num(loc, "longitude", 123.9092)
    tz_name = str(loc.get("timezone") or "Asia/Shanghai")
    normalized_location = {
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "timezone": tz_name,
        "name": str(loc.get("name") or "作业区"),
    }
    snapshot = {
        "schema_version": WEATHER_SNAPSHOT_SCHEMA,
        "kind": "weather",
        "mapping_version": WEATHER_MAPPING_VERSION,
        "source": str(source or "open-meteo"),
        "fetched_at": fetched_at or _now_iso(),
        "observed_at": observed_at or "",
        "location": normalized_location,
        "request": dict(request or {}),
        "raw": dict(raw),
        "derived": map_open_meteo_raw(raw),
    }
    return validate_weather_snapshot(snapshot)


def validate_weather_snapshot(data: Dict[str, Any]) -> Dict[str, Any]:
    """校验快照结构并**重新计算** derived，防止信任手改的派生值。"""
    if not isinstance(data, dict):
        raise RealDataError("快照顶层不是对象")
    try:
        schema_version = int(data.get("schema_version", -1))
    except (TypeError, ValueError) as exc:
        raise RealDataError(f"schema_version 非法：{data.get('schema_version')!r}") from exc
    if schema_version != WEATHER_SNAPSHOT_SCHEMA:
        raise RealDataError(
            f"快照 schema_version 不符：{data.get('schema_version')!r} "
            f"(需要 {WEATHER_SNAPSHOT_SCHEMA})")
    if str(data.get("kind") or "") != "weather":
        raise RealDataError(f"快照 kind 不符：{data.get('kind')!r}")
    if str(data.get("mapping_version") or "") != WEATHER_MAPPING_VERSION:
        raise RealDataError(
            f"mapping_version 不符：{data.get('mapping_version')!r} "
            f"(需要 {WEATHER_MAPPING_VERSION})")
    if not isinstance(data.get("raw"), dict):
        raise RealDataError("快照缺少 raw 对象")
    if not isinstance(data.get("location"), dict):
        raise RealDataError("快照缺少 location 对象")
    loc = data["location"]
    lat = _num(loc, "latitude")
    lon = _num(loc, "longitude")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise RealDataError("location 经纬度越界")
    fetched_at = data.get("fetched_at")
    observed_at = data.get("observed_at")
    if fetched_at is not None and not isinstance(fetched_at, str):
        raise RealDataError("fetched_at 必须是字符串或 null")
    if observed_at is not None and not isinstance(observed_at, str):
        raise RealDataError("observed_at 必须是字符串或 null")

    normalized = dict(data)
    normalized["schema_version"] = WEATHER_SNAPSHOT_SCHEMA
    normalized["kind"] = "weather"
    normalized["mapping_version"] = WEATHER_MAPPING_VERSION
    normalized["source"] = str(data.get("source") or "open-meteo")
    normalized["fetched_at"] = fetched_at
    normalized["observed_at"] = observed_at or ""
    normalized["location"] = {
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "timezone": str(loc.get("timezone") or "Asia/Shanghai"),
        "name": str(loc.get("name") or "作业区"),
    }
    normalized["raw"] = {
        "temperature_2m_c": _num(data["raw"], "temperature_2m_c"),
        "wind_speed_10m_kmh": _num(data["raw"], "wind_speed_10m_kmh"),
        "wind_direction_10m_deg": _num(data["raw"], "wind_direction_10m_deg"),
        "precipitation_mm": _num(data["raw"], "precipitation_mm"),
        "visibility_m": _num(data["raw"], "visibility_m"),
        "weather_code": int(round(_num(data["raw"], "weather_code"))),
    }
    normalized["derived"] = map_open_meteo_raw(normalized["raw"])
    normalized["request"] = dict(data.get("request") or {})
    return normalized


class RealDataProvider(ABC):
    """真实数据源抽象：采集、快照、从文件回放。"""

    kind: str = "unknown"

    @abstractmethod
    def fetch(self) -> Dict[str, Any]:
        """执行一次采集，返回可序列化快照。"""

    def snapshot(self) -> Dict[str, Any]:
        """返回当前快照；默认重新采集一次。"""
        return self.fetch()

    @classmethod
    def from_snapshot(cls, path: str | Path) -> "SnapshotWeatherProvider":
        return SnapshotWeatherProvider(path)


class OpenMeteoWeatherProvider(RealDataProvider):
    """Open-Meteo 免 key 天气源。"""

    kind = "open-meteo"

    def __init__(self, latitude: float = 41.8455, longitude: float = 123.9092,
                 timezone_name: str = "Asia/Shanghai", timeout_s: float = 10.0,
                 at: Optional[str] = None, name: str = "作业区") -> None:
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.timezone_name = str(timezone_name or "Asia/Shanghai")
        self.timeout_s = max(1.0, float(timeout_s))
        self.at = str(at).strip() if at else None
        self.name = str(name or "作业区")
        self._last: Optional[Dict[str, Any]] = None

    def _url(self) -> Tuple[str, Dict[str, Any]]:
        params: Dict[str, Any] = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone_name,
        }
        if self.at:
            params["hourly"] = ",".join(_HOURLY_FIELDS)
            params["past_days"] = 7
            params["forecast_days"] = 2
        else:
            params["current"] = ",".join(_CURRENT_FIELDS)
            params["forecast_days"] = 1
        return _OPEN_METEO_ENDPOINT + "?" + urllib.parse.urlencode(params), params

    @staticmethod
    def _nearest_index(times: list, target: str) -> int:
        if not times:
            raise RealDataError("hourly.time 为空")
        if target in times:
            return times.index(target)
        try:
            target_dt = datetime.fromisoformat(target)
            best_i = 0
            best_d = float("inf")
            for i, text in enumerate(times):
                delta = abs((datetime.fromisoformat(str(text)) - target_dt).total_seconds())
                if delta < best_d:
                    best_d = delta
                    best_i = i
            return best_i
        except (TypeError, ValueError) as exc:
            raise RealDataError(f"无法匹配 --at={target!r}") from exc

    def _raw_from_hourly(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict):
            raise RealDataError("Open-Meteo 响应缺少 hourly")
        times = hourly.get("time") or []
        idx = self._nearest_index(list(times), self.at or "")
        raw = {}
        for field in _HOURLY_FIELDS:
            values = hourly.get(field)
            if not isinstance(values, list) or idx >= len(values):
                raise RealDataError(f"Open-Meteo hourly 缺少字段 {field}")
            raw[field] = values[idx]
        canonical = _raw_from_current(raw)
        return canonical, str(times[idx])

    def fetch(self) -> Dict[str, Any]:
        url, params = self._url()
        req = urllib.request.Request(url, headers={"User-Agent": "uav-sched-sim/real-weather"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 统一转成可降级异常
            raise RealDataError(f"Open-Meteo 请求失败：{type(exc).__name__}: {exc}") from exc

        if self.at:
            raw, observed_at = self._raw_from_hourly(payload)
        else:
            current = payload.get("current")
            if not isinstance(current, dict):
                raise RealDataError("Open-Meteo 响应缺少 current")
            raw = _raw_from_current(current)
            observed_at = str(current.get("time") or "")
        snapshot = build_weather_snapshot(
            raw,
            source=self.kind,
            fetched_at=_now_iso(),
            observed_at=_observed_at_with_offset(observed_at, self.timezone_name),
            location={
                "latitude": self.latitude,
                "longitude": self.longitude,
                "timezone": self.timezone_name,
                "name": self.name,
            },
            request={
                "endpoint": _OPEN_METEO_ENDPOINT,
                "params": params,
                "provider_latitude": payload.get("latitude"),
                "provider_longitude": payload.get("longitude"),
                "provider_elevation": payload.get("elevation"),
            },
        )
        self._last = snapshot
        return snapshot

    def snapshot(self) -> Dict[str, Any]:
        return self._last if self._last is not None else self.fetch()


class SnapshotWeatherProvider(RealDataProvider):
    """从已录制 JSON 回放天气快照。"""

    kind = "snapshot"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._snapshot: Optional[Dict[str, Any]] = None

    def fetch(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RealDataError(f"快照不存在：{self.path}") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RealDataError(f"快照读取/解析失败：{type(exc).__name__}: {exc}") from exc
        snapshot = validate_weather_snapshot(data)
        self._snapshot = snapshot
        return snapshot

    def snapshot(self) -> Dict[str, Any]:
        return self._snapshot if self._snapshot is not None else self.fetch()


def resolve_snapshot_path(cfg: dict, path_override: Optional[str | Path] = None) -> Path:
    """解析快照路径；相对路径按项目根解析。"""
    raw = path_override if path_override is not None else get(
        cfg, "real.snapshot", "data/real/weather_snapshot.json")
    path = Path(str(raw))
    if not path.is_absolute():
        path = project_root() / path
    return path


def load_weather_snapshot(cfg: dict,
                          path_override: Optional[str | Path] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """按配置加载真实天气快照。

    Returns
    -------
    (snapshot, reason)
        snapshot=None 表示不可用，调用方应回退合成模型；reason 是给日志/UI 的
        中文说明（不会包含 API key 或服务器路径以外的敏感信息）。
    """
    real = get(cfg, "real", {}) or {}
    if not isinstance(real, dict):
        real = {}
    enabled = bool(real.get("enable", False))
    if path_override is None and not enabled:
        return None, "real.enable=false"

    source = str(real.get("source", "snapshot") or "snapshot").strip().lower()
    fallback = str(real.get("fallback", "synthetic") or "synthetic").strip().lower()
    if fallback != "synthetic":
        return None, f"不支持的 real.fallback={fallback!r}，按 synthetic 处理"

    path = resolve_snapshot_path(cfg, path_override)
    try:
        if path_override is not None or source == "snapshot":
            return SnapshotWeatherProvider(path).fetch(), ""
        if source == "open_meteo":
            ttl = max(0.0, float(get(cfg, "real.cache_ttl_s", 900) or 0.0))
            if path.is_file() and ttl > 0:
                age = max(0.0, datetime.now().timestamp() - path.stat().st_mtime)
                if age <= ttl:
                    return SnapshotWeatherProvider(path).fetch(), f"复用 {age:.0f}s 内快照"
            weather_cfg = get(cfg, "real.weather", {}) or {}
            if not isinstance(weather_cfg, dict):
                weather_cfg = {}
            provider = OpenMeteoWeatherProvider(
                latitude=float(weather_cfg.get("latitude", 41.8455)),
                longitude=float(weather_cfg.get("longitude", 123.9092)),
                timezone_name=str(weather_cfg.get("timezone", "Asia/Shanghai")),
                timeout_s=float(weather_cfg.get("timeout_s", 10.0)),
                name=str(weather_cfg.get("name", "作业区")),
            )
            return provider.fetch(), ""
        return None, f"未知 real.source={source!r}"
    except Exception as exc:  # noqa: BLE001 - 降级边界，任何异常都不能拖垮仿真
        return None, f"{type(exc).__name__}: {exc}"


def snapshot_sha256(snapshot: Dict[str, Any]) -> str:
    """快照派生层的稳定 SHA-256（用于回放一致性证据）。"""
    import hashlib

    payload = {
        "mapping_version": snapshot.get("mapping_version"),
        "raw": snapshot.get("raw"),
        "derived": snapshot.get("derived"),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_file_default(cfg: dict) -> Path:
    """给采集脚本/文档使用：返回默认快照文件路径（不创建目录）。"""
    return resolve_snapshot_path(cfg)


def write_snapshot_atomic(snapshot: Dict[str, Any], path: str | Path) -> Path:
    """原子写快照：先写同目录临时文件，再 os.replace。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp-{os.getpid()}")
    text = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target
