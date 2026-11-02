#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""fetch_real_env.py —— 采集真实天气并写成可回放快照。

默认使用 Open-Meteo（免费、无需 API key）：

    & ".\.venv\Scripts\python.exe" tools\fetch_real_env.py
    & ".\.venv\Scripts\python.exe" tools\fetch_real_env.py --at 2026-11-01T07:00 --force
    & ".\.venv\Scripts\python.exe" tools\fetch_real_env.py --out data\real\rain.json

采集完成后，仿真只读 JSON 快照；默认配置 ``real.enable=false`` 时完全不会
访问网络。采集失败不会写半截文件，返回码 2。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from uav_sim.config import get, load_config  # noqa: E402
from uav_sim.realsource import (  # noqa: E402
    OpenMeteoWeatherProvider,
    RealDataError,
    SnapshotWeatherProvider,
    resolve_snapshot_path,
    snapshot_sha256,
    write_snapshot_atomic,
)


def _float_arg(value: float | None, fallback: float) -> float:
    return float(fallback if value is None else value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="采集真实天气 -> JSON 快照（Open-Meteo 免 key）")
    ap.add_argument("--config", default=None, help="配置文件路径（默认项目根 config.yaml）")
    ap.add_argument("--lat", type=float, default=None, help="纬度（默认取 real.weather.latitude）")
    ap.add_argument("--lon", type=float, default=None, help="经度（默认取 real.weather.longitude）")
    ap.add_argument("--timezone", default=None, help="IANA 时区（默认 Asia/Shanghai）")
    ap.add_argument("--name", default=None, help="地点名称（只用于展示）")
    ap.add_argument("--at", default=None, help="可选：从 hourly 里选一个时间，如 2026-11-01T07:00")
    ap.add_argument("--timeout", type=float, default=None, help="HTTP 超时秒数（默认取配置）")
    ap.add_argument("--out", default=None, help="快照输出路径（默认取 real.snapshot）")
    ap.add_argument("--cache-ttl", type=float, default=None,
                    help="未加 --force 时复用该秒数内的旧快照；0=不复用")
    ap.add_argument("--force", action="store_true", help="强制重新采集并覆盖")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        weather_cfg = get(cfg, "real.weather", {}) or {}
        if not isinstance(weather_cfg, dict):
            weather_cfg = {}
        lat = _float_arg(args.lat, float(weather_cfg.get("latitude", 41.8455)))
        lon = _float_arg(args.lon, float(weather_cfg.get("longitude", 123.9092)))
        tz_name = str(args.timezone or weather_cfg.get("timezone") or "Asia/Shanghai")
        name = str(args.name or weather_cfg.get("name") or "作业区")
        timeout_s = _float_arg(args.timeout, float(weather_cfg.get("timeout_s", 10.0)))
        out = resolve_snapshot_path(cfg, args.out)
        ttl = _float_arg(args.cache_ttl, float(get(cfg, "real.cache_ttl_s", 900) or 0.0))

        if out.is_file() and not args.force and ttl > 0:
            age = max(0.0, time.time() - out.stat().st_mtime)
            if age <= ttl:
                old = SnapshotWeatherProvider(out).fetch()
                print(f"复用快照 {out}（{age:.0f}s 前写入）")
                print(f"observed_at={old.get('observed_at')} sha256={snapshot_sha256(old)}")
                return 0

        provider = OpenMeteoWeatherProvider(
            latitude=lat, longitude=lon, timezone_name=tz_name,
            timeout_s=timeout_s, at=args.at, name=name,
        )
        snap = provider.fetch()
        write_snapshot_atomic(snap, out)
        print(f"已写入快照 {out}")
        print(f"source={snap.get('source')} observed_at={snap.get('observed_at')}")
        print("derived=" + json.dumps(snap.get("derived"), ensure_ascii=False, sort_keys=True))
        print(f"sha256={snapshot_sha256(snap)}")
        return 0
    except (RealDataError, OSError, ValueError, KeyError) as exc:
        print(f"采集失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
