#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""serve.py —— 只起 HTTP 服务、不开窗口（自动化/无头截图专用）

用途：给 `tools/shot.mjs` 这类无头浏览器脚本提供 /api/state 与 /static 资源。
与 `app.py` 共用同一套 dashboard.make_server，因此页面行为与桌面版完全一致，
只是不创建 pywebview 窗口、不自动打开浏览器。

用法：
    python tools/serve.py                 # 随机空闲端口
    python tools/serve.py --port 8791     # 固定端口
    python tools/serve.py --port 8791 --auto-start PEAK --scheduler dynamic
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--auto-start", default="", help="启动后自动开跑的工况代号，如 PEAK")
    ap.add_argument("--scheduler", default="dynamic")
    ap.add_argument("--speed", type=float, default=2.0)
    ap.add_argument("--config", default=None, help="配置文件路径（默认项目根 config.yaml）")
    ap.add_argument("--real-weather", default=None, metavar="SNAPSHOT",
                    help="指定真实天气快照 JSON（覆盖 config.real）")
    args = ap.parse_args(argv)

    from uav_sim.config import load_config
    from uav_sim.dashboard import make_server

    cfg = load_config(args.config)
    httpd, dash, port = make_server(
        cfg, host=args.host, port=args.port, real_weather=args.real_weather)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                         name="DashHTTP", daemon=True)
    t.start()
    print(f"SERVER_READY http://{args.host}:{port}/", flush=True)

    if args.auto_start:
        try:
            dash.start_run(args.auto_start, args.scheduler, float(args.speed))  # type: ignore[attr-defined]
            print(f"AUTO_START {args.auto_start} {args.scheduler} speed={args.speed}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"AUTO_START_FAILED {exc}", flush=True)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
