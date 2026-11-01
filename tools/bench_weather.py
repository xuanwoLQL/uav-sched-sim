#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""bench_weather.py —— 多运行场景测评（雨/雪/沙尘/晴）的编排与报告
=============================================================================

回答老师那句「几种运行场景的测评，系统能够稳定高效运行」：

    对 clear / rain / snow / dust 四个场景各跑 12 秒真实渲染，
    采样**帧间隔**，给出 FPS、P95/P99、最坏帧、掉帧比例，
    并记录仿真是否在推进、JS 堆是否增长（内存泄漏）。

用法：
    python tools\\bench_weather.py                 # 自动起服务、跑完出报告
    python tools\\bench_weather.py --port 8791     # 复用已启动的服务
    python tools\\bench_weather.py --no-chart      # 不出图

产物（results/weather_eval/）：
    report.json   原始逐场景结果
    report.csv    一行一个场景（可直接贴进报告）
    report.png    四场景 FPS/帧时间对比图

判定标准（写进报告即可）：
    * 稳定：四个场景 P99 < 33.4ms（不跌破 30fps），且无 page error；
    * 高效：平均 FPS ≥ 60，且坏天气相对晴朗的 FPS 下降 < 20%；
    * 无泄漏：12s 内 JS 堆增长 < 20MB。
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "results" / "weather_eval"
PY = sys.executable


def _alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _start_server(port: int) -> subprocess.Popen:
    print(f"[bench] 启动本地服务 :{port} ...")
    proc = subprocess.Popen(
        [PY, str(ROOT / "tools" / "serve.py"), "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    for _ in range(60):
        if _alive(port):
            print("[bench] 服务就绪")
            return proc
        time.sleep(0.5)
    proc.kill()
    raise SystemExit("本地服务启动超时")


def _run_bench(port: int, out_json: Path) -> dict:
    # debug=1 只是为了拿到 renderer 句柄做像素比压力测试；面板会在脚本里隐藏
    url = f"http://127.0.0.1:{port}/static/app3d.html?quality=high&debug=1"
    cmd = [
        "node", str(ROOT / "tools" / "shot.mjs"),
        "--gpu", "--no-shot",
        "--url", url,
        "--wait", "14000",
        "--eval-file", str(ROOT / "tools" / "weather_bench.js"),
        "--eval-out", str(out_json),
        "--eval-delay", "0",
    ]
    print("[bench] 开始四场景测评（每场景 12s 采样，合计约 60~80s）...")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    cost = time.perf_counter() - t0
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit(f"shot.mjs 退出码 {proc.returncode}")
    print(f"[bench] 采样完成，实耗 {cost:.1f}s")
    if not out_json.exists():
        raise SystemExit(f"没有拿到结果文件：{out_json}")
    return json.loads(out_json.read_text(encoding="utf-8"))


def _print_table(rep: dict) -> None:
    rows = rep.get("presets") or []
    print("\n" + "=" * 92)
    print("  多运行场景测评（每场景 12s 连续采样；帧间隔口径）")
    print("=" * 92)
    print(f"  {'场景':<10}{'FPS':>8}{'平均ms':>9}{'P95ms':>9}{'P99ms':>9}"
          f"{'最坏ms':>9}{'掉帧%':>8}{'仿真t':>9}{'JS堆MB':>9}")
    print("  " + "-" * 88)
    for r in rows:
        f = r.get("frame") or {}
        sim = r.get("sim") or {}
        t = sim.get("t")
        heap = r.get("heapMB")
        label = str(r.get("label") or r.get("preset"))
        t_txt = f"{t:.1f}" if isinstance(t, (int, float)) else "-"
        heap_txt = f"{heap:.1f}" if isinstance(heap, (int, float)) else "-"
        print(f"  {label:<10}"
              f"{f.get('fps', 0):>8.1f}{f.get('avgMs', 0):>9.2f}{f.get('p95', 0):>9.2f}"
              f"{f.get('p99', 0):>9.2f}{f.get('worst', 0):>9.2f}{f.get('slowPct', 0):>8.2f}"
              f"{t_txt:>9}{heap_txt:>9}")

    stress = rep.get("stress") or []
    if stress:
        print("\n  压力测试（像素比 3x = 像素量 9x：1600x1000 -> 4800x3000）：")
        print(f"  {'场景':<10}{'FPS':>8}{'平均ms':>9}{'P95ms':>9}{'P99ms':>9}{'最坏ms':>9}")
        print("  " + "-" * 54)
        for s in stress:
            print(f"  {str(s.get('preset')):<10}{s.get('fps', 0):>8.1f}{s.get('avgMs', 0):>9.2f}"
                  f"{s.get('p95', 0):>9.2f}{s.get('p99', 0):>9.2f}{s.get('worst', 0):>9.2f}")

    # ---- 自动判定 ----
    ok_stable = all((r.get("frame") or {}).get("p99", 999) < 33.4 for r in rows) and not rep.get("errors")
    fps_list = [(r.get("frame") or {}).get("fps", 0) for r in rows]
    base = fps_list[0] if fps_list else 0
    worst_drop = 0.0
    if base > 0:
        for v in fps_list[1:]:
            worst_drop = max(worst_drop, 100.0 * (base - v) / base)
    ok_fast = (min(fps_list) if fps_list else 0) >= 60 and worst_drop < 20.0
    h0, h1 = rep.get("heapStartMB"), rep.get("heapEndMB")
    ok_mem = (h0 is None or h1 is None) or (h1 - h0) < 20.0

    print("\n  判定：")
    print(f"    [{'通过' if ok_stable else '未通过'}] 稳定：四场景 P99 均 < 33.4ms（不跌破 30fps），无页面错误")
    print(f"    [{'通过' if ok_fast else '未通过'}] 高效：最低 FPS ≥ 60，坏天气相对晴朗下降 {worst_drop:.1f}%（阈值 20%）")
    print(f"    [{'通过' if ok_mem else '未通过'}] 无泄漏：整个测评期间 JS 堆 {h0} → {h1} MB（阈值 +20MB）")
    if rep.get("errors"):
        print(f"    [错误] {rep['errors']}")
    print("=" * 92)


def _write_csv(rep: dict, path: Path) -> None:
    rows = rep.get("presets") or []
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["mode", "scenario", "label", "fps", "avg_ms", "p50_ms", "p95_ms", "p99_ms",
                    "worst_ms", "slow_pct", "frames", "intensity", "visibility_km",
                    "link_penalty", "sim_time", "submitted", "done", "js_heap_mb"])
        for r in rows:
            f = r.get("frame") or {}
            wx = r.get("weather") or {}
            sim = r.get("sim") or {}
            w.writerow(["normal", r.get("preset"), r.get("label"), f.get("fps"), f.get("avgMs"),
                        f.get("p50"), f.get("p95"), f.get("p99"), f.get("worst"),
                        f.get("slowPct"), f.get("n"), wx.get("intensity"),
                        wx.get("visibility_km"), wx.get("link_penalty"),
                        sim.get("t"), sim.get("submitted"), sim.get("done"), r.get("heapMB")])
        for s in (rep.get("stress") or []):
            w.writerow(["stress_x4", s.get("preset"), "", s.get("fps"), s.get("avgMs"),
                        s.get("p50"), s.get("p95"), s.get("p99"), s.get("worst"),
                        s.get("slowPct"), s.get("n"), "", "", "", "", "", "", ""])


def _plot(rep: dict, path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        for name in ("Microsoft YaHei", "SimHei", "SimSun"):
            try:
                font_manager.findfont(name, fallback_to_default=False)
                plt.rcParams["font.sans-serif"] = [name]
                break
            except Exception:
                continue
        plt.rcParams["axes.unicode_minus"] = False

        rows = rep.get("presets") or []
        if not rows:
            return False
        labels = [r.get("label") or r.get("preset") for r in rows]
        fps = [(r.get("frame") or {}).get("fps", 0) for r in rows]
        p95 = [(r.get("frame") or {}).get("p95", 0) for r in rows]
        p99 = [(r.get("frame") or {}).get("p99", 0) for r in rows]

        fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), dpi=130)
        ax = axes[0]
        bars = ax.bar(labels, fps, color=["#3b82f6", "#0ea5e9", "#94a3b8", "#d97706"])
        ax.set_title("各运行场景平均 FPS（越高越好）")
        ax.set_ylabel("FPS")
        ax.set_ylim(0, max(140, max(fps) * 1.2 if fps else 140))
        for b, v in zip(bars, fps):
            ax.text(b.get_x() + b.get_width() / 2, v + 2, f"{v:.0f}", ha="center", fontsize=9)
        ax.grid(axis="y", alpha=0.25)

        ax = axes[1]
        x = range(len(labels))
        ax.bar([i - 0.2 for i in x], p95, width=0.4, label="P95", color="#60a5fa")
        ax.bar([i + 0.2 for i in x], p99, width=0.4, label="P99", color="#f59e0b")
        ax.axhline(33.4, color="#ef4444", linestyle="--", linewidth=1, label="30fps 阈值")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels)
        ax.set_title("帧时间分位数（越低越稳）")
        ax.set_ylabel("ms")
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.25)

        fig.suptitle("无人机集群调度仿真系统 · 多运行场景稳定性测评", fontsize=12)
        fig.tight_layout()
        fig.savefig(path)
        plt.close(fig)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[bench] 出图失败（不影响结论）：{exc}")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="多运行场景（晴/雨/雪/沙尘）稳定性与效率测评")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--no-chart", action="store_true")
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = OUT_DIR / "report.json"
    server = None
    try:
        if not _alive(args.port):
            server = _start_server(args.port)
        else:
            print(f"[bench] 复用已运行的服务 :{args.port}")
        rep = _run_bench(args.port, out_json)
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except Exception:
                server.kill()

    _print_table(rep)
    csv_path = OUT_DIR / "report.csv"
    _write_csv(rep, csv_path)
    print(f"\n[out] 原始数据：{out_json}")
    print(f"[out] 汇总表格：{csv_path}")
    if not args.no_chart:
        png = OUT_DIR / "report.png"
        if _plot(rep, png):
            print(f"[out] 对比图  ：{png}")
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
