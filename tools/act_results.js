/* =============================================================================
   tools/act_results.js —— 打开「仿真结果」界面的无头验收脚本
   -----------------------------------------------------------------------------
   用法（服务已起：python tools\serve.py --port 8791）：
     node tools/shot.mjs --gpu --url "http://127.0.0.1:8791/static/app3d.html?quality=high" `
       --out docs\images\24-仿真结果界面.png --wait 9000 `
       --eval-file tools/act_results.js --eval-delay 3500
   脚本做的事：读 `&tab=` 打开对应页签（live/charts/table/replay/ci，默认 charts），
   等界面渲染完，返回一段摘要供人工核对"截到的到底是什么"。
   注意：不切天气、不启动仿真 —— 结果界面只看磁盘产物与内存态，不需要跑仿真。
   ========================================================================== */
(async () => {
  const q = new URLSearchParams(location.search);
  const tab = (q.get("tab") || "charts").toLowerCase();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = { tab: tab, log: [] };
  const ok = (c, m) => { out.log.push((c ? "[OK] " : "[FAIL] ") + m); return !!c; };
  try {
    // `&start=1`：先起一轮仿真（可选 &stations=triple&mec=1&speed=2），
    // 这样「本次运行」页签才有内存态数据可看（否则只能看到"还没运行过"的提示）。
    if (q.get("start") === "1") {
      const qs = "?scenario=" + (q.get("scenario") || "PEAK") +
        "&scheduler=" + (q.get("scheduler") || "cross_node") +
        "&speed=" + (q.get("speed") || "2") +
        "&stations=" + (q.get("stations") || "triple") +
        "&mec=" + (q.get("mec") || "1");
      const r = await (await fetch("/api/start" + qs, { cache: "no-store" })).json();
      ok(r && r.ok !== false, "已启动一轮仿真" + qs);
      for (let i = 0; i < 60; i++) {
        await sleep(400);
        const s = await (await fetch("/api/state", { cache: "no-store" })).json();
        const t = Number(((s || {}).live || {}).sim_time || 0);
        if (t >= Number(q.get("min_t") || 12)) break;
      }
    }
    const btn = document.getElementById("btnResults");
    ok(!!btn, "#btnResults 存在（顶栏「📊 仿真结果」）");
    btn.click();
    // 等 /api/results 回来 + 首屏渲染
    for (let i = 0; i < 40; i++) {
      await sleep(250);
      const src = (document.getElementById("rxSrc") || {}).textContent || "";
      if (src && src.indexOf("读取中") < 0) break;
    }
    ok(document.getElementById("rx").classList.contains("show"), "结果遮罩已打开");
    const srcTxt = (document.getElementById("rxSrc") || {}).textContent || "";
    out.source = srcTxt;
    ok(/共 \d+ 张图/.test(srcTxt), "已读到实验产物：" + srcTxt);

    // 切到目标页签
    const tabs = [...document.querySelectorAll("#rxTabs button")];
    out.tabs = tabs.map((b) => b.textContent);
    const lab = { live: "本次运行", charts: "图表", table: "15 组矩阵",
      replay: "时序回放", compare: "对比", ci: "置信区间" }[tab];
    const want = tabs.find((b) => b.textContent.indexOf(lab) === 0);
    if (want) {
      want.click();
      await sleep(1200);
      // 注意：重新渲染页签按钮会重建 DOM（renderRxTabs 先 innerHTML="" 再 append），
      // 所以不能拿 click 前那个元素判断 on 状态，必须重新查一次。
      const cur = [...document.querySelectorAll("#rxTabs button.on")].map((b) => b.textContent);
      out.active_tab = cur;
      ok(cur.length === 1 && cur[0].indexOf(lab) === 0, "已切到「" + lab + "」（当前 " + cur.join(",") + "）");
    } else ok(false, "找不到页签 " + tab + "（实际 " + out.tabs.join(" / ") + "）");

    const body = document.getElementById("rxBody");
    out.body_html_len = body.innerHTML.length;
    out.cards = body.querySelectorAll("[data-img]").length;
    out.empty = /class="empty"/.test(body.innerHTML);
    ok(!out.empty, "页签内容非空（没有落到「还没跑过实验」的提示）");

    if (tab === "charts") {
      // 等缩略图真正解码出来（懒加载，要确认至少一张 loaded）
      const imgs = [...body.querySelectorAll("img.thumb")];
      for (let i = 0; i < 40; i++) {
        await sleep(200);
        if (imgs.some((im) => im.complete && im.naturalWidth > 0)) break;
      }
      const loaded = imgs.filter((im) => im.complete && im.naturalWidth > 0).length;
      ok(loaded > 0, `缩略图已解码 ${loaded}/${imgs.length} 张`);
      out.thumb_loaded = loaded;
      // 顺带验证灯箱：点第一张 -> 应出现 #lb.show；默认关掉灯箱，
      // 这样截图拍到的是**网格总览**（灯箱单图另有用途，见 &lb=1）。
      if (imgs.length && q.get("lightbox") !== "never") {
        imgs[0].closest("[data-img]").click();
        await sleep(900);
        const lbOpen = document.getElementById("lb").classList.contains("show");
        ok(lbOpen, "灯箱可打开");
        out.lightbox = lbOpen;
        out.lightbox_meta = (document.getElementById("lbMeta") || {}).textContent || "";
        if (q.get("lb") !== "1") {
          document.getElementById("lb").classList.remove("show");
          await sleep(300);
        }
      }
    }
    if (tab === "replay") {
      for (let i = 0; i < 40; i++) {
        await sleep(250);
        if ((document.getElementById("rpInfo") || {}).textContent && !/读取/.test(document.getElementById("rpInfo").textContent)) break;
      }
      out.replay_info = (document.getElementById("rpInfo") || {}).textContent || "";
      ok(/采样点 \d+/.test(out.replay_info), "时序数据已加载：" + out.replay_info);
      // 拖到 60% 位置，看画布是否真的画了新游标
      const r = document.getElementById("rpRange");
      r.value = String(Math.floor(Number(r.max) * 0.6));
      r.dispatchEvent(new Event("input"));
      await sleep(400);
      out.replay_t = (document.getElementById("rpT") || {}).textContent || "";
      ok(/t=[\d.]+ s/.test(out.replay_t), "滑块拖动生效：" + out.replay_t);
    }
    if (tab === "table") {
      out.rows = body.querySelectorAll("tbody tr").length;
      ok(out.rows >= 1, "矩阵表有 " + out.rows + " 行");
    }
    if (tab === "ci") {
      out.rows = body.querySelectorAll("tbody tr").length;
      ok(out.rows >= 1, "置信区间表有 " + out.rows + " 行");
    }
    if (tab === "compare") {
      for (let i = 0; i < 40; i++) {
        await sleep(250);
        if (body.querySelector("#cmpBody table")) break;
      }
      const rows = body.querySelectorAll("#cmpBody tbody tr").length;
      out.cmp_rows = rows;
      ok(rows >= 5, "对比表有 " + rows + " 行指标");
      const cv = body.querySelector("#cmpCanvas");
      ok(!!cv && cv.width > 0, "对比曲线画布已初始化（" + (cv ? cv.width + "px" : "-") + "）");
      out.cmp_tip = (body.querySelector("#cmpTip") || {}).textContent || "";
    }

    // ---- 附加动作：重跑对话框 / 真的重跑 / 导出素材包 ----
    const action = (q.get("action") || "").toLowerCase();
    if (action === "rundlg") {
      document.getElementById("rxRun").click();
      await sleep(800);
      const dlg = document.getElementById("runDlg");
      ok(dlg.classList.contains("show"), "重跑对话框已打开");
      out.run_kinds = [...dlg.querySelectorAll(".opt")].map((o) => o.dataset.kind);
      out.run_warn = (dlg.querySelector(".warn") || {}).textContent || "";
      ok(/summary\.prev\.csv/.test(out.run_warn), "对话框明确写了会覆盖什么 + 有备份");
    }
    if (action === "run") {
      // 真的跑一次（默认 stations：独立目录，不动 15 组）
      const kind = (q.get("kind") || "stations").toLowerCase();
      document.getElementById("rxRun").click();
      await sleep(600);
      const opt = document.querySelector('#runDlg .opt[data-kind="' + kind + '"]');
      ok(!!opt, "找到实验选项 " + kind);
      if (opt) opt.click();
      document.getElementById("runGo").click();
      // 轮询直到结束（最长 6 分钟）
      let last = "";
      for (let i = 0; i < 360; i++) {
        await sleep(1000);
        const j = await (await fetch("/api/run_status", { cache: "no-store" })).json();
        const job = j && j.job;
        if (!job) continue;
        last = job.status + "|" + job.total_lines;
        if (i % 10 === 0) out["run_progress_" + i] = last;
        if (!job.running) {
          out.run_status = job.status;
          out.run_rc = job.returncode;
          out.run_lines = job.total_lines;
          out.run_tail = (job.lines || []).slice(-6);
          break;
        }
      }
      ok(out.run_status === "done", "一键重跑结束且成功（status=" + out.run_status +
        " rc=" + out.run_rc + " 输出 " + out.run_lines + " 行）");
      const log = document.getElementById("runLog");
      out.run_log_shown = !!(log && log.textContent && log.textContent.length > 50);
      ok(out.run_log_shown, "对话框里实时贴出了子进程输出");
      // 关掉对话框，回结果界面看有没有刷新（&keepdlg=1 时保留，便于截"跑完的日志"）
      if (q.get("keepdlg") !== "1") {
        document.getElementById("runClose").click();
        await sleep(500);
      } else {
        await sleep(300);
      }
      const src = (document.getElementById("rxSrc") || {}).textContent || "";
      out.after_src = src;
    }
    if (action === "export") {
      document.getElementById("rxExport").click();
      let btnTxt = "";
      for (let i = 0; i < 60; i++) {
        await sleep(500);
        btnTxt = (document.getElementById("rxExport") || {}).textContent || "";
        if (/已导出|导出失败/.test(btnTxt)) break;
      }
      out.export_btn = btnTxt;
      ok(/已导出/.test(btnTxt), "导出素材包完成：" + btnTxt);
    }
  } catch (e) {
    out.log.push("[FAIL] 脚本异常：" + (e && e.message ? e.message : String(e)));
    out.error = String(e && e.stack ? e.stack : e);
  }
  return out;
})()
