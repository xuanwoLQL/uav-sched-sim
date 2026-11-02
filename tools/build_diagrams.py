#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate the project's HTML/SVG diagram set.

The output follows the Diagram Design editorial light skin already used by
docs/diagrams/01 and 02. Every file is self-contained apart from the approved
Google Fonts stylesheet.
"""

from __future__ import annotations

import html
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "diagrams"

PAPER = "#f5f5f5"
PAPER2 = "#ececec"
INK = "#2d3142"
MUTED = "#4f5d75"
SOFT = "#5a6580"
ACCENT = "#bf4520"
LINK = "#2e5aa8"
RULE = "rgba(45,49,66,0.12)"
ACCENT_TINT = "rgba(191,69,32,0.08)"

SANS = "'Geist', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif"
SERIF = "'Instrument Serif', 'Noto Serif', serif"
MONO = "'Geist Mono', 'Cascadia Mono', ui-monospace, monospace"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def text_width(value: str, size: float = 8.0, mono: bool = True) -> float:
    # Conservative budget from the diagram skill: 1em per wide CJK glyph.
    width = 0.0
    for char in value:
        width += size if ord(char) > 0x2E7F else size * (0.62 if mono else 0.60)
    return width


def svg_text(x, y, value, size=9, fill=MUTED, family=MONO, weight=400,
             anchor="middle", spacing=None, extra=""):
    spacing_attr = f' letter-spacing="{spacing}"' if spacing is not None else ""
    return (
        f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" '
        f'font-weight="{weight}" font-family="{family}" text-anchor="{anchor}"'
        f'{spacing_attr}{extra}>{esc(value)}</text>'
    )


def node(x, y, w, h, tag, title, subs=(), focal=False, kind="backend"):
    styles = {
        "backend": ("#ffffff", INK, ""),
        "store": ("rgba(45,49,66,0.05)", MUTED, ""),
        "external": ("rgba(45,49,66,0.03)", "rgba(45,49,66,0.30)", ""),
        "input": ("rgba(79,93,117,0.10)", SOFT, ""),
        "optional": ("rgba(45,49,66,0.02)", "rgba(45,49,66,0.20)", ' stroke-dasharray="4,3"'),
        "security": ("rgba(191,69,32,0.05)", "rgba(191,69,32,0.50)", ' stroke-dasharray="4,4"'),
    }
    fill, stroke, dash = styles[kind]
    if focal:
        fill, stroke, dash = ACCENT_TINT, ACCENT, ""
    tag_w = max(30, int(text_width(tag.upper(), 7, True) + 12))
    if h <= 64:
        title_y = y + 28
    elif h <= 72:
        title_y = y + 30
    elif h <= 80:
        title_y = y + 32
    elif h <= 84:
        title_y = y + 34
    else:
        title_y = y + 38
    parts = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{PAPER}"/>',
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{1.2 if focal else 1}"{dash}/>',
        f'<rect x="{x+8}" y="{y+6}" width="{tag_w}" height="12" rx="2" fill="transparent" '
        f'stroke="{stroke}" stroke-opacity="0.42" stroke-width="0.8"/>',
        svg_text(x + 8 + tag_w / 2, y + 15, tag.upper(), 7, stroke, MONO, 500, spacing="0.08em"),
        svg_text(x + w / 2, title_y, title, 12, INK, SANS, 600),
    ]
    sub_step = 14 if h >= 84 else 13
    for index, sub in enumerate(subs):
        parts.append(svg_text(x + w / 2, title_y + (16 if h <= 72 else 17) + index * sub_step,
                              sub, 9, MUTED, MONO))
    return "\n".join(parts)


def entity(x, y, w, h, name, fields, focal=False):
    header_h = 44
    fill = ACCENT_TINT if focal else "#ffffff"
    stroke = ACCENT if focal else INK
    parts = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{PAPER}"/>',
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{1.2 if focal else 1}"/>',
        f'<line x1="{x}" y1="{y+header_h}" x2="{x+w}" y2="{y+header_h}" '
        f'stroke="{RULE}" stroke-width="0.8"/>',
        f'<rect x="{x+8}" y="{y+7}" width="36" height="12" rx="2" fill="transparent" '
        f'stroke="{stroke}" stroke-opacity="0.42" stroke-width="0.8"/>',
        svg_text(x + 26, y + 16, "ENTITY", 7, stroke, MONO, 500, spacing="0.08em"),
        svg_text(x + 12, y + 32, name, 12, INK, SANS, 600, anchor="start"),
    ]
    for index, field in enumerate(fields):
        fy = y + header_h + 18 + index * 17
        parts.append(svg_text(x + 12, fy, field, 9, MUTED if not field.startswith("#") else INK,
                              MONO, 500 if field.startswith("#") else 400, anchor="start"))
    return "\n".join(parts)


def arrow_defs():
    return f"""
      <defs>
        <marker id="arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
          <polygon points="0 0, 8 3, 0 6" fill="{MUTED}"/>
        </marker>
        <marker id="arrow-accent" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
          <polygon points="0 0, 8 3, 0 6" fill="{ACCENT}"/>
        </marker>
        <marker id="arrow-link" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
          <polygon points="0 0, 8 3, 0 6" fill="{LINK}"/>
        </marker>
        <marker id="arrow-open" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
          <polyline points="0 0, 8 3, 0 6" fill="none" stroke="{MUTED}" stroke-width="1.2"/>
        </marker>
      </defs>"""


def label_h(cx, y, value, accent=False):
    width = max(30, text_width(value, 8, True) + 12)
    fill = ACCENT if accent else SOFT
    return (
        f'<rect x="{cx-width/2:.1f}" y="{y-20}" width="{width:.1f}" height="12" rx="2" fill="{PAPER}"/>'
        + svg_text(cx, y - 11, value, 8, fill, MONO, 400, spacing="0.05em")
    )


def label_v(x, cy, value, accent=False, side=1):
    width = max(32, text_width(value, 8, True) + 12)
    tx = x + 12 if side > 0 else x - 12 - width
    fill = ACCENT if accent else SOFT
    return (
        f'<rect x="{tx:.1f}" y="{cy-6}" width="{width:.1f}" height="12" rx="2" fill="{PAPER}"/>'
        + svg_text(tx + 6, cy + 3, value, 8, fill, MONO, 400, anchor="start", spacing="0.05em")
    )


def legend(y, items):
    parts = [
        f'<line x1="40" y1="{y-16}" x2="1240" y2="{y-16}" stroke="rgba(45,49,66,0.10)" stroke-width="0.8"/>',
        svg_text(40, y + 2, "LEGEND", 8, MUTED, MONO, 500, anchor="start", spacing="0.14em"),
    ]
    x = 112
    for kind, label in items:
        if kind in {"solid", "dashed", "accent", "link"}:
            color = ACCENT if kind == "accent" else LINK if kind == "link" else MUTED
            dash = ' stroke-dasharray="5,4"' if kind == "dashed" else ""
            parts.append(f'<line x1="{x}" y1="{y}" x2="{x+34}" y2="{y}" stroke="{color}" '
                         f'stroke-width="1"{dash}/>')
            x += 42
        elif kind == "box":
            parts.append(f'<rect x="{x}" y="{y-7}" width="14" height="14" rx="2" '
                         f'fill="#ffffff" stroke="{INK}" stroke-width="1"/>')
            x += 22
        elif kind == "accentbox":
            parts.append(f'<rect x="{x}" y="{y-7}" width="14" height="14" rx="2" '
                         f'fill="{ACCENT_TINT}" stroke="{ACCENT}" stroke-width="1"/>')
            x += 22
        elif kind == "dot":
            parts.append(f'<circle cx="{x+7}" cy="{y}" r="5" fill="{INK}"/>')
            x += 22
        parts.append(svg_text(x, y + 3, label, 9, SOFT, SANS))
        x += text_width(label, 9, False) + 34
    return "\n".join(parts)


def page(slug, title, eyebrow, dek, svg, notes, position, prev_file, prev_label,
         next_file, next_label):
    nav_prev = (f'<a href="{esc(prev_file)}">← {esc(prev_label)}</a>' if prev_file else
                '<span class="muted">已是第一张</span>')
    nav_next = (f'<a href="{esc(next_file)}">{esc(next_label)} →</a>' if next_file else
                '<span class="muted">已是最后一张</span>')
    note_html = []
    for label, heading, items in notes:
        lis = "".join(f"<li>{esc(item)}</li>" for item in items)
        note_html.append(
            f'<article class="card"><p class="eyebrow">{esc(label)}</p>'
            f'<h3><span class="card-dot"></span>{esc(heading)}</h3><ul>{lis}</ul></article>'
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)} · 无人机集群调度仿真</title>
<link rel="icon" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600&family=Noto+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  :root{{
    --paper:{PAPER};--paper-2:{PAPER2};--ink:{INK};--muted:{MUTED};--soft:{SOFT};
    --accent:{ACCENT};--link:{LINK};--rule:{RULE};
    --sans:{SANS};--serif:{SERIF};--mono:{MONO};
  }}
  body{{font-family:var(--sans);background:var(--paper);color:var(--ink);min-height:100vh;
    display:flex;justify-content:center;padding:2rem 2rem 3rem}}
  .frame{{width:100%;max-width:1360px;min-width:0}}
  .topnav{{display:flex;align-items:center;justify-content:space-between;gap:1rem;
    padding-bottom:1rem;margin-bottom:2rem;border-bottom:1px solid var(--rule)}}
  .topnav a,.topnav span{{font-family:var(--mono);font-size:.68rem;letter-spacing:.1em;
    text-transform:uppercase;color:var(--muted);text-decoration:none}}
  .topnav a:hover{{color:var(--accent)}}
  .topnav .counter{{color:var(--soft)}}
  .eyebrow{{font-family:var(--mono);font-size:.66rem;font-weight:500;letter-spacing:.18em;
    text-transform:uppercase;color:var(--muted);margin-bottom:.55rem}}
  h1{{font-family:var(--serif);font-size:clamp(1.6rem,2.2vw + .8rem,2.15rem);
    font-weight:400;letter-spacing:-.02em;line-height:1.12;margin-bottom:.45rem}}
  .dek{{font-size:.86rem;line-height:1.65;color:var(--soft);max-width:72ch;margin-bottom:1.5rem}}
  .diagram-container{{width:100%;max-width:100%;min-width:0;overflow-x:auto;
    border:1px solid var(--rule);border-radius:8px;background:var(--paper-2);
    padding:1rem}}
  svg{{width:100%;min-width:1280px;display:block;background:var(--paper);border-radius:4px}}
  .notes{{display:grid;grid-template-columns:1.12fr .88fr;gap:1rem;margin-top:1.25rem}}
  .card{{background:#fff;border:1px solid var(--rule);border-radius:6px;padding:1.1rem 1.2rem}}
  .card h3{{display:flex;align-items:center;gap:.55rem;font-size:.88rem;margin-bottom:.7rem}}
  .card-dot{{width:7px;height:7px;border-radius:50%;background:var(--accent);flex:none}}
  .card ul{{list-style:none;display:grid;gap:.45rem}}
  .card li{{font-size:.76rem;line-height:1.55;color:var(--soft);padding-left:.85rem;position:relative}}
  .card li::before{{content:"";position:absolute;left:0;top:.55em;width:4px;height:4px;
    border-radius:50%;background:var(--muted);opacity:.55}}
  footer{{display:flex;justify-content:space-between;gap:1rem;align-items:center;
    margin-top:1.5rem;padding-top:.9rem;border-top:1px solid var(--rule);
    font-family:var(--mono);font-size:.64rem;letter-spacing:.06em;color:var(--muted)}}
  footer .pager{{display:flex;gap:1.2rem}}
  footer a{{color:var(--muted);text-decoration:none}}footer a:hover{{color:var(--accent)}}
  .muted{{color:var(--soft)}}
  @media (max-width:760px){{
    body{{padding:1.2rem .8rem 2rem}}.notes{{grid-template-columns:1fr}}
    footer{{align-items:flex-start;flex-direction:column}}
  }}
  @media print{{
    body{{padding:0;background:#fff}}.diagram-container{{overflow-x:visible;border:0;padding:0;background:#fff}}
    svg{{min-width:0}}.topnav,footer{{display:none}}.notes{{break-inside:avoid}}
  }}
</style>
</head>
<body>
<div class="frame">
  <nav class="topnav">
    <a href="00-图册索引.html">← 系统图册</a>
    <span class="counter">{esc(position)}</span>
  </nav>
  <p class="eyebrow">{esc(eyebrow)}</p>
  <h1>{esc(title)}</h1>
  <p class="dek">{esc(dek)}</p>
  <div class="diagram-container">
{svg}
  </div>
  <div class="notes">
    {''.join(note_html)}
  </div>
  <footer>
    <span>UAV Scheduler Simulation · Operating Systems Course Project</span>
    <span class="pager">{nav_prev}{nav_next}</span>
  </footer>
</div>
</body>
</html>
"""


def architecture_svg():
    p = [f'<svg viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="arch-title arch-desc">',
         '<title id="arch-title">无人机集群调度仿真系统分层架构</title>',
         '<desc id="arch-desc">展示现场无人机线程与链路监控、仿真内核的队列限流和动态权重调度，以及实验评测、持久化和三维监控台之间的调用关系。</desc>',
         arrow_defs(),
         f'<rect width="1280" height="720" fill="{PAPER}"/>']
    # zones
    for x, w, label in [(40, 300, "FIELD / EDGE"), (372, 432, "SIMULATION CORE"), (836, 404, "EVALUATION / EXPERIENCE")]:
        p.append(f'<rect x="{x}" y="120" width="{w}" height="460" rx="8" fill="rgba(45,49,66,0.02)" stroke="{RULE}" stroke-width="0.8"/>')
        p.append(f'<rect x="{x+12}" y="128" width="122" height="14" rx="2" fill="{PAPER}"/>')
        p.append(svg_text(x + 73, 139, label, 8, SOFT, MONO, 500, spacing="0.12em"))
    # arrows
    p += [
        f'<line x1="308" y1="206" x2="404" y2="206" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_h(356, 206, "SUBMIT"),
        f'<path d="M308 462 H356 Q364 462 364 470 V492 Q364 500 372 500 H404" fill="none" stroke="{ACCENT}" stroke-width="1.2" marker-end="url(#arrow-accent)"/>',
        label_v(364, 480, "LINK", True, 1),
        f'<path d="M772 206 H788 V492 Q788 500 780 500 H772" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(788, 350, "STEP", False, 1),
        f'<path d="M772 500 H804 V358 Q804 350 796 350 H772" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(804, 430, "ADMIT", False, 1),
        f'<path d="M772 532 H820 V214 Q820 206 828 206 H868" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(820, 320, "TRACE", False, 1),
        f'<line x1="1038" y1="248" x2="1038" y2="308" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(1038, 278, "WRITE", False, 1),
        f'<line x1="1038" y1="392" x2="1038" y2="452" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(1038, 422, "QUERY", False, 1),
    ]
    # nodes
    p += [
        node(72, 164, 236, 84, "EDGE", "无人机生产者线程 ×30", ("UAVWorker.run()", "TraceEvent → Task"), kind="input"),
        node(72, 420, 236, 84, "LINK", "链路状态监控", ("LinkMonitor.health_at()", "delay · loss · RSSI"), kind="input"),
        node(404, 164, 368, 84, "QUEUE", "多级就绪队列", ("MultiLevelQueue · high / low", "capacity 5000 · congestion")),
        node(404, 308, 368, 84, "LIMIT", "资源限流与配额", ("ResourceLimiter.try_admit()", "8 slots · per-UAV quota 2")),
        node(404, 452, 368, 96, "POLICY", "动态权重调度器", ("W = base × (w0 + αQ + βW + γL) × (1 − δS)", "饥饿补偿 · 弱网降权 · 自适应时间片"), focal=True),
        node(868, 164, 340, 84, "EVAL", "实验评测", ("ExperimentRunner.run_one()", "15 组矩阵 · 复现实验")),
        node(868, 308, 340, 84, "STORE", "指标与持久化", ("compute_metrics() · DBWriter", "MySQL 8 · CSV · JSON"), kind="store"),
        node(868, 452, 340, 84, "UI", "3D 监控台", ("pywebview + WebView2", "Three.js · /api/state 120ms")),
    ]
    p.append(legend(686, [("solid", "调用 / 数据流"), ("dashed", "跨层旁路"), ("accent", "链路感知关键路径"), ("accentbox", "算法核心")]))
    p.append("</svg>")
    return "\n".join(p)


def data_flow_svg():
    p = [f'<svg viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="flow-title flow-desc">',
         '<title id="flow-title">任务从生成到发布的数据流</title>',
         '<desc id="flow-desc">展示无人机任务生成、双队列入队、动态权重调度、限流准入、线程池执行、弱网重试、指标结算和结果发布的完整链路。</desc>',
         arrow_defs(),
         f'<rect width="1280" height="720" fill="{PAPER}"/>']
    lanes = [("FIELD", 120), ("READY QUEUES", 240), ("CORE", 360), ("EVALUATION", 480)]
    for index, (label, y) in enumerate(lanes):
        if index % 2 == 0:
            p.append(f'<rect x="200" y="{y}" width="1040" height="120" fill="rgba(45,49,66,0.018)"/>')
        p.append(f'<line x1="40" y1="{y}" x2="1240" y2="{y}" stroke="rgba(45,49,66,0.12)" stroke-width="0.8"/>')
        p.append(svg_text(112, y + 62, label, 8, MUTED, MONO, 500, spacing="0.12em"))
    p.append(f'<line x1="200" y1="120" x2="200" y2="600" stroke="rgba(45,49,66,0.20)" stroke-width="1"/>')
    steps = [(260, "01", "GENERATE"), (420, "02", "ENQUEUE"), (580, "03", "SCHEDULE"),
             (740, "04", "EXECUTE"), (900, "05", "SETTLE"), (1060, "06", "PUBLISH")]
    for cx, number, label in steps:
        focal = number == "03"
        p.append(f'<rect x="{cx-16}" y="14" width="32" height="16" rx="8" fill="{ACCENT_TINT if focal else "rgba(45,49,66,0.12)"}"/>')
        p.append(svg_text(cx, 26, number, 8, ACCENT if focal else INK, MONO, 500))
        p.append(svg_text(cx, 48, label, 7, ACCENT if focal else MUTED, MONO, 500, spacing="0.10em"))
    # arrows
    p += [
        f'<path d="M320 168 H412 Q420 168 420 176 V256" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(420, 216, "SUBMIT"),
        f'<path d="M480 288 H572 Q580 288 580 296 V376" fill="none" stroke="{ACCENT}" stroke-width="1.2" marker-end="url(#arrow-accent)"/>',
        label_v(580, 336, "STEP", True),
        f'<line x1="640" y1="408" x2="680" y2="408" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_h(660, 408, "ADMIT"),
        f'<line x1="800" y1="408" x2="840" y2="408" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_h(820, 408, "DISPATCH"),
        f'<line x1="900" y1="440" x2="900" y2="496" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(900, 468, "METRIC"),
        f'<line x1="960" y1="528" x2="1000" y2="528" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_h(980, 528, "PUBLISH"),
        f'<path d="M900 376 V184 Q900 176 892 176 H820" fill="none" stroke="{MUTED}" stroke-width="1" stroke-dasharray="5,4" marker-end="url(#arrow)"/>',
        label_v(900, 280, "RETRY"),
        f'<path d="M680 168 H496 Q488 168 488 176 V280 Q488 288 480 288" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(488, 224, "REQUEUE", False, -1),
    ]
    p += [
        node(200, 136, 120, 64, "FIELD", "生成任务", ("TraceEvent", "make_task()"), kind="input"),
        node(360, 256, 120, 64, "QUEUE", "双层就绪队列", ("HIGH / LOW", "congestion")),
        node(520, 376, 120, 64, "POLICY", "动态权重", ("W = f(Q,W,L,S)", "hard boost"), focal=True),
        node(680, 376, 120, 64, "LIMIT", "限流准入", ("try_admit()", "8 slots")),
        node(840, 376, 120, 64, "POOL", "线程池执行", ("dispatch()", "upload task")),
        node(680, 136, 120, 64, "RETRY", "弱网重试", ("backoff 2s", "CACHED"), kind="optional"),
        node(840, 496, 120, 64, "METRIC", "指标结算", ("24 metrics", "RunResult")),
        node(1000, 496, 120, 64, "REPORT", "结果发布", ("summary.csv", "charts/*.png")),
    ]
    p.append(legend(686, [("solid", "任务推进"), ("dashed", "弱网回退"), ("accent", "核心决策"), ("accentbox", "动态权重")]))
    p.append("</svg>")
    return "\n".join(p)


def state_svg():
    p = [f'<svg viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="state-title state-desc">',
         '<title id="state-title">任务生命周期状态机</title>',
         '<desc id="state-desc">展示任务从提交、就绪、运行到完成、弱网阻塞、重试等待、本地缓存、失败和取消的状态转换。</desc>',
         arrow_defs(),
         f'<rect width="1280" height="720" fill="{PAPER}"/>']
    # transitions first
    p += [
        f'<line x1="100" y1="200" x2="180" y2="200" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_h(140, 200, "SUBMIT"),
        f'<line x1="380" y1="200" x2="500" y2="200" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_h(440, 200, "DISPATCH"),
        f'<line x1="700" y1="200" x2="820" y2="200" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_h(760, 200, "UPLOAD OK"),
        f'<line x1="600" y1="240" x2="600" y2="360" stroke="{ACCENT}" stroke-width="1.2" marker-end="url(#arrow-accent)"/>',
        label_v(600, 300, "WEAK NET", True),
        f'<line x1="500" y1="400" x2="380" y2="400" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_h(440, 400, "RETRY ≤3"),
        f'<line x1="280" y1="360" x2="280" y2="240" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(280, 300, "BACKOFF 2S"),
        f'<path d="M600 440 V480 H288 Q280 480 280 488 V520" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(456, 480, "RETRY BUDGET"),
        f'<line x1="280" y1="520" x2="280" y2="440" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(280, 480, "NET RECOVERED"),
        f'<path d="M680 240 V320 H920 V360" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(800, 320, "WATCHDOG 3S"),
    ]
    # states
    p += [
        f'<circle cx="100" cy="200" r="6" fill="{INK}"/>',
        node(180, 160, 200, 80, "READY", "就绪", ("enqueue", "wait_ms 累计")),
        node(500, 160, 200, 80, "RUNNING", "运行", ("dispatch", "time slice")),
        node(820, 160, 200, 80, "DONE", "完成", ("upload ok", "finish_ts")),
        node(500, 360, 200, 80, "BLOCKED", "弱网阻塞", ("failure", "CACHED 前"), focal=True),
        node(180, 360, 200, 80, "RETRY", "退避等待", ("backoff 2s", "retry ≤3"), kind="optional"),
        node(180, 520, 200, 80, "CACHE", "本地缓存", ("network lost", "no slot"), kind="store"),
        node(820, 360, 200, 80, "FAILED", "失败", ("watchdog", "DROPPED"), kind="security"),
        node(820, 520, 200, 80, "CANCELLED", "取消", ("stop signal", "terminal"), kind="optional"),
    ]
    p.append(f'<rect x="1036" y="504" width="204" height="112" rx="6" fill="rgba(45,49,66,0.02)" stroke="{RULE}" stroke-width="0.8"/>')
    p.append(svg_text(1052, 530, "ANY ACTIVE STATE", 8, MUTED, MONO, 500, anchor="start", spacing="0.10em"))
    p.append(svg_text(1052, 554, "→ CANCELLED [STOP]", 10, SOFT, SANS, 500, anchor="start"))
    p.append(svg_text(1052, 578, "取消是跨状态事件，不逐条画线。", 9, SOFT, SANS, 400, anchor="start"))
    p.append(legend(686, [("dot", "起始"), ("box", "稳定状态"), ("accentbox", "弱网关键态"), ("dashed", "退避/缓存"), ("solid", "状态转换")]))
    p.append("</svg>")
    return "\n".join(p)


def decision_svg():
    p = [f'<svg viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="decision-title decision-desc">',
         '<title id="decision-title">动态权重调度决策流程</title>',
         '<desc id="decision-desc">展示动态权重调度器先检查饥饿硬补偿，再计算四因子权重，并据候选权重决定派发或回队。</desc>',
         arrow_defs(),
         f'<rect width="1280" height="720" fill="{PAPER}"/>']
    # arrows first
    p += [
        f'<line x1="560" y1="76" x2="560" y2="116" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        f'<line x1="560" y1="180" x2="560" y2="220" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        f'<line x1="460" y1="270" x2="400" y2="270" stroke="{ACCENT}" stroke-width="1.2" marker-end="url(#arrow-accent)"/>',
        label_h(430, 270, "YES", True),
        f'<line x1="560" y1="330" x2="560" y2="380" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(560, 356, "NO"),
        f'<line x1="560" y1="444" x2="560" y2="500" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(560, 474, "COMPUTE"),
        f'<path d="M660 560 H1020 V310" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(840, 560, "YES"),
        f'<path d="M460 560 H308 Q300 560 300 568 V600" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_h(380, 560, "NO"),
        f'<path d="M300 310 V350 H940 V310" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
    ]
    # shapes
    p += [
        f'<rect x="480" y="32" width="160" height="44" rx="20" fill="#ffffff" stroke="{INK}" stroke-width="1"/>',
        svg_text(560, 59, "START", 10, INK, MONO, 500, spacing="0.10em"),
        node(420, 116, 280, 64, "INPUT", "采集四因子", ("Q 拥堵 · W 等待 · L 链路 · S 负载",)),
        f'<path d="M560 220 L660 270 L560 320 L460 270 Z" fill="#ffffff" stroke="{INK}" stroke-width="1"/>',
        svg_text(560, 266, "饥饿阈值已到？", 10, INK, SANS, 600),
        node(200, 230, 200, 80, "PROMOTE", "低优硬补偿", ("bypass weight", "boost low"), focal=True),
        node(880, 230, 200, 80, "DISPATCH", "派发", ("try_admit", "slot")),
        node(420, 380, 280, 64, "WEIGHT", "计算动态权重", ("base × (w0+αQ+βW+γL) × (1−δS)",)),
        f'<path d="M560 500 L660 560 L560 620 L460 560 Z" fill="#ffffff" stroke="{INK}" stroke-width="1"/>',
        svg_text(560, 556, "候选权重最高？", 10, INK, SANS, 600),
        node(200, 600, 200, 72, "REQUEUE", "回队等待", ("push_front / back", "next tick"), kind="optional"),
    ]
    p.append(legend(686, [("accent", "硬补偿优先路径"), ("solid", "常规决策流"), ("box", "动作"), ("accentbox", "关键决策结果")]))
    p.append("</svg>")
    return "\n".join(p)


def sequence_svg():
    actors = [(100, "UAV"), (340, "QUEUE"), (580, "SCHEDULER"), (820, "LIMITER"), (1060, "POOL")]
    p = [f'<svg viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="seq-title seq-desc">',
         '<title id="seq-title">调度执行时序</title>',
         '<desc id="seq-desc">展示无人机提交任务、队列通知调度器、权重计算、限流准入、线程池执行、完成确认和弱网回队的时序。</desc>',
         arrow_defs(),
         f'<rect width="1280" height="720" fill="{PAPER}"/>']
    # lifelines
    for cx, _ in actors:
        p.append(f'<line x1="{cx}" y1="108" x2="{cx}" y2="650" stroke="rgba(45,49,66,0.20)" stroke-width="1" stroke-dasharray="3,3"/>')
    # activation bars
    for cx, y, h in [(100, 148, 60), (340, 150, 480), (580, 190, 410), (820, 290, 60), (1060, 380, 80)]:
        p.append(f'<rect x="{cx-4}" y="{y}" width="8" height="{h}" fill="rgba(45,49,66,0.06)" stroke="{MUTED}" stroke-width="0.8"/>')
    # fragment
    p += [
        f'<rect x="300" y="460" width="820" height="190" rx="4" fill="rgba(45,49,66,0.02)" stroke="rgba(45,49,66,0.22)" stroke-width="1"/>',
        f'<rect x="300" y="460" width="52" height="16" rx="2" fill="{PAPER}" stroke="rgba(45,49,66,0.22)" stroke-width="1"/>',
        svg_text(326, 472, "ALT", 8, MUTED, MONO, 500, spacing="0.12em"),
        svg_text(312, 492, "[upload ok]", 8, MUTED, MONO, 400, anchor="start", spacing="0.04em"),
        f'<line x1="308" y1="570" x2="1112" y2="570" stroke="rgba(45,49,66,0.20)" stroke-width="1" stroke-dasharray="4,3"/>',
        svg_text(312, 588, "[weak network]", 8, MUTED, MONO, 400, anchor="start", spacing="0.04em"),
    ]
    # messages
    msgs = [
        (160, 100, 340, "SUBMIT", False, False),
        (200, 340, 580, "READY", False, False),
        (300, 580, 820, "TRY_ADMIT", False, False),
        (340, 820, 580, "PERMIT", False, True),
        (390, 580, 1060, "DISPATCH", False, False),
        (520, 1060, 580, "COMPLETE", True, True),
        (550, 580, 340, "ACK", False, False),
        (610, 1060, 580, "UPLOAD FAIL", False, False),
        (640, 580, 340, "REQUEUE", False, True),
    ]
    for y, x1, x2, label, accent, dashed in msgs:
        color = ACCENT if accent else MUTED
        marker = "arrow-accent" if accent else "arrow"
        dash = ' stroke-dasharray="5,4"' if dashed else ""
        p.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{color}" '
                 f'stroke-width="{1.2 if accent else 1}"{dash} marker-end="url(#{marker})"/>')
        mid = (x1 + x2) / 2
        p.append(label_h(mid, y, label, accent))
    # self message
    p += [
        f'<path d="M580 250 H620 V270 H584" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        svg_text(628, 264, "WEIGHT(Q,W,L,S)", 8, SOFT, MONO, 400, anchor="start", spacing="0.04em"),
    ]
    # actor boxes
    for cx, label in actors:
        p.append(f'<rect x="{cx-80}" y="60" width="160" height="48" rx="6" fill="#ffffff" stroke="{INK}" stroke-width="1"/>')
        p.append(svg_text(cx, 89, label, 10, INK, MONO, 500, spacing="0.08em"))
    p.append(legend(686, [("solid", "同步调用"), ("dashed", "返回 / 回退"), ("accent", "成功完成"), ("box", "参与者")]))
    p.append("</svg>")
    return "\n".join(p)


def deploy_node(x, y, w, h, tag, title, artifacts, replicas="x1", focal=False, kind="backend"):
    base = node(x, y, w, h, tag, title, (), focal=focal, kind=kind)
    parts = [base]
    chip_y = y + 48
    for index, (name, version) in enumerate(artifacts):
        cy = chip_y + index * 30
        parts.append(f'<rect x="{x+10}" y="{cy}" width="{w-20}" height="24" rx="4" '
                     f'fill="rgba(45,49,66,0.05)" stroke="{MUTED}" stroke-width="0.8"/>')
        parts.append(svg_text(x + 18, cy + 16, name, 9, INK, MONO, 500, anchor="start"))
        parts.append(svg_text(x + w - 18, cy + 16, version, 8, MUTED, MONO, 400, anchor="end"))
    parts.append(f'<rect x="{x+w-42}" y="{y+7}" width="34" height="12" rx="2" fill="{PAPER}" stroke="{RULE}" stroke-width="0.8"/>')
    parts.append(svg_text(x + w - 25, y + 16, replicas, 7, MUTED, MONO, 500, spacing="0.08em"))
    return "\n".join(parts)


def deployment_svg():
    p = [f'<svg viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="deploy-title deploy-desc">',
         '<title id="deploy-title">多基站部署拓扑</title>',
         '<desc id="deploy-desc">展示边缘无人机代理、控制面仿真服务与桌面监控台、MySQL 和结果文件存储之间的网络边界和协议。</desc>',
         arrow_defs(),
         f'<rect width="1280" height="720" fill="{PAPER}"/>']
    zones = [(40, 320, "EDGE SITE"), (400, 420, "CONTROL PLANE"), (860, 380, "DATA PLANE")]
    for x, w, label in zones:
        p.append(f'<rect x="{x}" y="100" width="{w}" height="480" rx="8" fill="rgba(45,49,66,0.02)" '
                 f'stroke="rgba(45,49,66,0.20)" stroke-width="1" stroke-dasharray="4,4"/>')
        p.append(f'<rect x="{x+12}" y="108" width="116" height="14" rx="2" fill="{PAPER}"/>')
        p.append(svg_text(x + 70, 119, label, 8, SOFT, MONO, 500, spacing="0.12em"))
    # paths
    p += [
        f'<line x1="328" y1="212" x2="440" y2="212" stroke="{LINK}" stroke-width="1" marker-end="url(#arrow-link)"/>',
        label_h(384, 212, "HTTPS:443"),
        f'<path d="M328 392 H420 Q428 392 428 384 V260 Q428 252 436 252 H440" fill="none" stroke="{LINK}" stroke-width="1" marker-end="url(#arrow-link)"/>',
        label_v(428, 330, "HTTPS:443", False, -1),
        f'<line x1="780" y1="220" x2="900" y2="220" stroke="{LINK}" stroke-width="1" marker-end="url(#arrow-link)"/>',
        label_h(840, 220, "TLS:3306"),
        f'<line x1="780" y1="410" x2="900" y2="410" stroke="{LINK}" stroke-width="1" marker-end="url(#arrow-link)"/>',
        label_h(840, 410, "FS WRITE"),
        f'<line x1="610" y1="360" x2="610" y2="280" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        label_v(610, 320, "HTTP:127.0.0.1"),
        f'<line x1="1050" y1="280" x2="1050" y2="360" stroke="{MUTED}" stroke-width="1" stroke-dasharray="5,4" marker-end="url(#arrow)"/>',
        label_v(1050, 320, "SUMMARY"),
    ]
    # nodes
    p += [
        deploy_node(72, 160, 256, 104, "EDGE", "矿区边缘节点 A", (("uav-agent", "v1.0"),), "x30"),
        deploy_node(72, 340, 256, 104, "EDGE", "矿区边缘节点 B", (("uav-agent", "v1.0"),), "x30"),
        deploy_node(440, 160, 340, 120, "VM", "控制面仿真服务", (("simulator", "v1.0"), ("webview-ui", "v1.0")), "x1", focal=True),
        deploy_node(440, 360, 340, 104, "DESKTOP", "三维监控台", (("desktop", "v1.0"),), "x1"),
        deploy_node(900, 160, 300, 120, "MANAGED", "MySQL 8", (("uav_sched_sim", "schema-v1"),), "x1", focal=True, kind="store"),
        deploy_node(900, 360, 300, 104, "STORE", "结果与图表目录", (("results / charts", "runtime"),), "x1", kind="store"),
    ]
    p.append(legend(686, [("dashed", "网络边界"), ("link", "跨边界路径"), ("box", "主机 / 服务"), ("accentbox", "单点关键组件")]))
    p.append("</svg>")
    return "\n".join(p)


def er_svg():
    p = [f'<svg viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="er-title er-desc">',
         '<title id="er-title">仿真数据库实体关系</title>',
         '<desc id="er-desc">展示实验主表与指标、任务明细、队列采样、链路采样四个子表之间的一对多关系。</desc>',
         arrow_defs(),
         f'<rect width="1280" height="720" fill="{PAPER}"/>']
    # relationships
    p += [
        f'<path d="M480 280 H400 V210 H320" fill="none" stroke="{MUTED}" stroke-width="1"/>',
        svg_text(490, 270, "1", 9, MUTED, MONO, 500, anchor="start"),
        svg_text(330, 198, "N", 9, MUTED, MONO, 500, anchor="start"),
        f'<path d="M480 380 H400 V510 H320" fill="none" stroke="{MUTED}" stroke-width="1"/>',
        svg_text(490, 398, "1", 9, MUTED, MONO, 500, anchor="start"),
        svg_text(330, 500, "N", 9, MUTED, MONO, 500, anchor="start"),
        f'<path d="M800 280 H880 V210 H960" fill="none" stroke="{MUTED}" stroke-width="1"/>',
        svg_text(790, 270, "1", 9, MUTED, MONO, 500, anchor="end"),
        svg_text(948, 198, "N", 9, MUTED, MONO, 500, anchor="end"),
        f'<path d="M800 380 H880 V510 H960" fill="none" stroke="{MUTED}" stroke-width="1"/>',
        svg_text(790, 398, "1", 9, MUTED, MONO, 500, anchor="end"),
        svg_text(948, 500, "N", 9, MUTED, MONO, 500, anchor="end"),
    ]
    p += [
        entity(40, 120, 280, 180, "exp_metric", ("# id  BIGINT", "→ exp_id  VARCHAR(40)", "metric_name  VARCHAR(64)", "metric_value  DOUBLE", "unit · scope")),
        entity(40, 420, 280, 180, "task_record", ("# id  BIGINT", "→ exp_id  VARCHAR(40)", "task_id · priority", "state · wait_ms", "retry_count · cached")),
        entity(480, 230, 320, 200, "experiment", ("# exp_id  VARCHAR(40)", "scenario · scheduler", "seed · duration_s", "uav_count · status", "started_at · config_json"), focal=True),
        entity(960, 120, 280, 180, "queue_sample", ("# id  BIGINT", "→ exp_id  VARCHAR(40)", "ts  DOUBLE", "high_depth · low_depth", "system_load · weights")),
        entity(960, 420, 280, 180, "link_sample", ("# id  BIGINT", "→ exp_id  VARCHAR(40)", "ts · uav_id", "delay_ms · loss_rate", "rssi_dbm · health")),
    ]
    p.append(legend(686, [("box", "实体"), ("solid", "一对多关系"), ("accentbox", "聚合根"), ("dot", "主键 # / 外键 →")]))
    p.append("</svg>")
    return "\n".join(p)


def evaluation_svg():
    p = [f'<svg viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="eval-title eval-desc">',
         '<title id="eval-title">实验评测流水线</title>',
         '<desc id="eval-desc">展示工况标定、轨迹生成、多策略运行、时序采样、指标计算、持久化与图表报告发布的实验流水线。</desc>',
         arrow_defs(),
         f'<rect width="1280" height="720" fill="{PAPER}"/>']
    lanes = [("DESIGN", 120), ("SIMULATION", 204), ("OBSERVATION", 288), ("STORE", 372), ("REPORT", 456)]
    for index, (label, y) in enumerate(lanes):
        if index % 2 == 0:
            p.append(f'<rect x="200" y="{y}" width="1040" height="84" fill="rgba(45,49,66,0.018)"/>')
        p.append(f'<line x1="40" y1="{y}" x2="1240" y2="{y}" stroke="rgba(45,49,66,0.12)" stroke-width="0.8"/>')
        p.append(svg_text(112, y + 46, label, 8, MUTED, MONO, 500, spacing="0.12em"))
    p.append(f'<line x1="200" y1="120" x2="200" y2="540" stroke="rgba(45,49,66,0.20)" stroke-width="1"/>')
    steps = [(260, "01", "CALIBRATE"), (420, "02", "TRACE"), (580, "03", "RUN"),
             (740, "04", "SAMPLE"), (900, "05", "METRIC"), (1060, "06", "PUBLISH")]
    for cx, number, label in steps:
        p.append(f'<rect x="{cx-16}" y="14" width="32" height="16" rx="8" fill="rgba(45,49,66,0.12)"/>')
        p.append(svg_text(cx, 26, number, 8, INK, MONO, 500))
        p.append(svg_text(cx, 48, label, 7, MUTED, MONO, 500, spacing="0.10em"))
    # arrows
    p += [
        f'<line x1="320" y1="162" x2="360" y2="162" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        f'<path d="M480 162 H572 Q580 162 580 170 V214" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        f'<path d="M580 278 V298" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        f'<line x1="800" y1="330" x2="840" y2="330" stroke="{ACCENT}" stroke-width="1.2" marker-end="url(#arrow-accent)"/>',
        f'<line x1="900" y1="362" x2="900" y2="382" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
        f'<path d="M960 414 H1052 Q1060 414 1060 422 V466" fill="none" stroke="{MUTED}" stroke-width="1" marker-end="url(#arrow)"/>',
    ]
    p += [
        node(200, 130, 120, 64, "DESIGN", "工况标定", ("calibrate()", "load factor")),
        node(360, 130, 120, 64, "DESIGN", "离线轨迹", ("generate_trace()", "fixed seed")),
        node(520, 214, 120, 64, "SIM", "多策略运行", ("run_one()", "15 / 18 / 12")),
        node(680, 298, 120, 64, "OBSERVE", "时序采样", ("queue_sample", "link_sample")),
        node(840, 298, 120, 64, "METRIC", "指标计算", ("24 metrics", "RunResult"), focal=True),
        node(840, 382, 120, 64, "STORE", "持久化", ("MySQL · CSV", "JSON")),
        node(1000, 466, 120, 64, "REPORT", "图表与报告", ("charts/*.png", "summary.csv")),
    ]
    p.append(legend(686, [("solid", "流水线推进"), ("accent", "核心评测节点"), ("box", "阶段"), ("accentbox", "指标中枢")]))
    p.append("</svg>")
    return "\n".join(p)


def index_page():
    entries = [
        ("01", "ARCHITECTURE", "系统分层架构图", "现场线程、调度内核、评测存储和 3D 监控台的全景关系。", "01-系统框架图.html"),
        ("02", "DATA FLOW", "任务调度数据流图", "从任务生成、双队列入队、动态权重、限流执行到指标发布。", "02-数据流详细逻辑图.html"),
        ("03", "STATE MACHINE", "任务生命周期状态机", "READY / RUNNING / BLOCKED / RETRY / CACHED / DONE / FAILED / CANCELLED。", "03-任务生命周期状态机.html"),
        ("04", "FLOWCHART", "动态权重决策流程", "饥饿硬补偿、四因子权重、候选比较、派发与回队的决策路径。", "04-动态权重决策流程图.html"),
        ("05", "SEQUENCE", "调度执行时序图", "UAV、队列、调度器、限流器和线程池之间的消息顺序与弱网分支。", "05-调度执行时序图.html"),
        ("06", "DEPLOYMENT", "多基站部署拓扑", "边缘节点、控制面、桌面监控台、MySQL 与结果目录的部署边界。", "06-多基站部署拓扑图.html"),
        ("07", "ER / DATA MODEL", "数据库实体关系图", "experiment 聚合根与指标、任务、队列、链路四类明细表的一对多关系。", "07-数据库实体关系图.html"),
        ("08", "PROCESS", "实验评测流水线", "标定、轨迹生成、多策略运行、采样、指标、持久化和发布。", "08-实验评测流水线.html"),
    ]
    rows = []
    for number, kind, title, desc, href in entries:
        rows.append(f"""
        <a class="entry" href="{esc(href)}">
          <span class="number">{number}</span>
          <span class="entry-body">
            <span class="kind">{esc(kind)}</span>
            <strong>{esc(title)}</strong>
            <span class="desc">{esc(desc)}</span>
          </span>
          <span class="open">打开 ↗</span>
        </a>""")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>系统图册 · 无人机集群调度仿真</title>
<link rel="icon" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  :root{{--paper:{PAPER};--ink:{INK};--muted:{MUTED};--soft:{SOFT};--accent:{ACCENT};--rule:{RULE};
    --sans:{SANS};--serif:{SERIF};--mono:{MONO}}}
  body{{font-family:var(--sans);background:var(--paper);color:var(--ink);min-height:100vh;
    display:flex;justify-content:center;padding:2rem 2rem 3rem}}
  .frame{{width:100%;max-width:1120px}}
  .top{{display:flex;align-items:flex-start;justify-content:space-between;gap:2rem;
    padding-bottom:1.5rem;border-bottom:1px solid var(--rule);margin-bottom:2rem}}
  .eyebrow{{font-family:var(--mono);font-size:.66rem;font-weight:500;letter-spacing:.18em;
    text-transform:uppercase;color:var(--muted);margin-bottom:.55rem}}
  h1{{font-family:var(--serif);font-size:clamp(2rem,4vw,3.4rem);font-weight:400;
    line-height:1;letter-spacing:-.03em;margin-bottom:.6rem}}
  .dek{{font-size:.9rem;line-height:1.65;color:var(--soft);max-width:62ch}}
  .meta{{font-family:var(--mono);font-size:.68rem;line-height:1.8;color:var(--muted);
    text-align:right;white-space:nowrap;padding-top:.25rem}}
  .atlas-map{{width:100%;overflow-x:auto;margin-bottom:1.25rem;border:1px solid var(--rule);
    border-radius:6px;background:#fff;padding:.8rem 1rem}}
  .atlas-map svg{{width:100%;min-width:1000px;display:block}}
  .entries{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}
  .entry{{display:grid;grid-template-columns:52px 1fr auto;gap:1rem;align-items:start;
    min-height:118px;padding:1.1rem 1.15rem;background:#fff;border:1px solid var(--rule);
    border-radius:6px;text-decoration:none;color:inherit;transition:border-color .16s,transform .16s}}
  .entry:hover{{border-color:rgba(191,69,32,.45);transform:translateY(-2px)}}
  .number{{font-family:var(--mono);font-size:1.25rem;color:var(--accent);line-height:1}}
  .entry-body{{display:flex;flex-direction:column;gap:.32rem;min-width:0}}
  .kind{{font-family:var(--mono);font-size:.6rem;letter-spacing:.12em;color:var(--muted)}}
  .entry strong{{font-size:.9rem;font-weight:600}}
  .desc{{font-size:.74rem;line-height:1.55;color:var(--soft)}}
  .open{{font-family:var(--mono);font-size:.62rem;color:var(--muted);white-space:nowrap;padding-top:.15rem}}
  .foot{{display:flex;justify-content:space-between;gap:1rem;margin-top:1.5rem;padding-top:.9rem;
    border-top:1px solid var(--rule);font-family:var(--mono);font-size:.64rem;color:var(--muted)}}
  @media (max-width:760px){{body{{padding:1.2rem .8rem 2rem}}.entries{{grid-template-columns:1fr}}
    .top{{flex-direction:column}}.meta{{text-align:left}}.foot{{flex-direction:column}}}}
  @media print{{.atlas-map{{overflow-x:visible}}.atlas-map svg{{min-width:0}}}}
</style>
</head>
<body>
<div class="frame">
  <header class="top">
    <div>
      <p class="eyebrow">UAV SCHEDULER SIMULATION / SYSTEM ATLAS</p>
      <h1>系统图册</h1>
      <p class="dek">八张图把业务场景、OS 原理、算法决策、数据证据和部署形态连成一条可讲解、可核查的链路。</p>
    </div>
    <div class="meta">8 FIGURES<br>OS COURSE PROJECT<br>2026-11</div>
  </header>
  <div class="atlas-map">
    <svg viewBox="0 0 1000 140" xmlns="http://www.w3.org/2000/svg" role="img"
         aria-labelledby="atlas-title atlas-desc">
      <title id="atlas-title">系统图册阅读路径</title>
      <desc id="atlas-desc">从系统架构、数据流、状态机、决策、时序、部署、数据模型到实验评测的八站阅读路径。</desc>
      <line x1="70" y1="64" x2="930" y2="64" stroke="rgba(45,49,66,0.18)" stroke-width="1"/>
      <circle cx="70" cy="64" r="12" fill="#bf4520"/>
      <text x="70" y="68" fill="#f5f5f5" font-size="8" font-family="'Geist Mono', monospace"
            text-anchor="middle">01</text>
      <text x="70" y="98" fill="#2d3142" font-size="11" font-weight="600"
            font-family="'Geist', 'Noto Sans SC', sans-serif" text-anchor="middle">架构</text>
      <circle cx="193" cy="64" r="12" fill="#2d3142"/>
      <text x="193" y="68" fill="#f5f5f5" font-size="8" font-family="'Geist Mono', monospace"
            text-anchor="middle">02</text>
      <text x="193" y="98" fill="#2d3142" font-size="11" font-weight="600"
            font-family="'Geist', 'Noto Sans SC', sans-serif" text-anchor="middle">数据流</text>
      <circle cx="316" cy="64" r="12" fill="#2d3142"/>
      <text x="316" y="68" fill="#f5f5f5" font-size="8" font-family="'Geist Mono', monospace"
            text-anchor="middle">03</text>
      <text x="316" y="98" fill="#2d3142" font-size="11" font-weight="600"
            font-family="'Geist', 'Noto Sans SC', sans-serif" text-anchor="middle">状态机</text>
      <circle cx="439" cy="64" r="12" fill="#2d3142"/>
      <text x="439" y="68" fill="#f5f5f5" font-size="8" font-family="'Geist Mono', monospace"
            text-anchor="middle">04</text>
      <text x="439" y="98" fill="#2d3142" font-size="11" font-weight="600"
            font-family="'Geist', 'Noto Sans SC', sans-serif" text-anchor="middle">决策</text>
      <circle cx="562" cy="64" r="12" fill="#2d3142"/>
      <text x="562" y="68" fill="#f5f5f5" font-size="8" font-family="'Geist Mono', monospace"
            text-anchor="middle">05</text>
      <text x="562" y="98" fill="#2d3142" font-size="11" font-weight="600"
            font-family="'Geist', 'Noto Sans SC', sans-serif" text-anchor="middle">时序</text>
      <circle cx="685" cy="64" r="12" fill="#2d3142"/>
      <text x="685" y="68" fill="#f5f5f5" font-size="8" font-family="'Geist Mono', monospace"
            text-anchor="middle">06</text>
      <text x="685" y="98" fill="#2d3142" font-size="11" font-weight="600"
            font-family="'Geist', 'Noto Sans SC', sans-serif" text-anchor="middle">部署</text>
      <circle cx="808" cy="64" r="12" fill="#2d3142"/>
      <text x="808" y="68" fill="#f5f5f5" font-size="8" font-family="'Geist Mono', monospace"
            text-anchor="middle">07</text>
      <text x="808" y="98" fill="#2d3142" font-size="11" font-weight="600"
            font-family="'Geist', 'Noto Sans SC', sans-serif" text-anchor="middle">数据模型</text>
      <circle cx="930" cy="64" r="12" fill="#2d3142"/>
      <text x="930" y="68" fill="#f5f5f5" font-size="8" font-family="'Geist Mono', monospace"
            text-anchor="middle">08</text>
      <text x="930" y="98" fill="#2d3142" font-size="11" font-weight="600"
            font-family="'Geist', 'Noto Sans SC', sans-serif" text-anchor="middle">评测</text>
    </svg>
  </div>
  <main class="entries">
    {''.join(rows)}
  </main>
  <footer class="foot">
    <span>建议阅读顺序：01 → 02 → 04 → 05 → 03 → 06 → 07 → 08</span>
    <span>HTML / inline SVG · print-ready</span>
  </footer>
</div>
</body>
</html>
"""


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "01-系统框架图.html": page(
            "arch", "系统分层架构图", "Architecture · 系统框架",
            "从现场无人机线程到仿真内核，再到实验评测、持久化和三维监控台。主链路用一条强调路径标出：链路质量直接进入动态权重调度。",
            architecture_svg(),
            [("READING NOTE", "一眼看懂三层边界",
              ["左侧是现场输入，中间是调度内核，右侧是证据与呈现。",
               "队列负责隔离，限流器负责保护线程池，动态权重负责决定谁先走。",
               "虚线只表示跨层旁路，不表示另一条主流程。"]),
             ("CODE ANCHOR", "关键代码落点",
              ["Simulator.submit_task() / dispatch()",
               "MultiLevelQueue.enqueue() / dequeue_starving()",
               "DynamicWeightScheduler.step() / _weight()",
               "ExperimentRunner.run_one() · DBWriter.save_run()"])],
            "01 / 08", None, "", "02-数据流详细逻辑图.html", "数据流"),
        "02-数据流详细逻辑图.html": page(
            "flow", "任务调度数据流图", "Data flow · 从任务到结果",
            "五条泳道、六个阶段，追踪一次上传任务如何穿过生成、入队、调度、限流、执行、重试、结算和发布。",
            data_flow_svg(),
            [("READING NOTE", "控制变量法的数据链",
              ["任务与链路曲线先离线生成，所有策略只回放同一份输入。",
               "失败任务进入退避与本地缓存，恢复后重新回到就绪队列。",
               "指标结算与发布是两条独立出口：数据库和图表文件。"]),
             ("EVIDENCE", "可直接打开的产物",
              ["results/raw/*.json — 队列/链路时序",
               "results/summary.csv — 15 组矩阵",
               "charts/06-队列堆积时序.png",
               "charts/07-链路健康度与权重曲线.png"])],
            "02 / 08", "01-系统框架图.html", "系统架构", "03-任务生命周期状态机.html", "状态机"),
        "03-任务生命周期状态机.html": page(
            "state", "任务生命周期状态机", "State machine · 七态与终止态",
            "状态名称与 task_record.state 对齐。弱网、退避、缓存和看门狗不是旁注，而是任务能否完成的主路径。",
            state_svg(),
            [("READING NOTE", "为什么要画阻塞与缓存",
              ["弱网不是简单的失败，而是会触发重试、退避、缓存和重新入队。",
               "取消是跨状态事件，用旁注表达，避免从每个状态重复画一条线。",
               "完成、失败、取消是三个不同的终止语义。"]),
             ("CODE ANCHOR", "状态落点",
              ["Task.is_starved() / wait_ms()",
               "Simulator._on_upload_failed()",
               "DynamicWeightScheduler 饥饿硬补偿",
               "task_record.state ∈ READY/RUNNING/BLOCKED/CACHED/DONE/DROPPED"])],
            "03 / 08", "02-数据流详细逻辑图.html", "数据流", "04-动态权重决策流程图.html", "决策流程"),
        "04-动态权重决策流程图.html": page(
            "decision", "动态权重决策流程", "Flowchart · 一次调度怎么选",
            "先做饥饿硬补偿，再计算四因子权重；候选择优之后还要经过限流准入，才会真正占用线程池槽位。",
            decision_svg(),
            [("READING NOTE", "两条优先级规则",
              ["低优任务等待超过阈值时，硬补偿通道绕过常规权重比较。",
               "常规路径使用 Q 拥堵、W 等待、L 链路、S 系统负载四个因子。",
               "候选权重最高并不等于一定执行，资源限流仍可能把任务留在队列。"]),
             ("FORMULA", "权重口径",
              ["W = base × (w0 + αQ + βW + γL) × (1 − δS)",
               "健康度低于阈值时叠加 weaknet_downweight。",
               "动态时间片随队列拥堵度伸缩，避免大任务长期占槽。"])],
            "04 / 08", "03-任务生命周期状态机.html", "状态机", "05-调度执行时序图.html", "执行时序"),
        "05-调度执行时序图.html": page(
            "seq", "调度执行时序图", "Sequence · 一次派发与回队",
            "五条参与者生命线展示一次任务提交、准入、执行、完成确认，以及弱网条件下进入回队分支的顺序。",
            sequence_svg(),
            [("READING NOTE", "消息类型不是装饰",
              ["实线是调用，虚线是返回或回退，完成消息用唯一强调色。",
               "ALT 片段只覆盖弱网分支，不把正常路径也包进去。",
               "激活条表示参与者持有一段时间的控制权。"]),
             ("CODE ANCHOR", "调用链",
              ["UAVWorker.run() → Simulator.submit_task()",
               "DynamicWeightScheduler.step() → try_admit()",
               "Simulator.dispatch() → 线程池执行",
               "完成回调 → metrics / trace / ACK"])],
            "05 / 08", "04-动态权重决策流程图.html", "决策流程", "06-多基站部署拓扑图.html", "部署拓扑"),
        "06-多基站部署拓扑图.html": page(
            "deploy", "多基站部署拓扑", "Deployment · 运行在哪里",
            "三个边界：矿区边缘、控制面、数据面。每个节点都标注实际运行的服务和版本，路径标注协议与端口。",
            deployment_svg(),
            [("READING NOTE", "本地演示与多站演进",
              ["单站默认仍然是本地桌面 + 本地 HTTP 服务 + 本地 MySQL。",
               "多站模式把边缘节点、中心站和 MEC 分流作为独立部署面展示。",
               "控制面是单点关键组件，数据面保存指标、时序和结果文件。"]),
             ("OBSERVABILITY", "部署证据",
              ["dist/logs/app.log — frozen 启动与异常",
               "logs/ui.port — 实际本地服务端口",
               "results/ · charts/ — exe 可写产物目录",
               "MySQL uav_sched_sim — 结构化实验证据"])],
            "06 / 08", "05-调度执行时序图.html", "执行时序", "07-数据库实体关系图.html", "数据模型"),
        "07-数据库实体关系图.html": page(
            "er", "数据库实体关系图", "ER / Data model · 证据如何挂到实验",
            "experiment 是聚合根；指标、任务明细、队列采样和链路采样都沿 exp_id 回溯到同一次仿真。",
            er_svg(),
            [("READING NOTE", "长表 + 明细 + 时序",
              ["exp_metric 用长表容纳任意指标，避免每次新增指标都改表结构。",
               "task_record 对应任务控制块，保留状态、等待、重传和缓存证据。",
               "queue_sample 与 link_sample 是过载和弱网两条曲线的最小数据源。"]),
             ("TRACEABILITY", "一行数据怎么回溯",
              ["exp_id 是稳定业务主键。",
               "四个子表均以 exp_id 外键指向 experiment。",
               "v_exp_summary 视图把指标长表转成报告可读的宽表。"])],
            "07 / 08", "06-多基站部署拓扑图.html", "部署拓扑", "08-实验评测流水线.html", "评测流水线"),
        "08-实验评测流水线.html": page(
            "eval", "实验评测流水线", "Process · 从标定到报告",
            "把一次实验拆成设计、仿真、观测、存储和报告五个阶段。每个阶段的输入输出都能落到真实文件或数据库表。",
            evaluation_svg(),
            [("READING NOTE", "为什么先标定再跑矩阵",
              ["标定确定工况负载率，轨迹生成保证五个策略面对同一份输入。",
               "采样与指标是两层证据：时序解释过程，指标回答结论。",
               "持久化和图表发布分开，数据库失败也不阻断实验主流程。"]),
             ("DELIVERABLES", "最终产物",
              ["results/summary.csv · 15 组矩阵",
               "results/raw/*.json · 任务与链路时序",
               "charts/*.png · 对比图与机理图",
               "results/replications/*.csv · 置信区间"])],
            "08 / 08", "07-数据库实体关系图.html", "数据模型", None, ""),
    }
    for name, content in files.items():
        (OUT / name).write_text(content, encoding="utf-8")
    (OUT / "00-图册索引.html").write_text(index_page(), encoding="utf-8")
    print(f"generated {len(files) + 1} files in {OUT}")


if __name__ == "__main__":
    build()
