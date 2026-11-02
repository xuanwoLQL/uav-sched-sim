#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""verify_real_env.py —— 真实天气快照回放一致性证据。

用法：
    & ".\.venv\Scripts\python.exe" tools\verify_real_env.py ^
        --snapshot data\real\weather_rain.json --reps 2

可选对比实时采集：
    & ".\.venv\Scripts\python.exe" tools\verify_real_env.py ^
        --snapshot data\real\weather_rain.json --fetch-live

输出 `results/real_weather/verify.json`，包含：
  * 快照 raw/derived 的 SHA-256；
  * 同一快照两次回放的天气曲线 / 链路序列 SHA-256；
  * 整跑 submitted/done/throughput（两次差异如实报告，按 docs/11 §7.1
    只声明落在既有 wall-clock 抖动范围内，不宣称逐位一致）。
"""

from __future__ import annotations

import argparse
import hashlib
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
from uav_sim.link_monitor import LinkMonitor  # noqa: E402
from uav_sim.realsource import (  # noqa: E402
    OpenMeteoWeatherProvider,
    SnapshotWeatherProvider,
    snapshot_sha256,
)
from uav_sim.scenarios import generate_trace, get_scenario  # noqa: E402
from uav_sim.scheduler import create_scheduler  # noqa: E402
from uav_sim.simulator import Simulator  # noqa: E402
from uav_sim.weather import WeatherModel  # noqa: E402


def _weather_sha(w: WeatherModel) -> str:
    text = json.dumps([s.to_dict() for s in w.samples()],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _link_sha(mon: LinkMonitor, uavs: list[int], n: int) -> str:
    h = hashlib.sha256()
    for uid in uavs:
        for i in range(n):
            h.update(("%d:%.9f;" % (uid, mon.health_at(uid, i * 0.5))).encode())
    return h.hexdigest()


def _run_once(cfg: dict, snap: dict, scenario: str, scheduler: str,
              seed: int) -> dict:
    sc = get_scenario(scenario)
    trace = generate_trace(sc, seed, cfg)
    w = WeatherModel(seed=seed, preset="clear", duration_s=sc.duration_s,
                     cfg=cfg, real_snapshot=snap)
    uavs = list(range(1, int(sc.uav_count) + 1))
    n = int(round(sc.duration_s / 0.5)) + 1
    mon = LinkMonitor(seed=seed, weaknet_level=sc.weaknet_level,
                      duration_s=sc.duration_s, uav_ids=uavs, cfg=cfg, weather=w)
    t0 = time.perf_counter()
    sim = Simulator(sc, trace, cfg, scheduler_name=scheduler, seed=seed,
                    real_weather_snapshot=snap)
    sched = create_scheduler(scheduler, sim, cfg)
    raw = sim.run(sched, duration_s=sc.duration_s)
    wall = time.perf_counter() - t0
    return {
        "snapshot_sha256": snapshot_sha256(snap),
        "weather_sha256": _weather_sha(w),
        "link_sha256": _link_sha(mon, uavs, n),
        "submitted": int(raw.get("submitted", 0)),
        "done": int(raw.get("done", 0)),
        "throughput": round(int(raw.get("done", 0)) / max(1e-9, float(sc.duration_s)), 4),
        "wall_s": round(wall, 3),
        "weather": w.snapshot(sc.duration_s * 0.5),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="验证真实天气快照回放一致性")
    ap.add_argument("--config", default=None)
    ap.add_argument("--snapshot", default=None, help="回放快照路径；默认取 real.snapshot")
    ap.add_argument("--fetch-live", action="store_true",
                    help="额外用 Open-Meteo 当前实况作为对比（会访问网络）")
    ap.add_argument("--scenario", default="PEAK", choices=["LIGHT", "NORMAL", "PEAK"])
    ap.add_argument("--scheduler", default="dynamic",
                    choices=["fcfs", "rr", "priority", "mlfq", "dynamic", "cross_node"])
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--reps", type=int, default=2, help="同一快照重复跑几次（默认 2）")
    ap.add_argument("--out", default="results/real_weather/verify.json")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    seed = int(args.seed if args.seed is not None else
               get(cfg, "simulation.random_seed", 20261031))
    snap_path = args.snapshot or str(get(cfg, "real.snapshot",
                                         "data/real/weather_snapshot.json"))
    replay = SnapshotWeatherProvider(snap_path).fetch()
    runs = [_run_once(cfg, replay, args.scenario, args.scheduler, seed)
            for _ in range(max(1, int(args.reps)))]
    result = {
        "snapshot": str(snap_path),
        "scenario": args.scenario,
        "scheduler": args.scheduler,
        "seed": seed,
        "replay_runs": runs,
        "input_layer_equal": all(r["weather_sha256"] == runs[0]["weather_sha256"]
                                 and r["link_sha256"] == runs[0]["link_sha256"]
                                 for r in runs),
        "weather_sha256": runs[0]["weather_sha256"],
        "link_sha256": runs[0]["link_sha256"],
    }
    if args.fetch_live:
        weather_cfg = get(cfg, "real.weather", {}) or {}
        if not isinstance(weather_cfg, dict):
            weather_cfg = {}
        live = OpenMeteoWeatherProvider(
            latitude=float(weather_cfg.get("latitude", 41.8455)),
            longitude=float(weather_cfg.get("longitude", 123.9092)),
            timezone_name=str(weather_cfg.get("timezone", "Asia/Shanghai")),
            timeout_s=float(weather_cfg.get("timeout_s", 10.0)),
            name=str(weather_cfg.get("name", "作业区")),
        ).fetch()
        live_run = _run_once(cfg, live, args.scenario, args.scheduler, seed)
        result["live"] = {
            "observed_at": live.get("observed_at"),
            "derived": live.get("derived"),
            "snapshot_sha256": snapshot_sha256(live),
            "weather_sha256": live_run["weather_sha256"],
            "link_sha256": live_run["link_sha256"],
            "metrics": live_run,
        }

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
