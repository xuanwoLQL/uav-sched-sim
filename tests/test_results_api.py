# -*- coding: utf-8 -*-
"""tests/test_results_api.py —— 「仿真结果」界面的后端接口验收

覆盖三件事（都不跑真仿真，全部 < 2 s）：

    1. **路径白名单**：`/results_img/<相对路径>` 只能读白名单目录里的图片。
       `../`、绝对路径、非图片扩展名必须一律拒绝 —— 这是防目录穿越的关键闸门，
       因为它把"本地磁盘"暴露给了浏览器，一旦漏了就是任意文件读取。
    2. **结果数据聚合**：`/api/results` 返回图片分组 + 15 组矩阵 + raw 清单 +
       置信区间 + 本次运行快照，且**产物缺失时不抛异常**（界面上给可执行提示）。
    3. **时序回放接口**：`/api/raw_series` 拒绝非法文件名、正常文件返回时序数组。

为什么用 tmp 目录而不是直接读仓库里的 results/：
    仓库里的 results/ 是**被 .gitignore 忽略的产物**，CI/别的机器上可能不存在；
    测试必须能独立跑，所以这里用 monkeypatch 把 OUTPUT_ROOT 指到 tmp_path。
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from uav_sim import dashboard as dash_mod  # noqa: E402
from uav_sim.config import load_config  # noqa: E402

#: 1×1 透明 PNG（最小合法图片，用于验证"能读出真图片"）
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AGPj6QGAAAAAElFTkSuQmCC"
)


def _make_fake_root(root: Path) -> Path:
    """造一个"像项目根"的目录：charts/ + results/{raw,replications}。"""
    (root / "charts").mkdir(parents=True)
    (root / "charts" / "01-吞吐量对比.png").write_bytes(_PNG_1PX)
    (root / "charts" / "02-平均等待.png").write_bytes(_PNG_1PX)
    (root / "charts" / "notes.txt").write_text("不是图片", encoding="utf-8")

    raw = root / "results" / "raw"
    raw.mkdir(parents=True)
    sample = {
        "scenario": "PEAK", "scheduler": "dynamic", "seed": 20261031,
        "duration_s": 90.0,
        "metrics": {"throughput": 25.86, "completion_rate": 95.17},
        "queue_samples": [
            {"ts": 0.0, "high_depth": 0, "low_depth": 1, "system_load": 0.0,
             "running_count": 0, "blocked_count": 0},
            {"ts": 0.5, "high_depth": 3, "low_depth": 5, "system_load": 1.2,
             "running_count": 8, "blocked_count": 2},
        ],
    }
    (raw / "PEAK-dynamic-20261031-20260101000000.json").write_text(
        json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    return root


def _serve(root: Path):
    """把 OUTPUT_ROOT 指到假项目根，起一个随机端口的服务，返回 (base, httpd)。

    注意：`results_dir` 也要指到假根 —— 图片走 OUTPUT_ROOT 白名单，
    但 summary.csv / raw / replications 走 `config.output.results_dir`，
    两条路径都得指过去，测试才不依赖仓库里真实存在的产物。
    """
    cfg = load_config()
    cfg.setdefault("output", {})["results_dir"] = str(root / "results")
    httpd, _dash, port = dash_mod.make_server(cfg, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", httpd


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:   # noqa: S310 - 仅本机
        return r.status, r.headers.get("Content-Type", ""), r.read()


# ---------------------------------------------------------------------------
# 1) 路径白名单（纯函数层）
# ---------------------------------------------------------------------------
def test_safe_chart_path_allows_whitelisted_image(tmp_path, monkeypatch) -> None:
    root = _make_fake_root(tmp_path)
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    p = dash_mod._safe_chart_path("charts/01-吞吐量对比.png")
    assert p is not None and p.is_file()


@pytest.mark.parametrize("bad", [
    "charts/../../config.yaml",          # 上跳
    "charts/../results/raw/x.json",      # 上跳后落到别的目录
    "/etc/passwd",                       # 绝对路径
    "C:/Windows/win.ini",                # 盘符
    "charts/notes.txt",                  # 白名单目录内但**不是图片**
    "src/uav_sim/dashboard.py",          # 白名单目录之外
    "",                                  # 空
])
def test_safe_chart_path_rejects_traversal_and_non_images(tmp_path, monkeypatch, bad) -> None:
    root = _make_fake_root(tmp_path)
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    assert dash_mod._safe_chart_path(bad) is None


# ---------------------------------------------------------------------------
# 2) 图片清单 / 矩阵 / 本次运行快照
# ---------------------------------------------------------------------------
def test_list_result_images_groups_by_section(tmp_path, monkeypatch) -> None:
    root = _make_fake_root(tmp_path)
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    sections = dash_mod._list_result_images()
    assert sections, "至少应有一个含图片的分组"
    charts = [s for s in sections if s["dir"] == "charts"]
    assert charts, "charts/ 分组应存在"
    names = [im["name"] for im in charts[0]["images"]]
    assert "01-吞吐量对比.png" in names
    assert "notes.txt" not in names, "非图片文件不得进入清单"
    for sec in sections:
        assert sec["title"] and isinstance(sec["images"], list)
        for im in sec["images"]:
            assert im["rel"].startswith(sec["dir"] + "/")
            assert "mtime" in im and "size" in im


def test_read_summary_csv_missing_is_graceful(tmp_path, monkeypatch) -> None:
    root = _make_fake_root(tmp_path)
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    cfg = {"output": {"results_dir": str(root / "results")}}
    got = dash_mod._read_summary_csv(cfg)
    assert got["exists"] is False
    assert got["rows"] == []
    assert got["columns"], "即使没有产物，也要给出列定义供前端渲染表头"


def test_read_summary_csv_parses_rows(tmp_path) -> None:
    res = tmp_path / "results"
    res.mkdir(parents=True)
    head = ("exp_id,scenario,scenario_label,scheduler,scheduler_label,seed,"
            "duration_s,uav_count,throughput,completion_rate,avg_wait_high_ms,"
            "avg_wait_low_ms,starvation_rate_low,resource_utilization,retry_rate,"
            "station_load_imbalance,migration_count,edge_offload_ratio,avg_e2e_ms,"
            "quorum_commits,quorum_rejects\n")
    (res / "summary.csv").write_text(
        head + "e1,PEAK,高并发峰值勘查,dynamic,动态权重,20261031,90,30,25.86,95.17,"
               "2102.0,1345.0,0.0,51.09,29.57,0.551,0,0.0,106.4,2327,0\n",
        encoding="utf-8-sig")
    got = dash_mod._read_summary_csv({"output": {"results_dir": str(res)}})
    assert got["exists"] is True
    assert len(got["rows"]) == 1
    row = got["rows"][0]
    assert row["scheduler"] == "dynamic"
    assert abs(row["throughput"] - 25.86) < 1e-9
    assert got["seed"] == "20261031", "顶层应给出 seed（报告里要交代控制变量）"


def test_run_result_snapshot_without_run() -> None:
    d = dash_mod.Dashboard(load_config())
    got = d.run_result_snapshot()
    assert got["has_run"] is False
    assert "msg" in got


# ---------------------------------------------------------------------------
# 3) HTTP 层
# ---------------------------------------------------------------------------
def test_http_results_endpoint_shape(tmp_path, monkeypatch) -> None:
    root = _make_fake_root(tmp_path)
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    base, httpd = _serve(root)
    try:
        code, ctype, body = _get(base + "/api/results")
        assert code == 200 and "application/json" in ctype
        j = json.loads(body.decode("utf-8"))
        assert j["ok"] is True
        for key in ("images", "summary", "raw", "replication_ci", "live", "weather"):
            assert key in j
        assert j["weather"]["source"] == "synthetic"
        assert j["weather"]["fetched_at"] is None
        assert j["summary"]["exists"] is False        # 假根里没造 summary.csv
        assert any(it["name"].startswith("PEAK-dynamic") for it in j["raw"]["items"])
        assert j["live"]["has_run"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_results_img_serves_png_and_blocks_traversal(tmp_path, monkeypatch) -> None:
    root = _make_fake_root(tmp_path)
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    base, httpd = _serve(root)
    try:
        rel = urllib.parse.quote("charts/01-吞吐量对比.png")
        code, ctype, body = _get(f"{base}/results_img/{rel}")
        assert code == 200 and ctype.startswith("image/png")
        assert body[:8] == _PNG_1PX[:8]
        for bad in ("charts/../../config.yaml", "src/uav_sim/dashboard.py",
                    "charts/notes.txt", "/etc/passwd"):
            with pytest.raises(urllib.error.HTTPError) as ei:
                _get(f"{base}/results_img/{urllib.parse.quote(bad)}")
            assert ei.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_raw_series_ok_and_rejects_bad_name(tmp_path, monkeypatch) -> None:
    root = _make_fake_root(tmp_path)
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    base, httpd = _serve(root)
    try:
        name = urllib.parse.quote("PEAK-dynamic-20261031-20260101000000.json")
        code, _ctype, body = _get(f"{base}/api/raw_series?name={name}")
        j = json.loads(body.decode("utf-8"))
        assert code == 200 and j["ok"] is True
        assert len(j["series"]) == 2
        assert j["series"][1]["high"] == 3
        assert abs(j["metrics"]["throughput"] - 25.86) < 1e-9

        # 非法名字（上跳 / 非 json）一律 ok=False，不 500
        for bad in ("../config.yaml", "x.txt", "..%2F..%2Fsecret.json"):
            code, _c, body = _get(f"{base}/api/raw_series?name={bad}")
            jj = json.loads(body.decode("utf-8"))
            assert code == 200 and jj["ok"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# 4) 导出「报告素材包」
# ---------------------------------------------------------------------------
def test_export_bundle_contains_charts_summary_and_readme(tmp_path, monkeypatch) -> None:
    import io
    import zipfile

    root = _make_fake_root(tmp_path)
    # 造一份 summary.csv，让"结论摘要"有内容
    (root / "results" / "summary.csv").write_text(
        "exp_id,scenario,scenario_label,scheduler,scheduler_label,seed,duration_s,uav_count,"
        "throughput,completion_rate,avg_wait_high_ms,avg_wait_low_ms,starvation_rate_low,"
        "resource_utilization,retry_rate,station_load_imbalance,migration_count,"
        "edge_offload_ratio,avg_e2e_ms,quorum_commits,quorum_rejects\n"
        "e1,PEAK,高并发峰值勘查,dynamic,动态权重,20261031,90,30,25.86,95.17,2102,1345,"
        "0,51.09,29.57,0.551,0,0,106.4,2327,0\n",
        encoding="utf-8-sig")
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    cfg = {"output": {"results_dir": str(root / "results")}}

    name, blob = dash_mod.build_export_bundle(cfg, scope="charts")
    assert name.endswith(".zip") and len(blob) > 100
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    assert any(n.startswith("charts/") and n.endswith(".png") for n in names)
    assert "结论摘要.md" in names and "README.txt" in names
    # 中间产物不进包（raw/task_records）
    assert not any("/raw/" in n for n in names)
    md = zf.read("结论摘要.md").decode("utf-8")
    assert "25.86" in md and "20261031" in md
    # 文件清单与"跳过"说明都要有
    readme = zf.read("README.txt").decode("utf-8")
    assert "口径提醒" in readme and "完整文件清单" in readme


def test_export_bundle_without_artifacts_still_returns_zip(tmp_path, monkeypatch) -> None:
    """一个产物都没有时也要给出**可读的** zip（含提示），而不是抛异常/空响应。"""
    import io
    import zipfile

    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    cfg = {"output": {"results_dir": str(root / "results")}}
    name, blob = dash_mod.build_export_bundle(cfg, scope="charts")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    md = zf.read("结论摘要.md").decode("utf-8")
    assert "还没有" in md and "run.py all" in md


def test_http_export_bundle_headers(tmp_path, monkeypatch) -> None:
    root = _make_fake_root(tmp_path)
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    base, httpd = _serve(root)
    try:
        req = urllib.request.Request(base + "/api/export_bundle?scope=charts")
        with urllib.request.urlopen(req, timeout=30) as r:   # noqa: S310 - 仅本机
            assert r.status == 200
            assert r.headers.get("Content-Type") == "application/zip"
            cd = r.headers.get("Content-Disposition", "")
            # 中文文件名必须走 RFC 5987 的 filename*，否则浏览器按 latin-1 显示乱码
            assert "filename*=UTF-8''" in cd
            assert len(r.read()) > 100
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# 5) 一键重跑（白名单 + 状态机；**不真的跑实验**）
# ---------------------------------------------------------------------------
def test_run_experiment_rejects_unknown_kind(tmp_path, monkeypatch) -> None:
    root = _make_fake_root(tmp_path)
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    base, httpd = _serve(root)
    try:
        code, _c, body = _get(base + "/api/run_experiment?kind=rm+-rf+/")
        j = json.loads(body.decode("utf-8"))
        assert j["ok"] is False
        assert "可选" in j["msg"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_run_experiment_whitelisted_argv_and_backup(tmp_path, monkeypatch) -> None:
    """合法 kind：命令行只能由服务端拼装（Python + run.py + 白名单参数）。"""
    root = _make_fake_root(tmp_path)
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    (root / "results" / "summary.csv").write_text("exp_id\nx\n", encoding="utf-8-sig")
    started = {}
    monkeypatch.setattr(dash_mod.ExperimentJob, "start",
                        lambda self: started.update(kind=self.kind, argv=self.argv))
    d = dash_mod.Dashboard(load_config())
    d.cfg = {"output": {"results_dir": str(root / "results")}}
    monkeypatch.setattr(dash_mod, "_results_dir", lambda cfg: root / "results")

    got = d.start_experiment("all")
    assert got["ok"] is True
    assert got["kind"] == "all"
    # 备份：旧的 summary.csv 被复制成 summary.prev.csv
    assert (root / "results" / "summary.prev.csv").is_file()
    assert got["backup"].endswith("summary.prev.csv")
    # argv：python + <root>/run.py + 白名单子命令，且不接受前端任意参数
    assert started["argv"][0].endswith(("python.exe", "python", "python3"))
    assert started["argv"][1].endswith("run.py")
    assert started["argv"][2] == "all"
    assert "--no-db" in started["argv"]
    # 并发保护：已有任务在跑时第二次调用被拒
    again = d.start_experiment("stations")
    assert again["ok"] is False and "已有实验在跑" in again["msg"]
    # 状态接口
    st = d.experiment_status()
    assert st["ok"] is True and st["job"]["kind"] == "all"
    assert st["job"]["status"] == "running"


def test_experiment_status_idle_and_stop(tmp_path, monkeypatch) -> None:
    d = dash_mod.Dashboard(load_config())
    assert d.experiment_status()["job"] is None
    assert d.stop_experiment()["ok"] is False


# ---------------------------------------------------------------------------
# 6) 解释器解析（**exe 里 sys.executable 是 exe 自己**这个坑的回归守护）
# ---------------------------------------------------------------------------
def test_resolve_python_uses_venv_in_project_root(tmp_path, monkeypatch) -> None:
    root = _make_fake_root(tmp_path)
    py = root / ".venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"stub")
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    monkeypatch.delenv("UAV_PYTHON", raising=False)
    got, why = dash_mod._resolve_python()
    assert got == str(py)
    assert ".venv" in why


def test_resolve_python_never_falls_back_to_frozen_exe(tmp_path, monkeypatch) -> None:
    """frozen 时**绝不能**把 sys.executable（= exe）当成 Python —— 那会拼出
    `exe run.py all` 并被 exe 的 argparse 拒绝（用户实测退出码 2）。"""
    root = _make_fake_root(tmp_path)          # 故意**不**造 .venv
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    monkeypatch.delenv("UAV_PYTHON", raising=False)
    monkeypatch.setattr(dash_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(dash_mod.sys, "executable",
                        str(root / "dist" / "无人机集群调度仿真系统.exe"))
    got, why = dash_mod._resolve_python()
    assert got is None, f"frozen 下不得回退到 exe：{got}"
    assert "UAV_PYTHON" in why


def test_resolve_python_honours_env_override(tmp_path, monkeypatch) -> None:
    root = _make_fake_root(tmp_path)
    py = tmp_path / "custom" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"stub")
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    monkeypatch.setenv("UAV_PYTHON", str(py))
    got, why = dash_mod._resolve_python()
    assert got == str(py) and "UAV_PYTHON" in why


def test_start_experiment_without_interpreter_is_actionable(tmp_path, monkeypatch) -> None:
    """找不到解释器时：不拼命令、不启动子进程，返回 need_env + 可执行建议。"""
    root = _make_fake_root(tmp_path)
    # 注意：先 load_config() 再伪造 sys.frozen —— config.project_root() 也会看 frozen，
    # 顺序反了会去 tmp 里找 config.yaml（踩过）。
    cfg = load_config()
    monkeypatch.setattr(dash_mod, "OUTPUT_ROOT", root)
    monkeypatch.delenv("UAV_PYTHON", raising=False)
    monkeypatch.setattr(dash_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(dash_mod.sys, "executable", str(root / "x.exe"))
    d = dash_mod.Dashboard(cfg)
    got = d.start_experiment("all")
    assert got["ok"] is False and got.get("need_env") is True
    assert "python run.py" in got["msg"]
