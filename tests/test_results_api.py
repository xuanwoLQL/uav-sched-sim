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
        for key in ("images", "summary", "raw", "replication_ci", "live"):
            assert key in j
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
