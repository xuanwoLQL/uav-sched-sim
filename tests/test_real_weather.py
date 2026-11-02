"""tests/test_real_weather.py —— 真实天气采集/快照/降级/回放验收。

这些用例不访问真实网络：Open-Meteo 响应用固定 payload 注入，保证测试在
断网/无 key 的机器上仍可重复。真实网络只由 tools/fetch_real_env.py 手工采集。
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import urllib.request
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from uav_sim import dashboard as dash_mod  # noqa: E402
from uav_sim.config import load_config  # noqa: E402
from uav_sim.link_monitor import LinkMonitor  # noqa: E402
from uav_sim.realsource import (  # noqa: E402
    OpenMeteoWeatherProvider,
    SnapshotWeatherProvider,
    build_weather_snapshot,
    load_weather_snapshot,
    map_open_meteo_raw,
    snapshot_sha256,
    write_snapshot_atomic,
)
from uav_sim.weather import WeatherModel, WeatherSample  # noqa: E402

CFG = load_config()

_RAW = {
    "temperature_2m_c": 1.2,
    "wind_speed_10m_kmh": 5.2,
    "wind_direction_10m_deg": 333.0,
    "precipitation_mm": 0.0,
    "visibility_m": 23300.0,
    "weather_code": 0,
}

_RAIN_RAW = {
    "temperature_2m_c": 5.0,
    "wind_speed_10m_kmh": 4.8,
    "wind_direction_10m_deg": 155.0,
    "precipitation_mm": 1.0,
    "visibility_m": 5060.0,
    "weather_code": 55,
}

_OPEN_METEO_PAYLOAD = {
    "latitude": 41.792618,
    "longitude": 123.91304,
    "elevation": -214.0,
    "current": {
        "time": "2026-11-01T07:00",
        "interval": 900,
        "temperature_2m": 5.0,
        "wind_speed_10m": 4.8,
        "wind_direction_10m": 155.0,
        "precipitation": 1.0,
        "visibility": 5060.0,
        "weather_code": 55,
    },
}


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _snap(raw: dict | None = None) -> dict:
    return build_weather_snapshot(
        raw or _RAIN_RAW,
        source="open-meteo",
        fetched_at="2026-11-01T22:31:00+08:00",
        observed_at="2026-11-01T07:00:00+08:00",
        location={"latitude": 41.8455, "longitude": 123.9092,
                  "timezone": "Asia/Shanghai", "name": "作业区"},
    )


def _cfg_with_real(**overrides) -> dict:
    cfg = json.loads(json.dumps(CFG))
    real = cfg.setdefault("real", {})
    real.update({"enable": True, "source": "snapshot", "fallback": "synthetic"})
    real.update(overrides)
    return cfg


def _series_sha(mon: LinkMonitor, uavs=(1, 2, 3, 4, 5), n: int = 181) -> str:
    h = hashlib.sha256()
    for uid in uavs:
        for i in range(n):
            h.update(("%d:%.9f;" % (uid, mon.health_at(uid, i * 0.5))).encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 1) 映射单调性
# ---------------------------------------------------------------------------
def test_precipitation_monotonic() -> None:
    penalties = []
    intensities = []
    for precip in (0.0, 0.1, 1.0, 5.0, 20.0):
        raw = dict(_RAW, precipitation_mm=precip)
        d = map_open_meteo_raw(raw)
        penalties.append(float(d["link_penalty"]))
        intensities.append(float(d["intensity"]))
    assert penalties == sorted(penalties), penalties
    assert intensities == sorted(intensities), intensities

    healths = []
    for precip in (0.0, 0.1, 1.0, 5.0, 20.0):
        d = map_open_meteo_raw(dict(_RAW, precipitation_mm=precip))
        sample = WeatherSample(
            ts=0.0, preset=d["preset"], intensity=d["intensity"],
            visibility_km=d["visibility_km"], link_penalty=d["link_penalty"],
            wind_speed_mps=d["wind_speed_mps"], wind_dir_deg=d["wind_dir_deg"])
        healths.append(WeatherModel.apply_health(80.0, sample))
    assert healths == sorted(healths, reverse=True), healths


def test_visibility_and_wind_monotonic() -> None:
    vis_penalties = []
    for visibility in (20000.0, 10000.0, 5000.0, 1000.0, 100.0):
        d = map_open_meteo_raw(dict(_RAW, visibility_m=visibility))
        vis_penalties.append(float(d["link_penalty"]))
    assert vis_penalties == sorted(vis_penalties), vis_penalties

    wind_penalties = []
    for wind in (0.0, 20.0, 50.0, 100.0):
        d = map_open_meteo_raw(dict(_RAW, wind_speed_10m_kmh=wind))
        wind_penalties.append(float(d["link_penalty"]))
    assert wind_penalties == sorted(wind_penalties), wind_penalties


# ---------------------------------------------------------------------------
# 2) Open-Meteo 采集 → 快照
# ---------------------------------------------------------------------------
def test_open_meteo_payload_maps_to_snapshot(monkeypatch) -> None:
    import uav_sim.realsource as rs

    monkeypatch.setattr(rs.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResponse(_OPEN_METEO_PAYLOAD))
    snap = OpenMeteoWeatherProvider(
        latitude=41.8455, longitude=123.9092,
        timezone_name="Asia/Shanghai", timeout_s=3.0).fetch()
    assert snap["source"] == "open-meteo"
    assert snap["observed_at"] == "2026-11-01T07:00+08:00"
    assert snap["derived"]["preset"] == "rain"
    assert snap["derived"]["label"] == "大毛毛雨"
    assert snap["derived"]["visibility_km"] == 5.06
    assert snap["derived"]["link_penalty"] > 0.0


# ---------------------------------------------------------------------------
# 3) 快照回放 / SHA-256
# ---------------------------------------------------------------------------
def test_snapshot_roundtrip_sha256(tmp_path) -> None:
    snap = _snap()
    path = write_snapshot_atomic(snap, tmp_path / "weather.json")
    replay = SnapshotWeatherProvider(path).fetch()
    assert replay["raw"] == snap["raw"]
    assert replay["derived"] == snap["derived"]
    assert snapshot_sha256(replay) == snapshot_sha256(snap)


def test_same_snapshot_link_series_sha256() -> None:
    snap = _snap()
    w1 = WeatherModel(seed=20261031, preset="clear", duration_s=90.0,
                      cfg=CFG, real_snapshot=snap)
    w2 = WeatherModel(seed=20261031, preset="clear", duration_s=90.0,
                      cfg=CFG, real_snapshot=snap)
    m1 = LinkMonitor(seed=20261031, weaknet_level="high", duration_s=90.0,
                     uav_ids=[1, 2, 3, 4, 5], cfg=CFG, weather=w1)
    m2 = LinkMonitor(seed=20261031, weaknet_level="high", duration_s=90.0,
                     uav_ids=[1, 2, 3, 4, 5], cfg=CFG, weather=w2)
    assert _series_sha(m1) == _series_sha(m2)
    assert [s.to_dict() for s in w1.samples()] == [s.to_dict() for s in w2.samples()]


def test_live_and_snapshot_derived_match(tmp_path, monkeypatch) -> None:
    import uav_sim.realsource as rs

    monkeypatch.setattr(rs.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResponse(_OPEN_METEO_PAYLOAD))
    live = OpenMeteoWeatherProvider(
        latitude=41.8455, longitude=123.9092,
        timezone_name="Asia/Shanghai", timeout_s=3.0).fetch()
    path = write_snapshot_atomic(live, tmp_path / "live.json")
    replay = SnapshotWeatherProvider(path).fetch()
    assert replay["raw"] == live["raw"]
    assert replay["derived"] == live["derived"]
    assert snapshot_sha256(replay) == snapshot_sha256(live)


# ---------------------------------------------------------------------------
# 4) 降级路径
# ---------------------------------------------------------------------------
def test_missing_snapshot_falls_back(tmp_path) -> None:
    cfg = _cfg_with_real(snapshot=str(tmp_path / "missing.json"))
    snap, reason = load_weather_snapshot(cfg)
    assert snap is None
    assert "不存在" in reason


def test_malformed_snapshot_falls_back(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not-json", encoding="utf-8")
    cfg = _cfg_with_real(snapshot=str(path))
    snap, reason = load_weather_snapshot(cfg)
    assert snap is None
    assert "JSON" in reason or "解析" in reason


def test_network_error_falls_back(monkeypatch) -> None:
    import uav_sim.realsource as rs

    def _boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(rs.urllib.request, "urlopen", _boom)
    cfg = _cfg_with_real(source="open_meteo", snapshot="")
    snap, reason = load_weather_snapshot(cfg)
    assert snap is None
    assert "请求失败" in reason


def test_invalid_mapping_version_falls_back(tmp_path) -> None:
    snap = _snap()
    snap["mapping_version"] = "old-version"
    path = tmp_path / "old.json"
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    cfg = _cfg_with_real(snapshot=str(path))
    got, reason = load_weather_snapshot(cfg)
    assert got is None
    assert "mapping_version" in reason


# ---------------------------------------------------------------------------
# 5) 契约 / 默认路径
# ---------------------------------------------------------------------------
def test_real_weather_snapshot_contract() -> None:
    w = WeatherModel(seed=20261031, preset="clear", duration_s=90.0,
                     cfg=CFG, real_snapshot=_snap())
    weather = w.snapshot(45.0)
    need = {
        "preset", "label", "intensity", "visibility_km", "link_penalty",
        "wind_speed_mps", "wind_dir_deg", "source", "fetched_at",
        "observed_at", "available",
    }
    assert set(weather) == need
    assert weather["source"] == "real"
    assert weather["fetched_at"] == "2026-11-01T22:31:00+08:00"
    assert weather["observed_at"] == "2026-11-01T07:00:00+08:00"
    assert weather["preset"] == "rain"
    assert len(weather["available"]) == 4


def test_real_disabled_keeps_synthetic_clear() -> None:
    cfg = json.loads(json.dumps(CFG))
    cfg.setdefault("real", {})["enable"] = False
    snap, reason = load_weather_snapshot(cfg)
    assert snap is None
    assert "real.enable=false" in reason
    w = WeatherModel(seed=20261031, preset="clear", duration_s=90.0, cfg=cfg)
    assert w.source == "synthetic"
    assert w.snapshot(0.0)["source"] == "synthetic"
    assert w.snapshot(0.0)["fetched_at"] is None


def test_http_state_exposes_real_weather(tmp_path) -> None:
    path = write_snapshot_atomic(_snap(), tmp_path / "weather.json")
    httpd, _dash, port = dash_mod.make_server(
        load_config(), host="127.0.0.1", port=0, real_weather=str(path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(  # noqa: S310 - 仅本机测试
                f"http://127.0.0.1:{port}/api/state", timeout=10) as r:
            state = json.loads(r.read().decode("utf-8"))
        weather = state["weather"]
        assert weather["source"] == "real"
        assert weather["fetched_at"] == "2026-11-01T22:31:00+08:00"
        assert weather["observed_at"] == "2026-11-01T07:00:00+08:00"
    finally:
        httpd.shutdown()
        httpd.server_close()
