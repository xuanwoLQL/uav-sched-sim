#!/usr/bin/env node
/* =============================================================================
   cdp_eval.mjs —— 接上"已经开着的" Chromium/WebView2 调试端口，执行一段 JS 并打印结果
   -----------------------------------------------------------------------------
   用途：桌面软件（pywebview + WebView2）窗口里的页面无法用普通浏览器工具读，
   但可以用 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333
   打开调试端口，再用本脚本读取页面里的 GPU/画质读数（这就是"软件到底用 GPU 还是 CPU 渲染"的证据）。

   用法：
     node tools/cdp_eval.mjs --port 9333 --expr "document.getElementById('gpuBadge').textContent"
   ========================================================================== */
import { argv } from "node:process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function arg(name, def) {
  const i = argv.indexOf("--" + name);
  return i >= 0 ? argv[i + 1] : def;
}

const port = arg("port", "9333");
const expr = arg("expr", "document.title");

async function waitJson(url, timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try { const r = await fetch(url); if (r.ok) return await r.json(); } catch { /* retry */ }
    await sleep(300);
  }
  return null;
}

const targets = await waitJson(`http://127.0.0.1:${port}/json/list`, 30000);
if (!targets) { console.error("CDP 端口无响应（窗口可能没开或调试参数没生效）"); process.exit(1); }
const page = targets.find((t) => (t.type === "page" || t.type === "webview") && t.webSocketDebuggerUrl);
if (!page) { console.error("没有可用的页面 target：", JSON.stringify(targets.map(t => t.type))); process.exit(1); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws error")); });
let id = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((res, rej) => {
  const i = ++id;
  pending.set(i, { res, rej });
  ws.send(JSON.stringify({ id: i, method, params }));
});
ws.onmessage = (ev) => {
  let m; try { m = JSON.parse(ev.data); } catch { return; }
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); }
};

await send("Runtime.enable");
const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
console.log(JSON.stringify(r.result?.value !== undefined ? r.result.value
  : { __exception: r.exceptionDetails?.text + " " + (r.exceptionDetails?.exception?.description || "") }, null, 2));
ws.close();
