/* =============================================================================
   tools/shot_stations.js —— 多基站 / MEC 场景的无头截图驱动脚本（给 --eval-file 用）
   -----------------------------------------------------------------------------
   为什么用 fetch 驱动而不是直接改前端变量：
     页面每 120ms 拉一次 /api/state 并**全量重绘**，任何直接改 weatherFX / 场景
     对象的操作都会在下一拍被覆盖。所以本脚本只做两件事：
       ① 用 fetch 调后端接口（/api/start、/api/chaos）改变**服务器侧**的仿真状态；
       ② 点击真实 DOM 按钮（#osBtn、[data-tab="station"]）让页面自己表现。
     也**不使用 ?still=1**（那会冻结渲染，截不到粒子/迁移动画/故障态渐变）。

   阶段选择（同一份脚本出五张图，靠 URL 上的 &shot= 区分，避免多开文件）：
     &shot=scene （默认）起「3 站 + MEC + cross_node」仿真并等场景推进，
                          不打开 OS 面板 —— 出「3D 多基站场景」图
     &shot=tab           在场景基础上打开 OS 透视 →「多基站」页签 —— 出「OS 透视页签」图
     &shot=down          在 tab 基础上注入 node_down(station=2) 并等故障态稳定
                          —— 出「节点宕机」图
     &shot=partition     在 tab 基础上注入网络分区 {1} | {2,3}（duration=200 仿真秒，
                          保证截图窗口内不自动自愈）—— 出「网络分区」图：
                          3D 站点灰色/虚线描边 + 每站卡片「分区/脑裂」徽章 +
                          顶栏 quorum 徽章 {1}✗ | {2,3}✓
     &shot=split_brain   在 tab 基础上注入脑裂（CENTER 一组 / EDGE 一组），并**等少数派
                          一侧真的出现"拒写"**（quorum.rejects > 0）才返回 —— 出「脑裂」图：
                          一屏同时看到两侧各自在调度（都自认主），但只有 {2,3} 一侧
                          有多数派、1 号站的写入被拒（未确认任务数 > 0）

    诚实说明：shot=split_brain 的"拒写"证据依赖后端最小 quorum 实现（docs/11 §5）。
    若后端没落地 quorum，脚本会打 [WARN] 并仍然截图 —— 此时只能证明"分区"，
    不能证明"写入被拒"，不要拿这种图当 quorum 的证据。

   用法（在项目根目录，服务已用 python tools\serve.py --port 8791 起好）：
     node tools/shot.mjs --gpu --url "http://127.0.0.1:8791/static/app3d.html?quality=high&shot=scene" \
       --out results/19-多基站-3D场景.png --wait 12000 \
       --eval-file tools/shot_stations.js --eval-delay 8000

   返回值：一个 JSON 摘要（阶段、逐条检查结论、站级快照、页签文本片段），
   shot.mjs 会把它打进 stdout 的 evalResult 字段，便于人工核对"截到的到底是什么"。
   ========================================================================== */
(async () => {
  const q = new URLSearchParams(location.search);
  const stage = (q.get("shot") || "scene").toLowerCase();
  /* `&act=1`：scene 阶段在截图前额外注入一次脑裂、等少数派出现拒写，再切到
     「地面站」机位 —— 让 3D 全景图本身能讲清"两侧各自自认主，但只有多数派那侧
     能确认写入"（docs/11 §5.3）。默认关闭，避免影响原 19 号图的复现。 */
  const log = [];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const ok = (cond, msg) => { log.push((cond ? "[OK] " : "[FAIL] ") + msg); return !!cond; };
  // 软检查：条件不满足只标记 [WARN]（不把它伪装成通过，也不因此中断截图）
  const warn = (cond, msg) => { if (!cond) log.push("[WARN] " + msg); return !!cond; };

  // 宽松版接口调用：HTTP 非 200 也把 JSON 拿回来（后端未就绪时要能如实报告，而不是抛异常）
  async function api(path) {
    const r = await fetch(path, { cache: "no-store" });
    let body = null;
    try { body = await r.json(); } catch (e) { body = { __nonjson: String(e) }; }
    return { status: r.status, body: body };
  }
  // /api/state 把实时快照嵌在 live 下（顶层另有 status/error/summary）。
  // 这里统一摊平成"既有顶层、又有 live 字段"的视图，脚本后半段两种后端布局都能读。
  async function readState() {
    const r = await api("/api/state");
    const body = (r.body && typeof r.body === "object") ? r.body : {};
    const live = (body.live && typeof body.live === "object") ? body.live : {};
    return Object.assign({}, live, {
      status: body.status,
      error: body.error,
      summary: body.summary,
      scheduler: live.scheduler || body.scheduler,
      scenario: live.scenario || body.scenario,
      weather_preset: body.weather_preset,
    });
  }
  function isRunning(s) {
    return s.status === "running" || s.running === true;
  }
  async function waitFor(fn, timeoutMs, stepMs = 250) {
    const t0 = Date.now();
    for (;;) {
      let v = null;
      try { v = await fn(); } catch (e) { /* 重试 */ }
      if (v) return v;
      if (Date.now() - t0 >= timeoutMs) return null;
      await sleep(stepMs);
    }
  }
  const stationBrief = (s) => ({
    id: s.id, name: s.name, role: s.role, online: s.online, partitioned: !!s.partitioned,
    workers: s.workers, active: s.active, queue_depth: s.queue_depth, load: s.load,
    uav_count: s.uav_count, migrations_in: s.migrations_in, migrations_out: s.migrations_out,
    migration_cost_ms: s.migration_cost_ms, done: s.done, throughput: s.throughput,
  });

  try {
    // ---------- 1. 确保仿真以「3 站 + MEC + cross_node」在跑 ----------
    let st = await readState();
    const nStations = Array.isArray(st.stations) ? st.stations.length : 0;
    log.push(`[INFO] 初始状态：status=${st.status} scheduler=${st.scheduler} stations=${nStations} mec=${st.mec ? "有" : "无"}`);
    const needStart = !(nStations >= 3 && st.scheduler === "cross_node" && isRunning(st));
    if (needStart) {
      if (isRunning(st) || nStations > 0) {
        const stop = await api("/api/stop");
        log.push(`[INFO] /api/stop -> ${stop.status}`);
        await sleep(500);
      }
      const r = await api("/api/start?scheduler=cross_node&scenario=PEAK&stations=triple&mec=1&speed=2");
      ok(r.status === 200 && r.body && r.body.ok !== false,
        `/api/start?scheduler=cross_node&scenario=PEAK&stations=triple&mec=1 -> ${r.status} ${JSON.stringify(r.body).slice(0, 200)}`);
    } else {
      log.push("[INFO] 仿真已在 3 站 cross_node 模式下运行，复用当前运行（不重启，避免丢进度）");
    }

    // ---------- 2. 等仿真真正推进（沉住气等一体数据出来） ----------
    const progressed = await waitFor(async () => {
      const s = await readState();
      if (s.error) return null;
      const t = Number(s.sim_time || 0);
      const sub = Number((s.counters || {}).submitted || 0);
      return (t >= 6 && sub > 0) ? s : null;
    }, 60000, 300);
    if (progressed) {
      st = progressed;
      ok(true, `仿真推进到 sim_time=${st.sim_time}s，已提交任务 ${(st.counters || {}).submitted} 个，`
        + `完成 ${(st.counters || {}).done} 个`);
    } else {
      st = await readState();
      ok(false, `仿真未在 60s 内推进到 6 仿真秒（当前 sim_time=${st.sim_time} status=${st.status}）`);
    }

    const sts = Array.isArray(st.stations) ? st.stations : [];
    ok(sts.length === 3, `站数为 3（实际 ${sts.length}）`);
    ok(sts.some((s) => s.role === "EDGE"), `拓扑含 EDGE 站（role=${sts.map((s) => s.role).join("/")}）`);

    // ---------- 3. 打开「多基站」页签（tab / down 两个阶段需要） ----------
    let panelText = "";
    let activeTab = "";
    async function openStationTab() {
      const panel = document.getElementById("osPanel");
      const btn = document.getElementById("osBtn");
      if (!panel || !btn) { ok(false, "页面里找不到 #osBtn / #osPanel（os_inspect 未加载？）"); return; }
      if (panel.classList.contains("hidden")) { btn.click(); await sleep(400); }
      const tab = panel.querySelector('.osTabs button[data-tab="station"]');
      if (!tab) {
        ok(false, '未找到 data-tab="station" 的「多基站」页签按钮（T2 前端尚未落地？）');
      } else {
        tab.click();
        await sleep(1400);   // 等 250ms 轮询刷新 + 重绘
        ok(panel.querySelector(".osTabs button.on") === tab, "「多基站」页签已激活");
      }
      const sec = panel.querySelector("#os-station");
      activeTab = (panel.querySelector(".osTabs button.on") || {}).textContent || "";
      panelText = ((sec && sec.textContent) || panel.textContent || "").replace(/\s+/g, " ").trim().slice(0, 500);
    }

    // ---------- 4. 阶段分支 ----------
    if (stage === "tab") {
      await openStationTab();
    } else if (stage === "down") {
      // 顺序与时长都是为"截图一定落在故障态"服务的：
      //   ① **先**把「多基站」页签渲染好（切页签要几百毫秒，不能占用故障窗口）；
      //   ② 注入时 `duration=200`（≈"不自动恢复"）。注意 duration 是**仿真秒**，
      //      speed=2 下 200 仿真秒 = 100 秒真实时间，远大于 --eval-delay(8s)，
      //      因此从 eval 返回到真正截图这段时间里 2 号站**稳定 offline**，
      //      不会像 duration=20 那样（10s 真实）在截图前一刻自动恢复；
      //   ③ 注入成功后再 sleep(200) 让 250ms 轮询至少刷新一次，然后立即返回，
      //      把剩余时间全留给 --eval-delay 渲染故障态（暗红/熄灭 + 离线标注）。
      await openStationTab();

      async function injectDownOnce(label) {
        const r = await api("/api/chaos?kind=node_down&station=2&duration=200");
        ok(r.status === 200 && r.body && r.body.ok !== false,
          `${label} /api/chaos?kind=node_down&station=2&duration=200 -> ${r.status} `
          + JSON.stringify(r.body).slice(0, 160));
        return await waitFor(async () => {
          const s = await readState();
          const ids = ((s.chaos || {}).nodes_down) || [];
          const dead = (Array.isArray(s.stations) ? s.stations : [])
            .find((x) => x.id === 2 && x.online === false);
          return (ids.indexOf(2) >= 0 || dead) ? s : null;
        }, 6000, 120);
      }

      let down = await injectDownOnce("注入");
      if (!down) {
        down = await injectDownOnce("补注");
      }
      if (down) {
        st = down;
        const s2 = (Array.isArray(st.stations) ? st.stations : []).find((x) => x.id === 2) || {};
        ok(s2.online === false, "2 号站已置为 offline（online=False）");
        /* 口径提醒（docs/11 §5.2）：宕机只**疏散排队中**的任务；已经进到站内线程
           在跑的任务会在仿真里继续跑完 —— 所以 `active` 短暂为上 1 是**设计如此**，
           不是没清空。这里按"疏散后队列深度归零"判定更贴近真实语义。 */
        ok(Number(s2.queue_depth || 0) === 0,
          `2 号站排队任务已疏散（queue_depth=${s2.queue_depth}）`);
        if (Number(s2.active || 0) > 0) {
          log.push(`[INFO] 2 号站在跑任务数 active=${s2.active}：宕机不打断已进线程的任务（§5.2 口径）`);
        }
      } else {
        ok(false, "注入 node_down 后 6s 内未观察到 2 号站 offline / chaos.nodes_down 含 2");
      }
      // 让 250ms 轮询至少把"2 号站离线"渲染进页面，然后**立即返回**（剩余时间留给截图渲染）
      await sleep(200);
      const panel = document.getElementById("osPanel");
      const sec = panel && panel.querySelector("#os-station");
      if (sec) panelText = (sec.textContent || "").replace(/\s+/g, " ").trim().slice(0, 500);
    } else if (stage === "partition" || stage === "split_brain") {
      /* 分区 / 脑裂：两个阶段共用「注入 → 等分区落到快照」的骨架，差别在 kind、
         期望的快照字段，以及"要不要等少数派出现拒写"。
         时长统一 duration=200（**仿真秒**）：speed=2 下 ≈100 秒真实时间，远大于
         --eval-delay(6~8s)，因此从 eval 返回 → 真正截图的这段时间里分区稳定存在，
         不会像 duration=20 那样在截图前一刻自动自愈。 */
      const isSplit = (stage === "split_brain");
      await openStationTab();
      const q1 = isSplit ? "kind=split_brain&duration=200"
                         : "kind=partition&a=1&b=2,3&duration=200";
      const r = await api("/api/chaos?" + q1);
      ok(r.status === 200 && r.body && r.body.ok !== false,
        "/api/chaos?" + q1 + " -> " + r.status + " " + JSON.stringify(r.body).slice(0, 200));

      // ① 等分区真的落到快照里：chaos.partitions 两组 + 至少 2 个站 partitioned
      const got = await waitFor(async () => {
        const s = await readState();
        const parts = ((s.chaos || {}).partitions) || [];
        const nPart = (Array.isArray(s.stations) ? s.stations : []).filter((x) => x.partitioned).length;
        const sb = !!(s.chaos || {}).split_brain;
        return (parts.length >= 2 && nPart >= 2 && (!isSplit || sb)) ? s : null;
      }, 8000, 150);
      if (got) {
        st = got;
        const groups = st.chaos.partitions;
        const rep = (Array.isArray(st.stations) ? st.stations : [])
          .map((x) => "#" + x.id + (x.partition_group != null ? "(组" + x.partition_group + ")" : "")
            + (x.is_majority === false ? "✗无多数派" : "")).join(" ");
        ok(true, "分区已生效：partitions=" + JSON.stringify(groups) + "；站分布 " + rep
          + (isSplit ? ("；split_brain=" + st.chaos.split_brain) : ""));
      } else {
        st = await readState();
        ok(false, "注入 " + (isSplit ? "split_brain" : "partition")
          + " 后 8s 内未观察到 partitions>=2 / partitioned 站"
          + (isSplit ? " / split_brain" : ""));
      }

      // ② 脑裂阶段：等少数派一侧出现"拒写"证据 —— 没有它，图只能证明"分区"，
      //    证明不了"分区期间写入被拒"这个 quorum 语义（docs/11 §5 的验收点）。
      if (isSplit) {
        const rejected = await waitFor(async () => {
          const s = await readState();
          const qq = s.quorum || {};
          const n = Number(qq.rejects != null ? qq.rejects : qq.quorum_rejects) || 0;
          return n > 0 ? s : null;
        }, 25000, 250);
        if (rejected) {
          st = rejected;
          const qq = st.quorum || {};
          const nRej = qq.rejects != null ? qq.rejects : qq.quorum_rejects;
          const nUnc = qq.uncommitted != null ? qq.uncommitted : qq.uncommitted_tasks;
          ok(true, "少数派分区已出现拒写：rejects=" + nRej + "，uncommitted=" + nUnc
            + "，term=" + qq.term + "，majority=" + qq.majority);
        } else {
          st = await readState();
          warn(false, "25s 内没等到 quorum.rejects>0：可能后端未实现最小 quorum，"
            + "或少数派一侧还没跑完任务。此图只能作为「分区」证据，不能作为「拒写」证据。");
        }
      }
      // 让 250ms 轮询把新状态渲染进页面（顶栏 quorum 徽章 + 每站卡片徽章），然后立即返回
      await sleep(400);
      const panel = document.getElementById("osPanel");
      const sec = panel && panel.querySelector("#os-station");
      if (sec) panelText = (sec.textContent || "").replace(/\s+/g, " ").trim().slice(0, 500);
    } else {
      log.push("[INFO] shot=scene：不打开 OS 面板，保持 3D 多基站场景");
      if (q.get("act") === "1") {
        // 脑裂 + 等拒写 + 切「地面站」机位（见文件头 &act=1 说明）
        const r = await api("/api/chaos?kind=split_brain&duration=200");
        ok(r.status === 200 && r.body && r.body.ok !== false,
          `/api/chaos?kind=split_brain -> ${r.status}`);
        const rejected = await waitFor(async () => {
          const s = await readState();
          const n = Number((s.quorum || {}).rejects) || 0;
          return n > 0 ? s : null;
        }, 25000, 400);
        if (rejected) {
          st = rejected;
          ok(true, `少数派分区已出现拒写：rejects=${(st.quorum || {}).rejects}，`
            + `uncommitted=${(st.quorum || {}).uncommitted}，term=${(st.quorum || {}).term}`);
        } else {
          st = await readState();
          warn(false, "25s 内没等到 quorum.rejects>0（只当分区证据，不当拒写证据）");
        }
        const cam = document.querySelector('[data-cam="station"]');
        /* 用页面暴露的 __uavCam()（存在才用）：OrbitControls 开了阻尼，点按钮后
           下一帧的 update() 会把相机平滑拉回去，截图容易拍到"过渡中"的机位。 */
        let camOk = false;
        if (typeof window.__uavCam === "function") camOk = window.__uavCam("station");
        if (!camOk && cam) { cam.click(); camOk = true; }
        log.push(camOk ? "[OK] 已切到「地面站」机位（G1✗少 / G2✓多 标签在画面里）"
          : "[WARN] 找不到切机位入口，保持默认机位");
        await sleep(1500);   // 相机过渡 + 标签 1.2s 节流重建
      }
    }

    // ---------- 5. 汇总 ----------
    const out = {
      stage: stage,
      log: log,
      sim_time: st.sim_time,
      scheduler: st.scheduler,
      scenario: st.scenario,
      uav_count: st.uav_count,
      stations: (Array.isArray(st.stations) ? st.stations : []).map(stationBrief),
      station_of_uav_sample: Object.fromEntries(
        Object.entries(st.station_of_uav || {}).slice(0, 8)),
      link_sample: Object.fromEntries(Object.entries(st.link_matrix || {}).slice(0, 3)),
      mec: st.mec || null,
      chaos: st.chaos || null,
      /* quorum（最小共识）快照：partition / split_brain 两张图的核心证据在这里。
         后端未落地时是 null —— 脚本会明确标出，别把"缺证据"当成"通过"。 */
      quorum: st.quorum || null,
      os_panel_open: !!(document.getElementById("osPanel")
        && !document.getElementById("osPanel").classList.contains("hidden")),
      os_active_tab: activeTab,
      os_station_text: panelText,
    };
    console.log("SHOT_STATIONS " + JSON.stringify(out));
    return out;
  } catch (e) {
    log.push("[FAIL] 脚本异常：" + (e && e.message ? e.message : String(e)));
    return { stage: stage, log: log, error: String(e && e.stack ? e.stack : e) };
  }
})()
