/* =============================================================================
   hud_overlay.js —— HUD 数据可视化（任务流向箭头 + 队列三维瀑布图）
   =============================================================================

   为什么需要这个模块？
   --------------------
   `app3d.html` 里已经有"队列柱"（两根会升高的方块）和若干细线链路，但看不出两件
   课程报告最需要讲清楚的事：
     ① **业务往哪流、流多快**——哪架机正在把数据往地面站传、高优/低优的差别；
     ② **队列深度怎么随时间起伏**——调度策略（dynamic / 静态优先级）在时间轴上的
        差异，2D 折线图很难放进 3D 场景里讲。

   于是本模块提供两个纯"数据可视化"图层：
     A. 任务流向箭头：busy 无人机 →（二次贝塞尔弧）→ 地面站天线顶，一串流动的小
        棱锥箭头；颜色按优先级（HIGH 蓝 / LOW 琥珀），下行调度指令是反向的小号
        青色箭头；弱网（health < 55）时箭头变稀、变短、带抖动——对应真实无人机
        数传链路的重传/丢包现象。
     B. 队列三维瀑布：悬浮在地面站后上方，X = 时间（最近 N 个采样）、Z = 两条
        泳道（高优/低优）、Y = 队列深度，用三角带 + 顶点色的"半透明加色帘幕"
        表示，最新数据靠近地面站一端；网格基线 + 深度刻度 + 时间刻度 + 当前时刻
        亮边 + 文字 sprite 一起构成可读的 3D 图表。

   技术约束（必须遵守，否则会拖垮主场景 30 架机 + 地形）：
     * three.js **r128**、经典脚本（非 ES Module）、全局 THREE；
     * 脚本加载期只定义函数，绝不 new THREE.*（见契约 §2.3）；
     * update() 每帧只改**预分配**矩阵/顶点数组，不新建几何体、不重建 geometry、
       不在循环里 new 任何对象（零分配，避免 GC 抖动）；
     * 箭头全部塞进 **一个 InstancedMesh**（1 次 draw call），颜色用 instanceColor；
     * live == null（仿真未开始）时箭头全隐藏、瀑布只画空坐标系，不报错；
     * cpu 档（软件光栅）自动降采样/降箭头数，保证不崩。

   对外只暴露一个全局：window.UAVHUD = { version, create(opts), selfTest() }。
   ============================================================================= */
(function () {
  "use strict";

  var VERSION = "1.2.0";   // 1.2.0：世界尺寸自适应（352×256 新世界）+ 尺寸放大 + 修复重复 label

  /* -------------------------------------------------------------------------
     颜色：与 app3d.html 里的 C 常量逐位一致（验收会按十六进制核对）
     ------------------------------------------------------------------------- */
  var COL_HIGH = 0x4ea1ff;   // 高优任务上行数据流：蓝
  var COL_LOW = 0xf5a623;    // 低优任务上行数据流：琥珀
  var COL_CMD = 0x37e2d5;    // 下行调度指令：青
  var COL_LOAD = 0x3ddc97;   // system_load 细线：绿
  var COL_GRID = 0x2f5570;   // 瀑布网格
  var COL_GRID_DIM = 0x1e3a4d; // 时间淡网格
  var COL_EDGE = 0xbff4ff;   // "当前时刻"亮边

  /* -------------------------------------------------------------------------
     默认参数（契约 §4.4 / 任务书 / Lead 2026-10、2026-11 验收调整）
     ------------------------------------------------------------------------- */
  var DEF_MAX_ARROWS = 240;
  var DEF_SLOTS = 90;

  /* ---- 世界尺寸自适应基准 ----
     2026-11 世界从 176×128 放大到 352×256（面积 ×4，真实 DEM 1024×760），
     相机默认机位也拉远了 ~1.8 倍。为了让 HUD 在任何世界尺寸下都保持同样观感，
     这里以"旧世界 176×128"为设计基准：
       worldScale = max(world.width/176, world.depth/128)   // 当前 = 2.0
       worldAdapt = max(world.width/352, world.depth/256)   // 当前 = 1.0（新世界基准）
     anchor / 时间轴长度 / 高度比例 / 刻度数量都随 worldScale 缩放（见 create），
     这样以后换更大 DEM（比如再放大一倍）也不用改代码。 */
  var WORLD_REF_W = 176, WORLD_REF_D = 128;
  var WORLD_BASE_W = 352, WORLD_BASE_D = 256;

  // 深度比例：2026-11 世界放大后按 Lead 要求从 0.13 提到 0.22（旧基准 0.22 相当于
  // 新世界每"条"队列对应 0.22 世界单位高），MAX 放宽到 0.30 防止数据把图顶穿。
  var DEF_HEIGHT_SCALE = 0.22;
  var MAX_HEIGHT_SCALE = 0.30;
  // 时间轴长度倍率：25×2.2 = 55 世界单位（1000px 高、相机 ~290 距离下屏幕约 240px，
  // 能一眼读出队列起伏；45 单位实测仍偏小，故略放大；仍可用 quality.waterfallScale 调）。
  var DEF_WATERFALL_SCALE = 2.2;
  // 锚点：矿区采坑右上方、地面站前方（Lead 2026-11 指定）——基准坐标按旧世界设计，
  // create 里会乘 worldScale 自适应（当前 → (48,44,12)）。y 基准从 24 微调到 22：
  // 世界放大后图更高，地面站机位是仰拍，太高的锚点会把标题顶到顶部工具栏后面。
  var DEF_ANCHOR = { x: 24, y: 22, z: 6 };
  var DEF_STATION_TOP = { x: 0, y: 17, z: 44 }; // 主页面 UPLINK_TOP 的默认值

  // ---- 相机距离自适应阈值（Lead 2026-11 验收要求，基准按旧世界，create 里乘 worldScale）----
  // 相机到瀑布锚点距离 < CAM_FADE_NEAR：整体隐藏（近景不挡镜头）；
  // CAM_FADE_NEAR ~ CAM_FADE_FAR：smoothstep 淡出/淡入；>= CAM_FADE_FAR：完全恢复。
  var CAM_FADE_NEAR = 22;
  var CAM_FADE_FAR = 45;
  // 文字 sprite 的字号放大系数（Lead 2026-11：世界放大后标题/刻度要更清晰）。
  // 重要：主页面有全局 sprite 距离归一化（app3d.fitSprites，屏幕尺寸恒定），
  // sprite 的世界尺寸 = canvas 像素宽 × opt.scale。fontSize ×1.6 会让 canvas 变宽 1.6 倍，
  // 所以 scale 必须 ÷1.6 抵消，才能保持原有的世界尺寸/屏幕尺寸（否则标题会比整张图还宽——实测）。
  // 净效果：字体更清晰（更高分辨率纹理），屏幕尺寸与放大前一致，与图表尺寸比例正确。
  var SPRITE_FONT_GROW = 1.6;

  var MAX_LINKS = 96;        // 同时可视化的链路上限（30 架机足够，留余量）
  var VEIN_SEG = 12;         // 每条链路"弧线血管"的线段数
  var WF_SPAN = 25;          // 瀑布时间轴基准世界长度（旧世界 25；乘 waterfallScale 后为最终值）
  // 两条泳道在 Z 上各偏移 ±4.0：原来 ±1.8 在斜视角下会叠成一团，
  // 加宽后两条"瀑布带"在屏幕上明显错开，中间能看到空隙（Lead 2026-10 验收要求）。
  var WF_LANE_Z = 4.0;
  // 朝向相机时故意偏 40°：完全正对会把两条泳道在屏幕上压成一条线，
  // 看不出"Z 方向双泳道"的三维结构；偏一点既保持可读，又能看到两块帘幕的厚度与空隙。
  var FACING_BIAS = 0.7;
  var WF_LANE_HALF_W = 1.0;  // 每条泳道"瀑布带"的半宽（越宽越像一条有厚度的带子）
  var WF_DEPTH_MAX = 140;    // 队列深度可视化上限（超过就压平，避免冲出画面）
  // 箭头尺寸系数：3.0 单位长的棱锥在 100+ 距离上约 15~20 像素，既能看清又不糊成一片。
  // 太大 → 添加剂混合下会互相叠成白色光团；太小 → 截图里看不出流向。
  var ARROW_SIZE = 0.68;

  var MODES = { all: true, flow: true, waterfall: true, off: true };

  /* -------------------------------------------------------------------------
     纯逻辑小工具（不依赖 THREE —— selfTest 可以直接验证，不用 renderer）
     ------------------------------------------------------------------------- */
  function _num(v, d) {
    return (typeof v === "number" && isFinite(v)) ? v : d;
  }
  function _clamp(v, a, b) {
    return v < a ? a : (v > b ? b : v);
  }
  function _clampInt(v, a, b) {
    v = Math.round(v);
    return v < a ? a : (v > b ? b : v);
  }

  /**
   * 二次贝塞尔曲线求值：B(u) = (1-u)²·P0 + 2(1-u)u·P1 + u²·P2
   * cap 是扁平化的 Float32Array（每条链路 9 个数：P0xyz, P1xyz, P2xyz）。
   * 直接写进 out（Vector3 或 {x,y,z}），不产生临时对象 = update() 零分配。
   */
  function _bezPoint(out, cap, off, u) {
    var mu = 1 - u;
    var a = mu * mu, b = 2 * mu * u, c = u * u;
    out.x = a * cap[off] + b * cap[off + 3] + c * cap[off + 6];
    out.y = a * cap[off + 1] + b * cap[off + 4] + c * cap[off + 7];
    out.z = a * cap[off + 2] + b * cap[off + 5] + c * cap[off + 8];
  }

  /** 二次贝塞尔切线（未归一化）：B'(u) = 2(1-u)(P1-P0) + 2u(P2-P1) */
  function _bezTan(out, cap, off, u) {
    var mu = 1 - u;
    out.x = 2 * mu * (cap[off + 3] - cap[off]) + 2 * u * (cap[off + 6] - cap[off + 3]);
    out.y = 2 * mu * (cap[off + 4] - cap[off + 1]) + 2 * u * (cap[off + 7] - cap[off + 4]);
    out.z = 2 * mu * (cap[off + 5] - cap[off + 2]) + 2 * u * (cap[off + 8] - cap[off + 5]);
  }

  /**
   * 弱网 → 箭头密度系数。
   * health >= 75 正常；55~75 轻微掉；< 55 明显变稀（约 42%）。
   * 物理意义：链路健康度差 = 上行字节被重传/丢了，可视化上就是"能看见的包变少"。
   */
  function _healthDensity(health) {
    if (health >= 75) return 1.0;
    if (health >= 55) return 0.78;
    return 0.42;
  }

  /** 弱网 → 箭头飞行长度系数（< 1 表示只飞半程就"断"了，模拟丢包） */
  function _healthLen(health) {
    return health < 55 ? 0.62 : 1.0;
  }

  /** 速度倍频：∝ 任务上传进度（0.6 ~ 1.4 倍频），弱网再慢 0.55 倍 */
  function _arrowSpeed(progress, health) {
    var mul = 0.6 + 0.8 * _clamp(progress, 0, 1);
    if (health < 55) mul *= 0.55;
    return mul;
  }

  /**
   * 把每条链路的"期望箭头数"按总上限等比压缩，返回实际使用总数。
   * 注意：调用方渲染时仍要用 k < maxArrows 兜底（链路非常多时 1 支/条也会超限）。
   */
  function fitCounts(req, n, maxTotal, out) {
    var total = 0, i;
    for (i = 0; i < n; i++) total += req[i];
    var scale = total > maxTotal ? maxTotal / total : 1;
    var used = 0;
    for (i = 0; i < n; i++) {
      var c = Math.floor(req[i] * scale);
      if (c < 1) c = 1;
      out[i] = c;
      used += c;
    }
    return used;
  }

  /** 几何预算（selfTest 用来核对实际分配的顶点/三角数，防止悄悄改坏） */
  function _geomPlan(slots, maxArrows) {
    var seg = Math.max(0, slots - 1);
    var ribbonTris = seg * 8;          // 2 泳道 ×（帘幕 2 + 顶带 2）三角/段
    var arrowTris = maxArrows * 8;     // 每个四棱锥：4 侧面 + 4 底盖
    var ribbonVerts = slots * 2 * 4;   // 帘幕/顶带 × 2 泳道 × 每 slot 2 顶点
    var lineVerts = slots * 2;         // 两条顶边折线
    return {
      ribbonTris: ribbonTris,
      arrowTris: arrowTris,
      tris: ribbonTris + arrowTris,
      ribbonVerts: ribbonVerts,
      lineVerts: lineVerts,
    };
  }

  /* 共享的空数组/空 ctx：update 里拿不到数据时复用，避免每帧 new */
  var EMPTY_ARRAY = [];

  /**
   * 世界尺寸 → 缩放系数（以旧世界 176×128 为 1.0）。
   * HUD 的 anchor / 时间轴 / 高度 / 相机淡出阈值都按这个系数缩放，
   * 这样世界从 176→352（×2）时 HUD 跟着放大一倍，不用再改代码。
   */
  function _worldScale(w, d) {
    var sw = _num(w, WORLD_REF_W) / WORLD_REF_W;
    var sd = _num(d, WORLD_REF_D) / WORLD_REF_D;
    var s = Math.max(sw, sd);
    return (s > 0.05) ? s : 1;
  }

  /**
   * 相机到瀑布锚点距离 → 淡出系数（0 = 整体隐藏，1 = 完全显示）。
   * 为什么需要它：跟随 / 环绕 / 矿区近景机位下，瀑布图会悬在镜头正前方变成一大块
   * 半透明矩形挡住机群（Lead 2026-11 验收问题 1）。近距离让它淡出既不影响数据
   * （拉远立刻恢复），也不会挡近景。
   * threshold 按 worldScale 缩放（Lead 要求）：near=22*ws、far=45*ws。
   * 用 smoothstep 而不是线性，是避免阈值处出现"亮度突变"的硬边。
   */
  function _camFade(d, ws) {
    var s = (typeof ws === "number" && ws > 0) ? ws : 1;
    var dn = d / s;                       // 归一到"旧世界"距离再套阈值
    if (!(dn > 0)) return 0;
    if (dn <= CAM_FADE_NEAR) return 0;
    if (dn >= CAM_FADE_FAR) return 1;
    var u = (dn - CAM_FADE_NEAR) / (CAM_FADE_FAR - CAM_FADE_NEAR);
    return u * u * (3 - 2 * u);
  }

  /* =========================================================================
     create(opts) —— 在 initScene() 里调用一次
     ========================================================================= */
  function create(opts) {
    opts = opts || {};
    var log = (typeof opts.log === "function") ? opts.log : function (m) {
      try { console.log(m); } catch (e) { /* 无 console 也不崩 */ }
    };

    try {
      var THREE = opts.THREE;
      if (!THREE) throw new Error("opts.THREE 缺失");
      if (!opts.scene) throw new Error("opts.scene 缺失");
      if (!opts.camera) throw new Error("opts.camera 缺失");

      /* -------------------- 参数归一化 & 档位缩放 -------------------- */
      var tier = opts.tier || "high";
      var isCpu = tier === "cpu";
      var isMed = tier === "medium";
      var q = opts.quality || {};

      /* ---- 世界尺寸自适应（2026-11 世界从 176×128 放大到 352×256）---- */
      var worldW = _num(opts.world && opts.world.width, WORLD_REF_W);
      var worldD = _num(opts.world && opts.world.depth, WORLD_REF_D);
      var worldScale = _worldScale(worldW, worldD);          // 相对旧世界；当前 2.0
      var worldAdapt = Math.max(worldW / WORLD_BASE_W, worldD / WORLD_BASE_D); // 相对新世界；当前 1.0
      if (!(worldAdapt > 0.1)) worldAdapt = 1;

      var maxArrows = _clampInt(_num(q.maxArrows, DEF_MAX_ARROWS), 8, 1200);
      if (isCpu) maxArrows = Math.min(maxArrows, 96);       // cpu 档：软件光栅扛不住太多实例
      else if (isMed) maxArrows = Math.min(maxArrows, 180);
      var slots = _clampInt(_num(q.waterfallSlots, DEF_SLOTS), 16, 240);
      if (isCpu) slots = Math.min(slots, 56);
      else if (isMed) slots = Math.min(slots, 72);

      // ---- 瀑布尺寸：heightScale 控制深度方向，waterfallScale 控制时间轴长度 ----
      // 注意"下限保护"：app3d 里可能还传着世界放大前的旧值（heightScale 0.13 / waterfallScale 1.0），
      // 这里取 max(传入值, 新默认值)，保证旧接线也能自动拿到放大后的尺寸；传更大的值则按传入值。
      var heightScaleRaw = _num(q.heightScale, DEF_HEIGHT_SCALE);
      var heightScaleEff = Math.max(heightScaleRaw, DEF_HEIGHT_SCALE) * worldAdapt;
      var heightScale = _clamp(heightScaleEff, 0.04, MAX_HEIGHT_SCALE);
      if (Math.abs(heightScale - heightScaleRaw) > 1e-6) {
        log("[HUD] heightScale=" + heightScaleRaw + " → 实际 " + heightScale.toFixed(3) +
          "（世界自适应 ×" + worldAdapt.toFixed(2) + "，上限 " + MAX_HEIGHT_SCALE + "）");
      }
      var wfScaleRaw = _num(q.waterfallScale, DEF_WATERFALL_SCALE);
      var wfScale = Math.max(wfScaleRaw, DEF_WATERFALL_SCALE) * worldAdapt;
      if (!(wfScale > 0.2)) wfScale = DEF_WATERFALL_SCALE;
      var wfSpan = WF_SPAN * wfScale;              // 时间轴世界长度（当前 25×1.8 = 45）
      var wfMaxY = WF_DEPTH_MAX * heightScale;     // 深度轴最大高度（当前 140×0.22 = 30.8）
      var tickMul = _clamp(worldScale, 1, 3);      // 刻度密度随世界尺寸增加（当前 ×2）
      var fgrow = SPRITE_FONT_GROW;                // 文字字号放大系数（画布更清晰，世界尺寸不变）
      var fscl = 1 / fgrow;                        // sprite scale 同比缩小，抵消字号放大
      var wfLoadScale = wfMaxY / 3;                // system_load 1.0 对应的世界高度（占满图高 1/3）
      var arrowGrow = _clamp(worldScale, 1, 3);    // 箭头/弧线拱高等世界尺寸随世界放大

      var scene = opts.scene;
      var camera = opts.camera;

      /* ---- 幂等保护（Lead 2026-11 验收问题 2：标题/泳道名重复）----
         app3d.html 的 initUpgradeModules() 目前被调用了两次（1647 / 1667 行），
         会创建两套 HUD → 标题、泳道名、网格全部叠两份。这里在 create 时发现同一 scene
         上已有旧实例就先 dispose（移除旧 group + 释放几何/材质），保证场景里永远只有一套。 */
      try {
        var prevHud = scene.userData && scene.userData.__UAVHUD_ACTIVE;
        if (prevHud && typeof prevHud.dispose === "function") {
          log("[HUD] 检测到旧 HUD 实例，先 dispose 再重建（避免重复 label）");
          prevHud.dispose();
        }
      } catch (ePrev) { /* 旧实例释放失败也继续创建 */ }

      /* -------------------- 颜色管理（r128 线性工作流） --------------------
         主页面 renderer 固定 `outputEncoding = sRGBEncoding`（app3d.html 第 1325 行）：
         如果把设计稿色值（#4ea1ff）直接当线性值喂给材质，最终会被再编码一次 sRGB，
         屏幕上变成发白的浅蓝/浅琥珀——"加色混合"下甚至直接变白，验收就看不出配色。
         这里做两件事：
           ① 把十六进制色先转成**线性色**（convertSRGBToLinear），屏幕上看回来就是原色；
           ② 让 HUD 的数据材质 `toneMapped = false`：HUD 是数据图层，不该被场景曝光/ACES
              压暗或去饱和（主页面切黎明/黄昏/夜景时，箭头颜色也要保持不变）。
         若调用方传了 renderer，就按它的实际 outputEncoding 判断，没传则默认 sRGB。 */
      var srgbOut = true;
      if (opts.renderer && THREE.sRGBEncoding !== undefined &&
        opts.renderer.outputEncoding !== undefined) {
        srgbOut = (opts.renderer.outputEncoding === THREE.sRGBEncoding);
      }
      function lin(hex) {
        var c = new THREE.Color(hex);
        if (srgbOut && c.convertSRGBToLinear) c.convertSRGBToLinear();
        return c;
      }
      function srgb01ToLinear(c) {
        if (!srgbOut) return c;
        return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
      }
      function noTone(mat) {
        if (mat && "toneMapped" in mat) mat.toneMapped = false;
        return mat;
      }
      // 预先算好线性色常量（update 里只 copy / multiplyScalar，不 new、不重算 pow）
      var COL_HIGH_LIN = lin(COL_HIGH);
      var COL_LOW_LIN = lin(COL_LOW);
      var COL_CMD_LIN = lin(COL_CMD);

      // anchor：调用方显式给了世界坐标就原样使用；否则按世界尺寸缩放基准锚点
      // （基准 (24,24,6) 是旧世界 176×128 下 tuned 的位置 → 当前世界 ×2 = (48,48,12)）。
      var ax, ay, az;
      if (opts.anchor) {
        ax = _num(opts.anchor.x, DEF_ANCHOR.x);
        ay = _num(opts.anchor.y, DEF_ANCHOR.y);
        az = _num(opts.anchor.z, DEF_ANCHOR.z);
      } else {
        ax = DEF_ANCHOR.x * worldScale;
        ay = DEF_ANCHOR.y * worldScale;
        az = DEF_ANCHOR.z * worldScale;
      }

      /* -------------------- 场景图：一个根组，两个子层 -------------------- */
      var group = new THREE.Group();
      group.name = "UAVHUD";
      var flowGroup = new THREE.Group();       // 任务流向箭头层
      flowGroup.name = "UAVHUD_Flow";
      var wfGroup = new THREE.Group();         // 队列三维瀑布层（绕自身锚点旋转朝向相机）
      wfGroup.name = "UAVHUD_Waterfall";
      wfGroup.position.set(ax, ay, az);
      group.add(flowGroup);
      group.add(wfGroup);
      scene.add(group);                        // 契约：handle.group 已 add 到 scene

      /* =====================================================================
         A. 任务流向箭头
         ===================================================================== */

      // 箭头几何：四棱锥（小三角锥），锥尖朝 +Z。
      // 为什么朝 +Z：three 的 ConeGeometry 默认锥尖朝 +Y，绕 X 轴 +90° 后变成 +Z，
      // 这样每帧只要把 +Z 用 quaternion.setFromUnitVectors 对到曲线切线，就能让
      // 箭头"顺着弧线飞"，不用逐帧算欧拉角。
      // 世界放大后箭头也要同比例放大，否则在拉远的相机里会变成看不见的小点。
      var arrowGeo = new THREE.ConeGeometry(0.8 * ARROW_SIZE * arrowGrow,
        3.0 * ARROW_SIZE * arrowGrow, 4, 1);
      arrowGeo.rotateX(Math.PI / 2);
      arrowGeo.computeVertexNormals();

      // 加色混合 + 关闭深度写入：箭头互相叠加时更亮，也不会被半透明瀑布挡出黑边。
      // noTone：HUD 数据图层不参与场景 ACES 曝光，颜色才不会被"洗白"。
      // opacity 0.72（Lead 反馈：比瀑布图略暗一点，别抢视觉焦点；原来是 0.85）。
      var arrowMat = noTone(new THREE.MeshBasicMaterial({
        transparent: true,
        opacity: 0.72,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        side: THREE.DoubleSide,
        fog: false,
      }));

      var arrows = new THREE.InstancedMesh(arrowGeo, arrowMat, maxArrows);
      arrows.name = "UAVHUD_Arrows";
      arrows.frustumCulled = false;   // 每帧顶点都在动，包围球没意义，关掉更安全
      arrows.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      // 先 setColorAt(0) 一次，让 r128 按 maxArrows 尺寸分配 instanceColor；
      // 之后再缩小 arrows.count 就不会把颜色缓冲缩水（否则越界写颜色会被静默丢弃）。
      arrows.setColorAt(0, new THREE.Color(0x000000));
      if (arrows.instanceColor) arrows.instanceColor.setUsage(THREE.DynamicDrawUsage);
      arrows.count = 0;
      flowGroup.add(arrows);

      // 弧线"血管"：每条 busy 链路一条极淡的细线，用来解释"这支箭是从哪架机飞出来的"。
      // 用 LineSegments 批量画（1 次 draw call），顶点数组在 create 时一次性分配。
      var veinPos = new Float32Array(MAX_LINKS * VEIN_SEG * 2 * 3);
      var veinAttr = new THREE.BufferAttribute(veinPos, 3);
      veinAttr.setUsage(THREE.DynamicDrawUsage);
      var veinGeo = new THREE.BufferGeometry();
      veinGeo.setAttribute("position", veinAttr);
      veinGeo.setDrawRange(0, 0);
      var veinMat = noTone(new THREE.LineBasicMaterial({
        color: lin(0x2f6f96),
        transparent: true,
        opacity: 0.26,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        fog: false,
      }));
      var veins = new THREE.LineSegments(veinGeo, veinMat);
      veins.frustumCulled = false;
      flowGroup.add(veins);

      // ---- 每帧复用的标量/对象缓存（全部在 create 期分配好 = update 零 new） ----
      var linkCap = new Float32Array(MAX_LINKS * 9);   // 上行弧控制点
      var linkCap2 = new Float32Array(MAX_LINKS * 9);  // 下行弧控制点（反向）
      var linkHealth = new Float32Array(MAX_LINKS);
      var linkPrio = new Uint8Array(MAX_LINKS);        // 1 = HIGH
      var linkProg = new Float32Array(MAX_LINKS);
      var linkSpeed = new Float32Array(MAX_LINKS);
      var linkPhase = new Float32Array(MAX_LINKS);
      var linkDensity = new Float32Array(MAX_LINKS);
      var linkShort = new Float32Array(MAX_LINKS);
      var linkCount = new Int32Array(MAX_LINKS);       // 每条链路的期望箭头数
      var linkActual = new Int32Array(MAX_LINKS);      // 压缩后的实际箭头数

      var tVec = new THREE.Vector3();
      var tVec2 = new THREE.Vector3();
      var tTan = new THREE.Vector3();
      var tScale = new THREE.Vector3(1, 1, 1);
      var tQuat = new THREE.Quaternion();
      var tMat = new THREE.Matrix4();
      var tColor = new THREE.Color();
      var AXIS_Z = new THREE.Vector3(0, 0, 1);

      /* =====================================================================
         B. 队列三维瀑布图
         ===================================================================== */

      // 局部坐标约定：x ∈ [-wfSpan, 0]，最新采样在 x = 0（靠近地面站一端）；
      // 两条泳道 z = ±WF_LANE_Z（中间留明显空隙）；y = 队列深度 × heightScale。
      var wfDx = wfSpan / Math.max(1, slots - 1);

      // 高优泳道放在 +Z 一侧（默认相机方向）：蓝色是"主数据"，别被更长的琥珀色挡住。
      var Z_HIGH = WF_LANE_Z;
      var Z_LOW = -WF_LANE_Z;

      // 基础颜色拆成 0~1 的**线性**浮点数（线性工作流，见前面颜色管理注释）。
      // update 里直接写顶点色数组，避免每帧 Color→数组 的转换开销。
      function rgbOf(hex) {
        return {
          r: srgb01ToLinear(((hex >> 16) & 255) / 255),
          g: srgb01ToLinear(((hex >> 8) & 255) / 255),
          b: srgb01ToLinear((hex & 255) / 255),
        };
      }
      var C_HIGH = rgbOf(COL_HIGH);
      var C_LOW = rgbOf(COL_LOW);

      // 所有半透明数据面共用一个材质（顶点色决定颜色），减少材质切换。
      // 为什么用**普通混合 + 顶点 alpha**而不是加色混合：
      // 主场景天空/地形偏亮、还开了泛光，加色混合的半透明面叠在亮背景上会立刻烧成白色
      // （集成截图里那块"白墙"就是这么来的）。改成 alpha 渐变（底部透明 → 顶部约 0.8）后，
      // 蓝/琥珀色在任何背景上都保持饱和可读，而且"顶部亮、底部透明"的瀑布观感更强。
      var ribbonMat = noTone(new THREE.MeshBasicMaterial({
        vertexColors: true,       // 顶点色 itemSize=4（RGBA）→ r128 的 USE_COLOR_ALPHA 路径
        transparent: true,
        opacity: 0.95,
        blending: THREE.NormalBlending,
        side: THREE.DoubleSide,
        depthWrite: false,
        fog: false,
      }));

      /**
       * 创建一条"帘幕"：X=时间、Y=深度，立在 z = laneZ 平面上。
       * 每个采样点 2 个顶点（底边 y=0 / 顶边 y=depth），(slots-1)*2 个三角。
       * 顶点色带 alpha（RGBA）：底边 alpha=0、顶边 alpha≈0.8 → "顶部亮、底部透明"的渐变。
       */
      function makeCurtain(laneZ) {
        var vCount = slots * 2;
        var pos = new Float32Array(vCount * 3);
        var col = new Float32Array(vCount * 4);
        var idx = new Uint16Array(Math.max(0, (slots - 1) * 6));
        for (var i = 0; i < slots - 1; i++) {
          var a = i * 2, b = i * 2 + 1, c = (i + 1) * 2, d = (i + 1) * 2 + 1, o = i * 6;
          idx[o] = a; idx[o + 1] = b; idx[o + 2] = c;
          idx[o + 3] = b; idx[o + 4] = d; idx[o + 5] = c;
        }
        var geo = new THREE.BufferGeometry();
        var aPos = new THREE.BufferAttribute(pos, 3);
        var aCol = new THREE.BufferAttribute(col, 4);
        geo.setAttribute("position", aPos);
        geo.setAttribute("color", aCol);
        geo.setIndex(new THREE.Uint16BufferAttribute(idx, 1));
        var mesh = new THREE.Mesh(geo, ribbonMat);
        mesh.frustumCulled = false;
        wfGroup.add(mesh);
        return { mesh: mesh, geo: geo, pos: pos, col: col, aPos: aPos, aCol: aCol, z: laneZ };
      }

      /**
       * 顶带：帘幕顶边沿 Z 方向拉出宽度的一半，让"瀑布"有厚度、从斜上方看是三维曲面，
       * 而不是一张平面的折线图。每条泳道一条。
       */
      function makeTopBand(laneZ) {
        var vCount = slots * 2;
        var pos = new Float32Array(vCount * 3);
        var col = new Float32Array(vCount * 4);
        var idx = new Uint16Array(Math.max(0, (slots - 1) * 6));
        for (var i = 0; i < slots - 1; i++) {
          var a = i * 2, b = i * 2 + 1, c = (i + 1) * 2, d = (i + 1) * 2 + 1, o = i * 6;
          idx[o] = a; idx[o + 1] = b; idx[o + 2] = c;
          idx[o + 3] = b; idx[o + 4] = d; idx[o + 5] = c;
        }
        var geo = new THREE.BufferGeometry();
        var aPos = new THREE.BufferAttribute(pos, 3);
        var aCol = new THREE.BufferAttribute(col, 4);
        geo.setAttribute("position", aPos);
        geo.setAttribute("color", aCol);
        geo.setIndex(new THREE.Uint16BufferAttribute(idx, 1));
        var mesh = new THREE.Mesh(geo, ribbonMat);
        mesh.frustumCulled = false;
        wfGroup.add(mesh);
        return { mesh: mesh, geo: geo, pos: pos, col: col, aPos: aPos, aCol: aCol, z: laneZ };
      }

      /** 顶边折线：给每条泳道的曲线压一条亮边，远看也能读出深度起伏 */
      function makeTopLine(laneZ, hex) {
        var pos = new Float32Array(slots * 3);
        var geo = new THREE.BufferGeometry();
        var aPos = new THREE.BufferAttribute(pos, 3);
        geo.setAttribute("position", aPos);
        var mat = noTone(new THREE.LineBasicMaterial({
          color: lin(hex),
          transparent: true,
          opacity: 0.9,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
          fog: false,
        }));
        var line = new THREE.Line(geo, mat);
        line.frustumCulled = false;
        wfGroup.add(line);
        return { line: line, pos: pos, aPos: aPos, z: laneZ };
      }

      var curtainHigh = makeCurtain(Z_HIGH);
      var curtainLow = makeCurtain(Z_LOW);
      var bandHigh = makeTopBand(Z_HIGH);
      var bandLow = makeTopBand(Z_LOW);
      var topLineHigh = makeTopLine(Z_HIGH, COL_HIGH);
      var topLineLow = makeTopLine(Z_LOW, COL_LOW);

      // ---- 瀑布网格：基线 / 深度横线 / 时间竖线 / 老端竖线（create 时一次建好） ----
      var gridPts = [];
      var dimPts = [];
      (function buildGrid() {
        var zs = [Z_LOW, Z_HIGH];
        // 深度方向横向网格线：密度随世界尺寸增加（tickMul=2 → 每 12.5 条一根），
        // 放大后的图不会显得太空；刻度只标注 0 / 100，避免文字拥挤。
        var depthStep = 25 / tickMul;
        var levels = [];
        for (var lv = depthStep; lv <= 100 + 1e-6; lv += depthStep) levels.push(lv);
        for (var k = 0; k < zs.length; k++) {
          var z = zs[k];
          // 基线（0 条）
          gridPts.push(-wfSpan, 0, z, 0, 0, z);
          // 老端（最旧时间）竖线
          gridPts.push(-wfSpan, 0, z, -wfSpan, wfMaxY, z);
          for (var q2 = 0; q2 < levels.length; q2++) {
            var y = levels[q2] * heightScale;
            gridPts.push(-wfSpan, y, z, 0, y, z);
          }
        }
        // 时间刻度：每 (10/tickMul) s 一条（history 采样间隔 0.5 仿真秒 → 20 个采样 = 10 s）
        var secStep = 10 / tickMul;
        for (var sec = 40; sec >= -1e-6; sec -= secStep) {
          var idx = (slots - 1) - Math.round(sec * 2);
          if (idx < 0) continue;
          var x = -wfSpan + idx * wfDx;
          // 顶部短刻度，强调"这是一条时间轴"
          gridPts.push(x, 0, 0, x, wfMaxY * 0.06, 0);
          // 两泳道之间的淡竖向时间网格
          dimPts.push(x, 0, 0, x, wfMaxY * 0.9, 0);
        }
      })();

      function makeLineSegments(arr, color, opacity) {
        var geo = new THREE.BufferGeometry();
        var a = new THREE.BufferAttribute(new Float32Array(arr), 3);
        geo.setAttribute("position", a);
        var mat = noTone(new THREE.LineBasicMaterial({
          color: lin(color),
          transparent: true,
          opacity: opacity,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
          fog: false,
        }));
        var line = new THREE.LineSegments(geo, mat);
        line.frustumCulled = false;
        wfGroup.add(line);
        return line;
      }
      var gridLine = makeLineSegments(gridPts, COL_GRID, 0.55);
      var dimLine = makeLineSegments(dimPts, COL_GRID_DIM, 0.30);

      // ---- system_load 绿色细线（可选加分项：负载过载时这条线会顶到画面上方） ----
      var loadPos = new Float32Array(slots * 3);
      var loadAttr = new THREE.BufferAttribute(loadPos, 3);
      loadAttr.setUsage(THREE.DynamicDrawUsage);
      var loadGeo = new THREE.BufferGeometry();
      loadGeo.setAttribute("position", loadAttr);
      var loadLine = new THREE.Line(loadGeo, noTone(new THREE.LineBasicMaterial({
        color: lin(COL_LOAD),
        transparent: true,
        opacity: 0.8,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        fog: false,
      })));
      loadLine.frustumCulled = false;
      wfGroup.add(loadLine);

      // ---- "当前时刻"亮边：每个泳道一根竖直亮条（两块交叉平面，任何角度看都有厚度） ----
      var edgeGeo = new THREE.PlaneGeometry(0.6, 1);
      var edgeMat = noTone(new THREE.MeshBasicMaterial({
        color: lin(COL_EDGE),
        transparent: true,
        opacity: 0.8,
        blending: THREE.AdditiveBlending,
        side: THREE.DoubleSide,
        depthWrite: false,
        fog: false,
      }));
      var edgeHighA = new THREE.Mesh(edgeGeo, edgeMat);
      var edgeHighB = new THREE.Mesh(edgeGeo, edgeMat);
      var edgeLowA = new THREE.Mesh(edgeGeo, edgeMat);
      var edgeLowB = new THREE.Mesh(edgeGeo, edgeMat);
      edgeHighB.rotation.y = Math.PI / 2;
      edgeLowB.rotation.y = Math.PI / 2;
      edgeHighA.position.z = Z_HIGH; edgeHighB.position.z = Z_HIGH;
      edgeLowA.position.z = Z_LOW; edgeLowB.position.z = Z_LOW;
      wfGroup.add(edgeHighA); wfGroup.add(edgeHighB);
      wfGroup.add(edgeLowA); wfGroup.add(edgeLowB);

      /* ---- 相机距离自适应的材质登记（近景淡出用，箭头层不参与）----
         把瀑布图所有网格/折线/面材质的"设计 opacity"记下来，之后每帧统一乘 fade。
         edgeMat 例外：它的 opacity 在 updateWaterfall 里按"当前深度"做呼吸脉冲，
         所以单独乘 fade（见 updateWaterfall 末尾）。
         注意：sprite 不在这里 —— 它们有自己的材质列表（wfSpriteMats），只改 opacity，
         **绝不在 update 里改 scale**（主页面已做全局 sprite 距离等比缩放，见 Lead 约定）。 */
      var wfFadeMats = [];
      function regFadeMat(m, base) {
        if (!m) return;
        if (m.transparent !== true) m.transparent = true;
        wfFadeMats.push({ m: m, base: (typeof base === "number") ? base : _num(m.opacity, 1) });
      }
      regFadeMat(ribbonMat, 0.95);                 // 帘幕 + 顶带（共 4 个 mesh 共用）
      regFadeMat(topLineHigh.line.material, 0.9);  // 高优顶边亮线
      regFadeMat(topLineLow.line.material, 0.9);   // 低优顶边亮线
      regFadeMat(gridLine.material, 0.55);         // 深度/时间网格
      regFadeMat(dimLine.material, 0.30);          // 淡网格
      regFadeMat(loadLine.material, 0.8);          // system_load 绿线
      var wfSpriteMats = [];                       // 文字 sprite 材质（opacity 由 fade 控制）

      // ---- 文字 sprite（标题/泳道名/刻度）：优先用主页面传进来的 makeTextSprite ----
      function fallbackTextSprite(text, opt) {
        try {
          opt = opt || {};
          var fs = opt.fontSize || 28, pad = 9;
          var cv = document.createElement("canvas");
          var c0 = cv.getContext("2d");
          if (!c0) return null;
          c0.font = "600 " + fs + "px 'Microsoft YaHei',sans-serif";
          var txt = String(text);
          var w = Math.ceil(c0.measureText(txt).width) + pad * 2;
          var h = fs + pad * 2;
          cv.width = Math.max(4, w); cv.height = Math.max(4, h);
          var g = cv.getContext("2d");
          g.font = "600 " + fs + "px 'Microsoft YaHei',sans-serif";
          g.textBaseline = "middle";
          g.fillStyle = opt.bg || "rgba(5,14,24,.74)";
          g.strokeStyle = opt.border || "rgba(78,161,255,.55)";
          g.lineWidth = 3;
          var rr = 9;
          g.beginPath();
          g.moveTo(rr, 0); g.lineTo(w - rr, 0); g.quadraticCurveTo(w, 0, w, rr);
          g.lineTo(w, h - rr); g.quadraticCurveTo(w, h, w - rr, h);
          g.lineTo(rr, h); g.quadraticCurveTo(0, h, 0, h - rr);
          g.lineTo(0, rr); g.quadraticCurveTo(0, 0, rr, 0); g.closePath();
          g.fill(); g.stroke();
          g.fillStyle = opt.color || "#a9ecff";
          g.fillText(txt, pad, h / 2 + 1);
          var tex = new THREE.CanvasTexture(cv);
          tex.minFilter = THREE.LinearFilter;
          // 画布内容本身是 sRGB 颜色：标记编码后，渲染器才会先转线性、最后再编码回 sRGB，
          // 否则文字/描边会被"二次提亮"成发白的浅色（r128 常见的颜色偏白坑）。
          if (srgbOut && THREE.sRGBEncoding !== undefined) tex.encoding = THREE.sRGBEncoding;
          var sp = new THREE.Sprite(noTone(new THREE.SpriteMaterial({
            map: tex, transparent: true, depthWrite: false, fog: false,
          })));
          var s = opt.scale || 0.05;
          sp.scale.set(w * s, h * s, 1);
          return sp;
        } catch (e) {
          return null;
        }
      }
      var makeText = (typeof opts.makeTextSprite === "function") ? opts.makeTextSprite : fallbackTextSprite;

      var wfSpriteCount = 0;
      function addWfSprite(text, opt, x, y, z) {
        var sp = null;
        try { sp = makeText(text, opt); } catch (e) { sp = null; }
        if (!sp || !sp.position) return null;
        sp.position.set(x, y, z);
        // 主页面传入的 makeTextSprite 不知道 r128 的"sRGB 二次编码"坑，这里统一兜底：
        // 文字不参与 ACES 色调映射，canvas 纹理标记成 sRGB（否则文字会发白、描边变糊）。
        if (sp.material) {
          if ("toneMapped" in sp.material) sp.material.toneMapped = false;
          var mtex = sp.material.map;
          if (mtex && srgbOut && THREE.sRGBEncoding !== undefined &&
            mtex.encoding !== THREE.sRGBEncoding) {
            mtex.encoding = THREE.sRGBEncoding;
            mtex.needsUpdate = true;
            sp.material.needsUpdate = true;
          }
          // 登记 sprite 材质：相机贴太近时随瀑布整体淡出（只改 opacity，不改 scale）
          if (sp.material.transparent !== true) sp.material.transparent = true;
          wfSpriteMats.push({ m: sp.material, base: _num(sp.material.opacity, 1) });
        }
        wfGroup.add(sp);
        wfSpriteCount++;
        return sp;
      }

      // ---- 文字标签（Lead 2026-10 验收：数量减半、互不重叠；2026-11：世界放大后同步放大）----
      // 标题放在瀑布图**正上方居中**：不压数据、不压泳道名。
      // 只留 1 个标题 sprite（把 system_load 图例并进标题）——
      // 原来标题+图例两行在"地面站"仰视机位里会顶到画面上沿，精简后任何机位都完整可见。
      addWfSprite("队列深度瀑布 · 近 " + Math.round((slots - 1) * 0.5) + " s · 绿线=load",
        { fontSize: 24 * fgrow, scale: 0.066 * fscl, color: "#d6ecff" },
        -wfSpan * 0.5, wfMaxY, 0);

      // 泳道名：每条泳道**只留 1 个**，贴在各自"当前时刻"端点；
      // 两条标签的 Y 相差约 14 世界单位（要求 ≥8），不会再叠在一起看不清。
      addWfSprite("高优队列 HIGH",
        { fontSize: 22 * fgrow, scale: 0.062 * fscl, color: "#9fd0ff" },
        wfSpan * 0.072, wfMaxY * 0.09, Z_HIGH);
      addWfSprite("低优队列 LOW",
        { fontSize: 22 * fgrow, scale: 0.062 * fscl, color: "#ffd08a" },
        wfSpan * 0.072, wfMaxY * 0.555, Z_LOW);

      // 深度刻度：只留 0 / 100 两个标签（横向网格线数量已随世界尺寸加密）
      addWfSprite("0", { fontSize: 20 * fgrow, scale: 0.05 * fscl, color: "#8fb6cc" },
        -wfSpan * 1.11, wfMaxY * 0.04, 0);
      addWfSprite("100", { fontSize: 20 * fgrow, scale: 0.05 * fscl, color: "#8fb6cc" },
        -wfSpan * 1.11, 100 * heightScale, 0);

      // 时间刻度：只留 "-30s" 与 "现在"（竖向网格线已随世界尺寸加密）
      [30, 0].forEach(function (sec) {
        var idx = (slots - 1) - sec * 2;
        if (idx < 0) return;
        var x = -wfSpan + idx * wfDx;
        addWfSprite(sec === 0 ? "现在" : ("-" + sec + "s"),
          { fontSize: 18 * fgrow, scale: 0.05 * fscl, color: sec === 0 ? "#bff4ff" : "#6f93a8" },
          x, -wfMaxY * 0.08, Z_HIGH + 1.7);
      });

      /* =====================================================================
         状态 / API
         ===================================================================== */
      var mode = "all";
      var enabled = true;
      var facing = true;              // 默认让瀑布绕 Y 轴朝向相机（读图更清楚）
      var facingInit = false;
      var camFade = 1;                // 相机距离淡出系数（0~1，平滑后），近景时趋近 0
      var selfT = 0;                  // ctx.t 缺失时的兜底时钟
      var errReported = false;

      var stats = {
        arrows: 0,        // 本帧实际绘制的箭头实例数
        links: 0,         // busy 链路数（= 有任务在跑的无人机数）
        weak: 0,          // health < 55 的弱网链路数
        slots: 0,         // 瀑布当前有数据的采样槽数
        verts: 0,
        tris: 0,
        drawCalls: 0,
        ms: 0,            // update() 平滑耗时（ms）
        mode: mode,
        camDist: 0,       // 相机到瀑布锚点距离
        camFade: 1,       // 相机距离淡出系数（1=完全显示，0=隐藏）
        worldScale: worldScale,   // 世界缩放（相对旧世界 176×128；当前 2.0）
        worldAdapt: worldAdapt,   // 世界缩放（相对新世界 352×256；当前 1.0）
      };

      // 静态三角预算（每帧几乎不变的部分）+ 固定 draw call 估计
      var plan = _geomPlan(slots, maxArrows);
      var wfDrawCalls = 4 /* 帘幕×2 + 顶带×2 */ + 2 /* 顶边线 */ + 1 /* load */ +
        4 /* 当前边 ×2 交叉 */ + 2 /* 网格 */ + wfSpriteCount;

      /* ---------------- A. 箭头每帧更新 ---------------- */
      function updateFlow(dt, ctx, t) {
        var live = ctx.live;
        if (!live) {
          // 契约：live == null（仿真未开始）时全部隐藏，不能报错、不能残留上一轮箭头
          flowGroup.visible = false;
          arrows.count = 0;
          veins.geometry.setDrawRange(0, 0);
          stats.arrows = 0;
          stats.links = 0;
          stats.weak = 0;
          return;
        }
        flowGroup.visible = true;
        stats.weak = 0;

        var drones = ctx.drones || EMPTY_ARRAY;
        var st = ctx.stationTop || opts.stationTop || DEF_STATION_TOP;
        var sx = _num(st.x, DEF_STATION_TOP.x);
        var sy = _num(st.y, DEF_STATION_TOP.y);
        var sz = _num(st.z, DEF_STATION_TOP.z);

        // ---- ① 收集 busy 链路，并算好每条链路的二次贝塞尔控制点 ----
        var nLinks = 0;
        var i, j, d, o;
        for (i = 0; i < drones.length && nLinks < MAX_LINKS; i++) {
          d = drones[i];
          if (!d || !d.busy || d.online === false) continue;
          if (!d.pos) continue;

          var px = _num(d.pos.x, 0), py = _num(d.pos.y, 0), pz = _num(d.pos.z, 0);
          var dx = sx - px, dy = sy - py, dz = sz - pz;
          var horiz = Math.sqrt(dx * dx + dz * dz);
          // 拱高：距离越远拱得越高（避免弧线穿地形/穿采坑），上限 29 单位（旧世界），
          // 世界放大后按 arrowGrow 缩放，弧线观感保持一致。
          var lift = (9 + _clamp(horiz * 0.20, 0, 20)) * arrowGrow;
          // 侧向偏移：让 30 条弧在水平面里彼此错开，不至于糊成一根粗管子
          var side = ((i % 7) - 3) * 1.7 * arrowGrow;
          var invH = horiz > 1e-4 ? 1 / horiz : 0;
          var perpX = -dz * invH, perpZ = dx * invH;

          o = nLinks * 9;
          // 上行弧：无人机(P0) → 控制点(P1) → 地面站天线顶(P2)
          linkCap[o] = px; linkCap[o + 1] = py; linkCap[o + 2] = pz;
          linkCap[o + 3] = (px + sx) * 0.5 + perpX * side;
          linkCap[o + 4] = (py + sy) * 0.5 + lift;
          linkCap[o + 5] = (pz + sz) * 0.5 + perpZ * side;
          linkCap[o + 6] = sx; linkCap[o + 7] = sy; linkCap[o + 8] = sz;

          // 下行弧：地面站(P0) → 另一组控制点(P1) → 无人机(P2)，拱得再高一点、侧偏相反，
          // 这样青色小箭头和蓝/琥珀大箭头在空间上分开，不会互相盖住。
          linkCap2[o] = sx; linkCap2[o + 1] = sy; linkCap2[o + 2] = sz;
          linkCap2[o + 3] = (px + sx) * 0.5 - perpX * side * 0.55;
          linkCap2[o + 4] = (py + sy) * 0.5 + lift + 7.0 * arrowGrow;
          linkCap2[o + 5] = (pz + sz) * 0.5 - perpZ * side * 0.55;
          linkCap2[o + 6] = px; linkCap2[o + 7] = py; linkCap2[o + 8] = pz;

          var health = _clamp(_num(d.health, 100), 0, 100);
          var prog = _clamp(_num(d.progress, 0), 0, 1);
          var isHigh = (d.priority === "HIGH");
          linkHealth[nLinks] = health;
          linkPrio[nLinks] = isHigh ? 1 : 0;
          linkProg[nLinks] = prog;
          linkDensity[nLinks] = _healthDensity(health);
          linkShort[nLinks] = _healthLen(health);
          linkSpeed[nLinks] = 0.26 * _arrowSpeed(prog, health); // 0.26 x (0.6~1.4) ≈ 0.16~0.36 /s
          linkPhase[nLinks] = (nLinks * 0.173) % 1;
          // 箭头数 ∝ 任务进度与优先级；cpu 档更少
          var baseCnt = isCpu ? 2 : (isMed ? 4 : (isHigh ? 6 : 5));
          linkCount[nLinks] = _clampInt(
            Math.round(baseCnt * (0.55 + 0.75 * prog) * linkDensity[nLinks]), 1, 8);
          if (health < 55) stats.weak++;
          nLinks++;
        }

        // ---- ② 预算压缩：上行箭头占大头，给下行调度指令预留 ~16% ----
        var downBudget = Math.min(nLinks, Math.max(4, Math.floor(maxArrows * 0.16)));
        var upBudget = Math.max(1, maxArrows - downBudget);
        fitCounts(linkCount, nLinks, upBudget, linkActual);

        var k = 0;
        // ---- ③ 上行箭头：沿贝塞尔弧从无人机流向地面站 ----
        for (i = 0; i < nLinks && k < upBudget; i++) {
          o = i * 9;
          var cnt = linkActual[i];
          var weakLink = linkHealth[i] < 55;
          var weakAmt = weakLink ? 1 : 0;
          var shortLen = linkShort[i];
          var startU = shortLen < 1 ? 0.16 : 0;
          for (j = 0; j < cnt && k < maxArrows; j++) {
            // 相位 = 全局时间 × 速度 + 链路错峰 + 支序错峰 → 一串同向流动的箭头
            var u = (t * linkSpeed[i] + linkPhase[i] + j / cnt) % 1;
            if (u < 0) u += 1;
            var uu = startU + u * shortLen;

            _bezPoint(tVec, linkCap, o, uu);
            if (weakAmt) {
              // 弱网抖动：模拟丢包/重传时链路位置不稳（幅度很小，但看得出来"抖"），
              // 幅度同样按世界尺寸缩放。
              var seed = i * 1.7 + j * 0.9;
              tVec.x += Math.sin(t * 17.0 + seed * 1.3) * 0.30 * arrowGrow;
              tVec.y += Math.sin(t * 13.0 + seed) * 0.50 * arrowGrow;
              tVec.z += Math.cos(t * 15.0 + seed * 0.7) * 0.30 * arrowGrow;
            }
            _bezTan(tTan, linkCap, o, uu);
            tTan.normalize();
            if (tTan.lengthSq() < 1e-8) tTan.set(0, 1, 0); // 起终点重合的退化情况
            tQuat.setFromUnitVectors(AXIS_Z, tTan);

            var sc = (linkPrio[i] ? 1.0 : 0.9) * (weakAmt ? 0.78 : 1.0) * (isCpu ? 1.25 : 1.0);
            tScale.set(sc, sc, sc);
            tMat.compose(tVec, tQuat, tScale);
            arrows.setMatrixAt(k, tMat);

            tColor.copy(linkPrio[i] ? COL_HIGH_LIN : COL_LOW_LIN);
            if (weakAmt) tColor.multiplyScalar(0.62); // 弱网链路整体压暗（丢包 → 光晕变弱）
            arrows.setColorAt(k, tColor);
            k++;
          }
        }

        // ---- ④ 下行调度指令：反向小号青色箭头（地面站 → 无人机） ----
        var kDown = 0;
        for (i = 0; i < nLinks && k < maxArrows && kDown < downBudget; i++, kDown++) {
          o = i * 9;
          var ud = (t * 0.46 + linkPhase[i] * 0.71) % 1;
          if (ud < 0) ud += 1;
          _bezPoint(tVec, linkCap2, o, ud);
          _bezTan(tTan, linkCap2, o, ud);
          tTan.normalize();
          if (tTan.lengthSq() < 1e-8) tTan.set(0, 1, 0);
          tQuat.setFromUnitVectors(AXIS_Z, tTan);
          var sc2 = 0.62 * (isCpu ? 1.2 : 1.0);
          tScale.set(sc2, sc2, sc2);
          tMat.compose(tVec, tQuat, tScale);
          arrows.setMatrixAt(k, tMat);
          tColor.copy(COL_CMD_LIN).multiplyScalar(0.90);
          arrows.setColorAt(k, tColor);
          k++;
        }

        // ---- ⑤ 弧线"血管"（细线）：解释每条箭头从哪架机飞出来 ----
        var vp = veinPos, vk = 0;
        for (i = 0; i < nLinks; i++) {
          o = i * 9;
          var ln = linkShort[i];
          var su = ln < 1 ? 0.16 : 0;
          var segLen = ln < 1 ? ln : 1;
          for (var s = 0; s < VEIN_SEG; s++) {
            _bezPoint(tVec, linkCap, o, su + segLen * (s / VEIN_SEG));
            _bezPoint(tVec2, linkCap, o, su + segLen * ((s + 1) / VEIN_SEG));
            vp[vk++] = tVec.x; vp[vk++] = tVec.y; vp[vk++] = tVec.z;
            vp[vk++] = tVec2.x; vp[vk++] = tVec2.y; vp[vk++] = tVec2.z;
          }
        }
        veins.geometry.setDrawRange(0, nLinks * VEIN_SEG * 2);
        veinAttr.needsUpdate = true;

        // ---- ⑥ 写回实例缓冲 ----
        arrows.count = k;
        arrows.instanceMatrix.needsUpdate = true;
        if (arrows.instanceColor) arrows.instanceColor.needsUpdate = true;

        stats.arrows = k;
        stats.links = nLinks;
      }

      /* ---------------- B. 瀑布每帧更新 ---------------- */
      var curHighY = 0, curLowY = 0;   // 最新采样（当前时刻）的深度高度，供亮边使用

      function updateWaterfall(dt, ctx, t) {
        var live = ctx.live;
        var hist = (live && live.history) ? live.history : null;
        var hlen = hist ? hist.length : 0;
        var have = hlen > 0;

        // 没有数据：隐藏数据面，只留空坐标系（契约要求"画空坐标系不报错"）
        curtainHigh.mesh.visible = have;
        curtainLow.mesh.visible = have;
        bandHigh.mesh.visible = have;
        bandLow.mesh.visible = have;
        topLineHigh.line.visible = have;
        topLineLow.line.visible = have;
        loadLine.visible = have;
        edgeHighA.visible = have; edgeHighB.visible = have;
        edgeLowA.visible = have; edgeLowB.visible = have;
        if (!have) {
          stats.slots = 0;
          return;
        }

        var ph = curtainHigh.pos, ch = curtainHigh.col;
        var pl = curtainLow.pos, cl = curtainLow.col;
        var bh = bandHigh.pos, bch = bandHigh.col;
        var bl = bandLow.pos, bcl = bandLow.col;
        var tlh = topLineHigh.pos, tll = topLineLow.pos;
        var ldp = loadPos;

        var zH = Z_HIGH, zL = Z_LOW, hw = WF_LANE_HALF_W;
        var hs = heightScale, maxDepth = WF_DEPTH_MAX;
        var curH = 0, curL = 0;
        var lastSrc = hlen - 1;

        for (var i = 0; i < slots; i++) {
          // 取最近 slots 个采样：i = slots-1 对应最新（靠近地面站的一端）
          var src = hlen - slots + i;
          var hh = 0, ll = 0, ld = 0;
          if (src >= 0 && src < hlen) {
            var d = hist[src];
            if (d) {
              hh = _num(d.high, 0);
              ll = _num(d.low, 0);
              ld = _num(d.load, 0);
            }
          }
          if (src === lastSrc) {
            // 注意：要存"截断后"的值 —— 否则真实工况下队列爆到上千时，
            // "当前时刻"亮边会被拉成一根贯穿全屏的白线（踩过的坑）。
            curH = _clamp(hh, 0, maxDepth);
            curL = _clamp(ll, 0, maxDepth);
          }
          hh = _clamp(hh, 0, maxDepth);
          ll = _clamp(ll, 0, maxDepth);

          var x = -wfSpan + i * wfDx;
          var yH = hh * hs, yL = ll * hs;

          // "顶亮底透明"：alpha 随深度增加。
          // 帘幕刻意做"薄纱"（上限只有 0.25）：两条泳道在正面视角必然大面积重叠，
          // 帘幕太实会混成一团灰白；可读性交给更亮的"顶带"与顶边亮线。
          var aH = 0.03 + 0.22 * (hh / maxDepth);
          var aL = 0.03 + 0.22 * (ll / maxDepth);
          // 顶带（横向的"瀑布带"）才是主视觉：alpha 高、两条泳道错开后互不干扰
          var bH = 0.60 + 0.35 * (hh / maxDepth);
          var bL = 0.60 + 0.35 * (ll / maxDepth);
          var bi6 = i * 6;         // 帘幕位置下标（每 slot 2 顶点 × 3）
          var bi8 = i * 8;         // 帘幕颜色下标（每 slot 2 顶点 × 4，RGBA）
          var ti = i * 3;          // 顶边折线 / load 线：每 slot 1 顶点

          /* --- 高优泳道帘幕（蓝）：底边完全透明，顶边亮 --- */
          ph[bi6] = x; ph[bi6 + 1] = 0; ph[bi6 + 2] = zH;
          ph[bi6 + 3] = x; ph[bi6 + 4] = yH; ph[bi6 + 5] = zH;
          ch[bi8] = C_HIGH.r; ch[bi8 + 1] = C_HIGH.g; ch[bi8 + 2] = C_HIGH.b; ch[bi8 + 3] = 0;
          ch[bi8 + 4] = C_HIGH.r; ch[bi8 + 5] = C_HIGH.g; ch[bi8 + 6] = C_HIGH.b; ch[bi8 + 7] = aH;

          /* --- 低优泳道帘幕（琥珀） --- */
          pl[bi6] = x; pl[bi6 + 1] = 0; pl[bi6 + 2] = zL;
          pl[bi6 + 3] = x; pl[bi6 + 4] = yL; pl[bi6 + 5] = zL;
          cl[bi8] = C_LOW.r; cl[bi8 + 1] = C_LOW.g; cl[bi8 + 2] = C_LOW.b; cl[bi8 + 3] = 0;
          cl[bi8 + 4] = C_LOW.r; cl[bi8 + 5] = C_LOW.g; cl[bi8 + 6] = C_LOW.b; cl[bi8 + 7] = aL;

          /* --- 顶带：主视觉"瀑布带"，在 Z 方向拉出宽度，alpha 较高、清晰可读 --- */
          bh[bi6] = x; bh[bi6 + 1] = yH; bh[bi6 + 2] = zH - hw;
          bh[bi6 + 3] = x; bh[bi6 + 4] = yH; bh[bi6 + 5] = zH + hw;
          bch[bi8] = C_HIGH.r; bch[bi8 + 1] = C_HIGH.g; bch[bi8 + 2] = C_HIGH.b; bch[bi8 + 3] = bH;
          bch[bi8 + 4] = C_HIGH.r; bch[bi8 + 5] = C_HIGH.g; bch[bi8 + 6] = C_HIGH.b; bch[bi8 + 7] = bH * 0.78;

          bl[bi6] = x; bl[bi6 + 1] = yL; bl[bi6 + 2] = zL - hw;
          bl[bi6 + 3] = x; bl[bi6 + 4] = yL; bl[bi6 + 5] = zL + hw;
          bcl[bi8] = C_LOW.r; bcl[bi8 + 1] = C_LOW.g; bcl[bi8 + 2] = C_LOW.b; bcl[bi8 + 3] = bL;
          bcl[bi8 + 4] = C_LOW.r; bcl[bi8 + 5] = C_LOW.g; bcl[bi8 + 6] = C_LOW.b; bcl[bi8 + 7] = bL * 0.78;

          /* --- 顶边折线（亮线，远看也能读出起伏） --- */
          tlh[ti] = x; tlh[ti + 1] = yH; tlh[ti + 2] = zH;
          tll[ti] = x; tll[ti + 1] = yL; tll[ti + 2] = zL;

          /* --- system_load 绿色细线：两条泳道中间，按负载比例抬升 --- */
          ldp[ti] = x;
          ldp[ti + 1] = _clamp(ld, 0, 3) * wfLoadScale;
          ldp[ti + 2] = 0;
        }

        // 只标脏，不重建 geometry（契约硬要求：每帧禁止重建）
        curtainHigh.aPos.needsUpdate = true; curtainHigh.aCol.needsUpdate = true;
        curtainLow.aPos.needsUpdate = true; curtainLow.aCol.needsUpdate = true;
        bandHigh.aPos.needsUpdate = true; bandHigh.aCol.needsUpdate = true;
        bandLow.aPos.needsUpdate = true; bandLow.aCol.needsUpdate = true;
        topLineHigh.aPos.needsUpdate = true;
        topLineLow.aPos.needsUpdate = true;
        loadAttr.needsUpdate = true;

        // "当前时刻"亮边：高度跟随最新采样（靠地面站一端的一根竖直亮线）
        curHighY = curH * hs;
        curLowY = curL * hs;
        var hA = Math.max(0.001, curHighY), lA = Math.max(0.001, curLowY);
        edgeHighA.scale.y = hA; edgeHighB.scale.y = hA;
        edgeHighA.position.y = hA * 0.5; edgeHighB.position.y = hA * 0.5;
        edgeLowA.scale.y = lA; edgeLowB.scale.y = lA;
        edgeLowA.position.y = lA * 0.5; edgeLowB.position.y = lA * 0.5;
        edgeMat.opacity = (0.62 + 0.18 * Math.sin(t * 3.0)) * camFade; // 呼吸脉冲 × 相机距离淡出

        stats.slots = Math.min(slots, hlen);
      }

      /* ---------------- 统一 update ---------------- */
      var emptyCtx = { t: 0, live: null, drones: null, stationTop: null };

      function update(dt, ctx) {
        if (!enabled || mode === "off") {
          stats.arrows = 0;
          stats.ms = 0;
          return;
        }
        var now = (typeof performance !== "undefined" && performance.now)
          ? performance.now() : Date.now();
        var c = ctx || emptyCtx;
        var t = (typeof c.t === "number" && isFinite(c.t)) ? c.t : selfT;
        selfT += _num(dt, 0);
        try {
          // ---- 相机距离自适应：相机接近瀑布图锚点时整体淡出（近景机位不挡镜头）----
          // 相机优先取 ctx.cameraPos（Lead 可选传），否则用 create 时传入的活的 camera 对象。
          // 阈值随世界尺寸缩放：如当前世界 near=22*2=44、far=45*2=90（>60*worldScale 必已恢复）。
          var camPos = (c.cameraPos && typeof c.cameraPos.x === "number") ? c.cameraPos : camera.position;
          var aPos = wfGroup.position;
          var cdx = camPos.x - aPos.x, cdy = camPos.y - aPos.y, cdz = camPos.z - aPos.z;
          var camDist = Math.sqrt(cdx * cdx + cdy * cdy + cdz * cdz);
          var fadeTarget = _camFade(camDist, worldScale);
          // 平滑过渡（≈0.1~0.15s），避免快速穿过阈值时整块图"闪一下"
          camFade += (fadeTarget - camFade) * Math.min(1, _num(dt, 0) * 10);
          if (Math.abs(fadeTarget - camFade) < 0.004) camFade = fadeTarget;
          stats.camDist = camDist;
          stats.camFade = camFade;

          if (mode === "all" || mode === "flow") updateFlow(dt, c, t);
          if (mode === "all" || mode === "waterfall") {
            var wfShow = camFade > 0.02;
            wfGroup.visible = wfShow;
            if (wfShow) {
              // 各材质按"设计 opacity × fade"淡出/淡入；箭头层不受影响
              for (var fi = 0; fi < wfFadeMats.length; fi++) {
                wfFadeMats[fi].m.opacity = wfFadeMats[fi].base * camFade;
              }
              for (var si = 0; si < wfSpriteMats.length; si++) {
                wfSpriteMats[si].m.opacity = wfSpriteMats[si].base * camFade;
              }
              updateWaterfall(dt, c, t);
            } else {
              stats.slots = 0;
            }
          }

          // 瀑布绕 Y 轴朝向相机：局部 +Z 指向相机 = 读图时泳道不被压成一条线
          if (facing) {
            var cp = camera.position;
            var ddx = cp.x - wfGroup.position.x;
            var ddz = cp.z - wfGroup.position.z;
            if (ddx * ddx + ddz * ddz > 1e-6) {
              // 朝向相机 + 固定偏角：既让图"正对镜头"好读，又能看见两条泳道的 Z 向错位
              var wantYaw = Math.atan2(ddx, ddz) + FACING_BIAS;
              if (!facingInit) {
                wfGroup.rotation.y = wantYaw;       // 首帧直接对齐，避免截图时还在转动
                facingInit = true;
              } else {
                var dyaw = wantYaw - wfGroup.rotation.y;
                dyaw = ((dyaw + Math.PI) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2) - Math.PI;
                wfGroup.rotation.y += dyaw * Math.min(1, _num(dt, 0) * 3.5);
              }
            }
          } else {
            wfGroup.rotation.y += (0 - wfGroup.rotation.y) * Math.min(1, _num(dt, 0) * 3.5);
          }

          if (mode === "flow") stats.slots = 0;
          stats.tris = plan.ribbonTris + plan.arrowTris;
          stats.verts = plan.ribbonVerts + plan.lineVerts + slots;
          stats.drawCalls = (mode === "all" || mode === "flow" ? 2 : 0) +
            (mode === "all" || mode === "waterfall" ? wfDrawCalls : 0);
          stats.mode = mode;
        } catch (e) {
          // 异常安全：HUD 坏了也不能把主场景 30 架机搞崩，直接自我停用
          if (!errReported) {
            errReported = true;
            log("[HUD] update 异常，HUD 已自动停用：" + (e && e.message ? e.message : e));
          }
          enabled = false;
          group.visible = false;
          stats.arrows = 0;
        }
        var cost = ((typeof performance !== "undefined" && performance.now)
          ? performance.now() : Date.now()) - now;
        stats.ms += (cost - stats.ms) * 0.15;    // 平滑 ~20 帧
      }

      /* ---------------- 开关 / 朝向 / 释放 ---------------- */
      function setMode(m) {
        if (!MODES[m]) return mode;
        mode = m;
        flowGroup.visible = (m === "all" || m === "flow");
        wfGroup.visible = (m === "all" || m === "waterfall");
        stats.mode = mode;
        if (m === "off" || m === "waterfall") stats.arrows = 0;
        return mode;
      }

      function setEnabled(b) {
        enabled = !!b;
        group.visible = enabled;
        return enabled;
      }

      function setCameraFacing(b) {
        facing = !!b;
        facingInit = false;   // 重新打开时先瞬间对齐，再平滑跟随
        return facing;
      }

      function dispose() {
        try {
          // 注销"当前活跃实例"指针，避免下次 create 又去 dispose 一个已释放的 handle
          if (scene.userData && scene.userData.__UAVHUD_ACTIVE === handle) {
            scene.userData.__UAVHUD_ACTIVE = null;
          }
        } catch (eUnreg) { /* ignore */ }
        try {
          scene.remove(group);
          var seen = [];
          group.traverse(function (obj) {
            if (obj.geometry && seen.indexOf(obj.geometry) < 0) {
              seen.push(obj.geometry);
              obj.geometry.dispose();
            }
            if (obj.material) {
              var mats = Array.isArray(obj.material) ? obj.material : [obj.material];
              for (var mi = 0; mi < mats.length; mi++) {
                if (mats[mi] && mats[mi].map) mats[mi].map.dispose();
                if (mats[mi]) mats[mi].dispose();
              }
            }
          });
          seen.length = 0;
        } catch (e) { /* 释放失败不影响主场景 */ }
      }

      var handle = {
        version: VERSION,
        group: group,
        flowGroup: flowGroup,
        waterfallGroup: wfGroup,
        anchor: wfGroup.position,
        update: update,
        setMode: setMode,
        setEnabled: setEnabled,
        setCameraFacing: setCameraFacing,
        getMode: function () { return mode; },
        stats: stats,
        dispose: dispose,
        // 调试用：暴露当前参数（只读使用）
        config: {
          tier: tier, maxArrows: maxArrows, slots: slots,
          heightScale: heightScale, waterfallScale: wfScale,
          span: wfSpan, depthMax: WF_DEPTH_MAX, laneZ: WF_LANE_Z,
          anchor: { x: ax, y: ay, z: az },
          camFadeNear: CAM_FADE_NEAR * worldScale,
          camFadeFar: CAM_FADE_FAR * worldScale,
          world: { width: worldW, depth: worldD },
          worldScale: worldScale, worldAdapt: worldAdapt,
          tickMul: tickMul, spriteGrow: fgrow, loadScale: wfLoadScale,
        },
      };

      // 注册为"当前活跃 HUD"：app3d 若再次调用 create（目前 initUpgradeModules 被调两次），
      // 新实例会先 dispose 掉这里的旧实例，保证场景里只有一套 HUD（重复 label 问题）。
      try { if (scene.userData) scene.userData.__UAVHUD_ACTIVE = handle; } catch (eReg) { /* ignore */ }

      log("[HUD] ready：箭头上限 " + maxArrows + "，瀑布采样 " + slots +
        "，世界 " + worldW + "×" + worldD + "（×" + worldScale.toFixed(2) + "）" +
        "，时间轴 " + wfSpan.toFixed(1) + "，档位 " + tier + (isCpu ? "（降采样）" : ""));
      return handle;
    } catch (err) {
      // 异常安全：任何初始化失败都返回 null，主场景继续跑
      try {
        log("[HUD] create 失败：" + (err && err.message ? err.message : err));
      } catch (e2) { /* ignore */ }
      return null;
    }
  }

  /* =========================================================================
     selfTest() —— 纯逻辑自检，不依赖 renderer / 不依赖已 create
     ========================================================================= */
  function selfTest() {
    var checks = [];
    function chk(name, ok, detail) {
      checks.push({ name: name, ok: !!ok, detail: String(detail) });
    }
    function near(a, b) { return Math.abs(a - b) < 1e-9; }

    try {
      // 1) 二次贝塞尔端点性质：B(0)=P0, B(1)=P2
      var cap = new Float32Array([
        1, 2, 3,   4, 10, 6,   9, 20, 12,
      ]);
      var out = { x: 0, y: 0, z: 0 };
      _bezPoint(out, cap, 0, 0);
      chk("贝塞尔 t=0 经过起点", near(out.x, 1) && near(out.y, 2) && near(out.z, 3),
        out.x + "," + out.y + "," + out.z);
      _bezPoint(out, cap, 0, 1);
      chk("贝塞尔 t=1 经过终点", near(out.x, 9) && near(out.y, 20) && near(out.z, 12),
        out.x + "," + out.y + "," + out.z);

      // 2) t=0.5 时 B = (P0 + 2P1 + P2)/4 —— 拱高确实由控制点决定
      _bezPoint(out, cap, 0, 0.5);
      chk("贝塞尔 t=0.5 = (P0+2P1+P2)/4",
        near(out.x, (1 + 8 + 9) / 4) && near(out.y, (2 + 20 + 20) / 4) && near(out.z, (3 + 12 + 12) / 4),
        out.x + "," + out.y + "," + out.z);

      // 3) 切线方向：三点共线（P0=0, P1=1, P2=2 沿 +X）时，t=0.5 的导数为 2·(+X)
      var cap2 = new Float32Array([0, 0, 0, 1, 0, 0, 2, 0, 0]);
      var tan = { x: 0, y: 0, z: 0 };
      _bezTan(tan, cap2, 0, 0.5);
      chk("切线方向正确（+X，导数=2）",
        Math.abs(tan.x - 2) < 1e-9 && Math.abs(tan.y) < 1e-9 && Math.abs(tan.z) < 1e-9,
        tan.x + "," + tan.y + "," + tan.z);

      // 4) 颜色常量（验收按十六进制核对）
      chk("优先级/指令颜色常量",
        COL_HIGH === 0x4ea1ff && COL_LOW === 0xf5a623 && COL_CMD === 0x37e2d5,
        "#" + COL_HIGH.toString(16) + " / #" + COL_LOW.toString(16) + " / #" + COL_CMD.toString(16));

      // 5) 弱网：health<55 箭头更稀、更短、更慢
      chk("弱网箭头变稀", _healthDensity(40) < _healthDensity(90),
        _healthDensity(40) + " < " + _healthDensity(90));
      chk("弱网箭头变短", _healthLen(40) < _healthLen(90), _healthLen(40) + " < " + _healthLen(90));
      chk("速度随上传进度增大", _arrowSpeed(1, 90) > _arrowSpeed(0, 90),
        _arrowSpeed(0, 90).toFixed(2) + " → " + _arrowSpeed(1, 90).toFixed(2));
      chk("弱网速度下降", _arrowSpeed(1, 40) < _arrowSpeed(1, 90),
        _arrowSpeed(1, 40).toFixed(2) + " < " + _arrowSpeed(1, 90).toFixed(2));
      var sp0 = _arrowSpeed(0, 90), sp1 = _arrowSpeed(1, 90);
      chk("速度倍频落在 0.6~1.4", sp0 >= 0.6 - 1e-9 && sp1 <= 1.4 + 1e-9,
        sp0.toFixed(2) + "~" + sp1.toFixed(2));

      // 6) 箭头预算压缩：总和不超过上限
      var req = new Int32Array([6, 6, 6]);
      var got = new Int32Array(3);
      var used = fitCounts(req, 3, 12, got);
      chk("箭头预算压缩正确", used === 12 && got[0] === 4 && got[1] === 4 && got[2] === 4,
        "used=" + used + " each=" + got[0]);

      // 7) 几何预算：slots=90 时帘幕+顶带 712 个三角、箭头 240×8=1920
      var plan = _geomPlan(DEF_SLOTS, DEF_MAX_ARROWS);
      chk("瀑布三角数 = (90-1)*8", plan.ribbonTris === (DEF_SLOTS - 1) * 8, plan.ribbonTris);
      chk("箭头三角数 = 240*8", plan.arrowTris === DEF_MAX_ARROWS * 8, plan.arrowTris);
      chk("总三角预算在 3k 以内", plan.tris < 3000, plan.tris);

      // 8) 模式白名单
      chk("setMode 白名单 all/flow/waterfall/off",
        MODES.all === true && MODES.flow === true && MODES.waterfall === true && MODES.off === true,
        Object.keys(MODES).join(","));

      // 9) 瀑布深度截断：超过上限压平而不是画出天际线
      chk("队列深度截断到 140", _clamp(9999, 0, WF_DEPTH_MAX) === WF_DEPTH_MAX, WF_DEPTH_MAX);

      // 10) 世界自适应 + 2026-11 Lead 验收布局约束
      chk("世界缩放：176×128 → 1.0", _worldScale(176, 128) === 1, _worldScale(176, 128));
      chk("世界缩放：352×256 → 2.0", _worldScale(352, 256) === 2, _worldScale(352, 256));
      chk("世界缩放：704×512 → 4.0", _worldScale(704, 512) === 4, _worldScale(704, 512));
      chk("时间轴基准 25 × waterfallScale 2.2 = 55",
        WF_SPAN === 25 && DEF_WATERFALL_SCALE === 2.2, WF_SPAN + "×" + DEF_WATERFALL_SCALE);
      chk("两条泳道间隙 ≥ 3 世界单位",
        (WF_LANE_Z * 2 - WF_LANE_HALF_W * 2) >= 3, (WF_LANE_Z * 2 - WF_LANE_HALF_W * 2).toFixed(1));
      chk("默认高度比例 0.20~0.25（世界放大后 0.22）",
        DEF_HEIGHT_SCALE >= 0.2 && DEF_HEIGHT_SCALE <= 0.25, DEF_HEIGHT_SCALE);
      chk("高度比例上限 0.25~0.35（放宽到 0.30）",
        MAX_HEIGHT_SCALE >= 0.25 && MAX_HEIGHT_SCALE <= 0.35, MAX_HEIGHT_SCALE);
      chk("默认锚点在矿区右上方 / 地面站前方",
        DEF_ANCHOR.x > 0 && DEF_ANCHOR.y > DEF_STATION_TOP.y && DEF_ANCHOR.z < DEF_STATION_TOP.z,
        "(" + DEF_ANCHOR.x + "," + DEF_ANCHOR.y + "," + DEF_ANCHOR.z + ")");

      // 11) 相机距离自适应（Lead 2026-11 验收：近景不挡镜头，阈值随世界缩放）
      chk("近景 <22 时瀑布完全隐藏", _camFade(10, 1) === 0 && _camFade(21.9, 1) === 0, _camFade(10, 1));
      chk("远景 >=45 完全恢复", _camFade(45, 1) === 1 && _camFade(60, 1) === 1, _camFade(60, 1));
      chk("22~45 之间平滑淡出",
        _camFade(30, 1) > 0 && _camFade(30, 1) < 1 && _camFade(30, 1) < _camFade(40, 1),
        "fade(30)=" + _camFade(30, 1).toFixed(3));
      chk("淡出阈值基准 22 / 45", CAM_FADE_NEAR === 22 && CAM_FADE_FAR === 45,
        CAM_FADE_NEAR + " / " + CAM_FADE_FAR);
      chk("阈值随世界缩放：×2 世界 → 44 / 90",
        _camFade(43.9, 2) === 0 && _camFade(90, 2) === 1 &&
        Math.abs(_camFade(60, 2) - _camFade(30, 1)) < 1e-9,
        "fade(90,2)=" + _camFade(90, 2));

      // 12) 版本号
      chk("版本号存在", typeof VERSION === "string" && VERSION.length > 0, VERSION);
    } catch (e) {
      chk("selfTest 执行异常", false, e && e.message ? e.message : e);
    }

    var ok = checks.length > 0;
    for (var i = 0; i < checks.length; i++) if (!checks[i].ok) { ok = false; break; }

    var plan2 = _geomPlan(DEF_SLOTS, DEF_MAX_ARROWS);
    return {
      ok: ok,
      version: VERSION,
      checks: checks,
      stats: {
        maxArrows: DEF_MAX_ARROWS,
        slots: DEF_SLOTS,
        heightScale: DEF_HEIGHT_SCALE,
        waterfallSpanBase: WF_SPAN,
        waterfallScale: DEF_WATERFALL_SCALE,
        waterfallSpanAtW2: WF_SPAN * DEF_WATERFALL_SCALE,
        laneGap: WF_LANE_Z * 2 - WF_LANE_HALF_W * 2,
        anchor: DEF_ANCHOR,
        camFadeNear: CAM_FADE_NEAR,
        camFadeFar: CAM_FADE_FAR,
        spriteFontGrow: SPRITE_FONT_GROW,
        ribbonTris: plan2.ribbonTris,
        arrowTris: plan2.arrowTris,
        tris: plan2.tris,
        colorHigh: "#4ea1ff",
        colorLow: "#f5a623",
        colorCmd: "#37e2d5",
        modes: Object.keys(MODES),
      },
    };
  }

  /* 只暴露约定的一个全局（契约 §2.2 / §3.2） */
  window.UAVHUD = {
    version: VERSION,
    create: create,
    selfTest: selfTest,
  };
})();
