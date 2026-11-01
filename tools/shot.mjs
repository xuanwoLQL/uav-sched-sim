#!/usr/bin/env node
/* =============================================================================
   shot.mjs —— 无头 Edge(chromium/CDP) 截图 + 控制台错误采集
   -----------------------------------------------------------------------------
   为什么自己写 CDP 驱动：项目要求「零重型依赖」，不装 puppeteer/playwright，
   只用 Node 内置 fetch + WebSocket，直接跑 Chrome DevTools Protocol。

   用法（在项目根目录）：
     node tools/shot.mjs --url "http://127.0.0.1:8791/app3d.html?quality=high" \
                         --out shots/a.png --wait 9000

   参数：
     --url     目标页面（默认 http://127.0.0.1:8791/app3d.html?quality=high）
     --out     截图输出路径（默认 shots/shot.png）
     --wait    页面加载后额外等待毫秒（默认 8000，等 WebGL 场景构建 + 若干帧）
     --width   视口宽（默认 1600）      --height 视口高（默认 1000）
     --eval    等待结束后在页面里执行的 JS 表达式，结果打印到 stdout（模块自检用）
     --eval-file  同上，但表达式从 .js 文件读（避免 PowerShell 引号转义地狱）
     --eval-delay 执行 eval 后、截图前再等多少毫秒（默认 2500，用于切机位/切天气后等渲染）
     --gpu     **用物理显卡渲染**（无头 + ANGLE D3D11；页面徽章应显示 NVIDIA/AMD/Intel 真卡）
     --headed  开真窗口（屏幕外）用 GPU 合成再 CDP 截图（--gpu 不生效时的兜底）
     --no-shot 不截图，只跑诊断
     --debug   打开浏览器日志

   例子（本机 RTX 3050，实测 120fps）：
     node tools/shot.mjs --gpu --url "http://127.0.0.1:8791/?quality=high" \
       --out shots/gpu.png --wait 8000 --eval-file tools/gpu_pit.js --eval-delay 2500

   输出：stdout 打印一行摘要 JSON（含 console 错误、页面异常、页面自检结果）。
   退出码：0 = 无 page error；2 = 有 page error；1 = 驱动本身失败。
   ========================================================================== */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync, rmSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { tmpdir } from "node:os";

const BROWSERS = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
];

function parseArgs(argv) {
  const out = {
    url: "http://127.0.0.1:8791/app3d.html?quality=high",
    out: "shots/shot.png", wait: 8000, width: 1600, height: 1000,
    eval: "", shot: true, debug: false, evalDelay: 2500, evalFile: "",
    gpu: false, headed: false,
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--url") out.url = argv[++i];
    else if (a === "--out") out.out = argv[++i];
    else if (a === "--wait") out.wait = parseInt(argv[++i], 10);
    else if (a === "--width") out.width = parseInt(argv[++i], 10);
    else if (a === "--height") out.height = parseInt(argv[++i], 10);
    else if (a === "--eval") out.eval = argv[++i];
    else if (a === "--eval-file") out.evalFile = argv[++i];
    else if (a === "--eval-delay") out.evalDelay = parseInt(argv[++i], 10);
    else if (a === "--no-shot") out.shot = false;
    else if (a === "--debug") out.debug = true;
    else if (a === "--gpu") out.gpu = true;       // 用硬件 GPU 渲染（无头 + ANGLE D3D11）
    else if (a === "--headed") out.headed = true; // 开真窗口（屏幕外）用 GPU，再 CDP 截图
  }
  return out;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, timeoutMs, stepMs = 200) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try { const v = await fn(); if (v) return v; } catch { /* retry */ }
    await sleep(stepMs);
  }
  return null;
}

function pickBrowser() {
  for (const p of BROWSERS) if (existsSync(p)) return p;
  throw new Error("未找到 Edge/Chrome 可执行文件");
}

async function main() {
  const args = parseArgs(process.argv);
  const browser = pickBrowser();
  const port = 9222 + Math.floor(Math.random() * 600);
  const userDataDir = resolve(tmpdir(), `uavshot-${Date.now()}`);
  mkdirSync(userDataDir, { recursive: true });

  // ---- 渲染后端选择 ----
  // 默认（不加参数）沿用 SwiftShader 软件光栅：无头环境最稳，任何机器都能出图；
  // --gpu    ：无头 + ANGLE D3D11 走物理显卡（页面上 GPU 徽章应显示 NVIDIA/AMD/Intel 真卡）
  // --headed ：开真窗口（放到屏幕外），由 Composer 合成 → 必走物理 GPU，再 CDP 截图
  const gpuArgs = args.gpu
    ? ["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist",
       "--enable-features=VaapiVideoDecoder", "--disable-gpu-driver-bug-workarounds"]
    : ["--enable-unsafe-swiftshader", "--use-angle=swiftshader"];
  const modeArgs = args.headed
    ? ["--window-position=-2400,-2400", "--window-size=" + args.width + "," + args.height,
       "--new-window"]                       // 真窗口：GPU 合成走显卡
    : ["--headless=new", "--hide-scrollbars"];

  const child = spawn(browser, [
    ...modeArgs,
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${userDataDir}`,
    `--window-size=${args.width},${args.height}`,
    "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--mute-audio",
    ...gpuArgs,
    "--enable-webgl",
    ...(args.debug ? ["--enable-logging=stderr", "--v=0"] : []),
    "about:blank",
  ], { stdio: ["ignore", "pipe", "pipe"] });

  let stderrBuf = "";
  child.stderr.on("data", (d) => { stderrBuf += d.toString().slice(0, 4000); });
  const cleanup = () => {
    try { child.kill("SIGKILL"); } catch { /* ignore */ }
    try { rmSync(userDataDir, { recursive: true, force: true }); } catch { /* ignore */ }
  };

  try {
    const ver = await waitFor(async () => {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      return r.ok ? r.json() : null;
    }, 25000);
    if (!ver) throw new Error("浏览器 CDP 端口未就绪。" + stderrBuf.slice(0, 600));

    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = list.find((t) => t.type === "page") || list[0];
    if (!page) throw new Error("没有可用页面 target");

    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws 连接失败")); });

    let msgId = 0;
    const pending = new Map();
    const consoleErrors = [];
    const consoleWarns = [];
    const pageErrors = [];
    let consoleCount = 0;

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id && pending.has(msg.id)) {
        const { resolve: res, reject } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error))); else res(msg.result);
        return;
      }
      if (msg.method === "Runtime.consoleAPICalled") {
        const text = (msg.params.args || [])
          .map((a) => (a.value !== undefined ? String(a.value) : (a.description || a.type))).join(" ");
        consoleCount++;
        if (msg.params.type === "error") consoleErrors.push(text);
        else if (msg.params.type === "warning") consoleWarns.push(text);
      } else if (msg.method === "Runtime.exceptionThrown") {
        const d = msg.params.exceptionDetails;
        pageErrors.push(d.exception?.description || d.text || JSON.stringify(d));
      } else if (msg.method === "Log.entryAdded") {
        const e = msg.params.entry;
        if (e.level === "error") consoleErrors.push(`[LOG:${e.source}] ${e.text}`);
      }
    };

    const send = (method, params = {}) => new Promise((res, rej) => {
      const id = ++msgId;
      pending.set(id, { resolve: res, reject: rej });
      ws.send(JSON.stringify({ id, method, params }));
    });

    await send("Runtime.enable");
    await send("Log.enable");
    await send("Page.enable");
    await send("Emulation.setDeviceMetricsOverride", {
      width: args.width, height: args.height, deviceScaleFactor: 1, mobile: false });

    await send("Page.navigate", { url: args.url });
    await sleep(args.wait);

    let evalResult = null;
    // --eval-file：把页面操作脚本放进 .js 文件，避免 PowerShell 引号转义地狱
    if (args.evalFile) {
      const p = resolve(args.evalFile);
      if (existsSync(p)) args.eval = readFileSync(p, "utf8");
      else throw new Error("eval-file 不存在：" + p);
    }
    if (args.eval) {
      const r = await send("Runtime.evaluate", {
        expression: args.eval, returnByValue: true, awaitPromise: true, userGesture: true });
      evalResult = r.result && r.result.value !== undefined ? r.result.value
        : (r.exceptionDetails
          ? { __exception: r.exceptionDetails.text + " " + (r.exceptionDetails.exception?.description || "") }
          : r.result);
      // 让 eval 触发的副作用（切相机/切天气/点按钮）先渲染出若干帧，再截图
      if (args.evalDelay > 0) await sleep(args.evalDelay);
    }

    const diag = await send("Runtime.evaluate", {
      expression: `(() => {
        const g = (id) => { const e = document.getElementById(id); return e ? (e.textContent || "").trim() : null; };
        const loading = document.getElementById("loading");
        const dbg = document.getElementById("dbg");
        return {
          title: document.title,
          gpu: g("gpuBadge"), fps: g("fpsBadge"), status: g("pillStatus"),
          loadingVisible: !!(loading && getComputedStyle(loading).display !== "none"
            && getComputedStyle(loading).opacity !== "0"),
          loadingText: loading ? (loading.textContent || "").trim().slice(0, 300) : null,
          banner: g("banner"),
          debug: dbg ? dbg.textContent : null,
          hasThree: typeof THREE !== "undefined",
          threeRev: typeof THREE !== "undefined" ? THREE.REVISION : null,
          modules: {
            ssao: typeof window.UAVSSAO, terrain: typeof window.UAVTerrain,
            uav: typeof window.UAVModel, hud: typeof window.UAVHUD, weather: typeof window.UAVWeather,
          },
          canvas: (() => { const c = document.querySelector("#app canvas");
            return c ? c.width + "x" + c.height : null; })(),
        };
      })()`, returnByValue: true });
    const diagnostics = diag.result?.value ?? diag.exceptionDetails ?? null;

    let shotPath = null;
    if (args.shot) {
      const cap = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      shotPath = resolve(args.out);
      mkdirSync(dirname(shotPath), { recursive: true });
      writeFileSync(shotPath, Buffer.from(cap.data, "base64"));
    }

    const summary = {
      ok: pageErrors.length === 0,
      url: args.url,
      screenshot: shotPath,
      pageErrors,
      consoleErrors,
      consoleWarns: consoleWarns.slice(0, 20),
      consoleCount,
      evalResult,
      diagnostics,
    };
    console.log(JSON.stringify(summary, null, 2));
    ws.close();
    return pageErrors.length === 0 ? 0 : 2;
  } finally {
    cleanup();
  }
}

main().then((code) => process.exit(code)).catch((err) => {
  console.error("SHOT_FAILED:", err && err.stack ? err.stack : err);
  process.exit(1);
});
