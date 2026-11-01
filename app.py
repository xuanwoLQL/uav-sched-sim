#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
app.py —— UavSchedulerSim 桌面版启动器
===========================================================================

把「无人机集群并发自适应调度仿真系统」做成一个**独立桌面软件窗口**：

    * 双击 `启动仿真软件.bat`（或运行 `pythonw app.py`）即可打开；
    * 窗口是原生 Windows 窗口（pywebview + WebView2 内核），**没有地址栏、没有标签页**，
      关掉窗口 = 退出程序；
    * 内部自动起一个只监听 `127.0.0.1` 的本地服务（随机空闲端口，不占用固定端口、不对外暴露），
      仿真数据通过它实时喂给 3D 场景；
    * MySQL 持久化仍然生效：页面里跑完的实验会落 `uav_sched_sim` 库（走 `run.py all` 那条链路）。

为什么用"本地服务 + WebView2"而不是纯 Tkinter/Qt 自绘 3D：
    WebView2 直接复用系统自带 Chromium 内核，能跑 WebGL/Three.js 的真实 3D 场景
    （地形高程、机群姿态、数据链路光束、点云），而 Tkinter/Qt 的 Canvas 画不出这种效果；
    同时不引入 Electron/Node 那一整套，安装体积和启动速度都可接受。

兼容性兜底：
    若本机没有 WebView2 运行时（极少见，Win11 自带），会自动降级为默认浏览器打开，
    并在控制台给出提示，功能不受影响。

命令行：
    python app.py                 # 打开桌面窗口（默认 PEAK × 动态权重）
    pythonw app.py                # 同上，但不开控制台窗口（推荐给最终用户）
    python app.py --port 8791     # 指定端口（调试用）
    python app.py --browser       # 强制用浏览器打开（不建窗口）
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

#: 源码运行时 = 工程目录；打包成 exe 后 = exe 所在目录（用户可见、可写）
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    ROOT = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent
    ROOT = APP_DIR
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

APP_TITLE = "无人机集群并发自适应调度仿真系统 v1.0"

#: 日志文件（窗口版没有控制台，崩溃必须留痕）
LOG_PATH = APP_DIR / "logs" / "app.log"


def _log(msg: str) -> None:
    """追加一行日志：控制台（若有）+ APP_DIR/logs/app.log。"""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass
    print(line)
WINDOW_SIZE = (1480, 920)
MIN_SIZE = (1120, 720)


def _prepare_console() -> None:
    """修正控制台中文编码（pythonw 下没有控制台，直接跳过）。"""
    if os.name != "nt":
        return
    try:
        if sys.stdout is not None and not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _start_server(cfg: dict, port: int):
    """在后台线程启动本地 HTTP 服务，返回 (port, httpd)。"""
    from uav_sim.dashboard import make_server

    httpd, _dash, real_port = make_server(cfg, host="127.0.0.1", port=port)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                         name="DashHTTP", daemon=True)
    t.start()
    return real_port, httpd


def _write_port_file(port: int) -> None:
    """把实际端口写到 logs/ui.port，方便外部脚本/调试探测。"""
    try:
        (APP_DIR / "logs").mkdir(parents=True, exist_ok=True)
        (APP_DIR / "logs" / "ui.port").write_text(str(port), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def run_desktop(url: str) -> int:
    """用 pywebview 打开原生桌面窗口。"""
    try:
        import webview  # type: ignore
    except ImportError:
        print("[app] 未安装 pywebview，改用默认浏览器打开。")
        print("      安装桌面窗口壳：.venv\\Scripts\\python -m pip install pywebview "
              "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        webbrowser.open(url)
        return _hold()

    window = webview.create_window(
        APP_TITLE,
        url=url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
        background_color="#050a14",
        text_select=False,
        confirm_close=True,
    )

    print()
    print("=" * 74)
    print(f"  {APP_TITLE}")
    print("=" * 74)
    print(f"  窗口已打开；内部服务 {url}（仅本机可见）")
    print("  关闭窗口即退出程序。")
    print("=" * 74)
    print()

    try:
        webview.start()          # 阻塞直到窗口关闭
    except Exception:  # noqa: BLE001 - WebView2 缺失等，降级到浏览器
        print("[app] 桌面窗口启动失败，降级为浏览器打开：")
        traceback.print_exc()
        webbrowser.open(url)
        return _hold()
    return 0


def _hold() -> int:
    """浏览器降级模式下保持服务存活，直到用户 Ctrl+C。"""
    print("  按 Ctrl+C 退出。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    _prepare_console()
    ap = argparse.ArgumentParser(description="UavSchedulerSim 桌面版")
    ap.add_argument("--port", type=int, default=0, help="本地服务端口（0=自动分配空闲端口）")
    ap.add_argument("--browser", action="store_true", help="强制用浏览器打开（不建桌面窗口）")
    args = ap.parse_args(argv)

    from uav_sim.config import load_config

    _log(f"启动 / frozen={getattr(sys, 'frozen', False)} / APP_DIR={APP_DIR}")
    try:
        cfg = load_config()
        _log("配置加载成功")
    except Exception:  # noqa: BLE001
        _log("配置加载失败：\n" + traceback.format_exc())
        raise
    try:
        port, httpd = _start_server(cfg, args.port)
        _log(f"本地服务已启动，端口 {port}")
    except Exception:  # noqa: BLE001
        _log("本地服务启动失败：\n" + traceback.format_exc())
        raise
    _write_port_file(port)
    url = f"http://127.0.0.1:{port}/"

    try:
        if args.browser:
            webbrowser.open(url)
            return _hold()
        return run_desktop(url)
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - 窗口版必须把崩溃写进日志，否则用户只看到"闪退"
        _log("未捕获异常：\n" + traceback.format_exc())
        sys.exit(1)
