"""
dashboard.py —— 实时可视化仿真面板（本地网页版）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 运维人员盯着大屏看"30 架无人机同时在传数据"时，哪条链路在抖、哪个队列在堵、哪台机器被卡住 |
| OS 原理层   | 相当于把 `/proc` + `top` + `vmstat` 可视化：就绪队列长度曲线、运行队列（工作线程槽位）、I/O 阻塞（弱网重传）、CPU 利用率 |
| 算法设计层 | 面板只做"观测"，不做决策 —— 所有数据来自 `Simulator.snapshot()`，调度策略仍然由 scheduler 决定 |
| 代码实现层 | `Dashboard`（python 标准库 `http.server`）+ 内嵌单页 HTML/Canvas |

--------------------------------------------------------------------------
为什么用标准库而不是 Flask/Streamlit
--------------------------------------------------------------------------
1. **零新增依赖**：本项目 venv 只装了 PyYAML / matplotlib / pandas / PyMySQL，
   现场演示时最怕 `pip install` 失败（校园网/镜像站不稳定）。`http.server` 是标准库，永远可用。
2. 需求只是"一个本地页面 + 每 150ms 拉一次 JSON"，用不上框架。
3. 单文件，拷走就能跑。

--------------------------------------------------------------------------
启动方式
--------------------------------------------------------------------------
    .venv\\Scripts\\python.exe run.py ui
    .venv\\Scripts\\python.exe run.py ui --port 8790 --scenario PEAK --scheduler dynamic

启动后浏览器自动打开 http://127.0.0.1:8765 ，
页面上可以实时切换工况（LIGHT/NORMAL/PEAK）、策略（5 种）、速度（1×~4×），
点「开始仿真」即可看到无人机链路色块、两级就绪队列、8 个工作线程槽位、
系统负载与队列深度曲线随时间变化。

**重要**：面板为了"看得清"会把 `runtime.time_scale` 调大（仿真变慢），
这只影响**观看速度**，不影响任何指标口径 —— 正式实验仍用 `run.py all`（默认 time_scale=0.1）。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.config import cfg_get_int, load_config
from uav_sim.models import SCENARIO_CODES, SCENARIO_LABELS, SCHEDULER_LABELS, SCHEDULER_NAMES, TaskPriority
from uav_sim.scenarios import generate_trace, get_scenario
from uav_sim.scheduler import create_scheduler
from uav_sim.simulator import Simulator
from uav_sim.weather import WeatherModel, available_presets, config_preset, is_valid_preset

logger = logging.getLogger(__name__)

#: 面板默认端口
DEFAULT_PORT: int = 8765

#: 静态资源目录（three.min.js 与 3D 页面都在这里，**本地内置、不依赖 CDN**）
#: PyInstaller 打包后资源被解到 sys._MEIPASS/uav_sim/static，需要单独判断。
if getattr(sys, "frozen", False):
    STATIC_DIR: Path = Path(getattr(sys, "_MEIPASS", ".")) / "uav_sim" / "static"
else:
    STATIC_DIR = Path(__file__).resolve().parent / "static"

#: 扩展名 -> Content-Type
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


def _content_type(name: str) -> str:
    return _MIME.get(Path(name).suffix.lower(), "application/octet-stream")


def _read_page_raw(name: str) -> Optional[bytes]:
    """读取 static 目录下的文件；不存在返回 None。"""
    p = STATIC_DIR / name
    if not p.is_file():
        return None
    return p.read_bytes()


def _read_page(name: str) -> bytes:
    """读取 static 目录下的页面；缺失时返回可读提示页，而不是 500。"""
    body = _read_page_raw(name)
    if body is not None:
        return body
    return (
        "<meta charset='utf-8'><h1>3D 页面缺失</h1>"
        f"<p>找不到 <code>{STATIC_DIR / name}</code></p>"
        f"<p>请确认 <code>src/uav_sim/static/{name}</code> 存在；"
        "或访问 <a href='/classic'>/classic</a> 使用 2D 精简面板。</p>"
    ).encode("utf-8")

#: 面板专用的时间缩放（仿真变慢，便于观察）。1 倍速下 PEAK 90 仿真秒 ≈ 22.5 真实秒。
PANEL_TIME_SCALE: float = 0.25


# ===========================================================================
# 仿真运行器
# ===========================================================================
class SimRun:
    """一次可视化的仿真运行（在后台线程里跑）。"""

    def __init__(self, cfg: dict, scenario: str, scheduler: str, speed: float = 1.0,
                 weather: Optional[str] = None) -> None:
        self.base_cfg = cfg
        self.scenario = scenario
        self.scheduler = scheduler
        self.speed = max(0.25, min(8.0, float(speed)))
        #: 本次运行使用的天气预设（/api/weather 可在运行中/下次启动时改它）
        self.weather_preset: str = config_preset(cfg, weather)
        self.status = "idle"          # idle / running / finished / stopped / error
        self.error: Optional[str] = None
        self.sim: Optional[Simulator] = None
        self.summary: dict = {}
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._user_stopped = False    # 区分"用户主动停止"与"自然跑完"

    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self.status == "running":
                raise RuntimeError("已有仿真在运行，请先停止")
            self.status = "running"
            self.error = None
            self.summary = {}
            self._user_stopped = False
        self._thread = threading.Thread(target=self._run, name="DashSim", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._user_stopped = True
        sim = self.sim
        if sim is not None:
            sim.stop_event.set()

    # ------------------------------------------------------------------
    def _run(self) -> None:
        try:
            cfg = json.loads(json.dumps(self.base_cfg))  # 深拷贝，避免污染全局配置
            # 面板把仿真放慢，只影响观看速度，不影响指标口径
            cfg["runtime"]["time_scale"] = PANEL_TIME_SCALE / self.speed

            sc = get_scenario(self.scenario)
            trace = generate_trace(sc, int(cfg["simulation"]["random_seed"]), cfg)
            sim = Simulator(sc, trace, cfg, scheduler_name=self.scheduler,
                            seed=int(cfg["simulation"]["random_seed"]),
                            weather_preset=self.weather_preset)
            self.sim = sim
            sched = create_scheduler(self.scheduler, sim, cfg)
            raw = sim.run(sched, duration_s=sc.duration_s)

            # ---- 收尾统计（面板用，口径与正式指标一致但不落库）----
            tasks = raw["tasks"]
            done = sum(1 for t in tasks if t.state.name == "DONE")
            high = [t for t in tasks if t.priority is TaskPriority.HIGH]
            low = [t for t in tasks if t.priority is TaskPriority.LOW]
            thr = float(cfg["server"]["starvation_metric_threshold_ms"]) / 1000.0
            end = float(raw.get("end_ts", sc.duration_s))

            def _wait(t) -> float:
                if t.state.name in ("DONE", "DROPPED"):
                    return float(t.wait_ms_total)
                return float(t.wait_ms(end))

            w_high = [_wait(t) for t in high] or [0.0]
            w_low = [_wait(t) for t in low] or [0.0]
            starved = sum(1 for t in low if _wait(t) > thr * 1000.0)

            with self._lock:
                self.summary = {
                    "submitted": len(tasks),
                    "done": done,
                    "throughput": round(done / max(1e-9, float(raw["duration_s"])), 3),
                    "completion_rate": round(done / max(1, len(tasks)) * 100.0, 2),
                    "avg_wait_high_ms": round(sum(w_high) / len(w_high), 1),
                    "avg_wait_low_ms": round(sum(w_low) / len(w_low), 1),
                    "starvation_rate_low": round(starved / max(1, len(low)) * 100.0, 2),
                    "context_switches": int(raw.get("context_switches", 0)),
                    "overload_seconds": round(float(raw.get("overload_seconds", 0.0)), 2),
                    "wall_seconds": round(float(raw["duration_s"]) * float(cfg["runtime"]["time_scale"]), 2),
                }
                # 注意：Simulator.run() 收尾时**总会** set stop_event，所以不能拿它判断
                # 是否被用户停止，必须用独立标志位。
                self.status = "stopped" if self._user_stopped else "finished"
        except Exception as exc:  # noqa: BLE001 - 面板线程绝不能静默崩
            logger.exception("可视化仿真异常")
            with self._lock:
                self.status = "error"
                self.error = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    def state(self) -> dict:
        """返回给前端的完整状态（运行中 → 附带 snapshot）。"""
        with self._lock:
            status = self.status
            error = self.error
            summary = dict(self.summary)
            sim = self.sim
        payload: dict = {
            "status": status,
            "error": error,
            "summary": summary,
            "scenario": self.scenario,
            "scenario_label": SCENARIO_LABELS.get(self.scenario, self.scenario),
            "scheduler": self.scheduler,
            "scheduler_label": SCHEDULER_LABELS.get(self.scheduler, self.scheduler),
            "speed": self.speed,
            "weather_preset": self.weather_preset,
            "scenarios": [{"code": c, "label": SCENARIO_LABELS.get(c, c)} for c in SCENARIO_CODES],
            "schedulers": [{"code": s, "label": SCHEDULER_LABELS.get(s, s)} for s in SCHEDULER_NAMES],
        }
        if sim is not None:
            try:
                payload["live"] = sim.snapshot()
                # 顶层再放一份 weather：前端（2D 面板/3D 主页面）不必下钻 live 就能读到
                payload["weather"] = payload["live"].get("weather")
            except Exception:  # noqa: BLE001 - 快照失败不影响面板存活
                logger.exception("snapshot 失败")
        return payload


# ===========================================================================
# HTTP 服务
# ===========================================================================
class Dashboard:
    """面板服务：持有当前运行 + 处理请求。"""

    #: 未运行仿真时用于展示"该天气典型状态"的曲线时长（取 PEAK 的 90 s）。
    #: 只是给前端一个合理的 intensity/visibility 数值，不参与任何指标计算。
    IDLE_WEATHER_DURATION_S: float = 90.0

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.run: Optional[SimRun] = None
        self._lock = threading.Lock()
        # ---- 天气：默认值 / 下次启动的预设（契约 §4.6.4「未运行作下次默认」） ----
        self._seed = cfg_get_int(cfg, "simulation.random_seed", 20261031)
        self.weather_preset: str = config_preset(cfg)
        self._weather_default = WeatherModel(
            seed=self._seed, preset=self.weather_preset,
            duration_s=self.IDLE_WEATHER_DURATION_S, cfg=cfg,
        )

    # ------------------------------------------------------------------ 天气
    def _idle_weather_snapshot(self) -> dict:
        """未运行/无 sim 时的 weather 字段：取该天气**平台期**的典型值。

        为什么不取 t=0：t=0 是 onset 起点（intensity≈0），前端若拿它当默认天气，
        会显示"下雨但强度为 0、能见度 12km"的怪状态；也不能取 t=duration（尾部
        已衰减到 0）。``typical_snapshot()`` 固定取 50% 时长，一定落在平台期。
        """
        return self._weather_default.typical_snapshot()

    def set_weather(self, preset: str) -> dict:
        """切换天气（GET /api/weather?preset=xxx 的落点）。

        * 运行中：立刻调用 ``sim.set_weather`` 重建链路健康度曲线（秒级生效）；
        * 未运行 / 刚结束：只更新"下次启动的默认值"并返回**平台期典型快照**；
        * 返回 ``{"ok": true, "weather": {...}}``（契约 §4.6.4）。

        注意：刚结束时 ``sim.now()`` 已冻结在 ``duration``（天气曲线尾部已衰减到 0），
        此时若走 ``sim.set_weather`` 取快照，会出现"preset=dust 但 intensity=0、
        能见度=12"的自相矛盾读数（Lead 验收发现的问题）。所以只有 status=="running"
        才走实时快照，其余一律返回平台期典型值。
        """
        name = config_preset(self.cfg, preset)
        with self._lock:
            self.weather_preset = name
            self._weather_default = WeatherModel(
                seed=self._seed, preset=name,
                duration_s=self.IDLE_WEATHER_DURATION_S, cfg=self.cfg,
            )
            run = self.run
        snapshot = None
        if run is not None:
            run.weather_preset = name
            if run.status == "running" and run.sim is not None:
                snapshot = run.sim.set_weather(name)     # 运行中立即作用到链路健康度
        if snapshot is None:
            snapshot = self._idle_weather_snapshot()
        return {"ok": True, "weather": snapshot}

    def start_run(self, scenario: str, scheduler: str, speed: float,
                  weather: Optional[str] = None) -> dict:
        with self._lock:
            if self.run is not None and self.run.status == "running":
                return {"ok": False, "msg": "已有仿真在运行"}
            preset = config_preset(self.cfg, weather if weather is not None
                                   else self.weather_preset)
            self.weather_preset = preset
            self.run = SimRun(self.cfg, scenario, scheduler, speed, weather=preset)
            try:
                self.run.start()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "msg": str(exc)}
            return {"ok": True, "weather_preset": preset}

    # ------------------------------------------------------------------
    # 混沌工程 / Trace 导出（2026-11：OS 微观透视）
    # ------------------------------------------------------------------
    def chaos(self, kind: str, **kw) -> dict:
        """触发一次混沌注入：weaknet（弱网风暴）/ burst（并发洪峰）/ deadlock（死锁）。"""
        run = self.run
        if run is None or getattr(run, "sim", None) is None:
            return {"ok": False, "msg": "没有运行中的仿真，请先点「开始仿真」"}
        if run.status != "running":
            return {"ok": False, "msg": f"仿真当前状态是 {run.status}，只能在运行中注入"}
        sim = run.sim
        try:
            if kind == "weaknet":
                return sim.inject_weaknet(duration_s=float(kw.get("duration", 20.0)),
                                          frac=float(kw.get("frac", 0.4)))
            if kind == "burst":
                return sim.inject_burst(n=int(kw.get("n", 500)))
            if kind == "deadlock":
                return sim.inject_deadlock(hold_s=float(kw.get("hold", 20.0)))
        except Exception as exc:  # noqa: BLE001 - 接口层兜底
            logger.exception("混沌注入失败")
            return {"ok": False, "msg": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "msg": f"未知混沌类型 {kind}（可选 weaknet/burst/deadlock）"}

    def trace_export(self, fmt: str = "json"):
        """导出当前/最近一次运行的完整事件日志，返回 ``(content_type, bytes)``。"""
        run = self.run
        sim = getattr(run, "sim", None) if run is not None else None
        events = list(getattr(sim, "_event_log", []) or []) if sim is not None else []
        meta = {
            "scenario": getattr(run, "scenario", None) if run is not None else None,
            "scheduler": getattr(run, "scheduler", None) if run is not None else None,
            "status": getattr(run, "status", None) if run is not None else None,
            "events": len(events),
        }
        if str(fmt).lower() == "csv":
            buf = io.StringIO()
            cols = ["t", "k", "tid", "uav", "prio", "type", "slot", "slice_ms",
                    "size_mb", "wait_ms", "retry", "served_s", "held_s",
                    "duration_s", "uav_count", "requested", "injected"]
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for e in events:
                w.writerow(e)
            return "text/csv; charset=utf-8", ("\ufeff" + buf.getvalue()).encode("utf-8")
        body = json.dumps({"meta": meta, "events": events}, ensure_ascii=False, indent=1)
        return "application/json; charset=utf-8", body.encode("utf-8")

    def stop_run(self) -> dict:
        with self._lock:
            if self.run is not None:
                self.run.stop()
        return {"ok": True}

    def state(self) -> dict:
        with self._lock:
            run = self.run
            idle_weather = self._idle_weather_snapshot()
        if run is None:
            return {
                "status": "idle",
                "summary": {},
                "weather": idle_weather,
                "scenarios": [{"code": c, "label": SCENARIO_LABELS.get(c, c)}
                              for c in SCENARIO_CODES],
                "schedulers": [{"code": s, "label": SCHEDULER_LABELS.get(s, s)}
                               for s in SCHEDULER_NAMES],
            }
        payload = run.state()
        # 顶层 weather 始终存在（契约 §4.6.4）：
        #   * 运行中 -> 用 live 快照里的**实时**天气值（随时间变化，前端据此调雾/粒子）；
        #   * 未运行/刚结束 -> 用"下次启动预设"的**平台期典型值**，避免冻结时刻
        #     （duration，曲线尾部=0）导致"沙尘却显示晴朗数值"的矛盾读数。
        if run.status == "running" and payload.get("live"):
            live_weather = payload["live"].get("weather")
            payload["weather"] = live_weather if live_weather else idle_weather
        else:
            payload["weather"] = idle_weather
        return payload


def _make_handler(dash: Dashboard):
    """构造请求处理器（闭包持有 Dashboard）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "UavSchedSim/1.0"

        # ---- 工具 ----
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 浏览器刷新/关闭，忽略

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        # ---- 路由 ----
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            if path == "/":
                # 默认打开 3D 场景页；/classic 保留 2D 精简面板做兜底
                self._send(200, _read_page("app3d.html"), "text/html; charset=utf-8")
                return
            if path == "/classic":
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                # 防目录穿越：只允许 static 下的扁平文件名
                if "/" in name or "\\" in name or ".." in name:
                    self._json({"ok": False, "msg": "bad path"}, 400)
                    return
                body = _read_page_raw(name)
                if body is None:
                    self._json({"ok": False, "msg": "not found"}, 404)
                    return
                self._send(200, body, _content_type(name))
                return
            if path == "/api/state":
                self._json(dash.state())
                return
            if path == "/api/weather":
                # 契约 §4.6.4：GET /api/weather?preset=rain
                #   运行中 -> 立刻作用到链路健康度；未运行 -> 作为下次启动的默认值。
                #   非法 preset 必须返回 400 JSON（不能 500，也不能静默回退）。
                preset = (qs.get("preset") or [""])[0].strip().lower()
                if not preset or not is_valid_preset(preset):
                    self._json({
                        "ok": False,
                        "msg": f"未知天气 {preset or '(空)'}，可选：clear/rain/snow/dust",
                        "available": available_presets(),
                    }, 400)
                    return
                try:
                    self._json(dash.set_weather(preset))
                except Exception as exc:  # noqa: BLE001 - 接口层兜底，绝不 500 崩面板
                    logger.exception("切换天气失败")
                    self._json({"ok": False, "msg": f"切换天气失败：{exc}"}, 500)
                return
            if path == "/api/chaos":
                # GET /api/chaos?kind=weaknet|burst|deadlock[&duration=20&frac=0.4&n=500&hold=20]
                kind = (qs.get("kind") or [""])[0].strip().lower()
                kw = {}
                for k in ("duration", "frac", "n", "hold"):
                    if k in qs:
                        try:
                            kw[k] = float(qs[k][0])
                        except (TypeError, ValueError):
                            pass
                try:
                    self._json(dash.chaos(kind, **kw))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("混沌接口异常")
                    self._json({"ok": False, "msg": str(exc)}, 500)
                return
            if path == "/api/trace":
                # GET /api/trace?format=json|csv —— 导出完整事件日志（Trace）
                fmt = (qs.get("format") or ["json"])[0].strip().lower()
                ctype, body = dash.trace_export(fmt)
                self._send(200, body, ctype)
                return
            if path == "/api/start":
                scenario = (qs.get("scenario") or ["PEAK"])[0].upper()
                scheduler = (qs.get("scheduler") or ["dynamic"])[0].lower()
                speed = float((qs.get("speed") or ["1"])[0])
                weather = (qs.get("weather") or [""])[0].strip().lower() or None
                if scenario not in SCENARIO_CODES:
                    self._json({"ok": False, "msg": f"未知工况 {scenario}"}, 400)
                    return
                if scheduler not in SCHEDULER_NAMES:
                    self._json({"ok": False, "msg": f"未知策略 {scheduler}"}, 400)
                    return
                if weather is not None and not is_valid_preset(weather):
                    self._json({"ok": False,
                                "msg": f"未知天气 {weather}"}, 400)
                    return
                self._json(dash.start_run(scenario, scheduler, speed, weather=weather))
                return
            if path == "/api/stop":
                self._json(dash.stop_run())
                return
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            self._json({"ok": False, "msg": "not found"}, 404)

        def log_message(self, fmt: str, *args) -> None:  # 静音访问日志
            return

    return Handler


def make_server(cfg: Optional[dict] = None, host: str = "127.0.0.1",
                port: int = 0):
    """创建（但不启动）HTTP 服务，返回 ``(httpd, dashboard, port)``。

    供**桌面程序**使用：它需要一个可控端口的服务实例，由 pywebview 窗口托管。
    ``port=0`` 表示让系统分配空闲端口（避免端口冲突）。
    """
    cfg = cfg or load_config()
    dash = Dashboard(cfg)
    httpd = ThreadingHTTPServer((host, port), _make_handler(dash))
    httpd.daemon_threads = True
    return httpd, dash, httpd.server_address[1]


def serve(cfg: Optional[dict] = None, host: str = "127.0.0.1",
          port: int = DEFAULT_PORT, open_browser: bool = True,
          scenario: str = "PEAK", scheduler: str = "dynamic",
          speed: float = 1.0, autostart: bool = False) -> int:
    """启动面板并阻塞（Ctrl+C 退出）。返回进程退出码。"""
    cfg = cfg or load_config()
    dash = Dashboard(cfg)
    url = f"http://{host}:{port}/"

    httpd = ThreadingHTTPServer((host, port), _make_handler(dash))
    httpd.daemon_threads = True

    print()
    print("=" * 74)
    print("  无人机集群调度仿真 · 实时可视化面板")
    print("=" * 74)
    print(f"  地址        : {url}")
    print(f"  工况/策略   : {scenario} / {scheduler}（可在页面上切换）")
    print("  停止服务    : 在本窗口按 Ctrl+C")
    print("=" * 74)
    print()

    if autostart:
        dash.start_run(scenario, scheduler, speed)

    if open_browser:
        def _open() -> None:
            time.sleep(0.8)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_open, daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n  正在停止服务 ...")
    finally:
        try:
            dash.stop_run()
        except Exception:
            pass
        httpd.shutdown()
        httpd.server_close()
        print("  已退出。")
    return 0


# ===========================================================================
# 前端页面（单文件内嵌，无外部依赖、无 CDN，断网也能用）
# ===========================================================================
INDEX_HTML: str = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>无人机集群并发自适应调度仿真 · 实时面板</title>
<style>
  :root{
    --bg:#0f1420; --panel:#161d2c; --panel2:#1d2637; --line:#26314a;
    --txt:#e6ecf7; --dim:#8b9bb4; --hi:#4ea1ff; --lo:#f5a623;
    --ok:#3ddc97; --warn:#ffd166; --bad:#ff6b6b; --dyn:#ff7a45;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.5 "Microsoft YaHei","Segoe UI",system-ui,sans-serif}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
         padding:12px 18px;background:linear-gradient(90deg,#141b2b,#1b2436);
         border-bottom:1px solid var(--line);position:sticky;top:0;z-index:9}
  h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.5px}
  .tag{font-size:12px;color:var(--dim)}
  select,button{font:inherit;color:var(--txt);background:var(--panel2);
                border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
  button{cursor:pointer;transition:.15s}
  button:hover{border-color:#3d5378;background:#243149}
  button.primary{background:#1f6feb;border-color:#1f6feb}
  button.primary:hover{background:#2f7ff5}
  button.danger{background:#7f2a2a;border-color:#a33}
  #status{font-weight:600}
  .bar{height:6px;background:#0b0f18;border-radius:4px;overflow:hidden;min-width:160px;flex:1}
  .bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,#1f6feb,#3ddc97);transition:width .2s}
  main{display:grid;grid-template-columns:300px 1fr 320px;gap:14px;padding:14px 18px 0}
  @media (max-width:1200px){main{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
  .panel h2{font-size:13px;margin:0 0 10px;color:var(--dim);font-weight:600;
            letter-spacing:1px;text-transform:uppercase}
  /* 无人机网格 */
  #fleet{display:grid;grid-template-columns:repeat(6,1fr);gap:6px}
  .uav{position:relative;aspect-ratio:1;border-radius:6px;background:#22304a;
       border:1px solid #2c3b57;display:flex;align-items:flex-end;justify-content:center;
       font-size:10px;color:#cfe0ff;overflow:hidden}
  .uav>i{position:absolute;left:0;right:0;bottom:0;background:var(--ok);opacity:.55;
         transition:height .2s, background .2s}
  .uav>span{position:relative;z-index:2;padding-bottom:1px;text-shadow:0 0 3px #000}
  .uav.off{opacity:.28;filter:grayscale(1)}
  /* 队列 */
  .qrow{display:flex;align-items:center;gap:10px;margin:8px 0}
  .qrow b{width:52px;font-size:12px;color:var(--dim);font-weight:500}
  .qbar{flex:1;height:18px;background:#0b0f18;border-radius:5px;overflow:hidden;position:relative}
  .qbar>i{display:block;height:100%;width:0;transition:width .18s}
  .qbar>em{position:absolute;right:6px;top:0;font-style:normal;font-size:11px;line-height:18px;color:#dfe9ff}
  .hi>i{background:linear-gradient(90deg,#2f7ff5,#4ea1ff)}
  .lo>i{background:linear-gradient(90deg,#b9761a,#f5a623)}
  /* 工作槽 */
  #slots{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
  .slot{background:#101828;border:1px solid #26314a;border-radius:8px;padding:7px 8px;min-height:62px}
  .slot.empty{opacity:.45;display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--dim)}
  .slot .t{font-size:11px;color:var(--dim)}
  .slot .n{font-size:12px;font-weight:600;margin:2px 0 5px}
  .slot .p{height:5px;background:#0b0f18;border-radius:3px;overflow:hidden}
  .slot .p>i{display:block;height:100%;background:var(--hi)}
  .slot.LOW .p>i{background:var(--lo)}
  /* 指标 */
  .kv{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px dashed #202a3e;font-size:13px}
  .kv:last-child{border:0}
  .kv span{color:var(--dim)}
  .kv b{font-variant-numeric:tabular-nums}
  #chart-wrap{padding:14px 18px 22px}
  canvas{width:100%;height:180px;display:block;background:var(--panel);border:1px solid var(--line);border-radius:12px}
  .legend{display:flex;gap:16px;font-size:12px;color:var(--dim);padding:6px 4px 0}
  .dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}
  #banner{padding:8px 18px;background:#5a1f1f;color:#ffd9d9;display:none;font-size:13px}
</style>
</head>
<body>
<header>
  <h1>🚁 无人机集群并发自适应调度仿真</h1>
  <span class="tag">工况</span>
  <select id="scn"></select>
  <span class="tag">调度策略</span>
  <select id="sch"></select>
  <span class="tag">速度</span>
  <select id="spd">
    <option value="0.5">0.5×</option><option value="1" selected>1×</option>
    <option value="2">2×</option><option value="4">4×</option>
  </select>
  <button class="primary" id="btnStart">▶ 开始仿真</button>
  <button class="danger" id="btnStop">■ 停止</button>
  <div class="bar"><i id="prog"></i></div>
  <span id="status">未开始</span>
</header>
<div id="banner"></div>
<main>
  <section class="panel">
    <h2>无人机集群 <span id="fleetInfo" class="tag"></span></h2>
    <div id="fleet"></div>
    <p class="tag" style="margin:10px 0 0">色块高度/颜色 = 链路健康度（绿优 → 红劣）；灰暗 = 已返航下线</p>
  </section>

  <section class="panel">
    <h2>两级就绪队列（高优勘查 / 低优运维）</h2>
    <div class="qrow"><b>高优先级</b><div class="qbar hi"><i id="qHi"></i><em id="qHiN">0</em></div></div>
    <div class="qrow"><b>低优先级</b><div class="qbar lo"><i id="qLo"></i><em id="qLoN">0</em></div></div>
    <div class="qrow" style="margin-top:2px"><b style="width:70px">系统负载</b>
      <div class="qbar"><i id="qLoad" style="background:linear-gradient(90deg,#3ddc97,#ffd166,#ff6b6b)"></i>
      <em id="qLoadN">0.00</em></div></div>

    <h2 style="margin-top:16px">工作线程槽位（= 系统并发容量）</h2>
    <div id="slots"></div>
  </section>

  <section class="panel">
    <h2>实时指标</h2>
    <div class="kv"><span>仿真时间</span><b id="mTime">0 / 0 s</b></div>
    <div class="kv"><span>已提交 / 已完成</span><b id="mTasks">0 / 0</b></div>
    <div class="kv"><span>吞吐量</span><b id="mThr">0.00 个/s</b></div>
    <div class="kv"><span>弱网重传累计</span><b id="mRetry">0 次</b></div>
    <div class="kv"><span>阻塞中 / 待重传</span><b id="mBlocked">0 / 0</b></div>
    <div class="kv"><span>被抢占次数</span><b id="mPreempt">0</b></div>
    <div class="kv"><span>上下文切换</span><b id="mCtx">0</b></div>
    <div class="kv"><span>当前时间片(高/低)</span><b id="mSlice">- / -</b></div>
    <div class="kv"><span>限流触发 / 拒绝大任务</span><b id="mLim">0 / 0</b></div>
    <div class="kv"><span>过载累计</span><b id="mOver">0 s</b></div>
    <h2 style="margin-top:16px">本次结果</h2>
    <div class="kv"><span>完成率</span><b id="rCompl">-</b></div>
    <div class="kv"><span>高/低优先级平均等待</span><b id="rWait">-</b></div>
    <div class="kv"><span>低优先级饥饿率</span><b id="rStarve">-</b></div>
  </section>
</main>

<div id="chart-wrap">
  <canvas id="chart" width="1400" height="180"></canvas>
  <div class="legend">
    <span><i class="dot" style="background:#4ea1ff"></i>高优先级队列深度</span>
    <span><i class="dot" style="background:#f5a623"></i>低优先级队列深度</span>
    <span><i class="dot" style="background:#3ddc97"></i>系统负载</span>
    <span style="margin-left:auto">横轴：仿真时间（秒）</span>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let timer = null, lastKey = "";

function healthColor(h){
  // 90+ 绿 / 70 黄绿 / 50 黄 / 30 橙 / <30 红
  if (h >= 85) return "#3ddc97";
  if (h >= 70) return "#8bd450";
  if (h >= 55) return "#ffd166";
  if (h >= 35) return "#ff9f43";
  return "#ff6b6b";
}
function fmt(n,d=0){ return (n===null||n===undefined)? "-" : Number(n).toFixed(d); }

function renderFleet(live){
  const box = $("fleet");
  if(!live) return;
  const links = live.links || {}, on = live.uav_online || {};
  const n = live.uav_count || Object.keys(links).length;
  if(box.childElementCount !== n){
    box.innerHTML = "";
    for(let i=1;i<=n;i++){
      const d = document.createElement("div");
      d.className = "uav"; d.id = "uav"+i;
      d.innerHTML = '<i></i><span>'+i+'</span>';
      box.appendChild(d);
    }
  }
  for(let i=1;i<=n;i++){
    const el = $("uav"+i); if(!el) continue;
    const h = links[String(i)];
    const online = on[String(i)] !== false;
    el.classList.toggle("off", !online);
    const bar = el.querySelector("i");
    const hv = (h===undefined? 100 : h);
    bar.style.height = Math.max(6, hv) + "%";
    bar.style.background = healthColor(hv);
    el.title = "UAV-"+i+"  健康度 "+fmt(hv,1)+"  "+(online?"在线":"已下线");
  }
  $("fleetInfo").textContent = "共 " + n + " 架";
}

function renderQueues(live){
  if(!live) return;
  const cap = Math.max(20, live.queue.capacity ? Math.min(400, live.queue.capacity) : 400);
  const hi = live.queue.high, lo = live.queue.low;
  $("qHi").style.width = Math.min(100, hi/cap*100) + "%";
  $("qLo").style.width = Math.min(100, lo/cap*100) + "%";
  $("qHiN").textContent = hi; $("qLoN").textContent = lo;
  const load = live.system_load || 0;
  $("qLoad").style.width = Math.min(100, load/2*100) + "%";
  $("qLoadN").textContent = fmt(load,2) + (load>1? " 过载":"");
  $("mOver").textContent = fmt(live.overload_seconds,1) + " s";
}

function renderSlots(live){
  const box = $("slots");
  if(!live) return;
  const W = live.workers || 8;
  const runs = live.running_tasks || [];
  if(box.childElementCount !== W){
    box.innerHTML = "";
    for(let i=0;i<W;i++){
      const d = document.createElement("div"); d.className="slot empty"; d.id="slot"+i;
      d.textContent = "空闲";
      box.appendChild(d);
    }
  }
  for(let i=0;i<W;i++){
    const el = $("slot"+i);
    const t = runs[i];
    if(!t){ el.className="slot empty"; el.textContent="空闲"; continue; }
    el.className = "slot " + t.priority;
    el.innerHTML = '<div class="t">UAV-'+t.uav_id+' · '+t.task_type+'</div>'
                 + '<div class="n">'+(t.priority==="HIGH"?"高优":"低优")+'</div>'
                 + '<div class="p"><i style="width:'+Math.round((t.progress||0)*100)+'%"></i></div>';
    el.title = "剩余 "+fmt(t.remaining_s,2)+" / 共 "+fmt(t.service_s,2)+" 仿真秒";
  }
}

function renderMetrics(live, st){
  if(live){
    $("mTime").textContent = fmt(live.sim_time,1)+" / "+fmt(live.duration_s,0)+" s";
    $("mTasks").textContent = live.counters.submitted+" / "+live.counters.done;
    $("mThr").textContent = fmt(live.counters.done/Math.max(0.001,live.sim_time),2)+" 个/s";
    $("mRetry").textContent = live.counters.blocked+" 次";
    $("mBlocked").textContent = live.counters.blocked+" / "+live.counters.retry_pending;
    $("mPreempt").textContent = live.counters.preempted;
    $("mCtx").textContent = live.scheduler_stats.context_switches;
    $("mSlice").textContent = fmt(live.scheduler_stats.high_time_slice_ms,0)+" / "
                            + fmt(live.scheduler_stats.low_time_slice_ms,0)+" ms";
    $("mLim").textContent = live.limiter.throttled+" / "+live.limiter.rejected_large;
    $("prog").style.width = Math.min(100, live.sim_time/Math.max(0.001,live.duration_s)*100)+"%";
  }
  const s = st.summary || {};
  if(s.done !== undefined){
    $("rCompl").textContent = fmt(s.completion_rate,2)+" %";
    $("rWait").textContent = fmt(s.avg_wait_high_ms,0)+" / "+fmt(s.avg_wait_low_ms,0)+" ms";
    $("rStarve").textContent = fmt(s.starvation_rate_low,2)+" %";
  }
}

function renderChart(live){
  const cv = $("chart"), ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  ctx.clearRect(0,0,W,H);
  // 背景网格
  ctx.strokeStyle = "#202a3e"; ctx.lineWidth = 1;
  for(let i=1;i<5;i++){ const y=H*i/5; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke(); }
  if(!live || !live.history || live.history.length<2) return;
  const hist = live.history;
  const dur = Math.max(1, live.duration_s);
  const maxQ = Math.max(10, ...hist.map(d=>Math.max(d.high,d.low)));
  const X = t => t/dur*(W-40)+30;
  const Y = v => H-14 - v/maxQ*(H-28);
  const line = (key, color, getter) => {
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2;
    hist.forEach((d,i)=>{ const x=X(d.ts), y=Y(getter(d)); i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
    ctx.stroke();
  };
  line("high","#4ea1ff", d=>d.high);
  line("low","#f5a623", d=>d.low);
  line("load","#3ddc97", d=>d.load*maxQ/2);   // 负载按比例缩放叠画
  ctx.fillStyle="#8b9bb4"; ctx.font="11px Microsoft YaHei";
  ctx.fillText("max "+maxQ, 4, 14);
}

async function tick(){
  try{
    const r = await fetch("/api/state", {cache:"no-store"});
    const st = await r.json();
    const live = st.live;
    const statusMap = {idle:"未开始",running:"仿真中",finished:"已完成",stopped:"已停止",error:"出错"};
    $("status").textContent = statusMap[st.status] || st.status;
    if(st.scenario_label){
      $("status").textContent += " · " + st.scenario_label + " × " + (st.scheduler_label||st.scheduler);
    }
    if(st.error){ $("banner").style.display="block"; $("banner").textContent = "错误：" + st.error; }
    else { $("banner").style.display="none"; }
    renderFleet(live); renderQueues(live); renderSlots(live);
    renderMetrics(live, st); renderChart(live);
    if(st.status!=="running" && timer){ /* 保留定时器，便于再次启动 */ }
  }catch(e){ /* 服务未就绪时静默重试 */ }
}

async function initSelects(){
  const r = await fetch("/api/state", {cache:"no-store"});
  const st = await r.json();
  const scn = $("scn"), sch = $("sch");
  scn.innerHTML = ""; sch.innerHTML = "";
  (st.scenarios||[]).forEach(o=>{ const e=document.createElement("option"); e.value=o.code; e.textContent=o.code+" · "+o.label; scn.appendChild(e); });
  (st.schedulers||[]).forEach(o=>{ const e=document.createElement("option"); e.value=o.code; e.textContent=o.label; sch.appendChild(e); });
  scn.value = "PEAK"; sch.value = "dynamic";
}

$("btnStart").onclick = async () => {
  const q = `?scenario=${$("scn").value}&scheduler=${$("sch").value}&speed=${$("spd").value}`;
  const r = await fetch("/api/start"+q); const j = await r.json();
  if(!j.ok) alert(j.msg || "启动失败");
};
$("btnStop").onclick = () => fetch("/api/stop");

initSelects();
setInterval(tick, 180);
tick();
</script>
</body>
</html>
"""


if __name__ == "__main__":  # pragma: no cover - 便于单独调试
    logging.basicConfig(level=logging.INFO)
    sys.exit(serve())
