/* =============================================================================
   weather_system.js —— 天气视觉模块（雨 / 雪 / 沙尘粒子 + 能见度雾衰减）
   -----------------------------------------------------------------------------
   ★ 为什么"天气"必须真的改能见度，而不能只是一层贴图滤镜？
     真实世界里雨/雪/沙尘首先削弱的是「光学能见度」：空气里悬浮的雨滴/冰晶/沙粒
     对光线做多重散射，远处物体的对比度被指数级压低 —— three.js 的 FogExp2 正是
     这个现象的工程近似：
         fogFactor = 1 - exp( -(density * distance)^2 )
     能见度一掉，视距/激光/微波链路的误码率才会上去。本模块把后端的
     visibility_km 映射成 scene.fog.density，让"画面看不清"和"链路健康度变差"
     出自同一份物理数据 —— 这样课程报告里
         「沙尘 → 弱网 → 健康度下降 → 动态权重调度差异」
     才是一条完整、可解释的因果链，而不是两个互不相干的演示。

   ★ 契约：docs/07-3D渲染升级接口契约.md §4.5
     - 全局对象：window.UAVWeather（本文件唯一新增的全局，IIFE 经典脚本）
     - 粒子都在「相机跟随体积 90×50×90、中心=相机前方 30 单位」内循环，避免
       "只在眼前下一团雨"的廉价感，也不会为了全世界下雨把 CPU 打满。
     - update(dt) 只改 attribute / uniform / visible，绝不重建 geometry。
     - 纹理全部用 canvas 程序化生成（本项目禁止外网/CDN/外部贴图）。

   ★ three.js 版本：r128（全局 THREE，经典脚本，不能写 import/export）
   ========================================================================== */
(function () {
  "use strict";

  /* =========================================================== 版本与物理换算 */

  const VERSION = "1.0.0";

  /* 世界尺度换算：DEM 包围盒 176 世界单位 ≈ 7.3 km（契约 §4.5 明确给定），
     于是 1 世界单位 ≈ 41 m。所有"公里 → 世界单位"都必须经过这个常量：
     契约 §4.2 要求地形模块为"更大的 DEM"铺路，一旦世界尺寸变化，能见度映射
     也必须跟着走，不能把 176/7.3 写死在公式里。 */
  const M_PER_UNIT = 41;

  /* 项目原来的晴朗雾密度：app3d.html initScene() 里
       scene.fog = new THREE.FogExp2(0x0b1a26, 0.0016)
     clear 预设必须**逐位恢复**成它，否则默认工况画面与升级前不一致（硬约束）。 */
  const CLEAR_FOG_DENSITY = 0.0016;

  /* 能见度 → 雾密度系数：density = k / max(0.8, 能见度世界单位)
     取 k = 1.25（契约建议 1.2~1.5）：
       "能见度距离处"的雾权重 = 1 - exp(-k²) ≈ 79%，即刚好看不清；
       沙尘（强度 0.85 时能见度约 2.3 km ≈ 56 世界单位）在 100+ 单位外雾权重 ≈ 99%，
       远景山脊被吃掉；雨（约 4.4 km ≈ 107 单位）在 40~60 单位的中景还有 50%~70% 可见度。
     这两个效果的同时成立，就是选 1.25 而不是 1.5 的原因。 */
  const FOG_K = 1.25;

  const DEG = Math.PI / 180;

  /* ================================================================ 天气预设表 */
  /* 字段与后端 WeatherPreset（契约 §4.6.1）语义对齐，键名保持后端风格：
       preset / label / visibility_km / link_penalty_max / wind_dir_deg / wind_speed_mps
     fogColor 是契约 §4.5 指定的雾色（字符串便于直接读）：
       雨 #2a3340 / 雪 #cfd8e2 / 沙尘 #8a6a48 / 晴 = null（沿用当前 TOD 雾色）
     注意：后端 /api/state 快照不含 fog_color_hex（契约 §4.6.4 的 8 个键里没有），
     Lead 切天气后前端按 preset 名查这张表取雾色，不依赖后端补字段。 */
  const PRESETS = {
    clear: { preset: "clear", label: "晴朗", visibility_km: 12.0, link_penalty_max: 0.0,
             fogColor: null, wind_dir_deg: 90, wind_speed_mps: 1.5 },
    rain:  { preset: "rain", label: "中雨", visibility_km: 3.0, link_penalty_max: 18.0,
             fogColor: "#2a3340", wind_dir_deg: 215, wind_speed_mps: 8.0 },
    snow:  { preset: "snow", label: "降雪", visibility_km: 1.2, link_penalty_max: 24.0,
             fogColor: "#cfd8e2", wind_dir_deg: 300, wind_speed_mps: 4.0 },
    dust:  { preset: "dust", label: "沙尘", visibility_km: 0.6, link_penalty_max: 32.0,
             fogColor: "#8a6a48", wind_dir_deg: 250, wind_speed_mps: 14.0 },
  };

  /* 画质档位预算：契约 §2.4 要求 cpu 档粒子 ≤1200 且不画沙墙。
     high/medium 用多点让"雨丝/雪片"在 1080p 下读得出来；
     cpu 档（SwiftShader 软件光栅）必须让 update + 光栅都活着，所以再压一档。 */
  const BUDGET = {
    high:   { rain: 3000, snow: 3200, dust: 2400, walls: 3, size: 1.00 },
    medium: { rain: 2000, snow: 2200, dust: 1600, walls: 2, size: 0.92 },
    cpu:    { rain: 1000, snow: 1100, dust: 800,  walls: 0, size: 0.82 },
  };

  /* 相机跟随体积（世界单位）：X/Z = range（默认 90），Y = range * 50/90 ≈ 50。
     中心放在相机前方 30 单位，保证"眼前有雨、远处也有"而不是只糊在镜头上。 */
  const BOX_DEFAULT = 90;
  const BOX_Y_RATIO = 50 / 90;
  const BOX_FORWARD = 30;      // 体积中心 = 相机位置 + 前方 30 单位
  const CPU_PARTICLE_CAP = 1200;

  /* 沙墙（沙尘的 2~3 层半透明大面片）参数：距离相机越远越大、越淡、滚动越慢，
     形成视差 —— 近层纹理跑得快（近处沙粒角速度大），远层慢，看上去像"沙尘在流动"。 */
  const WALL_LAYERS = [
    { dist: 22, yOff: -1, w: 140, h: 80,  opacity: 0.44, scroll: 0.090, repX: 1, repY: 1 },
    { dist: 44, yOff: 5,  w: 240, h: 128, opacity: 0.34, scroll: 0.058, repX: 1, repY: 1 },
    { dist: 70, yOff: 11, w: 350, h: 185, opacity: 0.24, scroll: 0.036, repX: 1, repY: 1 },
  ];

  /* ====================================================== 纯函数（selfTest 复用） */
  /* 说明：selfTest 必须"纯逻辑、不依赖 renderer"，所以把全部数值换算抽成这里的
     纯函数。运行时与自检走同一份代码，报告里的数字 = 页面里真正生效的数字。 */

  function clampNum(v, a, b) { return v < a ? a : (v > b ? b : v); }

  function num(v, dflt) {
    const n = Number(v);
    return (v === null || v === undefined || v === "" || !isFinite(n)) ? dflt : n;
  }

  function smoothstep(a, b, x) {
    const t = clampNum((x - a) / (b - a || 1e-9), 0, 1);
    return t * t * (3 - 2 * t);
  }

  /** "#2a3340" / 0x2a3340 / 0x2a3340 数字 → 0x2a3340；非法返回 null */
  function toHexInt(v) {
    if (typeof v === "number" && isFinite(v)) return v | 0;
    if (typeof v === "string") {
      const s = v.replace(/^\s*#/, "").trim();
      if (!/^[0-9a-fA-F]{6}$/.test(s)) return null;
      return parseInt(s, 16) | 0;
    }
    return null;
  }

  /** 公里 → 世界单位（契约 §4.5：1 世界单位 ≈ 41 m）；下限 0.8 防止 0 能见度除零 */
  function visibilityWorld(visKm) {
    return Math.max(0.8, Math.abs(num(visKm, 0)) * 1000 / M_PER_UNIT);
  }

  /* --------------------------------------------------------------------------
     雾密度：**可读性优先**的双口径
     --------------------------------------------------------------------------
     物理口径：d = FOG_K / 能见度世界单位 —— 在"能见度距离"处遮蔽约 71%。

     问题（2026-11 截图复盘）：本项目相机看地形是 300~700 世界单位远
     （矿区宽 350 单位），而 0.6~3 km 的能见度换算成世界单位只有 15~73 单位。
     物理密度会让画面在 100 单位外就 100% 变成雾色 —— 雨/雪/沙尘三张截图
     都糊成"单色块"，地形轮廓完全消失，正是这个原因。

     视觉口径：给每种天气设一个"远景遮蔽率上限"，反解密度：
         cover(520 单位处) = 雨 72% / 雪 80% / 沙尘 88%
         d = sqrt(-ln(1-cover)) / 520
     近景清晰、中景可辨、远景淹没 —— 画面才有纵深，而不是一堵色墙。

     取两者较小值：既保留"能见度越低越糊"的单调性，又不把画面糊死。
     ⚠ 这只影响**画面**；链路健康度扣分仍由后端 weather.py 按真实能见度计算，
       评测口径不受影响。
  -------------------------------------------------------------------------- */
  const FOG_LEGIBLE_DIST = 520;
  const FOG_COVER = { rain: 0.72, snow: 0.80, dust: 0.88 };

  /** 能见度 → FogExp2.density（物理口径与可读性上限取较小值） */
  function fogDensityFor(preset, visKm) {
    if (preset === "clear") return CLEAR_FOG_DENSITY;
    const physical = FOG_K / visibilityWorld(visKm);
    const cover = FOG_COVER[preset] || 0.75;
    const legible = Math.sqrt(-Math.log(1 - cover)) / FOG_LEGIBLE_DIST;
    return Math.min(physical, legible);
  }

  /** FogExp2 在给定距离处的雾权重：0=完全清晰，1=完全被雾吃掉（与 three 的 shader 同式） */
  function fogFactorAt(density, dist) {
    const t = density * dist;
    return 1 - Math.exp(-t * t);
  }

  /** 天气对太阳/半球光的衰减系数（契约 §4.5 公式） */
  function lightFactors(intensity) {
    const i = clampNum(num(intensity, 0), 0, 1);
    return { sun: 1 - 0.55 * i, hemi: 1 - 0.35 * i };
  }

  /** 风向角 → 世界 XZ 平面上的单位漂移方向。
      约定：0° = +Z，顺时针增大（90° = +X）。纯粹为了画面风的一致，
      不参与任何链路计算，不需要气象学精度。 */
  function windVector(dirDeg) {
    const a = num(dirDeg, 0) * DEG;
    return { x: Math.sin(a), z: Math.cos(a) };
  }

  /** 各档位 × 各天气的粒子预算（cpu 档再夹一层 1200 硬上限） */
  function particleBudget(tier, preset, qualityParticles) {
    const t = BUDGET[tier] ? tier : "high";
    let n = BUDGET[t].rain;
    if (preset === "snow") n = BUDGET[t].snow;
    else if (preset === "dust") n = BUDGET[t].dust;
    else if (preset === "clear") n = 0;
    // 尊重 Lead 传入的总粒子预算：高/中档按 40% 给天气（其余给点云/机群效果），
    // cpu 档本身预算就小，按 85% 用足（仍受 1200 硬上限约束，保证软件光栅跑得动）。
    const qp = num(qualityParticles, 0);
    if (qp > 0) n = Math.min(n, Math.round(qp * (t === "cpu" ? 0.85 : 0.4)));
    if (t === "cpu") n = Math.min(n, CPU_PARTICLE_CAP);
    return Math.max(0, n | 0);
  }

  /** 沙墙层数（cpu 档不画：软件光栅下 3 张全屏大面片太贵） */
  function wallCountForTier(tier) {
    return (BUDGET[tier] || BUDGET.high).walls;
  }

  /** 把后端快照 / 本地预设统一成内部可用形态（防御式：后端字段缺失也不崩） */
  function normalizeSnapshot(snapshot) {
    const s = (snapshot && typeof snapshot === "object") ? snapshot : {};
    let preset = String(s.preset || "clear").toLowerCase();
    if (!PRESETS[preset]) preset = "clear";

    const def = PRESETS[preset];
    const intensity = preset === "clear" ? 0 : clampNum(num(s.intensity, 0), 0, 1);
    let vis = num(s.visibility_km, NaN);
    if (!isFinite(vis) || vis <= 0) vis = def.visibility_km;
    vis = clampNum(vis, 0.05, 60);

    return {
      preset: preset,
      label: s.label ? String(s.label) : def.label,
      intensity: intensity,
      visibility_km: vis,
      link_penalty: num(s.link_penalty, 0),
      wind_dir_deg: num(s.wind_dir_deg, def.wind_dir_deg),
      wind_speed_mps: clampNum(num(s.wind_speed_mps, def.wind_speed_mps), 0, 40),
    };
  }

  /* ================================================================ 小工具 */

  function defaultLog(msg) { if (typeof console !== "undefined" && console.log) console.log("[Weather] " + msg); }
  function warn(msg) { if (typeof console !== "undefined" && console.warn) console.warn("[Weather] " + msg); }

  /** 确定性 LCG：粒子初始分布可复现（截图/回归对比时画面不会"每次都不一样"） */
  function makeRng(seed) {
    let s = (seed >>> 0) || 1;
    return function () {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
  }

  function makeCanvas(w, h) {
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    return c;
  }

  /** 雪片贴图：64×64 canvas 径向渐变圆点（中心实、边缘透明），
      比 THREE.PointsMaterial 默认的方块贴图自然得多（否则漫天小方块）。 */
  function makeSnowTexture(T) {
    const c = makeCanvas(64, 64);
    const g = c.getContext("2d");
    const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grd.addColorStop(0.00, "rgba(255,255,255,1.00)");
    grd.addColorStop(0.30, "rgba(255,255,255,0.95)");
    grd.addColorStop(0.68, "rgba(255,255,255,0.38)");
    grd.addColorStop(1.00, "rgba(255,255,255,0.00)");
    g.fillStyle = grd;
    g.fillRect(0, 0, 64, 64);
    const tex = new T.CanvasTexture(c);
    tex.minFilter = T.LinearFilter;
    tex.magFilter = T.LinearFilter;
    tex.needsUpdate = true;
    return tex;
  }

  /** 沙尘颗粒贴图：64×64 柔边圆点（中心偏亮、边缘快速衰减）。
      沙尘点子用加色混合，中心亮一点更像"被夕阳照亮的一粒沙"，
      边缘平滑则避免出现一排排小方块（PointsMaterial 默认就是方块）。 */
  function makeDustTexture(T) {
    const c = makeCanvas(64, 64);
    const g = c.getContext("2d");
    const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grd.addColorStop(0.00, "rgba(255,246,228,0.95)");
    grd.addColorStop(0.35, "rgba(255,238,210,0.62)");
    grd.addColorStop(0.72, "rgba(240,214,170,0.20)");
    grd.addColorStop(1.00, "rgba(240,214,170,0.00)");
    g.fillStyle = grd;
    g.fillRect(0, 0, 64, 64);
    const tex = new T.CanvasTexture(c);
    tex.minFilter = T.LinearFilter;
    tex.magFilter = T.LinearFilter;
    tex.needsUpdate = true;
    return tex;
  }

  /** 一个简单值噪声（给沙墙贴图用）：随机格点 + 双线性插值 + 多倍频叠加，
      得到"絮状沙云"的 alpha 纹理。全部在 canvas 上算，不依赖外部图片。 */
  function makeValueLattice(cols, rows, rnd) {
    const arr = new Float32Array(cols * rows);
    for (let i = 0; i < arr.length; i++) arr[i] = rnd();
    return arr;
  }
  function sampleLattice(arr, cols, rows, u, v) {
    const fx = clampNum(u, 0, 0.999999) * (cols - 1);
    const fz = clampNum(v, 0, 0.999999) * (rows - 1);
    const i0 = fx | 0, j0 = fz | 0;
    const i1 = Math.min(i0 + 1, cols - 1), j1 = Math.min(j0 + 1, rows - 1);
    const tx = fx - i0, tz = fz - j0;
    const a = arr[j0 * cols + i0], b = arr[j0 * cols + i1];
    const c = arr[j1 * cols + i0], d = arr[j1 * cols + i1];
    return (a * (1 - tx) + b * tx) * (1 - tz) + (c * (1 - tx) + d * tx) * tz;
  }

  /** 沙墙贴图：512×256，土黄絮状沙云（带 alpha 洞），可横向滚动。
      纹理自身带"近地更浓、上方更淡"的纵向渐变，四周边缘 alpha 渐隐 → 贴在
      billboard 上时看不到面片边界（否则沙尘里会冒出几个大方块的轮廓）。
      不重复平铺（repeat=1）：边缘渐隐的纹理一旦平铺会出现规则的"接缝格"。 */
  function makeSandNoiseCanvas() {
    const W = 512, H = 256;
    const c = makeCanvas(W, H);
    const g = c.getContext("2d");
    const img = g.createImageData(W, H);
    const rnd = makeRng(0x5A17);
    const oct = [
      { cols: 6, rows: 4, arr: null, w: 0.46 },
      { cols: 13, rows: 8, arr: null, w: 0.26 },
      { cols: 29, rows: 16, arr: null, w: 0.16 },
      { cols: 61, rows: 32, arr: null, w: 0.12 },
    ];
    oct.forEach((o) => { o.arr = makeValueLattice(o.cols, o.rows, rnd); });

    for (let y = 0; y < H; y++) {
      const v = y / (H - 1);
      for (let x = 0; x < W; x++) {
        const u = x / (W - 1);
        let n = 0;
        for (let k = 0; k < oct.length; k++) {
          n += sampleLattice(oct[k].arr, oct[k].cols, oct[k].rows, u, v) * oct[k].w;
        }
        // alpha：噪声阈值化成"沙云团"，再叠加纵向浓度（下部浓、上部淡）
        const patch = smoothstep(0.28, 0.64, n);
        const vertical = 0.48 + 0.52 * (1 - smoothstep(0.0, 0.94, v));
        // 四周边缘渐隐：0 在边界、1 在内部 —— 让大面片"没有边"
        const edge = smoothstep(0.0, 0.16, u) * (1 - smoothstep(0.84, 1.0, u))
                   * smoothstep(0.0, 0.18, v) * (1 - smoothstep(0.82, 1.0, v));
        const a = clampNum(patch * vertical * edge, 0, 1);
        // 颜色：土黄 #c8a06a 上下浮动一点亮度，避免整片死板
        const br = 0.72 + 0.42 * n;
        const p = (y * W + x) * 4;
        img.data[p]     = clampNum(200 * br, 0, 255) | 0;
        img.data[p + 1] = clampNum(160 * br, 0, 255) | 0;
        img.data[p + 2] = clampNum(106 * br, 0, 255) | 0;
        img.data[p + 3] = (a * 255) | 0;
      }
    }
    g.putImageData(img, 0, 0);
    return c;
  }

  /** canvas → CanvasTexture（沙墙每层独立 repeat/offset，所以每层单独 new 一张纹理） */
  function makeSandTexture(T, canvas, repX, repY) {
    const tex = new T.CanvasTexture(canvas);
    tex.wrapS = T.RepeatWrapping;
    tex.wrapT = T.RepeatWrapping;
    tex.repeat.set(repX, repY);
    tex.offset.set(0.13, 0.07);
    tex.minFilter = T.LinearFilter;
    tex.magFilter = T.LinearFilter;
    tex.needsUpdate = true;
    return tex;
  }

  /* ================================================================ 粒子构建 */
  /* 三个 emitter 都遵循同一套约定：
       - 位置数组 = **相对天气 group 的局部坐标**（group 每帧跟随相机，见 update）
       - 局部 X/Z ∈ [-BOX/2, +BOX/2]，Y ∈ [-BOX_Y/2, +BOX_Y/2]
       - 越界用"环绕(wrap)"回到另一侧，永远只在相机附近这一盒子里，没有生成/销毁开销
       - geometry.frustumCulled = false：位置每帧在变，three 内置包围球会过期而误剔除 */

  /* --------------------------------------------------------------------------
     天气点粒子材质（雪 / 沙尘共用）
     --------------------------------------------------------------------------
     为什么不用 PointsMaterial：
       1) PointsMaterial 只能给**全场景一个 size**，雪花看起来大小一模一样，
          像"撒了一把白点"；真实雪片近大远小、有虚焦（bokeh）。
       2) 它没有逐粒子的透明度/近距淡出，贴脸的粒子会糊满镜头。
       3) 默认 addtive 混合叠在亮雾上会整片过曝（旧版截图就是这个毛病）。

     这个 shader 做四件事：
       * 逐粒子 aSize/aAlpha —— 大小、明暗都有差异；
       * gl_PointSize = aSize * uScale / 距离 —— 标准透视衰减（uScale 每帧按视口算）；
       * 近距淡出 + 远距淡出 —— 不会糊镜头，也不会在远景堆成一片白；
       * 片元做软圆 + 平方衰减 —— 柔边雪花，不是方块/硬点。
     -------------------------------------------------------------------------- */
  function makePointMaterial(T, color, nearFade, farFade) {
    return new T.ShaderMaterial({
      uniforms: {
        uColor: { value: new T.Color(color) },
        uOpacity: { value: 0 },
        uScale: { value: 900 },
        uNear: { value: nearFade },
        uFar: { value: farFade },
      },
      vertexShader: [
        "attribute float aSize;",
        "attribute float aAlpha;",
        "uniform float uScale;",
        "uniform float uNear;",
        "uniform float uFar;",
        "varying float vAlpha;",
        "void main() {",
        "  vec4 mv = modelViewMatrix * vec4(position, 1.0);",
        "  float d = max(0.001, -mv.z);",
        "  gl_PointSize = max(1.0, aSize * uScale / d);",
        "  gl_Position = projectionMatrix * mv;",
        "  float nearFade = smoothstep(uNear * 0.35, uNear, d);",
        "  float farFade  = 1.0 - smoothstep(uFar * 0.55, uFar, d);",
        "  vAlpha = aAlpha * nearFade * farFade;",
        "}",
      ].join("\n"),
      fragmentShader: [
        "uniform vec3 uColor;",
        "uniform float uOpacity;",
        "varying float vAlpha;",
        "void main() {",
        "  vec2 p = gl_PointCoord - 0.5;",
        "  float r = length(p) * 2.0;",
        "  float a = smoothstep(1.0, 0.12, r);",
        "  a *= a;",
        "  float alpha = a * vAlpha * uOpacity;",
        "  if (alpha < 0.004) discard;",
        "  gl_FragColor = vec4(uColor, alpha);",
        "}",
      ].join("\n"),
      transparent: true,
      depthWrite: false,
      depthTest: true,
      blending: T.NormalBlending,
    });
  }

  /** 统一设置材质透明度：兼容 PointsMaterial(.opacity) 与 ShaderMaterial(.uniforms.uOpacity) */
  function setMatOpacity(mat, v) {
    if (!mat) return;
    if (mat.uniforms && mat.uniforms.uOpacity) mat.uniforms.uOpacity.value = v;
    else mat.opacity = v;
  }

  function buildRain(T, count, hx, hy, hz, sizeScale) {
    const geo = new T.BufferGeometry();
    const arr = new Float32Array(count * 2 * 3);   // 每滴 2 顶点（头、尾）→ 雨丝
    const speed = new Float32Array(count);         // 每滴下落速度 28~40 单位/s
    const windX = new Float32Array(count);         // 每滴水平漂移（由风向/风速算）
    const windZ = new Float32Array(count);
    const dirX = new Float32Array(count);          // 单位速度方向（雨丝拉长方向）
    const dirY = new Float32Array(count);
    const dirZ = new Float32Array(count);
    const len = new Float32Array(count);           // 雨丝长度 0.8~2.2 单位
    const rnd = makeRng(0x5EED1);

    for (let i = 0; i < count; i++) {
      const x = (rnd() * 2 - 1) * hx;
      const y = (rnd() * 2 - 1) * hy;
      const z = (rnd() * 2 - 1) * hz;
      speed[i] = 28 + rnd() * 12;                 // 契约：28~40 单位/s
      len[i] = (0.8 + rnd() * 1.4) * sizeScale;   // 契约：0.8~2.2 单位
      dirX[i] = 0; dirY[i] = -1; dirZ[i] = 0;
      const o = i * 6;
      arr[o] = x; arr[o + 1] = y; arr[o + 2] = z;
      arr[o + 3] = x; arr[o + 4] = y + len[i]; arr[o + 5] = z;
    }
    geo.setAttribute("position", new T.BufferAttribute(arr, 3));
    if (T.DynamicDrawUsage !== undefined && geo.attributes.position.setUsage) {
      geo.attributes.position.setUsage(T.DynamicDrawUsage);
    }
    /* 雨丝：加色混合保留"被天光打亮"的观感，但把亮度压下来（旧版 0.62 太亮，
       在亮雾背景上糊成一片白条），并且打开 fog —— 远处的雨丝会自然融进雨雾。 */
    const mat = new T.LineBasicMaterial({
      color: 0xbfd6ea, transparent: true, opacity: 0, blending: T.AdditiveBlending,
      depthWrite: false, fog: true,
    });
    const mesh = new T.LineSegments(geo, mat);
    mesh.frustumCulled = false;
    mesh.visible = false;
    mesh.renderOrder = 6;
    return { kind: "rain", mesh: mesh, geo: geo, mat: mat, count: count, arr: arr,
             speed: speed, windX: windX, windZ: windZ, dirX: dirX, dirY: dirY, dirZ: dirZ, len: len };
  }

  function buildSnow(T, count, hx, hy, hz, sizeScale, tex) {
    const geo = new T.BufferGeometry();
    const arr = new Float32Array(count * 3);
    const speed = new Float32Array(count);       // 2~5 单位/s（雪比雨慢一个数量级）
    const baseX = new Float32Array(count);       // 去掉正弦摆动后的"漂移原点"
    const baseZ = new Float32Array(count);
    const phase = new Float32Array(count);       // 摆动相位
    const freq = new Float32Array(count);        // 摆动频率
    const amp = new Float32Array(count);         // 摆动幅度（单位）
    const windX = new Float32Array(count);
    const windZ = new Float32Array(count);
    const sizes = new Float32Array(count);       // 逐粒子世界尺寸（近大远小的"大"）
    const alphas = new Float32Array(count);      // 逐粒子透明度（远近/虚实差异）
    const rnd = makeRng(0x5EED2);

    for (let i = 0; i < count; i++) {
      const x = (rnd() * 2 - 1) * hx;
      const y = (rnd() * 2 - 1) * hy;
      const z = (rnd() * 2 - 1) * hz;
      speed[i] = 2 + rnd() * 3;                  // 契约：下落 2~5 单位/s
      baseX[i] = x; baseZ[i] = z;
      phase[i] = rnd() * Math.PI * 2;
      freq[i] = 0.5 + rnd() * 1.3;
      amp[i] = 0.35 + rnd() * 0.95;              // 正弦横向摆动，模拟雪花受气流的飘忽
      // 雪片尺寸 0.10~0.34 世界单位：近处成"虚焦光斑"，远处才是细碎小雪
      sizes[i] = (0.10 + rnd() * 0.24) * sizeScale;
      alphas[i] = 0.55 + rnd() * 0.45;
      windX[i] = 0; windZ[i] = 0;
      arr[i * 3] = x; arr[i * 3 + 1] = y; arr[i * 3 + 2] = z;
    }
    geo.setAttribute("position", new T.BufferAttribute(arr, 3));
    geo.setAttribute("aSize", new T.BufferAttribute(sizes, 1));
    geo.setAttribute("aAlpha", new T.BufferAttribute(alphas, 1));
    if (T.DynamicDrawUsage !== undefined && geo.attributes.position.setUsage) {
      geo.attributes.position.setUsage(T.DynamicDrawUsage);
    }
    /* 雪：改成逐粒子 shader（见 makePointMaterial 的注释）。
       旧版"统一尺寸 + 加色混合"在亮雾上就是一片白噪点，这是截图里最刺眼的问题。 */
    const mat = makePointMaterial(T, 0xf2f8ff, 2.5, 150);
    const mesh = new T.Points(geo, mat);
    mesh.frustumCulled = false;
    mesh.visible = false;
    mesh.renderOrder = 5;
    return { kind: "snow", mesh: mesh, geo: geo, mat: mat, count: count, arr: arr,
             speed: speed, baseX: baseX, baseZ: baseZ, phase: phase, freq: freq,
             amp: amp, windX: windX, windZ: windZ };
  }

  function buildDust(T, count, wallCount, hx, hy, hz, sizeScale) {
    const geo = new T.BufferGeometry();
    const arr = new Float32Array(count * 3);
    const phase = new Float32Array(count);
    const freq = new Float32Array(count);
    const turb = new Float32Array(count);        // 涡流强度（每颗粒子不同，避免整片同步平移）
    const windX = new Float32Array(count);
    const windZ = new Float32Array(count);
    const sizes = new Float32Array(count);
    const alphas = new Float32Array(count);
    const rnd = makeRng(0x5EED3);

    for (let i = 0; i < count; i++) {
      arr[i * 3] = (rnd() * 2 - 1) * hx;
      arr[i * 3 + 1] = (rnd() * 2 - 1) * hy;
      arr[i * 3 + 2] = (rnd() * 2 - 1) * hz;
      phase[i] = rnd() * Math.PI * 2;
      freq[i] = 0.4 + rnd() * 1.7;
      turb[i] = 1.2 + rnd() * 3.8;
      // 沙尘颗粒比雪片大得多、也更透明（悬浮的细尘 + 被风卷起的大颗粒）
      sizes[i] = (0.22 + rnd() * 0.72) * sizeScale;
      alphas[i] = 0.18 + rnd() * 0.42;
      windX[i] = 0; windZ[i] = 0;
    }
    geo.setAttribute("position", new T.BufferAttribute(arr, 3));
    geo.setAttribute("aSize", new T.BufferAttribute(sizes, 1));
    geo.setAttribute("aAlpha", new T.BufferAttribute(alphas, 1));
    if (T.DynamicDrawUsage !== undefined && geo.attributes.position.setUsage) {
      geo.attributes.position.setUsage(T.DynamicDrawUsage);
    }
    /* 沙尘：同样改成逐粒子 shader。旧版"加色 + 统一大小"叠在土黄雾上，
       只会把画面进一步提亮成一片米色，看不出颗粒感。 */
    const mat = makePointMaterial(T, 0xd8b483, 3.0, 190);
    const mesh = new T.Points(geo, mat);
    mesh.frustumCulled = false;
    mesh.visible = false;
    mesh.renderOrder = 5;

    // ---- 沙墙：2~3 层大面片（billboard 每帧朝相机），程序化噪声贴图横向滚动 ----
    const walls = [];
    const sharedGeo = new T.PlaneGeometry(1, 1);
    const canvas = wallCount > 0 ? makeSandNoiseCanvas() : null;
    for (let i = 0; i < wallCount; i++) {
      const cfg = WALL_LAYERS[i % WALL_LAYERS.length];
      const tex = makeSandTexture(T, canvas, cfg.repX, cfg.repY);
      const wmat = new T.MeshBasicMaterial({
        map: tex, transparent: true, opacity: 0, depthWrite: false,
        side: T.DoubleSide, fog: false, blending: T.NormalBlending,
      });
      const wm = new T.Mesh(sharedGeo, wmat);
      wm.scale.set(cfg.w, cfg.h, 1);
      wm.frustumCulled = false;
      wm.visible = false;
      wm.renderOrder = 3;                        // 在粒子之前画，粒子仍可见
      walls.push({ mesh: wm, mat: wmat, tex: tex, cfg: cfg, phase: i * 2.1 });
    }
    return { kind: "dust", mesh: mesh, geo: geo, mat: mat, count: count, arr: arr,
             phase: phase, freq: freq, turb: turb, windX: windX, windZ: windZ,
             walls: walls, wallGeo: sharedGeo };
  }

  /* ================================================================ selfTest */
  /**
   * 纯逻辑自检（不依赖 renderer / DOM / three 实例）：
   * 验收会用无头浏览器 --eval "JSON.stringify(UAVWeather.selfTest())" 抓结果。
   */
  function selfTest() {
    const checks = [];
    function add(name, ok, detail) { checks.push({ name: name, ok: !!ok, detail: String(detail) }); }

    // 1) 版本号
    add("version 语义化", /^\d+\.\d+\.\d+$/.test(VERSION), VERSION);

    // 2) 4 个预设齐备，且雾色与契约 §4.5 一致
    const need = ["clear", "rain", "snow", "dust"];
    add("预设齐备(clear/rain/snow/dust)", need.every(function (k) {
      return PRESETS[k] && PRESETS[k].label && PRESETS[k].visibility_km > 0;
    }), need.map(function (k) { return k + ":" + presetLabel(k); }).join(" / "));
    add("雾色符合契约", PRESETS.rain.fogColor === "#2a3340" &&
        PRESETS.snow.fogColor === "#cfd8e2" &&
        PRESETS.dust.fogColor === "#8a6a48" && PRESETS.clear.fogColor === null,
        "rain " + PRESETS.rain.fogColor + " / snow " + PRESETS.snow.fogColor +
        " / dust " + PRESETS.dust.fogColor + " / clear=TOD");

    // 3) 能见度换算（1 世界单位 ≈ 41 m，契约 §4.5）
    add("1km → 24.4 世界单位", Math.abs(visibilityWorld(1) - 1000 / 41) < 1e-9,
        visibilityWorld(1).toFixed(3) + " 单位");

    // 4) clear 必须恢复项目原雾密度 0.0016（默认工况画面不变）
    add("clear 雾密度=0.0016", Math.abs(fogDensityFor("clear", 12) - CLEAR_FOG_DENSITY) < 1e-12,
        String(fogDensityFor("clear", 12)));

    // 5) 物理口径确实被"可读性上限"截断（否则画面会糊成一堵色墙）
    const dRain3 = fogDensityFor("rain", 3.0);
    const physRain3 = FOG_K / (3000 / M_PER_UNIT);
    const legibleRain = Math.sqrt(-Math.log(1 - FOG_COVER.rain)) / FOG_LEGIBLE_DIST;
    add("雾密度取物理/可读性较小值", Math.abs(dRain3 - Math.min(physRain3, legibleRain)) < 1e-12 &&
        dRain3 < physRain3,
        "rain@3km=" + dRain3.toFixed(5) + " 物理 " + physRain3.toFixed(5) +
        " 可读性 " + legibleRain.toFixed(5));

    // 6) 单调性：能见度越低，雾越浓（dust > snow > rain > 0）
    const dSnow = fogDensityFor("snow", 1.2), dDust = fogDensityFor("dust", 0.6);
    add("能见度越低雾越浓", dDust > dSnow && dSnow > dRain3 && dRain3 > 0,
        "dust " + dDust.toFixed(4) + " > snow " + dSnow.toFixed(4) + " > rain " + dRain3.toFixed(4));

    // 7) 视觉验收的定量等价位（改版后以"纵深感"为准，而不是"糊死"）：
    //    * 近景（30 单位）必须清晰 —— 否则贴脸的无人机/地面站也会被雾吃掉；
    //    * 远景（520 单位）按预设的 cover 遮蔽（沙尘最重 88%），山脊仍留轮廓；
    //    * 三档遮蔽率必须单调：沙尘 > 雪 > 雨。
    const dustNear = fogFactorAt(dDust, 30);
    const dustFar = fogFactorAt(dDust, FOG_LEGIBLE_DIST);
    const snowFar = fogFactorAt(dSnow, FOG_LEGIBLE_DIST);
    const rainFar = fogFactorAt(dRain3, FOG_LEGIBLE_DIST);
    add("近景(30 单位)清晰 <10%", dustNear < 0.10, (dustNear * 100).toFixed(2) + "%");
    add("远景(520 单位)按 cover 遮蔽", Math.abs(dustFar - FOG_COVER.dust) < 0.02,
        "沙尘 " + (dustFar * 100).toFixed(1) + "% / 雪 " + (snowFar * 100).toFixed(1) +
        "% / 雨 " + (rainFar * 100).toFixed(1) + "%");
    add("远景遮蔽率单调 沙尘>雪>雨", dustFar > snowFar && snowFar > rainFar,
        "dust " + (dustFar * 100).toFixed(1) + " > snow " + (snowFar * 100).toFixed(1) +
        " > rain " + (rainFar * 100).toFixed(1));

    // 8) 光照衰减系数（契约 §4.5）
    const lf0 = lightFactors(0), lf1 = lightFactors(1);
    add("光照衰减 int=0 → 1.00/1.00", Math.abs(lf0.sun - 1) < 1e-12 && Math.abs(lf0.hemi - 1) < 1e-12,
        lf0.sun + " / " + lf0.hemi);
    add("光照衰减 int=1 → 0.45/0.65", Math.abs(lf1.sun - 0.45) < 1e-12 && Math.abs(lf1.hemi - 0.65) < 1e-12,
        lf1.sun + " / " + lf1.hemi);

    // 9) 风向单位向量（0°=+Z，90°=+X）
    const w0 = windVector(0), w90 = windVector(90);
    const unitOk = Math.abs(w0.x * w0.x + w0.z * w0.z - 1) < 1e-9;
    add("风向单位向量", unitOk && Math.abs(w90.x - 1) < 1e-9 && Math.abs(w0.z - 1) < 1e-9,
        "0°=(" + w0.x.toFixed(2) + "," + w0.z.toFixed(2) + ") 90°=(" +
        w90.x.toFixed(2) + "," + w90.z.toFixed(2) + ")");

    // 10) cpu 档预算：粒子 ≤1200 且不画沙墙（契约 §2.4）
    const cpuRain = particleBudget("cpu", "rain", 2500);
    const cpuSnow = particleBudget("cpu", "snow", 2500);
    const cpuDust = particleBudget("cpu", "dust", 2500);
    add("cpu 档粒子≤1200", Math.max(cpuRain, cpuSnow, cpuDust) <= CPU_PARTICLE_CAP,
        "rain " + cpuRain + " / snow " + cpuSnow + " / dust " + cpuDust);
    add("cpu 档不画沙墙", wallCountForTier("cpu") === 0 &&
        wallCountForTier("high") >= 2 && wallCountForTier("high") <= 3,
        "cpu=" + wallCountForTier("cpu") + " high=" + wallCountForTier("high"));

    // 11) 快照归一化：非法 preset / 缺字段 / 越界 intensity 都不得抛异常
    let norm = null;
    try {
      norm = normalizeSnapshot({ preset: "unknown", intensity: 9, visibility_km: -3 });
    } catch (e) { norm = null; }
    add("非法快照安全归一化", !!norm && norm.preset === "clear" && norm.intensity === 0 &&
        norm.visibility_km > 0,
        norm ? norm.preset + "/" + norm.intensity + "/" + norm.visibility_km.toFixed(2) : "exception");
    const n2 = normalizeSnapshot(null);
    add("空快照=clear", n2.preset === "clear" && n2.intensity === 0, n2.preset);

    // 12) 后端真实工况抽查：dust intensity=0.85、visibility≈2.3km →
    //     density 落在"近景清晰、远景留轮廓"的合理区间
    const visB = 12 - (12 - PRESETS.dust.visibility_km) * 0.85;   // 后端按 intensity 线性插值
    const densB = fogDensityFor("dust", visB);
    const coverB = fogFactorAt(densB, FOG_LEGIBLE_DIST);
    add("沙尘工况密度在合理区间", densB > 0.0015 && densB < 0.0035 && coverB > 0.5 && coverB < 0.92,
        "vis=" + visB.toFixed(2) + "km density=" + densB.toFixed(4) +
        " 520单位处遮蔽=" + (coverB * 100).toFixed(1) + "%");

    const stats = {
      version: VERSION,
      presets: Object.keys(PRESETS).length,
      mPerUnit: M_PER_UNIT,
      fogK: FOG_K,
      clearFogDensity: CLEAR_FOG_DENSITY,
      clearDensity: CLEAR_FOG_DENSITY,
      densityRain3km: Number(dRain3.toFixed(5)),
      densitySnow1_2km: Number(dSnow.toFixed(5)),
      densityDust0_6km: Number(dDust.toFixed(5)),
      visWorldPerKm: Number((1000 / M_PER_UNIT).toFixed(3)),
      cpuParticleCap: CPU_PARTICLE_CAP,
      tiers: Object.keys(BUDGET),
    };

    let ok = true;
    for (let i = 0; i < checks.length; i++) if (!checks[i].ok) ok = false;
    return { ok: ok, checks: checks, stats: stats };
  }
  // 小工具：selfTest 里拼 detail 用的标签，避免在 checks 里出现 undefined
  function presetLabel(k) { return PRESETS[k] && PRESETS[k].label ? PRESETS[k].label : "?"; }

  /* ================================================================== create */
  /**
   * 创建天气系统。
   * @param {object} opts {
   *   THREE, scene, camera, renderer,
   *   sun: THREE.DirectionalLight, hemi: THREE.HemisphereLight,
   *   skyMat: THREE.ShaderMaterial,   // 可选：主页面天空球材质（uniforms.top/mid/bot）
   *                                   // 传了 → 天气会把天空球的顶/中/底色向雾色混合，
   *                                   // 否则雨/雪/尘时"地面已经是雾色、天空还是深色渐变"，天地交界突兀
   *   tier: "high"|"medium"|"cpu",
   *   quality: { particles: 9000, range: 90, sizeScale: 1.0, instant: false },
   *   log: function(msg){}
   * }
   * @returns {null | {
   *   group, apply(snapshot), setTod(tod), update(dt, camera), setEnabled(b),
   *   current:{preset,intensity,visibility_km,...}, stats:{particles,drawCalls,ms,...}, dispose()
   * }}
   * 契约要求：初始化失败返回 null 并打一行日志，**绝不**把异常抛给主场景。
   */
  function create(opts) {
    try {
      opts = opts || {};
      const T = opts.THREE || (typeof THREE !== "undefined" ? THREE : null);
      if (!T) throw new Error("THREE 未加载（请先加载 three.min.js）");
      if (!opts.scene || !opts.camera) throw new Error("缺少 scene / camera");
      const log = typeof opts.log === "function" ? opts.log : defaultLog;

      const scene = opts.scene;
      const camera = opts.camera;
      const sun = opts.sun || null;
      const hemi = opts.hemi || null;
      const renderer = opts.renderer || null;
      /* 可选：天空球材质联动。app3d.html 的 buildSky 用 uniforms.top/mid/bot 三段渐变色，
         天气重时如果不把这三段色拉向雾色，画面会出现"地面沙尘/天空还是黄昏紫"的割裂。 */
      const skyCandidate = opts.skyMat || opts.sky || null;
      const skyU = (skyCandidate && skyCandidate.uniforms &&
                    skyCandidate.uniforms.top && skyCandidate.uniforms.mid && skyCandidate.uniforms.bot)
        ? skyCandidate.uniforms : null;

      const tier = BUDGET[opts.tier] ? opts.tier : "high";
      const quality = opts.quality || {};
      const range = clampNum(num(quality.range, BOX_DEFAULT), 20, 400);
      const sizeScale = clampNum(num(quality.sizeScale, 1), 0.5, 3);
      /* instant=true：切天气立刻生效（不做缓动）。给 ?still=1 这类"只渲染十帧就截图"
         的静态验收模式用；正常运行时保持默认缓动，切天气有过渡。 */
      const instant = opts.instant === true || quality.instant === true;
      const boxX = range, boxY = range * BOX_Y_RATIO, boxZ = range;
      const hx = boxX * 0.5, hy = boxY * 0.5, hz = boxZ * 0.5;

      /* ---- 基准值：TOD（时段）是"底色"，天气在其上做衰减叠加。
         先抓一份当时的场景状态做兜底，真正的基准由 Lead 的 setTod() 覆盖。 ---- */
      const fog0 = (scene.fog && scene.fog.isFogExp2) ? scene.fog : null;
      const base = {
        fogDensity: (fog0 && isFinite(fog0.density) && fog0.density > 0) ? fog0.density : CLEAR_FOG_DENSITY,
        fogColor: new T.Color(fog0 && fog0.color ? fog0.color.getHex() : 0x0b1a26),
        sunIntensity: sun && isFinite(sun.intensity) ? sun.intensity : 1.0,
        hemiIntensity: hemi && isFinite(hemi.intensity) ? hemi.intensity : 0.5,
        exposure: renderer && isFinite(renderer.toneMappingExposure) ? renderer.toneMappingExposure : 0.95,
      };

      /* ---- 粒子预算 ---- */
      const nRain = particleBudget(tier, "rain", quality.particles);
      const nSnow = particleBudget(tier, "snow", quality.particles);
      const nDust = particleBudget(tier, "dust", quality.particles);
      const nWalls = wallCountForTier(tier);

      /* ---- group：跟随相机；粒子坐标都在 group 局部空间里循环 ---- */
      const group = new T.Group();
      group.name = "UAVWeather";
      group.matrixAutoUpdate = true;
      scene.add(group);

      const snowTex = makeSnowTexture(T);
      const rain = buildRain(T, nRain, hx, hy, hz, sizeScale);
      const snow = buildSnow(T, nSnow, hx, hy, hz, sizeScale, snowTex);
      const dust = buildDust(T, nDust, nWalls, hx, hy, hz, sizeScale);
      group.add(rain.mesh);
      group.add(snow.mesh);
      group.add(dust.mesh);
      for (let i = 0; i < dust.walls.length; i++) group.add(dust.walls[i].mesh);

      /* ---- 可观测状态 ---- */
      const current = {
        preset: "clear", label: PRESETS.clear.label, intensity: 0,
        visibility_km: PRESETS.clear.visibility_km, link_penalty: 0,
        wind_dir_deg: PRESETS.clear.wind_dir_deg, wind_speed_mps: PRESETS.clear.wind_speed_mps,
      };
      const stats = {
        particles: 0, verts: 0, drawCalls: 0, ms: 0,
        tier: tier, preset: "clear", active: false,
        budgets: { rain: nRain, snow: nSnow, dust: nDust, walls: nWalls },
        fogDensity: base.fogDensity, fogTarget: base.fogDensity,
      };

      /* ---- 缓动状态：apply() 只设"目标值"，update() 指数缓动，
             切天气时雾/光/粒子透明度是"过渡"而不是硬跳（演示更好看，也不会闪）。 ---- */
      const cur = {
        effect: 0,            // 0=晴，1=满强度天气；所有视觉量都乘它
        rainShow: 0,          // 当前绘制粒子数（缓动）
        snowShow: 0,
        dustShow: 0,
      };
      const target = {
        effect: 0,
        fogDensity: base.fogDensity,
        fogColor: new T.Color(base.fogColor.getHex()),
        sunIntensity: base.sunIntensity,
        hemiIntensity: base.hemiIntensity,
      };
      const tmpColor = new T.Color();       // 复用的临时色（避免每帧 new）
      const tmpDir = new T.Vector3();       // 复用的相机前方向量
      const clock = { t: 0 };               // 天气内部累计时间（用于摆动/滚动；不用外部传入，避免受仿真速度影响）

      let enabled = true;
      let disposed = false;
      let lastVisKm = current.visibility_km;
      let lastWindDir = current.wind_dir_deg;
      let lastWindSpeed = current.wind_speed_mps;
      let windXNorm = 0, windZNorm = 0;     // 单位风向
      let windNorm = 0;                     // 0~1 风速归一化

      /* 记录"最后一次 apply 的快照"，切时段(setTod)时重新叠加用 */
      const snapshot = { preset: "clear", intensity: 0, visibility_km: PRESETS.clear.visibility_km,
                         wind_dir_deg: PRESETS.clear.wind_dir_deg, wind_speed_mps: PRESETS.clear.wind_speed_mps };

      /* ---------------------------------------------------- 天气视觉目标值计算 */
      function computeTargets() {
        const preset = snapshot.preset;
        const eff = preset === "clear" ? 0 : clampNum(snapshot.intensity, 0, 1);
        target.effect = eff;

        // 1) 能见度 → 雾密度（clear 恢复项目原值）
        target.fogDensity = fogDensityFor(preset, snapshot.visibility_km);

        // 2) 雾色：非晴用契约指定色；晴=当前 TOD 雾色
        if (preset === "clear") tmpColor.copy(base.fogColor);
        else tmpColor.setHex(toHexInt(PRESETS[preset].fogColor) || 0x0b1a26);
        target.fogColor.copy(tmpColor);

        // 3) 太阳/半球光衰减（把"阴天更暗"做进光照，而不是只靠雾遮）
        //    这里直接内联系数而不是调用 lightFactors()，避免 /api/state 每 120ms 一次
        //    apply 时产生临时对象（虽然不在 update 里，但保持"可预测的零分配"更好）。
        target.sunIntensity = base.sunIntensity * (1 - 0.55 * eff);
        target.hemiIntensity = base.hemiIntensity * (1 - 0.35 * eff);

        // 4) 各天气粒子目标数量（强度 0 时仍留 25% 说明"类别"，避免强度低时完全空场）
        //    非当前天气直接归零：切类别时不能残留上一种粒子。
        const scale = 0.25 + 0.75 * eff;
        cur.rainShow = preset === "rain" ? nRain * scale : 0;
        cur.snowShow = preset === "snow" ? nSnow * scale : 0;
        cur.dustShow = preset === "dust" ? nDust * scale : 0;
      }

      /* ---------------------------------------------------- 把目标值写进场景 */
      /* hard=true：立刻写（apply(clear) / setTod / setEnabled(true) 用）；
         hard=false：只记录目标，由 update() 缓动（apply(天气) 用）。 */
      function writeScene(hard) {
        const fog = (scene.fog && scene.fog.isFogExp2) ? scene.fog : null;
        if (fog) {
          const d = hard ? target.fogDensity : fog.density;
          fog.density = d;
          if (hard) fog.color.copy(target.fogColor);
          stats.fogDensity = d;
          stats.fogTarget = target.fogDensity;
        }
        if (sun) sun.intensity = hard ? target.sunIntensity : sun.intensity;
        if (hemi) hemi.intensity = hard ? target.hemiIntensity : hemi.intensity;
        if (hard) {
          cur.effect = target.effect;
          stats.fogDensity = target.fogDensity;
        }
      }

      /* ---------------------------------------------------- 速度方向表（仅 apply 时算） */
      /* 为什么不在 update 里算风：风只随快照变，apply 是低频的；
         每帧对所有粒子重算方向表 = 白烧 CPU，违背"update ≪1ms"。 */
      function rebuildWind() {
        const wv = windVector(snapshot.wind_dir_deg);
        windXNorm = wv.x; windZNorm = wv.z;
        windNorm = clampNum(snapshot.wind_speed_mps / 20, 0, 1);

        // 雨：水平漂移 ≈ 下落速度 × 倾斜系数（风越大越斜），雨丝沿合速度方向拉长
        const rainTilt = 0.10 + 0.55 * windNorm;
        for (let i = 0; i < rain.count; i++) {
          const wx = rain.speed[i] * rainTilt * windXNorm;
          const wz = rain.speed[i] * rainTilt * windZNorm;
          const vy = -rain.speed[i];
          const il = 1 / Math.sqrt(wx * wx + vy * vy + wz * wz);
          rain.windX[i] = wx; rain.windZ[i] = wz;
          rain.dirX[i] = wx * il; rain.dirY[i] = vy * il; rain.dirZ[i] = wz * il;
        }
        // 雪：横向漂移很小（雪片轻但不快），主要靠正弦摆动
        const snowDrift = 0.6 + 5.0 * windNorm;
        for (let i = 0; i < snow.count; i++) {
          snow.windX[i] = snowDrift * windXNorm;
          snow.windZ[i] = snowDrift * windZNorm;
        }
        // 沙尘：整体被强风推着走（沙尘暴的风速本来就大）
        const dustDrift = 2.0 + 16.0 * windNorm;
        for (let i = 0; i < dust.count; i++) {
          dust.windX[i] = dustDrift * windXNorm;
          dust.windZ[i] = dustDrift * windZNorm;
        }
      }

      /* ---- 天空球基准色（可选联动用）：TOD 切时段后由 setTod 重新抓取 ---- */
      const baseSky = { top: new T.Color(0x050a16), mid: new T.Color(0x0d1c30), bot: new T.Color(0x1c3341) };
      let skyTinted = false;
      function captureSkyBase() {
        if (!skyU) return;
        baseSky.top.copy(skyU.top.value);
        baseSky.mid.copy(skyU.mid.value);
        baseSky.bot.copy(skyU.bot.value);
      }
      /** 把天空球三段色按"当前天气强度 × 雾浓度"拉向雾色。
          顶/中/底混合比例不同：天顶变化小、地平线附近完全变成雾色 —— 与真实大气一致。 */
      function syncSky() {
        if (!skyU) return;
        const dens = (scene.fog && scene.fog.isFogExp2) ? scene.fog.density : 0;
        const haze = clampNum(cur.effect * (1 - Math.exp(-Math.pow(dens * 140, 2))), 0, 1);
        if (haze < 0.002) {
          if (skyTinted) {                       // 天气结束：恢复 TOD 原本的天空色
            skyU.top.value.copy(baseSky.top);
            skyU.mid.value.copy(baseSky.mid);
            skyU.bot.value.copy(baseSky.bot);
            skyTinted = false;
          }
          return;
        }
        skyTinted = true;
        skyU.top.value.copy(baseSky.top).lerp(target.fogColor, haze * 0.42);
        skyU.mid.value.copy(baseSky.mid).lerp(target.fogColor, haze * 0.74);
        skyU.bot.value.copy(baseSky.bot).lerp(target.fogColor, haze * 0.96);
      }
      captureSkyBase();

      /* ---------------------------------------------------- 粒子显隐（apply/update 共用） */
      function syncVisibility() {
        const preset = snapshot.preset;
        const act = enabled && !disposed && preset !== "clear" && cur.effect > 0.004;
        rain.mesh.visible = act && preset === "rain" && cur.rainShow >= 1;
        snow.mesh.visible = act && preset === "snow" && cur.snowShow >= 1;
        dust.mesh.visible = act && preset === "dust" && cur.dustShow >= 1;
        for (let i = 0; i < dust.walls.length; i++) {
          dust.walls[i].mesh.visible = dust.mesh.visible;
        }
        group.visible = enabled && !disposed;
        stats.active = act;
      }

      /* ---------------------------------------------------- 粒子透明度（uniform 级） */
      function syncOpacity() {
        const e = cur.effect;
        // 亮度刻意压低：旧版 0.62/0.95/0.55 叠在亮雾上会整片过曝（截图里就是一片白）
        setMatOpacity(rain.mat, 0.34 * e);
        setMatOpacity(snow.mat, 0.86 * e);
        setMatOpacity(dust.mat, 0.58 * e);
        for (let i = 0; i < dust.walls.length; i++) {
          const w = dust.walls[i];
          // 阵风：沙墙浓度轻微起伏，"沙尘在流动"的观感来自滚动 + 浓度呼吸
          const gust = 0.84 + 0.16 * Math.sin(clock.t * 0.8 + w.phase);
          setMatOpacity(w.mat, w.cfg.opacity * e * gust);
        }
      }

      /* --------------------------------------------------------- 公开：apply 快照 */
      function apply(snap) {
        if (disposed) return;
        try {
          const n = normalizeSnapshot(snap);
          snapshot.preset = n.preset;
          snapshot.intensity = n.intensity;
          snapshot.visibility_km = n.visibility_km;
          snapshot.wind_dir_deg = n.wind_dir_deg;
          snapshot.wind_speed_mps = n.wind_speed_mps;

          // 对外 current：Lead 的面板直接读它显示（天气名/能见度数字由 Lead 写 DOM）
          current.preset = n.preset;
          current.label = n.label;
          current.intensity = n.intensity;
          current.visibility_km = n.visibility_km;
          current.link_penalty = n.link_penalty;
          current.wind_dir_deg = n.wind_dir_deg;
          current.wind_speed_mps = n.wind_speed_mps;
          stats.preset = n.preset;

          // 风向/风速变化才重算速度表（/api/state 每 120ms 调一次 apply，必须便宜）。
          // 量化阈值：后端 wind_speed 会随 intensity 缓慢漂移，差 0.5m/s / 2° 以内
          // 对雨丝倾角的视觉影响 <2%，没必要每 120ms 重算 8000+ 个粒子方向。
          if (Math.abs(n.wind_dir_deg - lastWindDir) > 2 ||
              Math.abs(n.wind_speed_mps - lastWindSpeed) > 0.5) {
            rebuildWind();
            lastWindDir = n.wind_dir_deg;
            lastWindSpeed = n.wind_speed_mps;
          }
          if (n.visibility_km !== lastVisKm) lastVisKm = n.visibility_km;

          computeTargets();

          // 晴：立刻恢复原雾/原光照（"与升级前一模一样"是硬指标，不能有过渡残留）；
          // 非晴：设目标，交给 update 缓动出过渡感（instant=true 时也直接生效）。
          if (n.preset === "clear" || instant) {
            writeScene(true);
            group.visible = enabled && !disposed;
          } else {
            writeScene(false);
          }
          syncVisibility();
          // 关键：apply 之后必须立刻写一次透明度。
          // 之前只在 update() 里写，遇到 ?still=1（渲染循环被冻结）时，
          // 切了天气却看不到粒子 —— 截图验收会直接判"天气没生效"。
          syncOpacity();
          if (!enabled) group.visible = false;
          syncSky();
        } catch (e) {
          warn("apply 异常（已忽略）：" + (e && e.message ? e.message : e));
        }
      }

      /* --------------------------------------------------------- 公开：setTod 时段 */
      /**
       * Lead 切"黎明/正午/黄昏/夜景"时调用：保存基准，再按当前天气重新叠加。
       * tod = { fogColor:int, sunIntensity:number, exposure:number, hemiIntensity?:number }
       * 注意契约 §4.5 只规定了 3 个字段；hemiIntensity 缺省时从当前 hemi.intensity 读
       * （app3d.html 的 applyTOD 会先把 hemi.intensity 设成新 TOD 值再调 setTod，所以读到的是基准）。
       */
      function setTod(tod) {
        if (disposed) return;
        try {
          if (!tod || typeof tod !== "object") {
            warn("setTod 需要对象 {fogColor, sunIntensity, exposure[, hemiIntensity]}");
            return;
          }
          const hex = toHexInt(tod.fogColor);
          if (hex !== null) base.fogColor.setHex(hex);
          const si = num(tod.sunIntensity, NaN);
          if (isFinite(si)) base.sunIntensity = si;
          const hi = num(tod.hemiIntensity !== undefined ? tod.hemiIntensity : tod.hemi, NaN);
          if (isFinite(hi)) base.hemiIntensity = hi;
          else if (hemi && isFinite(hemi.intensity)) base.hemiIntensity = hemi.intensity;
          const ex = num(tod.exposure, NaN);
          if (isFinite(ex)) base.exposure = ex;

          if (!enabled) return;     // 关闭状态只更新基准，不动场景（Lead 自己管光照）

          // Lead 的 applyTOD 是"先改 skyMat 三段色、再调 setTod"，所以此刻抓到的就是新 TOD 基准
          captureSkyBase();
          computeTargets();
          // 时段切换必须"立刻"重新叠加：否则按一下"正午"，画面会有一瞬间的旧光色
          writeScene(true);
          syncVisibility();
          syncOpacity();
          syncSky();
        } catch (e) {
          warn("setTod 异常（已忽略）：" + (e && e.message ? e.message : e));
        }
      }

      /* --------------------------------------------------------- 公开：setEnabled */
      function setEnabled(on) {
        if (disposed) return;
        enabled = !!on;
        if (!enabled) {
          // 关闭天气：恢复"只有 TOD"的画面（雾色=当前时段雾色，密度=项目原值，光照=TOD 基准）
          group.visible = false;
          const fog = (scene.fog && scene.fog.isFogExp2) ? scene.fog : null;
          if (fog) { fog.density = base.fogDensity; fog.color.copy(base.fogColor); }
          if (sun) sun.intensity = base.sunIntensity;
          if (hemi) hemi.intensity = base.hemiIntensity;
          cur.effect = 0;
          stats.active = false;
          stats.particles = 0; stats.verts = 0; stats.drawCalls = 0;
          syncSky();                // 恢复 TOD 天空色
        } else {
          group.visible = true;
          computeTargets();
          writeScene(true);         // 重新打开时当前天气立刻复位
          syncVisibility();
          syncOpacity();
          syncSky();
        }
      }

      /* --------------------------------------------------------- 公开：update */
      /**
       * 每帧：缓动雾/光/透明度的过渡 + 推进粒子 + 维护 stats。
       * 契约硬要求：只改 attribute / uniform / visible，不重建 geometry，每帧零分配。
       */
      function update(dt, cam) {
        if (!enabled || disposed) return;
        const camObj = cam || camera;      // 契约签名 update(dt, camera)；缺省用 create 时的相机
        const t0 = (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
        dt = clampNum(num(dt, 0), 0, 0.1);
        clock.t += dt;

        const fog = (scene.fog && scene.fog.isFogExp2) ? scene.fog : null;
        const preset = snapshot.preset;
        const act = preset !== "clear" && target.effect > 0.004;

        // ---- 1) 天气视觉参数缓动（clear 状态下绝不接管 fog/光照，避免与 Lead 的 TOD 抢）----
        if (act || cur.effect > 0.004) {
          const k = 1 - Math.exp(-dt * 9);       // 时间常数 ≈0.11s，0.3s 内到位 95%
          cur.effect += (target.effect - cur.effect) * k;
          if (cur.effect < 0.0005) cur.effect = 0;
          if (fog) {
            fog.density += (target.fogDensity - fog.density) * k;
            fog.color.lerp(target.fogColor, k);
            stats.fogDensity = fog.density;
            stats.fogTarget = target.fogDensity;
          }
          if (sun) sun.intensity += (target.sunIntensity - sun.intensity) * k;
          if (hemi) hemi.intensity += (target.hemiIntensity - hemi.intensity) * k;
        }

        syncOpacity();
        syncVisibility();
        syncSky();            // 天空球色调跟随天气（仅当 Lead 传了 skyMat 才生效）
        stats.active = act;

        // ---- 2) group 跟随相机：中心 = 相机位置 + 前方 30 单位 ----
        group.position.copy(camObj.position);
        camObj.getWorldDirection(tmpDir);
        group.position.addScaledVector(tmpDir, BOX_FORWARD);

        // ---- 2.5) 点粒子透视缩放：gl_PointSize = aSize * uScale / 视距 ----
        // uScale = 视口高度 / (2*tan(fov/2))，即"1 世界单位在 1 单位距离处占多少像素"。
        // 每帧算一次（两次乘除），换来雪花/沙尘真正的近大远小。
        {
          const vh = (renderer && renderer.domElement && renderer.domElement.height) || 1000;
          const fov = ((camObj && camObj.fov) || 50) * Math.PI / 180;
          const uScale = vh / (2 * Math.tan(fov / 2));
          if (snow.mat.uniforms && snow.mat.uniforms.uScale) snow.mat.uniforms.uScale.value = uScale;
          if (dust.mat.uniforms && dust.mat.uniforms.uScale) dust.mat.uniforms.uScale.value = uScale;
        }

        // ---- 3) 粒子推进（只写 Float32Array；越界环绕，不生成/销毁）----
        let particles = 0, verts = 0, drawCalls = 0;

        if (rain.mesh.visible) {
          const arr = rain.arr, n = Math.min(rain.count, Math.max(0, Math.round(cur.rainShow)));
          for (let i = 0; i < n; i++) {
            const o = i * 6;
            let x = arr[o] + rain.windX[i] * dt;
            let y = arr[o + 1] - rain.speed[i] * dt;
            let z = arr[o + 2] + rain.windZ[i] * dt;
            if (y < -hy) { y += boxY; } else if (y > hy) { y -= boxY; }
            if (x > hx) { x -= boxX; } else if (x < -hx) { x += boxX; }
            if (z > hz) { z -= boxZ; } else if (z < -hz) { z += boxZ; }
            const L = rain.len[i];
            arr[o] = x; arr[o + 1] = y; arr[o + 2] = z;
            arr[o + 3] = x + rain.dirX[i] * L;
            arr[o + 4] = y + rain.dirY[i] * L;
            arr[o + 5] = z + rain.dirZ[i] * L;
          }
          rain.geo.attributes.position.needsUpdate = true;
          rain.geo.setDrawRange(0, n * 2);
          particles += n; verts += n * 2; drawCalls++;
        }

        if (snow.mesh.visible) {
          const arr = snow.arr, n = Math.min(snow.count, Math.max(0, Math.round(cur.snowShow)));
          const tt = clock.t;
          for (let i = 0; i < n; i++) {
            const o = i * 3;
            let y = arr[o + 1] - snow.speed[i] * dt;
            if (y < -hy) { y += boxY; }
            let bx = snow.baseX[i] + snow.windX[i] * dt;
            let bz = snow.baseZ[i] + snow.windZ[i] * dt;
            if (bx > hx) { bx -= boxX; } else if (bx < -hx) { bx += boxX; }
            if (bz > hz) { bz -= boxZ; } else if (bz < -hz) { bz += boxZ; }
            snow.baseX[i] = bx; snow.baseZ[i] = bz;
            // 正弦横向摆动：雪花受局部气流影响左右飘（也是"雪比雨慢"的视觉特征）
            const sw = Math.sin(tt * snow.freq[i] + snow.phase[i]) * snow.amp[i];
            const sw2 = Math.cos(tt * snow.freq[i] * 0.83 + snow.phase[i] + 1.7) * snow.amp[i] * 0.7;
            arr[o] = bx + sw; arr[o + 1] = y; arr[o + 2] = bz + sw2;
          }
          snow.geo.attributes.position.needsUpdate = true;
          snow.geo.setDrawRange(0, n);
          particles += n; verts += n; drawCalls++;
        }

        if (dust.mesh.visible) {
          const arr = dust.arr, n = Math.min(dust.count, Math.max(0, Math.round(cur.dustShow)));
          const tt = clock.t;
          for (let i = 0; i < n; i++) {
            const o = i * 3;
            // 水平：整体被风推 + 每颗粒子的涡流扰动（不同相位 → 像"翻滚的沙")
            let x = arr[o] + (dust.windX[i] + Math.sin(tt * dust.freq[i] + dust.phase[i]) * dust.turb[i]) * dt;
            let z = arr[o + 2] + (dust.windZ[i] + Math.cos(tt * dust.freq[i] * 0.91 + dust.phase[i]) * dust.turb[i]) * dt;
            // 竖直：缓慢上下翻卷（沙尘不是"下落"，是"悬浮+翻滚"）
            let y = arr[o + 1] + Math.sin(tt * 1.6 + dust.phase[i]) * 1.5 * dt;
            if (x > hx) { x -= boxX; } else if (x < -hx) { x += boxX; }
            if (z > hz) { z -= boxZ; } else if (z < -hz) { z += boxZ; }
            if (y > hy) { y -= boxY; } else if (y < -hy) { y += boxY; }
            arr[o] = x; arr[o + 1] = y; arr[o + 2] = z;
          }
          dust.geo.attributes.position.needsUpdate = true;
          dust.geo.setDrawRange(0, n);
          particles += n; verts += n; drawCalls++;

          // ---- 沙墙：billboard 朝相机 + 贴图横向滚动（视差：近层快、远层慢）----
          for (let i = 0; i < dust.walls.length; i++) {
            const w = dust.walls[i];
            if (!w.mesh.visible) continue;
            w.mesh.quaternion.copy(camObj.quaternion);
            // 局部坐标 = 世界轴向（group 不旋转），所以"相机前方 dist 单位"就是 forward*(dist-30)
            w.mesh.position.copy(tmpDir).multiplyScalar(w.cfg.dist - BOX_FORWARD);
            w.mesh.position.y += w.cfg.yOff;
            // 滚动速度 ∝ 风速（风大沙尘跑得快），x 方向随单位风向东西摆动
            const spd = w.cfg.scroll * (0.6 + 2.4 * windNorm);
            w.tex.offset.x -= spd * dt * (windXNorm >= 0 ? 1 : -1);
            w.tex.offset.y -= spd * 0.22 * dt;
            if (w.tex.offset.x > 1) w.tex.offset.x -= 1;
            else if (w.tex.offset.x < -1) w.tex.offset.x += 1;
            drawCalls++;
          }
        }

        if (!act) { particles = 0; verts = 0; drawCalls = 0; }

        stats.particles = particles;
        stats.verts = verts;
        stats.drawCalls = drawCalls;

        const t1 = (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
        const el = t1 - t0;
        stats.ms = stats.ms > 0 ? stats.ms * 0.9 + el * 0.1 : el;
      }

      /* --------------------------------------------------------- 公开：dispose */
      function dispose() {
        if (disposed) return;
        disposed = true;
        try {
          // 先把天空球恢复成 TOD 原色，避免 dispose 后页面还残留雾色天空
          if (skyU && skyTinted) {
            skyU.top.value.copy(baseSky.top);
            skyU.mid.value.copy(baseSky.mid);
            skyU.bot.value.copy(baseSky.bot);
            skyTinted = false;
          }
          scene.remove(group);
          const kill = function (m) {
            if (!m) return;
            if (m.geometry && m.geometry.dispose) m.geometry.dispose();
            if (m.material && m.material.dispose) m.material.dispose();
          };
          kill(rain.mesh); kill(snow.mesh); kill(dust.mesh);
          if (snow.mat.map && snow.mat.map.dispose) snow.mat.map.dispose();
          if (dust.mat.map && dust.mat.map.dispose) dust.mat.map.dispose();
          for (let i = 0; i < dust.walls.length; i++) {
            const w = dust.walls[i];
            if (w.mat && w.mat.dispose) w.mat.dispose();
            if (w.tex && w.tex.dispose) w.tex.dispose();
          }
          if (dust.wallGeo && dust.wallGeo.dispose) dust.wallGeo.dispose();
        } catch (e) { /* dispose 阶段忽略异常 */ }
      }

      /* ---- 初始化：先按 clear 立刻恢复基准（不改动画面），后续由 apply 驱动 ---- */
      cur.effect = 0;
      computeTargets();
      writeScene(true);
      syncVisibility();
      rebuildWind();
      syncOpacity();
      log("ready v" + VERSION + " tier=" + tier +
          " 粒子预算 rain=" + nRain + "/snow=" + nSnow + "/dust=" + nDust +
          " 沙墙=" + nWalls + " 能见度映射 " + M_PER_UNIT + "m/单位");

      return {
        group: group,
        apply: apply,
        setTod: setTod,
        update: update,
        setEnabled: setEnabled,
        current: current,
        stats: stats,
        dispose: dispose,
        // 调试辅助（Lead 的 ?debug=1 面板可用；不属于冻结契约）
        box: { x: boxX, y: boxY, z: boxZ },
        fogK: FOG_K,
        instant: instant,
        debug: function () {
          const fog = (scene.fog && scene.fog.isFogExp2) ? scene.fog : null;
          return {
            preset: snapshot.preset, intensity: snapshot.intensity,
            visibility_km: snapshot.visibility_km,
            fogDensity: fog ? Number(fog.density.toFixed(6)) : null,
            fogColor: fog ? ("#" + fog.color.getHexString()) : null,
            fogTarget: Number(target.fogDensity.toFixed(6)),
            fogTargetColor: "#" + target.fogColor.getHexString(),
            baseFogColor: "#" + base.fogColor.getHexString(),
            sun: sun ? Number(sun.intensity.toFixed(3)) : null,
            hemi: hemi ? Number(hemi.intensity.toFixed(3)) : null,
            sunTarget: Number(target.sunIntensity.toFixed(3)),
            hemiTarget: Number(target.hemiIntensity.toFixed(3)),
            curEffect: Number(cur.effect.toFixed(3)),
            sky: skyU ? { top: "#" + skyU.top.value.getHexString(),
                          mid: "#" + skyU.mid.value.getHexString(),
                          bot: "#" + skyU.bot.value.getHexString(), tinted: skyTinted } : null,
            counts: { rain: Math.round(cur.rainShow), snow: Math.round(cur.snowShow),
                      dust: Math.round(cur.dustShow), walls: dust.walls.length },
          };
        },
      };
    } catch (err) {
      warn("create 失败：" + (err && err.message ? err.message : err));
      return null;
    }
  }

  /* ================================================================ 全局导出 */
  /* 一个文件只暴露一个全局（契约 §2.2 / §3.1）。 */
  window.UAVWeather = {
    version: VERSION,
    PRESETS: PRESETS,
    create: create,
    selfTest: selfTest,
    // 纯函数导出：方便 Lead/测试直接算数（不算新增全局，只是挂在同一个对象上）
    fogDensityFor: fogDensityFor,
    visibilityWorld: visibilityWorld,
    fogFactorAt: fogFactorAt,
    particleBudget: particleBudget,
  };
})();
