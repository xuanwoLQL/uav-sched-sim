/* =============================================================================
   os_inspect.js —— 「OS 微观透视镜」
   =============================================================================
   把 3D 场景上方的宏观数字，下钻成"能看见的 OS 过程"：

     * 线程页：30 架无人机 = 30 个线程，五态色块 + 绑定核号 + 当前任务 + 切换间隔
     * 队列页：两级就绪队列的「传送带」——每个方块是一个任务，大小=数据量，颜色=类型
     * 权重页：动态权重的四因子雷达 + 高/低两个候选的权重天平
     * 混沌页：弱网风暴 / 并发洪峰 / 死锁注入（含看门狗回收计数）
     * 回放页：时间轴拖动复盘 + Trace 导出（JSON / CSV）

   数据来源：GET /api/state（250ms 轮询，只读）。本模块不修改任何仿真状态，
   唯一的写操作是用户主动点「混沌」按钮（走 /api/chaos）。
   ============================================================================= */
(function (global) {
  "use strict";

  var VERSION = "1.0.0";
  var POLL_MS = 250;
  var RING_MAX = 600;          // 回放环形缓冲（250ms × 600 = 150s）

  var state = {
    open: false,
    tab: "thread",
    snap: null,
    selected: 1,
    ring: [],
    scrubbing: false,
    scrubIdx: -1,
    lastError: "",
    frames: 0,
  };

  var els = {};

  var CSS = [
    "#osBtn{background:linear-gradient(180deg,#1b3352,#12233a);border:1px solid #2f6fa8;",
    "  color:#bfe6ff;border-radius:8px;padding:7px 12px;font-size:12px;cursor:pointer;margin-left:8px}",
    "#osBtn:hover{border-color:#5aa9e6;color:#fff}",
    "#osPanel{position:fixed;left:250px;top:84px;width:700px;max-height:calc(100vh - 190px);",
    "  background:rgba(6,14,24,.94);border:1px solid rgba(78,161,255,.45);border-radius:12px;",
    "  box-shadow:0 18px 50px rgba(0,0,0,.6);z-index:60;display:flex;flex-direction:column;",
    "  font-family:Consolas,Menlo,monospace;color:#cfe6ff;backdrop-filter:blur(6px)}",
    "#osPanel.hidden{display:none}",
    ".osHead{display:flex;align-items:center;gap:10px;padding:9px 12px;",
    "  border-bottom:1px solid rgba(78,161,255,.25)}",
    ".osHead b{font-size:13px;color:#eaf6ff;letter-spacing:.5px}",
    ".osHead .hint{font-size:11px;color:#7fa6c8;flex:1}",
    ".osTabs{display:flex;gap:4px}",
    ".osTabs button{background:transparent;border:1px solid rgba(78,161,255,.3);color:#9dc4e6;",
    "  border-radius:6px;padding:4px 9px;font-size:11px;cursor:pointer}",
    ".osTabs button.on{background:#1d4b7a;color:#fff;border-color:#5aa9e6}",
    "#osClose{background:transparent;border:none;color:#7fa6c8;font-size:18px;cursor:pointer}",
    ".osBody{padding:10px 12px 12px;overflow:auto}",
    ".osBody section{display:none}.osBody section.on{display:block}",
    ".osFleet{display:grid;grid-template-columns:repeat(10,1fr);gap:5px}",
    ".osCell{border:1px solid #1e3a55;border-radius:5px;padding:3px 4px;font-size:10px;",
    "  background:#0b1a2a;cursor:pointer;line-height:1.35}",
    ".osCell.sel{outline:2px solid #5aa9e6}",
    ".osCell .id{font-weight:bold;color:#dff0ff}",
    ".osCell .st{font-size:9px}",
    ".osDot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:3px}",
    ".osDetail{margin-top:9px;border-top:1px dashed #1e3a55;padding-top:8px;font-size:11px;line-height:1.7}",
    ".osDetail b{color:#eaf6ff}",
    ".osLane{display:flex;align-items:center;gap:6px;margin:6px 0;min-height:34px;",
    "  border:1px dashed #1e3a55;border-radius:6px;padding:4px 6px;overflow-x:auto}",
    ".osLane .tag{font-size:10px;color:#7fa6c8;width:46px;flex:none}",
    ".osBlk{flex:none;height:22px;border-radius:4px;position:relative;",
    "  border:1px solid rgba(255,255,255,.18);font-size:9px;color:#04121f;",
    "  display:flex;align-items:center;justify-content:center;overflow:hidden}",
    ".osBlk .w{position:absolute;left:0;bottom:0;height:3px;background:#ff5d5d}",
    ".osBlk.hot{animation:osPulse 0.9s ease-in-out infinite;box-shadow:0 0 10px #ff4d4d}",
    "@keyframes osPulse{0%,100%{filter:brightness(1)}50%{filter:brightness(1.45)}}",
    ".osLegend{font-size:10px;color:#7fa6c8;margin-top:6px}",
    ".osKv{display:flex;justify-content:space-between;font-size:11px;padding:2px 0}",
    ".osKv b{color:#eaf6ff}",
    ".osChaos{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}",
    ".osChaos button{flex:1;min-width:150px;padding:10px;border-radius:8px;cursor:pointer;",
    "  border:1px solid #2f6fa8;background:#12233a;color:#cfe6ff;font-size:12px}",
    ".osChaos button:hover{background:#1b3352;border-color:#5aa9e6}",
    ".osChaos button.danger{border-color:#a83a3a;background:#2a1414;color:#ffb3b3}",
    ".osChaos button.warn{border-color:#a8863a;background:#2a2314;color:#ffe0a3}",
    ".osScrub{width:100%;margin:8px 0}",
    ".osNote{font-size:10px;color:#7fa6c8;line-height:1.6}",
    ".osErr{color:#ff8a8a;font-size:11px}",
  ].join("\n");

  function h(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt !== undefined) e.textContent = txt;
    return e;
  }

  function fmt(v, n) {
    var x = Number(v);
    return isFinite(x) ? x.toFixed(n === undefined ? 1 : n) : "-";
  }

  function buildDom() {
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);

    var btn = h("button", "", "🔬 OS 透视");
    btn.id = "osBtn";
    btn.title = "线程五态 / 队列传送带 / 权重天平 / 混沌注入 / 回放";
    var top = document.getElementById("top");
    if (top) top.appendChild(btn);
    els.btn = btn;

    var panel = h("div", "hidden");
    panel.id = "osPanel";
    panel.innerHTML = [
      '<div class="osHead">',
      '  <b>OS 微观透视镜</b>',
      '  <span class="hint" id="osHint">点击 3D 场景里的无人机可选中</span>',
      '  <div class="osTabs">',
      '    <button data-tab="thread" class="on">线程</button>',
      '    <button data-tab="queue">队列</button>',
      '    <button data-tab="weight">权重</button>',
      '    <button data-tab="chaos">混沌</button>',
      '    <button data-tab="replay">回放</button>',
      '  </div>',
      '  <button id="osClose">×</button>',
      '</div>',
      '<div class="osBody">',
      '  <section id="os-thread" class="on">',
      '    <div class="osFleet" id="osFleet"></div>',
      '    <div class="osDetail" id="osDetail">点击任意无人机查看线程详情</div>',
      '  </section>',
      '  <section id="os-queue">',
      '    <div class="osLane" id="osLaneHi"><span class="tag">高优</span></div>',
      '    <div class="osLane" id="osLaneLo"><span class="tag">低优</span></div>',
      '    <div class="osLegend">方块大小 = 数据量（对数）；颜色 = 任务类型；底部红条 = 等待占饥饿阈值的比例（满了就提权）</div>',
      '  </section>',
      '  <section id="os-weight">',
      '    <div style="display:flex;gap:14px;align-items:flex-start">',
      '      <canvas id="osRadar" width="240" height="240"></canvas>',
      '      <div style="flex:1" id="osWeightText"></div>',
      '    </div>',
      '  </section>',
      '  <section id="os-chaos">',
      '    <div class="osChaos">',
      '      <button class="warn" data-chaos="weaknet">🌪 弱网风暴<br><span style="font-size:10px">40% 无人机失败率×2.5</span></button>',
      '      <button class="danger" data-chaos="burst">💥 并发洪峰<br><span style="font-size:10px">瞬时注入 500 个高优任务</span></button>',
      '      <button class="danger" data-chaos="deadlock">🔒 死锁注入<br><span style="font-size:10px">卡死一个任务，看门狗 3s 回收</span></button>',
      '    </div>',
      '    <div id="osChaosText" class="osNote"></div>',
      '  </section>',
      '  <section id="os-replay">',
      '    <input type="range" id="osScrub" class="osScrub" min="0" max="0" value="0">',
      '    <div id="osReplayText" class="osNote"></div>',
      '    <div style="margin-top:10px;display:flex;gap:8px">',
      '      <button id="osDlJson">⬇ 导出 Trace (JSON)</button>',
      '      <button id="osDlCsv">⬇ 导出 Trace (CSV)</button>',
      '    </div>',
      '    <div class="osNote" style="margin-top:8px">回放只回放"面板数据"（队列/负载/时间片/线程状态），不回放 3D 画面 —— 3D 由实时仿真驱动。</div>',
      '  </section>',
      '</div>',
    ].join("");
    document.body.appendChild(panel);
    els.panel = panel;
    els.hint = panel.querySelector("#osHint");
    els.fleet = panel.querySelector("#osFleet");
    els.detail = panel.querySelector("#osDetail");
    els.laneHi = panel.querySelector("#osLaneHi");
    els.laneLo = panel.querySelector("#osLaneLo");
    els.radar = panel.querySelector("#osRadar");
    els.weightText = panel.querySelector("#osWeightText");
    els.chaosText = panel.querySelector("#osChaosText");
    els.scrub = panel.querySelector("#osScrub");
    els.replayText = panel.querySelector("#osReplayText");

    btn.addEventListener("click", function () {
      state.open = !state.open;
      panel.classList.toggle("hidden", !state.open);
      els.btn.style.borderColor = state.open ? "#5aa9e6" : "";
      if (state.open) render();
    });
    panel.querySelector("#osClose").addEventListener("click", function () {
      state.open = false;
      panel.classList.add("hidden");
    });
    Array.prototype.forEach.call(panel.querySelectorAll(".osTabs button"), function (b) {
      b.addEventListener("click", function () {
        state.tab = b.getAttribute("data-tab");
        Array.prototype.forEach.call(panel.querySelectorAll(".osTabs button"), function (x) {
          x.classList.toggle("on", x === b);
        });
        Array.prototype.forEach.call(panel.querySelectorAll(".osBody section"), function (s) {
          s.classList.toggle("on", s.id === "os-" + state.tab);
        });
        render();
      });
    });
    Array.prototype.forEach.call(panel.querySelectorAll("[data-chaos]"), function (b) {
      b.addEventListener("click", function () { fireChaos(b.getAttribute("data-chaos")); });
    });
    panel.querySelector("#osDlJson").addEventListener("click", function () { downloadTrace("json"); });
    panel.querySelector("#osDlCsv").addEventListener("click", function () { downloadTrace("csv"); });
    els.scrub.addEventListener("input", function () {
      state.scrubbing = true;
      state.scrubIdx = parseInt(els.scrub.value, 10) || 0;
      renderReplay();
    });
    els.scrub.addEventListener("change", function () {
      var max = Math.max(0, state.ring.length - 1);
      if (parseInt(els.scrub.value, 10) >= max) {
        state.scrubbing = false;
        state.scrubIdx = -1;
        renderReplay();
      }
    });
  }

  /* ------------------------------------------------------------------ 数据 */
  function poll() {
    fetch("/api/state", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        state.snap = j;
        state.lastError = "";
        var live = j.live || {};
        if (live && live.sim_time !== undefined) {
          // 新一轮仿真：sim_time 回退 -> 清空环形缓冲，避免回放混进上一轮的数据
          var last = state.ring.length ? state.ring[state.ring.length - 1] : null;
          if (last && live.sim_time + 0.5 < last.t) {
            state.ring.length = 0;
            state.scrubbing = false;
            state.scrubIdx = -1;
          }
          state.ring.push({
            t: live.sim_time,
            qh: (live.queue || {}).high || 0,
            ql: (live.queue || {}).low || 0,
            load: live.system_load || 0,
            hi: (live.scheduler_stats || {}).high_time_slice_ms || 0,
            lo: (live.scheduler_stats || {}).low_time_slice_ms || 0,
            act: (live.pool || {}).active || 0,
            idle: (live.pool || {}).idle || 0,
            ctx: (live.scheduler_stats || {}).context_switches || 0,
            done: (live.counters || {}).done || 0,
            sub: (live.counters || {}).submitted || 0,
            status: j.status,
          });
          if (state.ring.length > RING_MAX) state.ring.shift();
        }
        if (state.open && !state.scrubbing) render();
      })
      .catch(function (e) {
        state.lastError = String(e && e.message ? e.message : e);
        if (state.open) render();
      });
  }

  function liveOf() {
    var s = state.snap || {};
    return s.live || {};
  }

  function render() {
    if (!state.snap) {
      els.hint.textContent = state.lastError ? ("接口错误：" + state.lastError) : "等待 /api/state …";
      return;
    }
    var j = state.snap;
    var live = liveOf();
    var st = j.status || "idle";
    els.hint.textContent = "工况 " + (j.scenario || "-") + " · 策略 " + (j.scheduler || "-") +
      " · 状态 " + st + " · 采样 " + state.ring.length + " 帧";
    if (state.tab === "thread") renderThread(live);
    else if (state.tab === "queue") renderQueue(live);
    else if (state.tab === "weight") renderWeight(live);
    else if (state.tab === "chaos") renderChaos(live);
    else if (state.tab === "replay") renderReplay();
  }

  /* ------------------------------------------------------------ 线程透视 */
  var STATE_COLOR = {
    RUNNING: "#3ddc97", READY: "#4ea1ff", BLOCKED: "#ffd166",
    CACHED: "#c084fc", OFFLINE: "#556677", NEW: "#8899aa", DONE: "#8899aa",
  };

  function renderThread(live) {
    var uav = live.uav_states || {};
    var ids = Object.keys(uav).map(Number).sort(function (a, b) { return a - b; });
    var frag = document.createDocumentFragment();
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i];
      var u = uav[String(id)] || {};
      var c = h("div", "osCell" + (id === state.selected ? " sel" : ""));
      var stName = u.state || (u.online ? "READY" : "OFFLINE");
      c.innerHTML = '<div class="id">UAV-' + (id < 10 ? "0" : "") + id + '</div>' +
        '<div class="st"><i class="osDot" style="background:' +
        (STATE_COLOR[stName] || "#889") + '"></i>' + stName + '</div>' +
        '<div class="st">核 ' + (u.core >= 0 ? "C" + u.core : "-") +
        ' · ' + (u.priority ? (u.priority === "HIGH" ? "高优" : "低优") : "空闲") + '</div>';
      (function (uid) {
        c.addEventListener("click", function () { state.selected = uid; render(); });
      })(id);
      frag.appendChild(c);
    }
    els.fleet.innerHTML = "";
    els.fleet.appendChild(frag);
    renderDetail(uav[String(state.selected)] || {}, live);
  }

  function renderDetail(u, live) {
    var kernel = live.kernel || {};
    var pool = live.pool || {};
    var counters = live.counters || {};
    var thr = counters.starvation_threshold_ms || 3000;
    var wait = Number(u.wait_ms || 0);
    var ratio = Math.min(1, wait / Math.max(1, thr));
    els.detail.innerHTML =
      '<div><b>UAV-' + state.selected + '</b> · 线程状态 <b style="color:' +
      (STATE_COLOR[u.state] || "#889") + '">' + (u.state || "-") + '</b>' +
      ' · 绑定核 <b>' + (u.core >= 0 ? "C" + u.core : "未绑定") + '</b></div>' +
      '<div>当前任务 <b>' + (u.task_id || "—") + '</b>' +
      (u.task_type ? '（' + u.task_type + '）' : "") + '</div>' +
      '<div>任务进度 <b>' + fmt((u.progress || 0) * 100, 1) + '%</b>' +
      ' · 已等待 <b style="color:' + (ratio > 0.8 ? "#ff6b6b" : ratio > 0.5 ? "#ffd166" : "#3ddc97") +
      '">' + fmt(wait, 0) + ' ms</b>（阈值 ' + fmt(thr, 0) + '）</div>' +
      '<div>距上次任务结束 <b>' + fmt(u.switch_ms, 0) + ' ms</b>' +
      ' · 链路健康 <b>' + fmt(u.health, 1) + '</b></div>' +
      '<div style="margin-top:6px;color:#7fa6c8">' +
      '线程池 ' + (pool.active || 0) + ' 忙 / ' + (pool.idle || 0) + ' 闲 · ' +
      '上下文切换 ' + (kernel.context_switches || 0) + ' · ' +
      '进程 CPU 用户 ' + fmt(kernel.cpu_user_s, 2) + 's / 内核 ' + fmt(kernel.cpu_kernel_s, 2) + 's' +
      '</div>';
  }

  /* ------------------------------------------------------------ 队列传送带 */
  var TYPE_COLOR = {
    IMAGERY: "#ff7b7b", MULTISPECTRAL: "#c084fc", LIDAR: "#fb923c",
    GPS: "#67e8f9", ATTITUDE: "#93c5fd", HEARTBEAT: "#a7f3d0", ALARM: "#fca5a5",
  };

  function renderQueue(live) {
    var tasks = live.queue_tasks || [];
    var thr = Number((live.counters || {}).starvation_threshold_ms || 3000);
    var hi = [], lo = [];
    for (var i = 0; i < tasks.length; i++) {
      (tasks[i].priority === "HIGH" ? hi : lo).push(tasks[i]);
    }
    fillLane(els.laneHi, hi, thr, 16);
    fillLane(els.laneLo, lo, thr, 24);
  }

  function fillLane(lane, list, thr, cap) {
    while (lane.children.length > 1) lane.removeChild(lane.lastChild);
    var n = Math.min(list.length, cap);
    for (var i = 0; i < n; i++) {
      var t = list[i];
      var w = Math.max(18, Math.min(70, 18 + Math.log10((t.data_size_mb || 1) + 1) * 16));
      var b = h("div", "osBlk");
      b.style.width = w + "px";
      b.style.background = TYPE_COLOR[t.task_type] || "#9ab";
      b.title = t.task_id + "\n" + t.task_type + " · " + t.data_size_mb + " MB\n等待 " +
        fmt(t.wait_ms, 0) + " ms";
      b.textContent = t.task_type.slice(0, 4);
      if (t.priority === "LOW") {
        var ratio = Math.min(1, (t.wait_ms || 0) / Math.max(1, thr));
        if (ratio > 0.9) b.classList.add("hot");   // 濒临饥饿：脉冲高亮（提权前夜）
        var bar = h("i", "w");
        bar.style.width = (ratio * 100) + "%";
        bar.style.background = ratio > 0.85 ? "#ff4d4d" : ratio > 0.5 ? "#ffd166" : "#3ddc97";
        b.appendChild(bar);
      }
      lane.appendChild(b);
    }
    if (list.length > n) {
      var more = h("span", "tag", "+" + (list.length - n));
      more.style.color = "#7fa6c8";
      lane.appendChild(more);
    }
    if (list.length === 0) {
      lane.appendChild(h("span", "tag", "（空）"));
    }
  }

  /* ------------------------------------------------------------ 权重天平 */
  function renderWeight(live) {
    var w = live.weight || {};
    var hi = w.high || w.factors || {};
    var lo = w.low || {};
    var cv = els.radar;
    var ctx = cv.getContext("2d");
    var W = cv.width, H = cv.height, cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.36;
    ctx.clearRect(0, 0, W, H);

    var axes = [["Q 拥堵", "Q"], ["W 等待", "W"], ["L 链路", "L"], ["S 负载", "S"]];
    // 网格
    ctx.strokeStyle = "rgba(78,161,255,.25)";
    ctx.lineWidth = 1;
    for (var g = 1; g <= 4; g++) {
      ctx.beginPath();
      for (var k = 0; k <= 4; k++) {
        var a = -Math.PI / 2 + k * Math.PI / 2;
        var rr = R * g / 4;
        var x = cx + Math.cos(a) * rr, y = cy + Math.sin(a) * rr;
        if (k === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    ctx.fillStyle = "#7fa6c8";
    ctx.font = "10px Consolas";
    for (var i = 0; i < 4; i++) {
      var ang = -Math.PI / 2 + i * Math.PI / 2;
      ctx.fillText(axes[i][0], cx + Math.cos(ang) * (R + 16) - 14,
        cy + Math.sin(ang) * (R + 16) + 4);
    }

    function poly(src, color) {
      if (!src || src.Q === undefined) return;
      ctx.beginPath();
      for (var i2 = 0; i2 < 4; i2++) {
        var key = axes[i2][1];
        var v = Math.max(0, Math.min(1, Number(src[key] || 0)));
        var a2 = -Math.PI / 2 + i2 * Math.PI / 2;
        var x2 = cx + Math.cos(a2) * R * v, y2 = cy + Math.sin(a2) * R * v;
        if (i2 === 0) ctx.moveTo(x2, y2); else ctx.lineTo(x2, y2);
      }
      ctx.closePath();
      ctx.fillStyle = color.replace(")", ",.22)").replace("rgb", "rgba");
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
      ctx.fill();
      ctx.stroke();
    }
    poly(hi, "rgb(78,161,255)");
    poly(lo, "rgb(245,166,35)");

    var name = (w.model || live.scheduler || "-");
    if (hi.Q === undefined) {
      els.weightText.innerHTML =
        '<div class="osKv"><span>当前策略</span><b>' + name + '</b></div>' +
        '<div class="osNote">该策略不使用四因子权重（只有「动态权重自适应」才有）。' +
        '切换到策略 = 动态权重自适应，即可看到 Q/W/L/S 的实时跳动。</div>';
      return;
    }
    var better = (hi.weight || 0) >= (lo.weight || 0) ? "高优" : "低优";
    els.weightText.innerHTML =
      '<div class="osKv"><span>策略</span><b>' + name + '</b></div>' +
      '<div class="osKv"><span>W 高优</span><b style="color:#4ea1ff">' + fmt(hi.weight, 3) + '</b></div>' +
      '<div class="osKv"><span>W 低优</span><b style="color:#f5a623">' + fmt(lo.weight, 3) + '</b></div>' +
      '<div class="osKv"><span>本次选择</span><b>' + better + '</b></div>' +
      '<div class="osNote" style="margin-top:6px">' +
      'W = base·(w0 + αQ + βW + γL)·(1−δS)<br>' +
      '高优 base=' + fmt(hi.base, 2) + ' · Q=' + fmt(hi.Q, 2) + ' · W=' + fmt(hi.W, 2) +
      ' · L=' + fmt(hi.L, 2) + ' · S=' + fmt(hi.S, 2) +
      (hi.boosted ? ' · <b style="color:#ff6b6b">饥饿提权</b>' : "") +
      (hi.degraded ? ' · <b style="color:#c084fc">弱网降权</b>' : "") + '<br>' +
      '低优 base=' + fmt(lo.base, 2) + ' · Q=' + fmt(lo.Q, 2) + ' · W=' + fmt(lo.W, 2) +
      ' · L=' + fmt(lo.L, 2) + ' · S=' + fmt(lo.S, 2) +
      (lo.boosted ? ' · <b style="color:#ff6b6b">饥饿提权</b>' : "") +
      (lo.degraded ? ' · <b style="color:#c084fc">弱网降权</b>' : "") +
      '</div>';
  }

  /* ------------------------------------------------------------ 混沌控制台 */
  function renderChaos(live) {
    var c = live.chaos || {};
    var k = live.kernel || {};
    els.chaosText.innerHTML =
      '<div class="osKv"><span>弱网风暴</span><b>' +
      (c.weaknet_active ? ('进行中 · ' + (c.weaknet_uavs || 0) + ' 架 · 到 t=' + fmt(c.weaknet_until, 1)) : "未启用") +
      '</b></div>' +
      '<div class="osKv"><span>并发洪峰累计注入</span><b>' + (c.burst || 0) + ' 个任务</b></div>' +
      '<div class="osKv"><span>死锁看门狗</span><b>可疑 ' + (k.deadlock_suspects || 0) +
      ' · 已强制回收 ' + (k.deadlock_recovered || 0) + '</b></div>' +
      '<div class="osKv"><span>进程 CPU</span><b>用户 ' + fmt(k.cpu_user_s, 2) +
      's · 内核 ' + fmt(k.cpu_kernel_s, 2) + 's</b></div>' +
      '<div class="osNote">注入只能在「运行中」生效。弱网风暴会让被选中的 40% 无人机失败率 ×2.5；' +
      '并发洪峰一次塞 500 个高优任务压测限流；死锁注入会把一个运行中任务卡住，看门狗 3 仿真秒后强制回收。</div>';
  }

  function fireChaos(kind) {
    var url = "/api/chaos?kind=" + encodeURIComponent(kind);
    if (kind === "burst") url += "&n=500";
    if (kind === "weaknet") url += "&duration=20&frac=0.4";
    if (kind === "deadlock") url += "&hold=20";
    els.chaosText.textContent = "注入中…";
    fetch(url, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        els.chaosText.innerHTML = j.ok
          ? ('<span style="color:#3ddc97">已注入：' + kind + '</span> ' + JSON.stringify(j))
          : ('<span class="osErr">注入失败：' + (j.msg || "未知错误") + '</span>');
        poll();
      })
      .catch(function (e) { els.chaosText.textContent = "注入请求失败：" + e; });
  }

  /* ------------------------------------------------------------ 回放 */
  function renderReplay() {
    var n = state.ring.length;
    els.scrub.max = String(Math.max(0, n - 1));
    if (!state.scrubbing) {
      state.scrubIdx = n - 1;
      els.scrub.value = String(Math.max(0, n - 1));
    }
    var idx = Math.max(0, Math.min(n - 1, state.scrubIdx));
    var f = state.ring[idx];
    if (!f) {
      els.replayText.textContent = "还没有采样帧（先点「开始仿真」）。";
      return;
    }
    els.replayText.innerHTML =
      '<div class="osKv"><span>帧</span><b>' + (idx + 1) + ' / ' + n +
      (state.scrubbing ? '（回放中）' : '（实时）') + '</b></div>' +
      '<div class="osKv"><span>仿真时刻</span><b>t = ' + fmt(f.t, 1) + ' s</b></div>' +
      '<div class="osKv"><span>队列 高/低</span><b>' + f.qh + ' / ' + f.ql + '</b></div>' +
      '<div class="osKv"><span>系统负载</span><b>' + fmt(f.load, 2) + '</b></div>' +
      '<div class="osKv"><span>时间片 高/低</span><b>' + fmt(f.hi, 0) + ' / ' + fmt(f.lo, 0) + ' ms</b></div>' +
      '<div class="osKv"><span>线程池 忙/闲</span><b>' + f.act + ' / ' + f.idle + '</b></div>' +
      '<div class="osKv"><span>完成 / 提交</span><b>' + f.done + ' / ' + f.sub + '</b></div>';
  }

  function downloadTrace(fmt) {
    fetch("/api/trace?format=" + fmt, { cache: "no-store" })
      .then(function (r) { return r.blob(); })
      .then(function (b) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(b);
        a.download = "uav_trace_" + new Date().toISOString().replace(/[:.]/g, "-") + "." + fmt;
        document.body.appendChild(a);
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 4000);
      })
      .catch(function (e) { els.replayText.textContent = "导出失败：" + e; });
  }

  /* ------------------------------------------------------------ 3D 点选 */
  function openPanel() {
    if (!state.open) {
      state.open = true;
      els.panel.classList.remove("hidden");
      els.btn.style.borderColor = "#5aa9e6";
    }
  }

  function installPick() {
    var S = global.UAV_SCENE;
    if (!S || !S.scene || !S.camera || !S.renderer) return false;
    if (typeof THREE === "undefined") return false;
    var dom = S.renderer.domElement;
    var ray = new THREE.Raycaster();
    var ndc = new THREE.Vector2();
    var downX = 0, downY = 0;
    dom.addEventListener("pointerdown", function (ev) { downX = ev.clientX; downY = ev.clientY; });
    dom.addEventListener("pointerup", function (ev) {
      // 拖动旋转视角时不触发选中
      if (Math.abs(ev.clientX - downX) > 4 || Math.abs(ev.clientY - downY) > 4) return;
      var rect = dom.getBoundingClientRect();
      ndc.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
      ray.setFromCamera(ndc, S.camera);
      var drones = S.state.drones || [];
      var groups = [];
      for (var i = 0; i < drones.length; i++) if (drones[i].group) groups.push(drones[i].group);
      var hits = ray.intersectObjects(groups, true);
      if (!hits.length) return;
      var o = hits[0].object;
      while (o) {
        for (var j = 0; j < drones.length; j++) {
          if (drones[j].group === o) {
            state.selected = drones[j].id;
            state.tab = "thread";
            Array.prototype.forEach.call(els.panel.querySelectorAll(".osTabs button"), function (x) {
              x.classList.toggle("on", x.getAttribute("data-tab") === "thread");
            });
            Array.prototype.forEach.call(els.panel.querySelectorAll(".osBody section"), function (s) {
              s.classList.toggle("on", s.id === "os-thread");
            });
            openPanel();
            render();
            return;
          }
        }
        o = o.parent;
      }
    });
    return true;
  }

  function init() {
    buildDom();
    poll();
    setInterval(poll, POLL_MS);
    // 3D 场景由 app3d.html 稍后创建：轮询等待 UAV_SCENE 出现后再装点选
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (installPick() || tries > 120) clearInterval(timer);
    }, 250);
  }

  global.UAVInspect = { version: VERSION, open: openPanel, state: state };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(typeof window !== "undefined" ? window : this);
