#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
04-下载真实DEM.py —— 下载真实 SRTM 高程数据，转成本仿真用的高程场 JSON
=============================================================================

数据源：AWS Open Data 的 `elevation-tiles-prod`（Mapzen **Terrarium** 编码瓦片，
        由 NASA SRTM 等多源 DEM 合成，公开、免密钥、无需登录）。

    https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png

Terrarium 编码：每像素 RGB 三个字节合成高程（米）

    elevation_m = (R * 256 + G + B / 256) - 32768

默认目标区域：**江西德兴铜矿**（中国最大的露天铜矿之一，坑口直径约 2 km，
坑深 200~300 m，在 8 m/px 的 SRTM 网格上能清楚看出采坑与排土场）。

输出：`src/uav_sim/static/dem.json`（前端 fetch 一次即可，约 200~400 KB）

    {
      "source": "AWS elevation-tiles-prod / Terrarium (SRTM-derived)",
      "zoom": 14, "nx": 512, "nz": 380,
      "center": {"lat": 28.95, "lon": 117.77},
      "bbox": {"south":..., "north":..., "west":..., "east":...},
      "min_m": ..., "max_m": ...,     # 真实海拔（米）
      "grid_m": ...,                  # 水平分辨率（米/像素），用于把真实米制换算成场景单位
      "data_b64": "..."               # int16 小端，行优先，单位：分米（0.1 m）
    }

用法：
    .venv\\Scripts\\python.exe scripts\\04-下载真实DEM.py
    .venv\\Scripts\\python.exe scripts\\04-下载真实DEM.py --lat 28.95 --lon 117.77 --zoom 14 --out 512x380
    .venv\\Scripts\\python.exe scripts\\04-下载真实DEM.py --preview   # 只打印 ASCII 预览，不写文件
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import urllib.request
from pathlib import Path

# Windows 控制台默认 cp936，打印 ✓/✗ 之类字符会 UnicodeEncodeError，降级为 ?
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
OUT_DEFAULT = ROOT / "src" / "uav_sim" / "static" / "dem.json"
TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE = 256


# ---------------------------------------------------------------- 瓦片数学
def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple:
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n
    return x, y


def tile_to_lonlat(x: float, y: float, z: int) -> tuple:
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    return lon, math.degrees(rad)


def meters_per_tile(lat: float, z: int) -> float:
    """一个瓦片在给定纬度的大致边长（米）—— 赤道周长 40075016.686 m"""
    return 40075016.686 * math.cos(math.radians(lat)) / (2 ** z)


# ---------------------------------------------------------------- 下载解码
def fetch_tile(z: int, x: int, y: int) -> "list":
    """下载并解码一个 terrarium 瓦片为高程矩阵（单位米）。失败返回 None。"""
    from io import BytesIO

    import numpy as np
    from PIL import Image

    url = TILE_URL.format(z=z, x=x, y=y)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "UavSchedSim/1.0 (course project)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"    [跳过] {url} -> {exc}")
        return None
    img = Image.open(BytesIO(raw)).convert("RGB")
    arr = np.asarray(img, dtype=np.float64)
    return arr[:, :, 0] * 256.0 + arr[:, :, 1] + arr[:, :, 2] / 256.0 - 32768.0


def build(lat: float, lon: float, z: int, nx: int, nz: int) -> dict:
    import numpy as np

    cx, cy = lonlat_to_tile(lon, lat, z)
    mpt = meters_per_tile(lat, z)          # 米/瓦片
    mpp = mpt / TILE                         # 米/像素
    span_x_m = nx * mpp
    span_z_m = nz * mpp

    # 需要的瓦片范围（多留 1 圈余量）
    tx0 = int(math.floor(cx - span_x_m / 2 / mpt)) - 1
    tx1 = int(math.floor(cx + span_x_m / 2 / mpt)) + 1
    ty0 = int(math.floor(cy - span_z_m / 2 / mpt)) - 1
    ty1 = int(math.floor(cy + span_z_m / 2 / mpt)) + 1
    gw = (tx1 - tx0 + 1) * TILE
    gh = (ty1 - ty0 + 1) * TILE
    print(f"  瓦片 z={z}: x {tx0}..{tx1}, y {ty0}..{ty1}  ->  拼接 {gw}×{gh} px")
    print(f"  分辨率 ≈ {mpp:.1f} m/px，目标区域 ≈ {span_x_m/1000:.1f} × {span_z_m/1000:.1f} km")

    mosaic = np.full((gh, gw), np.nan, dtype=np.float64)
    ok = 0
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            t = fetch_tile(z, tx, ty)
            if t is None:
                continue
            ox = (tx - tx0) * TILE
            oy = (ty - ty0) * TILE
            mosaic[oy:oy + TILE, ox:ox + TILE] = t
            ok += 1
    if ok == 0:
        raise SystemExit("  ✗ 一个瓦片都没下载成功，检查网络")
    print(f"  ✓ 成功 {ok} 个瓦片")

    # 中心像素在拼接图里的位置
    px = (cx - tx0) * TILE
    py = (cy - ty0) * TILE

    # 目标窗口（截取 nx×nz）
    x0 = int(round(px - nx / 2))
    y0 = int(round(py - nz / 2))
    win = mosaic[y0:y0 + nz, x0:x0 + nx]
    if win.shape != (nz, nx):
        raise SystemExit(f"  ✗ 截取窗口越界：得到 {win.shape}，期望 ({nz}, {nx})")
    if np.isnan(win).any():
        med = np.nanmedian(win)
        win = np.where(np.isnan(win), med, win)
        print("  ! 有缺失像素，已用中位数填补")

    # 轻微平滑（SRTM 有少量噪声尖刺；3×3 均值一次足够，保留采坑边缘）
    k = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], dtype=np.float64) / 16.0
    pad = np.pad(win, 1, mode="edge")
    smooth = (pad[0:-2, 0:-2] * k[0, 0] + pad[0:-2, 1:-1] * k[0, 1] + pad[0:-2, 2:] * k[0, 2] +
              pad[1:-1, 0:-2] * k[1, 0] + pad[1:-1, 1:-1] * k[1, 1] + pad[1:-1, 2:] * k[1, 2] +
              pad[2:, 0:-2] * k[2, 0] + pad[2:, 1:-1] * k[2, 1] + pad[2:, 2:] * k[2, 2])

    west, north = tile_to_lonlat(tx0 + x0 / TILE, ty0 + y0 / TILE, z)
    east, south = tile_to_lonlat(tx0 + (x0 + nx) / TILE, ty0 + (y0 + nz) / TILE, z)

    return {
        "source": "AWS elevation-tiles-prod / Mapzen Terrarium (SRTM-derived，公开数据)",
        "zoom": z,
        "nx": int(nx), "nz": int(nz),
        "center": {"lat": round(lat, 5), "lon": round(lon, 5)},
        "bbox": {"south": round(south, 6), "north": round(north, 6),
                 "west": round(west, 6), "east": round(east, 6)},
        "min_m": float(np.min(smooth)), "max_m": float(np.max(smooth)),
        "grid_m": float(mpp),
        "data_b64": base64.b64encode(
            np.round(smooth * 10.0).astype("<i2").tobytes()).decode("ascii"),
        "_grid": smooth,
    }


def ascii_preview(g, w: int = 96, h: int = 40) -> str:
    import numpy as np
    nz, nx = g.shape
    yi = (np.linspace(0, nz - 1, h)).astype(int)
    xi = (np.linspace(0, nx - 1, w)).astype(int)
    sub = g[np.ix_(yi, xi)]
    lo, hi = float(np.min(sub)), float(np.max(sub))
    chars = " .:-=+*#%@"
    out = []
    for row in sub:
        line = "".join(chars[min(len(chars) - 1, int((v - lo) / max(1e-6, hi - lo) * (len(chars) - 1)))]
                       for v in row)
        out.append(line)
    return "\n".join(out) + f"\n   高程范围 {lo:.0f} ~ {hi:.0f} m"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="下载真实 DEM 并生成 dem.json")
    ap.add_argument("--lat", type=float, default=28.95, help="中心纬度（默认德兴铜矿）")
    ap.add_argument("--lon", type=float, default=117.77, help="中心经度")
    ap.add_argument("--zoom", type=int, default=14, help="瓦片层级（14 ≈ 8 m/px）")
    ap.add_argument("--out", default="512x380", help="输出网格尺寸 NXxNZ")
    ap.add_argument("--file", default=str(OUT_DEFAULT), help="输出文件")
    ap.add_argument("--preview", action="store_true", help="只打印 ASCII 预览")
    args = ap.parse_args(argv)

    nx, nz = (int(v) for v in args.out.lower().split("x"))
    print(f"目标：lat={args.lat} lon={args.lon} zoom={args.zoom} 网格 {nx}×{nz}")

    data = build(args.lat, args.lon, args.zoom, nx, nz)
    grid = data.pop("_grid")

    print(f"  高程：{data['min_m']:.1f} ~ {data['max_m']:.1f} m"
          f"（相对高差 {data['max_m'] - data['min_m']:.1f} m）")
    print(f"  分辨率 {data['grid_m']:.1f} m/px；经纬度范围 "
          f"({data['bbox']['west']:.4f},{data['bbox']['south']:.4f}) ~ "
          f"({data['bbox']['east']:.4f},{data['bbox']['north']:.4f})")
    print()
    print(ascii_preview(grid))
    print()

    if args.preview:
        return 0

    out = Path(args.file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  ✓ 已写入 {out}（{out.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
