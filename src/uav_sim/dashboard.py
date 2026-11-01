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
import os
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


def _detect_output_root() -> Path:
    """定位「实验产物根目录」（`charts/` 与 `results/` 所在的那一层）。

    为什么需要探测而不是写死：同一个程序有三种运行形态，产物位置不同 ——
      * **源码运行**（`python run.py ui` / `app.py`）：产物在项目根；
      * **打包成 exe 从 `dist/` 启动**：exe 旁边是 `dist/`，那里通常**没有** `charts/`，
        而项目根（`dist/..`）才有 —— 写死 exe 目录会让「仿真结果」界面在 exe 里永远空白
        （实测踩过：exe 里 `/api/results` 返回 0 张图）；
      * **exe 被单独拷到别处**：那就以 exe 旁边为准（用户自己在那儿跑实验）。
    因此按优先级探测，取第一个"看起来像产物根"的目录；可用环境变量
    ``UAV_RESULTS_ROOT`` 显式指定（CI / 换机器最省事）。
    """
    cands: list[Path] = []
    env = os.environ.get("UAV_RESULTS_ROOT")
    if env:
        cands.append(Path(env))
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        cands += [exe_dir, exe_dir.parent, Path.cwd()]
    else:
        cands += [Path(__file__).resolve().parents[2], Path.cwd()]
    for c in cands:
        try:
            if (c / "charts").is_dir() or (c / "results").is_dir():
                return c.resolve()
        except OSError:
            continue
    return cands[0] if cands else Path.cwd()


#: 「仿真结果」界面读取产物的根目录（`charts/`、`results/`、`docs/images/` 都在它下面）
OUTPUT_ROOT: Path = _detect_output_root()

#: 「仿真结果」界面能读的**图片白名单目录**（相对项目根 / exe 目录）。
#: 只允许这几处、只允许图片扩展名 —— 这是防目录穿越的第一道闸（第二道见 _safe_chart_path）。
RESULT_IMG_DIRS: tuple = (
    "charts",
    "results/stations/charts",
    "results/station_probe",
    "results/mec",
    "results/weather",
    "results/sensitivity",
    "results/weather_eval",
    "results/gpu",
    "docs/images",
)

#: 结果界面里图片分组的**展示顺序与中文标题**（分组键 = 上面的相对目录）
RESULT_SECTIONS: tuple = (
    ("charts", "核心结论（15 组矩阵）",
     "该目录下的 01~07 来自 run.py all；09/10 来自伴生探针与 run.py stations"),
    ("results/stations/charts", "多站 × 多策略矩阵（18 组）",
     "run.py stations 产出，独立口径，不与 15 组混比"),
    ("results/station_probe", "站级 / MEC 伴生探针", "run.py all 的伴生 3 组"),
    ("results/mec", "MEC 端-边-云对照", "run.py mec 产出"),
    ("results/weather", "天气对照", "run.py weather 产出"),
    ("results/weather_eval", "运行场景测评", "run.py bench-weather 产出"),
    ("results/sensitivity", "参数敏感性", "run.py sensitivity 产出"),
    ("results/gpu", "GPU 大规模重复实验", "run.py gpu 产出"),
    ("docs/images", "文档截图", "docs/images 下已归档的 3D/OS 透视截图"),
)

#: 图片扩展名白名单（只读图片，绝不把 .py/.csv 之类经这个路由吐出去）
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")

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


def _parse_id_list(raw) -> Optional[list]:
    """把 ``"2,3"`` 这类查询串解析成 ``[2, 3]``；空/非法返回 None（= 用缺省分组）。"""
    if raw in (None, "", []):
        return None
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = str(raw).split(",")
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            out.append(int(p))
        except (TypeError, ValueError):
            continue
    return out or None


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
# 实验产物读取（「仿真结果」界面用，**只读**、白名单、绝不抛异常）
# ===========================================================================
def _mtime_iso(path: Path) -> str:
    """文件修改时间（本地时区 ISO8601，秒级）。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    except OSError:
        return ""


def _safe_chart_path(rel: str) -> Optional[Path]:
    """把 ``"charts/01-吞吐量对比.png"`` 解析成**白名单目录内的绝对路径**。

    两道闸（与 ``/api/state`` 的“不信任前端”设计一致）：
      ① 只能落在 :data:`RESULT_IMG_DIRS` 里的目录下（用 ``resolve()`` 后比较前缀，
         因此 ``../``、绝对路径、盘符都过不去）；
      ② 只允许图片扩展名。
    返回 None 表示"不允许"，调用方据此回 404 —— **绝不把失败原因带路径吐回去**。
    """
    raw = str(rel or "").replace("\\", "/").strip().lstrip("/")
    if not raw or ".." in raw.split("/"):
        return None
    if Path(raw).suffix.lower() not in _IMG_EXT:
        return None
    # 白名单：声音上的"第一段目录"必须是允许的目录之一
    cand = (OUTPUT_ROOT / raw).resolve()
    for d in RESULT_IMG_DIRS:
        base = (OUTPUT_ROOT / d).resolve()
        try:
            cand.relative_to(base)
        except ValueError:
            continue
        return cand if cand.is_file() else None
    return None


def _list_result_images() -> list:
    """扫描白名单目录里的图片，按章节分组返回（给「仿真结果」界面用）。"""
    out = []
    for rel_dir, title, note in RESULT_SECTIONS:
        base = OUTPUT_ROOT / rel_dir
        if not base.is_dir():
            continue
        items = []
        try:
            files = sorted(base.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for p in files:
            if not p.is_file() or p.suffix.lower() not in _IMG_EXT:
                continue
            items.append({
                "rel": f"{rel_dir}/{p.name}",
                "name": p.name,
                "title": p.stem,
                "mtime": _mtime_iso(p),
                "size": int(p.stat().st_size) if p.exists() else 0,
            })
        if items:
            out.append({"dir": rel_dir, "title": title, "note": note, "images": items})
    return out


#: 结果表格里展示的指标列（键 -> 中文名；顺序即列顺序）
RESULT_TABLE_COLUMNS: tuple = (
    ("throughput", "吞吐"), ("completion_rate", "完整率%"),
    ("avg_wait_high_ms", "高优等待ms"), ("avg_wait_low_ms", "低优等待ms"),
    ("starvation_rate_low", "低优饥饿%"), ("resource_utilization", "利用率%"),
    ("retry_rate", "重传率%"), ("station_load_imbalance", "站间不均衡"),
    ("migration_count", "迁移次数"), ("edge_offload_ratio", "边缘分流%"),
    ("avg_e2e_ms", "端到端ms"), ("quorum_commits", "q提交"),
    ("quorum_rejects", "q拒绝"),
)


def _results_dir(cfg: dict) -> Path:
    """定位 `results/` 目录（CSV/JSON 产物）。

    与 :func:`_detect_output_root` 同理：exe 里 `config.yaml` 是**打包内置**的那份，
    它的 `output.results_dir` 是相对路径，按 `_MEIPASS` 解析会落到临时目录；更坑的是
    `load_config()` 会**顺手把输出目录建出来**，于是"目录存在"这个判据在 exe 里恒为真
    （实测：exe 里 `/api/results` 的 summary_rows=0，路径指向 `_MEIxxxx\\results`）。
    所以这里判据不是"目录存在"，而是"**目录里真的有产物**"（summary.csv 或 raw/）。
    """
    try:
        from uav_sim.config import resolve_path

        p = Path(resolve_path(cfg, "output.results_dir"))
    except Exception:  # noqa: BLE001 - 配置异常不该让界面崩
        p = OUTPUT_ROOT / "results"

    def _has_artifacts(d: Path) -> bool:
        try:
            return (d / "summary.csv").is_file() or (d / "raw").is_dir()
        except OSError:
            return False

    if _has_artifacts(p):
        return p
    alt = OUTPUT_ROOT / "results"
    if _has_artifacts(alt):
        return alt
    return p


def _read_summary_csv(cfg: dict) -> dict:
    """读 ``results/summary.csv``（15 组矩阵）→ 结构化表格。

    只读前 :data:`RESULT_TABLE_COLUMNS` 这些列 + 元信息列；CSV 不存在/损坏时
    返回 ``{"exists": False, ...}``，**不抛异常**（界面上给一句"还没跑过 run.py all"即可）。
    """
    path = _results_dir(cfg) / "summary.csv"
    if not path.is_file():
        return {"exists": False, "path": str(path), "generated_at": "", "rows": [],
                "columns": [{"key": k, "label": v} for k, v in RESULT_TABLE_COLUMNS]}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            rows_in = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:  # noqa: BLE001
        return {"exists": True, "path": str(path), "generated_at": "", "rows": [],
                "columns": [{"key": k, "label": v} for k, v in RESULT_TABLE_COLUMNS],
                "error": f"{type(exc).__name__}: {exc}"}

    rows = []
    seeds = set()
    for r in rows_in:
        item = {
            "scenario": r.get("scenario", ""),
            "scenario_label": r.get("scenario_label", ""),
            "scheduler": r.get("scheduler", ""),
            "scheduler_label": r.get("scheduler_label", ""),
            "exp_id": r.get("exp_id", ""),
        }
        for key, _label in RESULT_TABLE_COLUMNS:
            try:
                item[key] = float(r.get(key) or 0.0)
            except (TypeError, ValueError):
                item[key] = 0.0
        seeds.add(r.get("seed", ""))
        rows.append(item)
    return {
        "exists": True,
        "path": str(path),
        "generated_at": _mtime_iso(path),
        "seed": ",".join(sorted(s for s in seeds if s)),
        "rows": rows,
        "columns": [{"key": k, "label": v} for k, v in RESULT_TABLE_COLUMNS],
    }


def _read_last_run_raw(cfg: dict) -> dict:
    """挑 ``results/raw`` 里**最新**的一份 JSON，取它的任务级记录给"队列回放"用。

    为什么只取一份：回放面板要能左右对比两个策略，但"最新一次 all"的 15 份 raw
    是同批产物；这里给出**整批清单**（含 queue_samples 的轻量摘要）+ 每份的取样数据按需取，
    避免一次把 15 个 JSON（每个几百 KB）全塞进一个响应。
    """
    raw_dir = _results_dir(cfg) / "raw"
    if not raw_dir.is_dir():
        return {"exists": False, "items": []}
    files = sorted(raw_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for p in files[:40]:
        items.append({
            "rel": f"results/raw/{p.name}",
            "name": p.name,
            "mtime": _mtime_iso(p),
            "size": int(p.stat().st_size),
            # 从文件名解析（LIGHT-dynamic-20261031-20261101183758）
            "scenario": p.stem.split("-")[0] if "-" in p.stem else "",
            "scheduler": p.stem.split("-")[1] if p.stem.count("-") >= 3 else "",
        })
    return {"exists": True, "items": items}


def _read_raw_series(cfg: dict, name: str) -> dict:
    """读单份 raw JSON，抽出"队列回放"需要的时序（**只返回必需字段**，控体积）。"""
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".json"):
        return {"ok": False, "msg": "非法文件名"}
    raw_dir = _results_dir(cfg) / "raw"
    p = raw_dir / name
    if not p.is_file():
        return {"ok": False, "msg": "文件不存在"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # noqa: BLE001
        return {"ok": False, "msg": f"读取失败 {type(exc).__name__}"}
    samples = data.get("queue_samples") or []
    series = []
    for s in samples:
        try:
            series.append({
                "ts": round(float(s.get("ts", 0.0)), 3),
                "high": int(s.get("high_depth", 0) or 0),
                "low": int(s.get("low_depth", 0) or 0),
                "load": round(float(s.get("system_load", 0.0) or 0.0), 3),
                "running": int(s.get("running_count", 0) or 0),
                "blocked": int(s.get("blocked_count", 0) or 0),
            })
        except (TypeError, ValueError):
            continue
    metrics = data.get("metrics") or {}
    return {
        "ok": True,
        "name": name,
        "scenario": data.get("scenario", ""),
        "scheduler": data.get("scheduler", ""),
        "seed": data.get("seed", ""),
        "duration_s": float(data.get("duration_s", 0.0) or 0.0),
        "series": series,
        "metrics": {k: float(v) for k, v in metrics.items()
                    if isinstance(v, (int, float))} if isinstance(metrics, dict) else {},
    }


def _read_replication_ci(cfg: dict) -> dict:
    """读 ``results/replications/replication_summary.csv``，给"均值 ± 95% CI"用。"""
    path = _results_dir(cfg) / "replications" / "replication_summary.csv"
    if not path.is_file():
        return {"exists": False, "rows": [], "generated_at": ""}
    keep = {"completion_rate", "throughput", "station_load_imbalance", "avg_e2e_ms",
            "edge_offload_ratio", "migration_count", "avg_wait_all_ms"}
    rows = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fp:
            for r in csv.DictReader(fp):
                if r.get("metric") not in keep:
                    continue
                try:
                    rows.append({
                        "variant": r.get("variant", "single"),
                        "scenario": r.get("scenario", ""),
                        "scheduler": r.get("scheduler", ""),
                        "metric": r.get("metric", ""),
                        "metric_label": r.get("metric_label", ""),
                        "n": int(float(r.get("n") or 0)),
                        "mean": float(r.get("mean") or 0.0),
                        "ci95_half": float(r.get("ci95_half") or 0.0),
                    })
                except (TypeError, ValueError):
                    continue
    except (OSError, UnicodeDecodeError, csv.Error):  # noqa: BLE001
        return {"exists": True, "rows": [], "generated_at": _mtime_iso(path)}
    return {"exists": True, "rows": rows, "generated_at": _mtime_iso(path)}


# ===========================================================================
# 导出「报告素材包」（zip，纯标准库）
# ===========================================================================
#: 导出时打包的目录（相对 OUTPUT_ROOT）→ zip 内前缀
_EXPORT_DIRS = (
    ("charts", "charts"),
    ("results/stations", "results/stations"),
    ("results/station_probe", "results/station_probe"),
    ("results/mec", "results/mec"),
    ("results/replications", "results/replications"),
    ("docs/images", "screenshots"),
)

#: 单个文件上限（MB）：避免把 1.7 MB 的 raw JSON × N 份全塞进包（raw 默认不进包）
_EXPORT_MAX_FILE_MB = 12


def build_export_bundle(cfg: dict, scope: str = "charts") -> tuple:
    """把"报告要用的图 + 表 + 结论"打成一个 zip，返回 ``(文件名, bytes)``。

    设计取舍：
      * **默认 scope=charts**：只打 `charts/*.png` + 三个 CSV（summary / stations / probe）
        + 一份自动生成的 `结论摘要.md` + `README.txt`（说明每张图哪来的、口径是什么）。
        体积约 2~4 MB，能直接发微信/邮件。
      * **scope=all** 再加多站/MEC/CI 目录与 `docs/images` 截图（**不含** `results/raw`：
        15 份 JSON 就 11 MB，且它们是中间产物，报告用不上）。
      * 单文件 > 12 MB 跳过（在 README 里列出来，避免"悄悄少东西"）。
    """
    import zipfile

    root = OUTPUT_ROOT
    res = _results_dir(cfg)
    scope = str(scope or "charts").lower()
    want_dirs = [("charts", "charts")]
    if scope == "all":
        want_dirs = list(_EXPORT_DIRS)
    else:
        # charts 模式：只带三张"结论表"所在的目录，不搬 raw
        want_dirs += [("results/stations", "results/stations"),
                      ("results/station_probe", "results/station_probe")]

    buf = io.BytesIO()
    skipped = []
    added = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_dir, prefix in want_dirs:
            base = root / rel_dir
            if not base.is_dir():
                continue
            for p in sorted(base.rglob("*")):
                if not p.is_file():
                    continue
                # 只带图与表：raw/task_records 这类中间产物不进包
                if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".csv", ".md"):
                    continue
                if "raw" in p.parts or "task_records" in p.parts:
                    continue
                try:
                    size_mb = p.stat().st_size / 1048576.0
                except OSError:
                    continue
                arc = f"{prefix}/{p.relative_to(base).as_posix()}"
                if size_mb > _EXPORT_MAX_FILE_MB:
                    skipped.append(f"{arc}（{size_mb:.1f} MB，超过单文件上限）")
                    continue
                try:
                    zf.write(p, arc)
                    added.append(arc)
                except OSError as exc:  # noqa: BLE001
                    skipped.append(f"{arc}（读取失败：{exc}）")

        # 结论摘要（自动从 summary.csv 生成，避免"包里只有图、没有数"）
        summary = _read_summary_csv(cfg)
        zf.writestr("结论摘要.md", _bundle_summary_md(summary, cfg))
        zf.writestr("README.txt", _bundle_readme_txt(added, skipped, scope, summary))
        _ = res  # 保留：路径说明写进 README
    stamp = time.strftime("%Y%m%d-%H%M")
    name = f"仿真报告素材包-{stamp}.zip"
    return name, buf.getvalue()


def _bundle_summary_md(summary: dict, cfg: dict) -> str:
    """由 summary.csv 生成 Markdown 结论表（可直接粘进课程报告）。"""
    lines = ["# 实验结果摘要（自动生成）", ""]
    if not summary.get("exists") or not summary.get("rows"):
        lines += ["磁盘上还没有 `results/summary.csv`。",
                  "先在项目根执行 `python run.py all --no-db` 再导出。", ""]
        return "\n".join(lines)
    lines += [
        f"- 数据源：`results/summary.csv`（生成于 {summary.get('generated_at', '-')}）",
        f"- 随机种子：`{summary.get('seed', '-')}`（控制变量）",
        f"- 规模：3 工况 × 5 个经典策略 = {len(summary['rows'])} 组",
        "",
        "> 口径提醒：整跑指标存在 wall-clock 抖动（完整率 0.2~0.5 pp 量级），"
        "结论看**方向与量级**，不要把小数位当精确值（docs/11 §7）。",
        "",
        "| 工况 | 策略 | 吞吐 | 完整率% | 高优等待ms | 低优等待ms | 低优饥饿% | 利用率% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in summary["rows"]:
        lines.append(
            f"| {r.get('scenario_label') or r.get('scenario')} | "
            f"{r.get('scheduler_label') or r.get('scheduler')} | "
            f"{float(r.get('throughput', 0)):.2f} | "
            f"{float(r.get('completion_rate', 0)):.2f} | "
            f"{float(r.get('avg_wait_high_ms', 0)):.0f} | "
            f"{float(r.get('avg_wait_low_ms', 0)):.0f} | "
            f"{float(r.get('starvation_rate_low', 0)):.2f} | "
            f"{float(r.get('resource_utilization', 0)):.1f} |")
    lines += ["", "## 图表清单", ""]
    try:
        charts = (OUTPUT_ROOT / "charts")
        names = sorted(p.name for p in charts.glob("*.png")) if charts.is_dir() else []
    except OSError:
        names = []
    lines += [f"- `charts/{n}`" for n in names] or ["- （charts/ 为空）"]
    lines += ["", "## 复现命令", "", "```powershell",
              "& \".\\.venv\\Scripts\\python.exe\" run.py all --no-db",
              "& \".\\.venv\\Scripts\\python.exe\" run.py stations",
              "& \".\\.venv\\Scripts\\python.exe\" run.py replicate --reps 10 --scenarios PEAK "
              "--schedulers dynamic cross_node --variants single,triple,triple_mec --no-db",
              "```", ""]
    return "\n".join(lines)


def _bundle_readme_txt(added: list, skipped: list, scope: str, summary: dict) -> str:
    """zip 内的 README：说清"包里有什么、每张图哪来的、哪些被跳过"。"""
    out = [
        "仿真报告素材包",
        "=" * 60,
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"导出范围：{scope}（charts = 只带核心图表与三张结论表；all = 再加多站/MEC/CI/截图）",
        f"文件数：{len(added)}",
        "",
        "目录说明",
        "-" * 60,
        "charts/            15 组核心结论图（01~08）+ 站级/MEC 与多站矩阵图（09、10）",
        "results/stations/  多站 × 6 策略 × 3 工况 = 18 组矩阵（CSV + charts/）",
        "results/station_probe/  run.py all 的伴生探针（3 组）",
        "results/mec/       MEC 端-边-云对照（scope=all 时）",
        "results/replications/  重复实验均值 ± 95% 置信区间（scope=all 时）",
        "screenshots/       docs/images 下归档的界面截图（scope=all 时）",
        "结论摘要.md        由 results/summary.csv 自动生成的 Markdown 表（可直接粘进报告）",
        "",
        "口径提醒",
        "-" * 60,
        "1) 单次实跑的整跑指标有 wall-clock 抖动，结论看方向与量级，不要抠小数位；",
        "2) 15 组矩阵口径固定为 3 工况 × 5 个经典策略；cross_node / 多站 / MEC 是**独立对照实验**，",
        "   不与 15 组混在一张表里比绝对值（docs/11 §7.2）；",
        "3) 图上的时间戳是文件生成时间，若与 summary.csv 不是同一次实验，引用前请核对。",
        "",
    ]
    if not summary.get("exists"):
        out += ["注意：导出时没有找到 results/summary.csv，结论摘要为空。", ""]
    if skipped:
        out += ["未打包的文件（超过单文件上限或读取失败）", "-" * 60]
        out += [f"  - {s}" for s in skipped]
        out.append("")
    out += ["完整文件清单", "-" * 60] + [f"  {a}" for a in added] + [""]
    return "\n".join(out)


# ===========================================================================
# 一键重跑实验（后台子进程）
# ===========================================================================
#: 允许从界面触发的实验（**白名单**：命令行由服务端拼装，绝不接受前端传来的任意参数）
EXPERIMENT_KINDS: dict = {
    "all": {
        "label": "全量 15 组矩阵 + 伴生探针",
        "argv": ["all", "--no-db"],
        "eta_s": 120,
        "overwrites": "results/summary.csv（会先备份为 summary.prev.csv）",
    },
    "stations": {
        "label": "多站 × 多策略 18 组矩阵",
        "argv": ["stations"],
        "eta_s": 150,
        "overwrites": "results/stations/*（独立目录，不动 15 组）",
    },
    "mec": {
        "label": "MEC 端-边-云对照 12 组",
        "argv": ["mec"],
        "eta_s": 140,
        "overwrites": "results/mec/*（独立目录）",
    },
    "replicate": {
        "label": "重复实验（均值 ± 95% CI）",
        "argv": ["replicate", "--scenarios", "PEAK",
                 "--schedulers", "dynamic", "cross_node",
                 "--variants", "single", "triple", "triple_mec", "--no-db"],
        "eta_s": 200,
        "overwrites": "results/replications/*（独立目录）",
        "reps_flag": True,
    },
}


def _resolve_python() -> tuple:
    """找出**真正能跑 `run.py` 的 Python 解释器**，返回 ``(路径, 来源说明)`` 或 ``(None, 原因)``。

    为什么不能直接用 ``sys.executable``（实测踩坑）：
      * 源码运行时它是 python.exe，没问题；
      * **打包成 exe 后它是 exe 自己** —— 于是 `[exe, run.py, "all", ...]` 会被 exe 的
        argparse 当成"无法识别的参数"直接退出 2（用户看到的正是这条报错）。
    所以这里按优先级找解释器：环境变量 ``UAV_PYTHON`` → 项目根下的 `.venv` / `venv` →
    （非 frozen 时）`sys.executable`。找不到就**明确拒绝并给出可执行的建议**，
    而不是拼一条注定失败的命令行。
    """
    cands: list = []
    env = os.environ.get("UAV_PYTHON")
    if env:
        cands.append((Path(env), "环境变量 UAV_PYTHON"))
    root = OUTPUT_ROOT
    for rel in (".venv/Scripts/python.exe", "venv/Scripts/python.exe",
                ".venv/bin/python", "venv/bin/python"):
        cands.append((root / rel, f"项目虚拟环境 {rel}"))
    if not getattr(sys, "frozen", False):
        cands.append((Path(sys.executable), "当前解释器 sys.executable"))
    for p, why in cands:
        try:
            if p.is_file():
                return str(p), why
        except OSError:
            continue
    return None, (
        "找不到可用的 Python 解释器：一键重跑需要在项目目录里有 .venv（"
        f"期望 {root / '.venv' / 'Scripts' / 'python.exe'}）。"
        "exe 独立分发时没有 Python，请用源码环境跑 `python run.py ...`，"
        "或安装 Python 后设置环境变量 UAV_PYTHON 指向它。")


def _resolve_run_py() -> Optional[Path]:
    """定位 `run.py`（实验入口）。源码运行时在项目根；打包后可能被一起塞进 `_MEIPASS`。"""
    for p in (OUTPUT_ROOT / "run.py",
              Path(getattr(sys, "_MEIPASS", ".")) / "run.py",
              Path(__file__).resolve().parents[2] / "run.py"):
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


class ExperimentJob:
    """一次"一键重跑"的后台任务（子进程 + 环形日志缓冲）。

    为什么用子进程而不是线程里直接跑：`run.py all` 会跑 15 次真实仿真（约 100~150 s），
    放在面板进程里会与正在播放的 3D 仿真抢 GIL；子进程隔离后**面板照常刷新**，
    而且失败也不会把面板带崩。
    """

    #: 日志只保留最后 N 行（前端一次拉全量会越来越慢）
    MAX_LINES: int = 400

    def __init__(self, kind: str, argv: list, cwd: Path) -> None:
        self.kind = kind
        self.argv = list(argv)
        self.cwd = Path(cwd)
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = ""
        self.returncode: Optional[int] = None
        self.status: str = "running"       # running | done | failed
        self.lines: list = []
        self._lock = threading.Lock()
        self._proc = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        import subprocess

        try:
            # 子进程强制 UTF-8：run.py 会打印中文与 ✓/✗ 等符号，若继承父进程的 GBK
            # 代码页，日志会变成乱码或直接 UnicodeEncodeError（本项目踩过的老坑）。
            env = dict(os.environ)
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUTF8", "1")
            self._proc = subprocess.Popen(          # noqa: S603 - argv 由白名单拼装
                self.argv, cwd=str(self.cwd), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            with self._lock:
                self.status = "failed"
                self.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self.lines.append(f"[启动失败] {type(exc).__name__}: {exc}")
            return
        threading.Thread(target=self._pump, name="ExpJob", daemon=True).start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                self._append(line.rstrip("\n"))
        finally:
            code = self._proc.wait()
            with self._lock:
                self.returncode = code
                self.status = "done" if code == 0 else "failed"
                self.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self.lines.append(f"[结束] 退出码 {code}")

    def _append(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > self.MAX_LINES:
                del self.lines[: len(self.lines) - self.MAX_LINES]

    # ------------------------------------------------------------------
    def snapshot(self, tail: int = 80) -> dict:
        with self._lock:
            return {
                "kind": self.kind,
                "status": self.status,
                "returncode": self.returncode,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "lines": list(self.lines[-tail:]),
                "total_lines": len(self.lines),
                "running": self.status == "running",
            }

    def kill(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass


# ===========================================================================
# 仿真运行器
# ===========================================================================
class SimRun:
    """一次可视化的仿真运行（在后台线程里跑）。"""

    def __init__(self, cfg: dict, scenario: str, scheduler: str, speed: float = 1.0,
                 weather: Optional[str] = None, stations: Optional[str] = None,
                 mec: Optional[bool] = None) -> None:
        self.base_cfg = cfg
        self.scenario = scenario
        self.scheduler = scheduler
        self.speed = max(0.25, min(8.0, float(speed)))
        #: 本次运行使用的天气预设（/api/weather 可在运行中/下次启动时改它）
        self.weather_preset: str = config_preset(cfg, weather)
        #: 多基站拓扑（None/""/"1" = 单站默认；"triple" = 中心 1 + 边缘 2）
        self.stations: Optional[str] = (str(stations).strip() or None) if stations else None
        #: 是否启用 MEC 边缘计算（None = 取 config.mec.enable）
        self.mec: Optional[bool] = mec
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
                            weather_preset=self.weather_preset,
                            station_spec=self.stations, mec_enabled=self.mec)
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
            # 本次运行的多基站拓扑 / MEC 开关（null = 单站默认）
            "stations_spec": self.stations,
            "mec_enabled": self.mec,
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
        # ---- 多基站 / MEC 的"会话默认值" ----
        # 为什么需要它：3D 页面的「开始仿真」按钮只会发 /api/start（不带 stations/mec），
        # 若没有服务端默认值，用户在 UI 里永远只能跑单站。有了这两个默认值，
        # 可以用 `run.py ui --stations triple --mec` 或 `GET /api/topology?...`
        # 一次性设定，之后点按钮就是多站演示（无需改前端文件）。
        self.default_stations: Optional[str] = None
        self.default_mec: Optional[bool] = None
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
                  weather: Optional[str] = None,
                  stations: Optional[str] = None,
                  mec: Optional[bool] = None) -> dict:
        with self._lock:
            if self.run is not None and self.run.status == "running":
                return {"ok": False, "msg": "已有仿真在运行"}
            preset = config_preset(self.cfg, weather if weather is not None
                                   else self.weather_preset)
            self.weather_preset = preset
            # 未显式指定时用会话默认值（见 __init__ 的说明）
            if stations is None:
                stations = self.default_stations
            if mec is None:
                mec = self.default_mec
            self.run = SimRun(self.cfg, scenario, scheduler, speed, weather=preset,
                              stations=stations, mec=mec)
            try:
                self.run.start()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "msg": str(exc)}
            return {"ok": True, "weather_preset": preset,
                    "stations": self.run.stations, "mec": self.run.mec}

    # ------------------------------------------------------------------
    # 混沌工程 / Trace 导出（2026-11：OS 微观透视）
    # ------------------------------------------------------------------
    def chaos(self, kind: str, **kw) -> dict:
        """触发一次混沌注入。

        单站混沌：weaknet（弱网风暴）/ burst（并发洪峰）/ deadlock（死锁）。
        多基站混沌（改造建议第五章 C）：node_down（节点宕机）/
        partition（网络分区）/ split_brain（脑裂）/ recover（一键恢复）。
        """
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
            # ---- 多基站混沌 ----
            if kind == "node_down":
                sid = kw.get("station")
                return sim.inject_node_down(
                    station_id=(int(sid) if sid not in (None, "") else None),
                    duration_s=float(kw.get("duration", 20.0)))
            if kind == "partition":
                ga = _parse_id_list(kw.get("a"))
                gb = _parse_id_list(kw.get("b"))
                return sim.inject_partition(a=ga, b=gb,
                                            duration_s=float(kw.get("duration", 20.0)))
            if kind == "split_brain":
                return sim.inject_split_brain(duration_s=float(kw.get("duration", 20.0)))
            if kind == "recover":
                return sim.recover_nodes()
        except Exception as exc:  # noqa: BLE001 - 接口层兜底
            logger.exception("混沌注入失败")
            return {"ok": False, "msg": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "msg": f"未知混沌类型 {kind}（可选 weaknet/burst/deadlock/"
                                    f"node_down/partition/split_brain/recover）"}

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

    def set_topology_default(self, stations: Optional[str] = None,
                             mec: Optional[bool] = None) -> dict:
        """设定**下一次启动**用的多基站拓扑 / MEC 开关（``GET /api/topology``）。

        设计为"下次生效"而不是立刻重跑：运行中改拓扑会让正在跑的仿真状态错乱，
        而重跑又会丢掉当前进度。想立刻用新拓扑，就再点一次「开始仿真」。
        """
        with self._lock:
            if stations is not None:
                s = str(stations).strip().lower()
                self.default_stations = None if s in ("", "1", "single", "none") else s
            if mec is not None:
                self.default_mec = bool(mec)
            running = self.run is not None and self.run.status == "running"
            return {
                "ok": True,
                "stations": self.default_stations,
                "mec": self.default_mec,
                "applies": "next_run" if running else "now",
                "msg": ("已记录；当前有仿真在跑，重启仿真后生效"
                        if running else "已记录，下次点「开始仿真」生效"),
            }

    def stop_run(self) -> dict:
        with self._lock:
            if self.run is not None:
                self.run.stop()
        return {"ok": True}

    def start_experiment(self, kind: str, reps: int = 0) -> dict:
        """一键重跑实验（``GET /api/run_experiment?kind=...``）。

        三条纪律：
          ① **白名单**：kind 只能取 :data:`EXPERIMENT_KINDS` 的键，命令行由服务端拼装；
          ② **先备份再覆盖**：`run.py all` 会重写 `results/summary.csv`，所以跑之前把旧表
             另存为 `summary.prev.csv`（覆盖式实验最容易丢的就是"上一次的对照"）；
          ③ **不并发**：同一时刻只允许一个实验任务，避免两个 `run.py all` 抢同一批输出文件。

        解释器解析见 :func:`_resolve_python` —— **exe 里 `sys.executable` 是 exe 自己**，
        直接拿它拼命令行会得到 `exe run.py all`，被 exe 的 argparse 拒绝（退出码 2）。
        """
        k = str(kind or "").strip().lower()
        spec = EXPERIMENT_KINDS.get(k)
        if spec is None:
            return {"ok": False,
                    "msg": f"未知实验 {k or '(空)'}，可选：" + " / ".join(EXPERIMENT_KINDS)}
        # ① 先确认"跑得起来"：解释器与 run.py 都得真的存在，否则直接给可执行建议
        py, why = _resolve_python()
        run_py = _resolve_run_py()
        if py is None or run_py is None:
            missing = "Python 解释器" if py is None else "run.py"
            return {"ok": False, "need_env": True,
                    "msg": f"找不到{missing}，无法从界面重跑。{why if py is None else ''}"
                           " 请在项目目录用命令行执行：python run.py all --no-db"}
        with self._lock:
            job = getattr(self, "_exp_job", None)
            if job is not None and job.status == "running":
                return {"ok": False, "msg": "已有实验在跑，等它结束再试（或先点停止）"}
            # ② 备份会被覆盖的汇总表
            backup = ""
            if k == "all":
                try:
                    src = _results_dir(self.cfg) / "summary.csv"
                    if src.is_file():
                        dst = src.with_name("summary.prev.csv")
                        dst.write_bytes(src.read_bytes())
                        backup = str(dst)
                except OSError:
                    backup = ""
            # ③ 拼命令行：`<python> run.py <argv...> [--reps N]`
            root = OUTPUT_ROOT
            argv = [py, str(run_py), *spec["argv"]]
            if spec.get("reps_flag") and int(reps or 0) > 0:
                argv += ["--reps", str(int(reps))]
            job = ExperimentJob(k, argv, cwd=root)
            self._exp_job = job
        job.start()
        return {"ok": True, "kind": k, "label": spec["label"],
                "eta_s": spec["eta_s"], "overwrites": spec["overwrites"],
                "backup": backup, "python": py, "python_from": why,
                "argv": argv[2:]}

    def experiment_status(self, tail: int = 80) -> dict:
        """``GET /api/run_status`` —— 一键重跑的进度（供界面轮询）。"""
        job = getattr(self, "_exp_job", None)
        if job is None:
            return {"ok": True, "job": None}
        return {"ok": True, "job": job.snapshot(tail=tail)}

    def stop_experiment(self) -> dict:
        job = getattr(self, "_exp_job", None)
        if job is None or job.status != "running":
            return {"ok": False, "msg": "当前没有正在跑的实验"}
        job.kill()
        return {"ok": True, "msg": "已请求终止（子进程收到 terminate）"}

    def run_result_snapshot(self) -> dict:
        """「仿真结果」界面顶部的**本次运行**卡片（内存态，不是磁盘产物）。

        为什么要单独有它：磁盘上的 `charts/*.png` 与 `summary.csv` 是"上一次落盘的实验"，
        而用户刚点「开始仿真」跑的这一轮**根本不落盘**（面板走的是 time_scale 放大的演示口径）。
        两者混在一起最容易得出"图对了但数不对"的错误结论，所以这里显式分开：
        `{"has_run": bool, "status": ..., "source": "panel(演示口径)", ...}`。
        """
        run = self.run
        if run is None:
            return {"has_run": False, "msg": "还没有运行过仿真（点「▶ 开始仿真」）"}
        try:
            summary = run.state().get("summary") or {}
        except Exception:  # noqa: BLE001
            summary = {}
        sim = getattr(run, "sim", None)
        live = {}
        if sim is not None:
            try:
                # 注意：`Simulator.snapshot()` 返回的**就是**前端看到的 live 对象
                # （SimRun.state() 直接把它塞进 payload["live"]），没有嵌套一层 live 键。
                snap = sim.snapshot() or {}
                # 运行中 summary 还没算出来（指标是**跑完才结算**的），所以这里额外给一份
                # "实时计数" —— 否则「本次运行」卡片在跑的时候全是 "-"，等于没用。
                live = {
                    "stations": snap.get("stations") or [],
                    "quorum": snap.get("quorum"),
                    "mec": snap.get("mec"),
                    "sim_time": snap.get("sim_time"),
                    "duration_s": snap.get("duration_s"),
                    "counters": snap.get("counters") or {},
                    "queue": snap.get("queue") or {},
                    "system_load": snap.get("system_load"),
                    "running_count": snap.get("running_count"),
                }
            except Exception:  # noqa: BLE001
                live = {}
        keep = ("throughput", "completion_rate", "avg_wait_high_ms", "avg_wait_low_ms",
                "p95_wait_high_ms", "starvation_rate_low", "resource_utilization",
                "retry_rate", "drop_rate", "station_load_imbalance",
                "migration_count", "migration_cost_ms", "edge_offload_ratio",
                "avg_e2e_ms", "quorum_commits", "quorum_rejects", "uncommitted_tasks",
                "runtime_wall_s")
        metrics = {k: float(summary.get(k, 0.0) or 0.0) for k in keep
                   if summary.get(k) is not None}
        return {
            "has_run": True,
            "status": getattr(run, "status", ""),
            "scenario": getattr(run, "scenario", ""),
            "scheduler": getattr(run, "scheduler", ""),
            "stations": getattr(run, "stations", None),
            "mec": getattr(run, "mec", None),
            "source": "panel（演示口径：time_scale 放大，不落盘）",
            "metrics": metrics,
            "live": live,
        }

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
            if path == "/api/results":
                # GET /api/results —— 「仿真结果」界面的**全部数据源**（只读本地实验产物）
                #   {images:[{dir,title,note,images:[...]}], summary:{...}, raw:{...},
                #    replication_ci:{...}, live:{...}}
                # 设计要点：一次返回**元数据**（文件名/时间/大小），图片本身走
                # /results_img/<相对路径> 懒加载 —— 10 张图 ~1.5 MB，不该塞进 JSON。
                try:
                    live = dash.run_result_snapshot()
                    self._json({
                        "ok": True,
                        "output_root": str(OUTPUT_ROOT),
                        "images": _list_result_images(),
                        "summary": _read_summary_csv(dash.cfg),
                        "raw": _read_last_run_raw(dash.cfg),
                        "replication_ci": _read_replication_ci(dash.cfg),
                        "live": live,
                    })
                except Exception as exc:  # noqa: BLE001 - 结果界面挂了也不能 500 崩面板
                    logger.exception("读取实验结果失败")
                    self._json({"ok": False, "msg": f"{type(exc).__name__}: {exc}"}, 500)
                return
            if path.startswith("/results_img/"):
                # GET /results_img/charts/01-吞吐量对比.png
                # 两道闸：白名单目录 + 图片扩展名（见 _safe_chart_path）；失败一律 404，
                # 且**不回显服务器路径**（避免把目录结构泄露给页面）。
                rel = path[len("/results_img/"):]
                from urllib.parse import unquote

                try:
                    rel = unquote(rel)
                except Exception:  # noqa: BLE001 - 非法百分号编码
                    rel = rel
                img = _safe_chart_path(rel)
                if img is None:
                    self._json({"ok": False, "msg": "not found"}, 404)
                    return
                try:
                    body = img.read_bytes()
                except OSError:
                    self._json({"ok": False, "msg": "not found"}, 404)
                    return
                self._send(200, body, _content_type(img.name))
                return
            if path == "/api/raw_series":
                # GET /api/raw_series?name=PEAK-dynamic-...json —— 队列回放用的时序
                name = (qs.get("name") or [""])[0].strip()
                self._json(_read_raw_series(dash.cfg, name))
                return
            if path == "/api/run_experiment":
                # GET/POST /api/run_experiment?kind=all|stations|mec|replicate&reps=3
                #   「仿真结果」界面里的**一键重跑**（后台子进程，不阻塞面板）
                #   安全：kind 走白名单，命令行由服务端拼装，**不接受任意命令**
                kind = (qs.get("kind") or [""])[0].strip().lower()
                reps_raw = (qs.get("reps") or [""])[0].strip()
                try:
                    reps = int(reps_raw) if reps_raw else 0
                except ValueError:
                    reps = 0
                self._json(dash.start_experiment(kind, reps=reps))
                return
            if path == "/api/run_status":
                self._json(dash.experiment_status())
                return
            if path == "/api/stop_experiment":
                self._json(dash.stop_experiment())
                return
            if path == "/api/export_bundle":
                # GET /api/export_bundle?scope=charts|all —— 导出"报告素材包"（zip 流）
                scope = (qs.get("scope") or ["charts"])[0].strip().lower()
                try:
                    name, blob = build_export_bundle(dash.cfg, scope=scope)
                except Exception as exc:  # noqa: BLE001 - 导出失败要给人话，不要 500 崩
                    logger.exception("导出素材包失败")
                    self._json({"ok": False, "msg": f"{type(exc).__name__}: {exc}"}, 500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                # 文件名含中文：用 RFC 5987 的 filename* 编码，避免浏览器按 latin-1 乱码
                from urllib.parse import quote as _quote

                self.send_header(
                    "Content-Disposition",
                    "attachment; filename=\"bundle.zip\"; "
                    f"filename*=UTF-8''{_quote(name)}")
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(blob)
                except (BrokenPipeError, ConnectionResetError):
                    pass
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
                #   多基站：kind=node_down&station=2 / partition&a=1&b=2,3 /
                #           split_brain / recover
                kind = (qs.get("kind") or [""])[0].strip().lower()
                kw = {}
                for k in ("duration", "frac", "n", "hold"):
                    if k in qs:
                        try:
                            kw[k] = float(qs[k][0])
                        except (TypeError, ValueError):
                            pass
                for k in ("station", "a", "b"):
                    if k in qs:
                        kw[k] = qs[k][0]
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
                # 多基站拓扑：stations=triple / stations=3 / stations=1（单站默认）
                stations = (qs.get("stations") or [""])[0].strip().lower() or None
                # MEC 边缘计算：mec=1/0/true/false（缺省取 config.mec.enable）
                mec_raw = (qs.get("mec") or [""])[0].strip().lower()
                mec: Optional[bool] = None
                if mec_raw in ("1", "true", "on", "yes"):
                    mec = True
                elif mec_raw in ("0", "false", "off", "no"):
                    mec = False
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
                self._json(dash.start_run(scenario, scheduler, speed, weather=weather,
                                          stations=stations, mec=mec))
                return
            if path == "/api/topology":
                # GET /api/topology?stations=triple&mec=1
                #   设定"下一次启动"的多基站拓扑与 MEC 开关（3D 页面上的「开始仿真」
                #   按钮不带这两个参数，因此需要服务端记住它们）。
                st = (qs.get("stations") or [""])[0].strip().lower()
                mec_raw2 = (qs.get("mec") or [""])[0].strip().lower()
                mec_val: Optional[bool] = None
                if mec_raw2 in ("1", "true", "on", "yes"):
                    mec_val = True
                elif mec_raw2 in ("0", "false", "off", "no"):
                    mec_val = False
                self._json(dash.set_topology_default(
                    stations=(st if st else None), mec=mec_val))
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
          speed: float = 1.0, autostart: bool = False,
          stations: Optional[str] = None, mec: Optional[bool] = None) -> int:
    """启动面板并阻塞（Ctrl+C 退出）。返回进程退出码。

    ``stations`` / ``mec``：多基站拓扑名（如 ``"triple"``）与 MEC 开关。
    它们会被记为**会话默认值**，因此用户在 3D 页面上点「开始仿真」也能跑多站
    （页面的按钮不带这两个参数）。``None`` = 单站 + 取 config.mec.enable。
    """
    cfg = cfg or load_config()
    dash = Dashboard(cfg)
    dash.set_topology_default(stations=stations, mec=mec)
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
        dash.start_run(scenario, scheduler, speed, stations=stations, mec=mec)

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
