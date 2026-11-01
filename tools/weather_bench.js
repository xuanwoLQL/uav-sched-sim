/*
 * weather_bench.js —— 在页面内跑「多运行场景测评」（由 shot.mjs 的 --eval-file 执行）
 * =============================================================================
 * 这是老师要求的「几种运行场景的测评，系统能够稳定高效运行」的取证脚本：
 *
 *   对 clear / rain / snow / dust 四种运行场景，各做一次：
 *     1) 通过 /api/weather 切场景（与用户点按钮走同一条路径）；
 *     2) 等 2.5s 让缓动/着色器编译到位（不把过渡期抖动算进指标）；
 *     3) 用 requestAnimationFrame 连续采样 12s 的帧间隔；
 *     4) 同时记录仿真是否真的在推进（sim time / submitted / done）与 JS 堆占用。
 *
 * 输出：一段 JSON 字符串（shot.mjs --eval-out 会原样写到文件）。
 *
 * 为什么用帧间隔而不是读 HUD 上的 FPS：
 *   HUD 只显示一个平滑后的瞬时值，看不到卡顿。帧间隔能给出 P95/P99/最坏帧，
 *   而「最坏帧」才是用户感知到的卡顿，也是「系统是否稳定」的直接证据。
 */
(async function () {
  "use strict";

  var PRESETS = ["clear", "rain", "snow", "dust"];
  var SAMPLE_MS = 12000;
  var SETTLE_MS = 2500;
  var WARMUP_MS = 1200;

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  async function api(path, method) {
    var r = await fetch(path, { cache: "no-store", method: method || "GET" });
    var txt = await r.text();
    try { return JSON.parse(txt); } catch (e) { return { raw: txt }; }
  }

  function heapMB() {
    var m = (typeof performance !== "undefined") ? performance.memory : null;
    return (m && m.usedJSHeapSize) ? +(m.usedJSHeapSize / 1048576).toFixed(1) : null;
  }

  function stats(frames) {
    var f = frames.filter(function (x) { return x > 0 && x < 5000; }).sort(function (a, b) { return a - b; });
    var n = f.length;
    if (!n) return { n: 0 };
    var sum = 0;
    for (var i = 0; i < n; i++) sum += f[i];
    var avg = sum / n;
    function q(x) { return f[Math.min(n - 1, Math.max(0, Math.floor(x * n)))]; }
    var slow = 0;
    for (var j = 0; j < n; j++) if (f[j] > 33.4) slow++;
    return {
      n: n,
      avgMs: +avg.toFixed(2),
      fps: +(1000 / avg).toFixed(1),
      p50: +q(0.50).toFixed(2),
      p95: +q(0.95).toFixed(2),
      p99: +q(0.99).toFixed(2),
      worst: +f[n - 1].toFixed(2),
      best: +f[0].toFixed(2),
      slowFrames: slow,
      slowPct: +(100 * slow / n).toFixed(2)
    };
  }

  var out = {
    ok: true,
    startedAt: new Date().toISOString(),
    sampleMs: SAMPLE_MS,
    userAgent: navigator.userAgent,
    heapStartMB: heapMB(),
    presets: [],
    stress: [],
    errors: []
  };

  try {
    // 隐藏调试面板（用 ?debug=1 打开只是为了拿到 renderer 句柄做压力测试）
    var dbgEl = document.getElementById("dbg");
    if (dbgEl) dbgEl.style.display = "none";
    var R = (window.__DBG && window.__DBG.renderer) || null;
    var pr0 = (R && R.getPixelRatio) ? R.getPixelRatio() : 1;

    // 服务端只实现了 do_GET 路由，这里统一用 GET（与页面按钮同一条路径）
    await api("/api/start");
    await sleep(1500);

    for (var i = 0; i < PRESETS.length; i++) {
      var p = PRESETS[i];
      try {
        // 每个场景都重新起跑一次仿真：保证四种天气都是在"运行态"下采样，
        // 而不是"晴朗跑到一半、其余场景仿真已经结束"。
        await api("/api/start");
        await sleep(800);
        await api("/api/weather?preset=" + p);
        await sleep(SETTLE_MS);

        var frames = [];
        var last = performance.now();
        var stop = false;
        var started = last;

        function tick() {
          var now = performance.now();
          if (now - started > WARMUP_MS) frames.push(now - last);
          last = now;
          if (!stop) requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);

        await sleep(SAMPLE_MS);
        stop = true;
        await sleep(120);

        var st = await api("/api/state");
        var live = (st && st.live) || {};
        var wx = (st && st.weather) || {};
        var s = stats(frames);

        out.presets.push({
          preset: p,
          label: wx.label || p,
          status: st && st.status,
          frame: s,
          weather: {
            intensity: wx.intensity,
            visibility_km: wx.visibility_km,
            link_penalty: wx.link_penalty
          },
          sim: {
            t: (live.t !== undefined ? live.t : (live.sim_time !== undefined ? live.sim_time : null)),
            submitted: live.submitted,
            done: live.done
          },
          heapMB: heapMB()
        });
      } catch (e) {
        out.errors.push(p + ": " + (e && e.message ? e.message : e));
        out.ok = false;
      }
    }

    /* ---- 压力测试：像素比翻倍（像素量 x4），量出还有多少性能余量 ----
       垂直同步会把正常场景全部锁在 120fps，看不出天气之间的差别；
       翻倍分辨率后 GPU 开始吃紧，掉帧才会暴露真实的性能阶梯。 */
    if (R && R.setPixelRatio) {
      for (var k = 0; k < PRESETS.length; k++) {
        var ps = PRESETS[k];
        try {
          await api("/api/weather?preset=" + ps);
          R.setPixelRatio(pr0 * 3);   // 9 倍像素量：真正把 GPU 压出性能阶梯
          await sleep(2000);
          var sf = [];
          var slast = performance.now();
          var sstop = false;
          var sstart = slast;
          (function () {
            function stick() {
              var now2 = performance.now();
              if (now2 - sstart > 800) sf.push(now2 - slast);
              slast = now2;
              if (!sstop) requestAnimationFrame(stick);
            }
            requestAnimationFrame(stick);
          })();
          await sleep(4000);
          sstop = true;
          await sleep(100);
          var ss = stats(sf);
          ss.preset = ps;
          ss.pixelRatio = (R.getPixelRatio ? R.getPixelRatio() : pr0 * 3);
          out.stress.push(ss);
        } catch (e3) {
          out.errors.push("stress " + ps + ": " + (e3 && e3.message ? e3.message : e3));
        }
      }
      R.setPixelRatio(pr0);
    }
  } catch (e) {
    out.ok = false;
    out.errors.push("fatal: " + (e && e.message ? e.message : e));
  } finally {
    try { await api("/api/stop"); } catch (e2) { /* 忽略 */ }
  }

  out.finishedAt = new Date().toISOString();
  out.heapEndMB = heapMB();
  return JSON.stringify(out);
})()
