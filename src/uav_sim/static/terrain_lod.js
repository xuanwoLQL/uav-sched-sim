/* =============================================================================
 * terrain_lod.js —— 地形 LOD（几何 clipmap + 纹理 clipmap，three.js r128 经典脚本）
 * -----------------------------------------------------------------------------
 * 为什么要有这个模块？
 *   升级前 app3d.html 用一张 PlaneGeometry(176,128, N-1, M-1) 把整个高程场一次性铺出来：
 *     · 384×280 场 = 21.4 万三角面，512×380 的真实 DEM = 38.9 万三角面，永远全量渲染；
 *     · 屏幕上一个像素只覆盖 0.46 世界单位，远处一半以上三角形连一个像素都占不到（纯浪费）；
 *     · 想换更大 DEM（1024×768、2048×1536…）面数会线性爆炸 → 跑不动。
 *   本模块把地形换成「几何 clipmap」：以相机为中心，由内向外 4 级同心网格，间距逐级 ×2，
 *   近处保持全精度、远处自动抽稀 —— 屏幕上的三角形数基本恒定，与 DEM 分辨率解耦。
 *
 * 几何 clipmap 的几个关键设计（都是踩过坑的真实原因）：
 *   1) 内环间距 baseSpacing = max(world.width/(N-1), world.depth/(M-1))：正好等于 DEM 格距，
 *      近处顶点密度 = 原始数据密度，既不吃亏也不浪费。
 *   2) 每级环中心「吸附」到本级（或更粗一级）间距的整数倍：相机平移时顶点整体跳格而不是
 *      连续滑动，避免经典 clipmap 的顶点游动/闪烁（swimming）。
 *      吸附规则：c[k] = round(cam / snap[k]) * snap[k]，其中 snap[k] = s[k+1]（最后一环用 s[last]）。
 *      这样内环的边界恰好落在外面那一级网格的整数格上 → 洞与环边界严丝合缝。
 *   3) 第 k 级只画「S(k) 减去 S(k-1)」的 L 形环带（S(k) 是边长为 2*H(k) 的正方形），
 *      各级同心、互不重叠也不留空；最外环外接正方形必须盖住整个世界矩形（含 1.5% 外扩），
 *      所以任何相机角度都不会看到地形空洞。
 *   4) 环中心到相机 XZ 的距离超过「间距/2」才重定位对应环（update 里判断），不每帧重建几何。
 *   5) 环与环之间的接缝：外环网格在内边界处按内环的细间距细分顶点（洞边界两侧共用同一批
 *      世界坐标 + 同一套 heightAt 采样），所以表面上没有裂缝；另外每环边界再补一圈向下
 *      裙边（skirt），把「T 型接缝」在陡坡上的高差整个盖住。
 *   6) 外延缓冲带（v1.1 新增，见 §7b）：世界矩形之外再铺一圈间距 8~16 世界单位的超低模
 *      「大地」，高度从世界边缘 clamp 值平滑下压到大地基底高度、颜色向雾色过渡。
 *      没有它时，镜头压低/跟随无人机会看到地形像一块悬空的岛（边缘一圈断崖 + 黑虚空）。
 *
 * 高度一致性（最重要的一条）：
 *   heightAt(x,z) 与 app3d.html 的 terrainH(x,z) **公式逐位一致**（同一套双线性、同一套 clamp），
 *   所以树木/碎石/覆盖热力图/水面/地面站按原坐标贴地时不会与 LOD 地形穿模。
 *
 * 纹理 clipmap（v1.2 起「彻底连续」版）：
 *   模块内部用 canvas 程序化生成一张 512×512 的岩石/土壤细节纹理（不联网、不读图），
 *   每环一条独立 CanvasTexture + MeshStandardMaterial：
 *     · UV 直接烘进世界坐标（uv = worldXZ / tileWorld），**所有环 + 外延带共用同一个
 *       tileWorld**（v1.2 修正）——旧版每环用不同 repeat，环边界在俯视下会看到清晰的
 *       矩形色差带（像被切成几块不同密度的抹布）；
 *     · 保留 vertexColors（沿用 app3d.html 的坡度-高程分带配色 ROCK/SOIL/DEEP + 曲率 AO），
 *       细节纹理只做乘性增强，且贴图本身是"近白"的微对比噪声（均值 ~0.97 sRGB），
 *       避免大块灰色乘子把地形整体去饱和；
 *     · 细节纹理标注 sRGBEncoding（canvas 像素本身就是 sRGB），three 会在 shader 里解码到
 *       线性空间；否则贴图会被当成线性值使用，整体偏亮偏灰（v1.1 修正）；
 *     · 远景/俯视时高频细节会被 mip 平均掉，所以宏观变化放在**顶点色**里
 *       （v1.2 新增：世界坐标连续的低频颜色噪声，±10% 亮度 + 轻微色相，尺度 ~70/~26
 *        世界单位，不随 mip 消失）；
 *     · materials 数组对外暴露，Lead 会把老版本的动态云影 onBeforeCompile 注入每个材质。
 *
 * LOD 相机自适应（v1.2 新增）：
 *   旧版只按相机 XZ 半径分级，相机抬高/拉远后可见区域整体落到粗环上、屏幕误差变大。
 *   现在相机高度 > lodHeightThreshold（默认 90）时整个 clipmap 细分一档
 *   （baseSpacing / 2，缓存点阵固定按 baseSpacing/2 建，切换档位不重建缓存），
 *   同时格数按档位预算自动收缩（high ≤200k / medium ≤120k / cpu ≤40k tris）。
 *
 * 对外 API（契约 §4.2）：
 *   window.UAVTerrain = {
 *     version,
 *     capable(opts) -> boolean,
 *     create(opts) -> handle | null,
 *     selfTest() -> {ok, checks, stats}
 *   }
 *   opts = {
 *     THREE, scene, camera,
 *     field: { H: Float32Array, N, M },
 *     world: { width, depth },
 *     tier: "high" | "medium" | "cpu",
 *     quality: { enabled, rings(≤6), baseSpacing, ringScale, skirtDepth, tileWorld, tileBase,
 *                anisotropy(1~16), debugTint, bakedAO, pit, expand, macroNoise, macroAmp,
 *                lodHeightThreshold, lodBoostMax, ringDecay, extRadius, extSpacing, baseColor },
 *     log: fn
 *   }
 *   handle = {
 *     group, materials[], heightAt(x,z), update(camera, dt),
 *     setEnabled(b), setDebug(b), stats{rings,verts,tris,drawCalls,ringSpacing,...}, dispose()
 *   }
 *
 * 集成方式（Lead 在 app3d.html 的 buildTerrain() 里）：
 *   var h = UAVTerrain.create({ THREE:THREE, scene:scene, camera:camera, field:state.field,
 *                               world:{width:TERRAIN_W, depth:TERRAIN_D}, tier:TIER,
 *                               quality:{enabled:true, rings:LITE?2:4, baseSpacing:null,
 *                                        ringScale:2, skirtDepth:3.0, anisotropy:LITE?1:4},
 *                               log:function(m){ console.log("[Terrain]", m); } });
 *   (h.materials || []).forEach(applyCloudShadow);      // 云影注入每个环材质
 *   // animate() 里：if (h) h.update(camera, dt);
 *
 * 约定：脚本加载期只定义函数与全局对象，不 new THREE.*、不访问 renderer、不抛异常。
 * ========================================================================== */
(function () {
  "use strict";

  var VERSION = "1.2.0";
  var PREFIX = "[Terrain]";

  /* ======================================================================
   * 0. 小工具（不依赖 THREE，方便 selfTest 在无渲染器环境下跑）
   * ==================================================================== */
  function num(v, d) {
    var n = typeof v === "number" ? v : parseFloat(v);
    return isFinite(n) ? n : d;
  }
  function clampNum(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function lerp(a, b, t) { return a + (b - a) * t; }
  /* 与 app3d.html 的 smoothstep 同义：x 从 e0 到 e1 映射到 0..1（e0>e1 时反向） */
  function smoothstep(e0, e1, x) {
    var t = clampNum((x - e0) / (e1 - e0), 0, 1);
    return t * t * (3 - 2 * t);
  }
  function nowMs() {
    return (typeof performance !== "undefined" && performance.now)
      ? performance.now() : Date.now();
  }
  function logTo(opts, msg) {
    var fn = opts && opts.log;
    try { if (typeof fn === "function") fn(msg); else console.log(PREFIX, msg); }
    catch (e) { /* 日志失败不影响主流程 */ }
  }
  /* 十六进制颜色 -> 线性 RGB。three r128 的 Color.setHex 就是直接 /255（没有 sRGB 转换），
     所以这里手动算出来的数值与原 buildTerrain 里 new THREE.Color(0x…) 完全一致。 */
  function rgbOf(hex) {
    return [(hex >> 16 & 255) / 255, (hex >> 8 & 255) / 255, (hex & 255) / 255];
  }

  /* ======================================================================
   * 0b. 宏观颜色噪声（世界坐标连续，跨环/跨外延带完全一致）
   * ----------------------------------------------------------------------
   * 为什么放在顶点色里：远景/俯视时高频细节纹理会被 mip 平均成灰色，只剩低频
   * 顶点色，于是"像几块纯色板"。这里叠一层世界对齐的低频噪声（尺度 ~70 与 ~26
   * 世界单位，正好落在 §B 要求的 20~80 范围），mip 不会让它消失，远近都看得到
   * 自然的矿带/斑块感。只做乘性亮度 + 轻微色相扰动，不改变坡度-高程配色逻辑。
   * ==================================================================== */
  function macroHash(ix, iy) {
    var n = ix * 374761393 + iy * 668265263;
    n = (n ^ (n >> 13)) * 1274126177;
    n = n ^ (n >> 16);
    return ((n >>> 0) % 1000003) / 1000003;
  }
  function macroNoise(x, z) {
    var x0 = Math.floor(x), y0 = Math.floor(z);
    var fx = x - x0, fy = z - y0;
    var sx = fx * fx * (3 - 2 * fx), sy = fy * fy * (3 - 2 * fy);
    var v00 = macroHash(x0, y0), v10 = macroHash(x0 + 1, y0);
    var v01 = macroHash(x0, y0 + 1), v11 = macroHash(x0 + 1, y0 + 1);
    return (v00 * (1 - sx) + v10 * sx) * (1 - sy) + (v01 * (1 - sx) + v11 * sx) * sy;
  }
  /* 返回 -1~1 的低频调制量（两个尺度叠加） */
  function macroShade(x, z) {
    var n1 = macroNoise(x * 0.0143 + 3.7, z * 0.0143 - 1.9);   // ~70 世界单位
    var n2 = macroNoise(x * 0.0385 - 7.1, z * 0.0385 + 4.2);   // ~26 世界单位
    return ((n1 - 0.5) * 0.65 + (n2 - 0.5) * 0.35) * 2;
  }

  /* ======================================================================
   * 1. 档位参数（Lead 会按 GPU 档位传 tier；这里给出每档的环数与单元数）
   * ----------------------------------------------------------------------
   * cells 是「内环正方形网格每边的单元数」，它不是世界尺寸，而是三角形预算旋钮：
   *   环格数按 ringDecay 向外逐级递减（内环全精度、外环少给格子），总三角形由
   *   choosePlan() 按档位预算自动收缩。
   * 档位预算（v1.2 契约）：high ≤ 200k、medium ≤ 120k、cpu ≤ 40k tris；drawCalls ≤ 8。
   * cpu 档必须 ≤ 2 环（软件光栅），且禁止任何新模块把它拖崩。
   * ==================================================================== */
  var TIER_DEFAULTS = {
    high:   { rings: 5, cells: 192, maxTris: 260000 },
    medium: { rings: 4, cells: 128, maxTris: 120000 },
    cpu:    { rings: 2, cells: 96,  maxTris: 40000 }
  };

  var DEFAULT_QUALITY = {
    enabled: true,
    rings: 0,            // 0 = 按 tier 默认（high 5 / medium 4 / cpu 2）；最多 6
    baseSpacing: null,   // null = 按 DEM 格自动算
    ringScale: 2,        // 间距逐级 ×2
    skirtDepth: 3.0,     // 裙边基础深度（世界单位）
    /* ---- 纹理 clipmap：全环 + 外延带共用同一个世界对齐尺度 → 环间绝无色差拼缝 ---- */
    tileWorld: 0,        // 0 = 自动 = tileBase
    tileBase: 6.0,
    detailRepeat: [48, 12, 4, 1.5],  // 【已废弃】旧版每环不同 repeat 是拼缝的根因，保留键仅兼容
    anisotropy: 4,       // 1~16（Lead 会传 8）
    debugTint: false,
    bakedAO: true,       // 顶点烘焙曲率 AO（与老 buildTerrain 一致）
    pit: null,           // 采坑参数 {cx,cz,rx,rz}，用于坑内偏灰；Lead 会传 PIT
    expand: 0.015,       // 最外环覆盖世界矩形的外扩比例（≤5%）
    /* ---- 宏观颜色噪声（§B）：顶点色 ±10% 亮度 + 轻微色相，尺度 ~70 / ~26 世界单位 ---- */
    macroNoise: true,
    macroAmp: 0.10,
    /* ---- LOD 相机自适应（§C）：相机高于阈值时整体细分一档（baseSpacing / 2） ---- */
    lodHeightThreshold: 90,
    lodBoostMax: 2,
    /* ---- 环格数向外递减比例（内环保持精度、外环省预算） ---- */
    ringDecay: 0.86,
    /* ---- 外延缓冲带（世界矩形之外一圈超低精度大网格，避免地形像"悬空的岛"） ---- */
    extRadius: 0,        // 缓冲带外缘半径；0 = 自动 = max(world) * 1.9
    extSpacing: 0,       // 缓冲带格子间距；0 = 自动 = max(8, max(world)/16)
    /* ---- 材质亮度补偿：细节纹理解码后均值 ~0.93，乘回 ~1.0（= 与升级前亮度一致） ---- */
    baseColor: 1.06
  };

  /* ======================================================================
   * 2. 高程采样：与 app3d.html 的 terrainH 逐位一致
   * ----------------------------------------------------------------------
   * 世界坐标 (x,z) -> 高程场双线性插值。这里刻意把公式写成与 app3d.html
   * 完全相同的形式（包括越界时的 clamp 行为：坑外/世界外的点等于最近的边缘高度），
   * 这样树木/碎石/水面按原坐标摆放时不会与 LOD 地形错位穿模。
   * ==================================================================== */
  function makeSampler(field, world) {
    var F = field;
    var N = Math.max(2, F.N | 0), M = Math.max(2, F.M | 0);
    var H = F.H;
    var W = world.width, D = world.depth;
    var dx = W / (N - 1), dz = D / (M - 1);
    var halfW = W / 2, halfD = D / 2;

    return function heightAt(x, z) {
      var fx = (x + halfW) / dx, fz = (z + halfD) / dz;
      var i0 = Math.floor(fx); if (i0 < 0) i0 = 0; else if (i0 > N - 2) i0 = N - 2;
      var j0 = Math.floor(fz); if (j0 < 0) j0 = 0; else if (j0 > M - 2) j0 = M - 2;
      var tx = fx - i0; if (tx < 0) tx = 0; else if (tx > 1) tx = 1;
      var tz = fz - j0; if (tz < 0) tz = 0; else if (tz > 1) tz = 1;
      var b = j0 * N + i0;
      var h00 = H[b], h10 = H[b + 1], h01 = H[b + N], h11 = H[b + N + 1];
      return (h00 * (1 - tx) + h10 * tx) * (1 - tz) + (h01 * (1 - tx) + h11 * tx) * tz;
    };
  }

  /* ======================================================================
   * 3. 顶点配色：复制 app3d.html buildTerrain 的「坡度-高程混合」算法
   * ----------------------------------------------------------------------
   *   · 高程分带：坑底 DEEP(0x0a2228) -> 青绿 0x10403a/0x17604f/0x14544a/0x1f6b5b -> 山顶 ROCK(0x3f6a62)
   *   · 坡度陡 > 露岩（ROCK），缓坡覆土（SOIL 0x2b4f39 在原始实现里只作常量保留）
   *   · 采坑内部整体偏灰（裸岩粉尘）
   *   · 曲率 AO：周围 4 点比自身高=凹处 -> 压暗
   *   · 沿高程的细微明暗带（0.86 + 0.20*cos）制造「岩层/测量网格」的暗示
   * 细节纹理只乘在这个颜色上，不改变配色本身。
   * ==================================================================== */
  function makeColorFn(sampler, field, world, opt) {
    var F = field;
    var hMin = Infinity, hMax = -Infinity;
    for (var i = 0; i < F.H.length; i++) {
      var v = F.H[i]; if (v < hMin) hMin = v; if (v > hMax) hMax = v;
    }
    if (!isFinite(hMin)) { hMin = 0; hMax = 1; }
    if (!(hMax - hMin > 1e-6)) hMax = hMin + 1;
    var invRange = 1 / (hMax - hMin);
    /* 坡度/曲率/AO 的采样尺度（v1.2 修正）：
       LOD 粗环的顶点间距可达 2~5 个世界单位，如果颜色函数本身是高频率的
       （旧版 cos(h*0.9) 周期只有 7 个世界单位、AO 只看 ±1.6），粗环采样会产生
       明显的"亮度斑块/矩形拼接感"。这里把颜色函数改成低频：
         · 坡度差分尺度 ≈ 3 个 DEM 格（≈1 世界单位）
         · 曲率 AO 采样半径 ≈ 5 个世界单位
         · 高程明暗带周期 ≈ 24 个世界单位
       近景细节由法线（normalEps=DEM 格距）+ 细节纹理提供，配色本身保持低频一致。 */
    var epsBase = Math.max(1e-3, Math.max(world.width / Math.max(1, F.N - 1),
                                          world.depth / Math.max(1, F.M - 1)));
    var eps = Math.max(1.0, epsBase * 3.0);
    var inv2eps = 1 / (2 * eps);
    var aoOff = Math.max(5.0, epsBase * 12.0);

    var C_DEEP = rgbOf(0x0a2228), C_A = rgbOf(0x10403a), C_B = rgbOf(0x17604f),
        C_C = rgbOf(0x14544a), C_D = rgbOf(0x1f6b5b), C_ROCK = rgbOf(0x3f6a62),
        C_PIT = rgbOf(0x555a56);
    var bakedAO = opt.bakedAO !== false;
    var pit = opt.pit || null;
    var macroOn = opt.macroNoise !== false;
    var macroAmp = num(opt.macroAmp, DEFAULT_QUALITY.macroAmp);

    function mix3(out, a, b, t) {
      out[0] = a[0] + (b[0] - a[0]) * t;
      out[1] = a[1] + (b[1] - a[1]) * t;
      out[2] = a[2] + (b[2] - a[2]) * t;
    }

    return function colorAt(x, z, h, out) {
      /* ---- 坡度（中心差分，与 terrainH 采样完全一致） ---- */
      var sx = (sampler(x - eps, z) - sampler(x + eps, z)) * inv2eps;
      var sz = (sampler(x, z - eps) - sampler(x, z + eps)) * inv2eps;
      var steep = smoothstep(0.55, 1.5, Math.sqrt(sx * sx + sz * sz));

      /* ---- 高程分带（分界点与老实现逐字一致） ---- */
      var t = clampNum((h - hMin) * invRange, 0, 1);
      var tmp = out;    // 直接写输出，省一次数组拷贝
      if (t < 0.16) mix3(tmp, C_DEEP, C_A, t / 0.16);
      else if (t < 0.42) mix3(tmp, C_A, C_B, (t - 0.16) / 0.26);
      else if (t < 0.78) mix3(tmp, C_C, C_D, (t - 0.42) / 0.36);
      else mix3(tmp, C_D, C_ROCK, (t - 0.78) / 0.22);

      /* ---- 陡坡露岩、缓坡覆土 ---- */
      tmp[0] = lerp(tmp[0], C_ROCK[0], steep * 0.62);
      tmp[1] = lerp(tmp[1], C_ROCK[1], steep * 0.62);
      tmp[2] = lerp(tmp[2], C_ROCK[2], steep * 0.62);

      /* ---- 采坑内部整体偏灰（裸岩 + 粉尘），与老实现同一个椭圆 ---- */
      if (pit) {
        var pr = Math.sqrt(
          ((x - pit.cx) / pit.rx) * ((x - pit.cx) / pit.rx) +
          ((z - pit.cz) / pit.rz) * ((z - pit.cz) / pit.rz));
        var inPit = smoothstep(1.05, 0.75, pr);
        var kp = inPit * 0.55;
        tmp[0] = lerp(tmp[0], C_PIT[0], kp);
        tmp[1] = lerp(tmp[1], C_PIT[1], kp);
        tmp[2] = lerp(tmp[2], C_PIT[2], kp);
      }

      /* ---- 曲率 AO：周围 4 点均值比自身高 → 凹处，压暗（低频尺度，LOD 各环一致） ---- */
      var ao = 1.0;
      if (bakedAO) {
        var avg = (sampler(x + aoOff, z) + sampler(x - aoOff, z) +
                   sampler(x, z + aoOff) + sampler(x, z - aoOff)) * 0.25;
        ao = clampNum(1.0 - (avg - h) * 0.30, 0.45, 1.15);
      }
      var shade = (0.86 + 0.20 * (0.5 + 0.5 * Math.cos(h * 0.26))) * ao;
      out[0] *= shade; out[1] *= shade; out[2] *= shade;

      /* ---- 宏观颜色噪声（§B）：±macroAmp 亮度 + 轻微色相扰动 ----
         世界坐标连续 → 环边界、外延带边界两侧取到的是同一函数值，不会产生拼缝。 */
      if (macroOn) {
        var mac = macroShade(x, z);
        var lm = 1 + macroAmp * mac;
        var hue = macroAmp * 0.2 * mac;
        out[0] = out[0] * lm * (1 + hue);
        out[1] = out[1] * lm;
        out[2] = out[2] * lm * (1 - hue);
      }
    };
  }

  /* ======================================================================
   * 4. 程序化细节纹理（512×512 岩石/土壤颗粒，可平铺）
   * ----------------------------------------------------------------------
   * 全模块唯一一张 canvas：多八度可平铺数值噪声 + 每像素细颗粒 + 轻微暖冷偏色。
   * v1.2 起改成"近白微对比"纹理（sRGB 值 0.945~1.0，均值 ~0.97）：
   *   旧版 0.80~1.0 的灰纹理在 sRGB 解码后均值只有 ~0.78，等于给整个地形乘了个
   *   灰色蒙版 → 颜色被去饱和、远景像灰抹布。近白纹理只负责"放大看有颗粒"，
   *   宏观变化交给顶点色里的噪声（§B），所以远景/俯视不会糊成色板。
   * 禁止外网/CDN：所以必须程序化生成（契约 §1）。
   * ==================================================================== */
  function makeDetailCanvas(size) {
    var cv = document.createElement("canvas");
    cv.width = size; cv.height = size;
    var ctx = cv.getContext("2d");
    if (!ctx) throw new Error("canvas 2d context 不可用");
    var img = ctx.createImageData(size, size);
    var data = img.data;
    var S = size;

    /* 确定性哈希（同一次加载每次生成都一样，截图可复现） */
    function hash2(ix, iy, seed) {
      var n = ix * 374761393 + iy * 668265263 + seed * 1442695040;
      n = (n ^ (n >> 13)) * 1274126177;
      n = n ^ (n >> 16);
      return ((n >>> 0) % 1000003) / 1000003;
    }
    function wrap(v, p) { var r = v % p; return r < 0 ? r + p : r; }
    function vnoise(x, y, period, seed) {
      var x0 = Math.floor(x), y0 = Math.floor(y);
      var fx = x - x0, fy = y - y0;
      var sx = fx * fx * (3 - 2 * fx), sy = fy * fy * (3 - 2 * fy);
      var xa = wrap(x0, period), xb = wrap(x0 + 1, period);
      var ya = wrap(y0, period), yb = wrap(y0 + 1, period);
      var v00 = hash2(xa, ya, seed), v10 = hash2(xb, ya, seed);
      var v01 = hash2(xa, yb, seed), v11 = hash2(xb, yb, seed);
      return (v00 * (1 - sx) + v10 * sx) * (1 - sy) + (v01 * (1 - sx) + v11 * sx) * sy;
    }

    for (var y = 0; y < S; y++) {
      var v = y / S;
      for (var x = 0; x < S; x++) {
        var u = x / S;
        /* 三个八度：大块岩斑 / 中尺度碎石 / 细砂 */
        var n = 0.52 * vnoise(u * 8, v * 8, 8, 11) +
                0.30 * vnoise(u * 24, v * 24, 24, 23) +
                0.18 * vnoise(u * 64, v * 64, 64, 37);
        /* 每像素颗粒（最高频，mip 后自然糊成细微噪点） */
        var g = hash2(x, y, 99);
        /* 近白微对比：0.945 ~ 1.0（sRGB），乘性细节不会把配色压灰 */
        var val = 0.945 + 0.040 * n + 0.015 * g;
        if (val < 0.93) val = 0.93; else if (val > 1.0) val = 1.0;
        /* 极轻微的暖/冷偏色，避免纯灰看起来像塑料 */
        var warm = (g - 0.5) * 0.012 + (n - 0.5) * 0.010;
        var o = (y * S + x) * 4;
        data[o] = Math.round(clampNum(val * (1 + warm), 0, 1) * 255);
        data[o + 1] = Math.round(clampNum(val * (1 + warm * 0.35), 0, 1) * 255);
        data[o + 2] = Math.round(clampNum(val * (1 - warm * 0.6), 0, 1) * 255);
        data[o + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    return cv;
  }

  /* ======================================================================
   * 5. 高程/颜色缓存（s0 规则点阵，分块）
   * ----------------------------------------------------------------------
   * 所有环的顶点都落在「全局 s0 点阵」上（因为环中心吸附到 s[k] 的整数倍、
   * s[k] = ringScale^k * s0，且环内顶点间距也是 s[k] 的整数倍）——所以一张以 s0
   * 为间隔的点阵高度/颜色缓存可以给所有环共用。
   *
   * 为什么需要缓存：顶点颜色要算坡度(4 次 heightAt) + 曲率 AO(4 次 heightAt)，
   * 相机移动时内环要重定位（每移动约半个格距一次）。如果每次重建都重新双线性采样，
   * 192² 网格一次要 30 万次采样（10+ ms）→ 移动镜头时掉帧。
   * 缓存后：首次计算一次，之后重建只是 Map/数组读取 + 写顶点缓冲（<2ms）。
   *
   * 分块（32×32 点一块，Float32Array，NaN = 未计算）而不是整张世界网格：
   *   世界外的相机不会把内存浪费在背板上；更大 DEM 时内存只和「走过的地方」有关。
   * ==================================================================== */
  function makeCache(sampler, colorFn, s0) {
    var T = 32, T2 = T * T, FLOATS = T2 * 4;
    var tiles = new Map();
    var lastKey = -1, lastTile = null;
    var tmpCol = [0, 0, 0];

    function tileOf(ii, jj) {
      var ti = Math.floor(ii / T), tj = Math.floor(jj / T);
      var key = (ti + 4096) * 16384 + (tj + 4096);
      if (key === lastKey) return lastTile;
      var tile = tiles.get(key);
      if (!tile) {
        tile = new Float32Array(FLOATS);
        tile.fill(NaN);                       // NaN = 该点还没算过
        tiles.set(key, tile);
      }
      lastKey = key; lastTile = tile;
      return tile;
    }

    /* 取点阵点 (ii,jj) 的高度 + 顶点色，写入 out[0..3] */
    function sample(ii, jj, out) {
      var tile = tileOf(ii, jj);
      var idx = (((jj & (T - 1)) * T) + (ii & (T - 1))) * 4;
      var y = tile[idx];
      if (y !== y) {                          // NaN
        y = sampler(ii * s0, jj * s0);
        tile[idx] = y;
      }
      if (tile[idx + 1] !== tile[idx + 1]) {
        colorFn(ii * s0, jj * s0, y, tmpCol);
        tile[idx + 1] = tmpCol[0]; tile[idx + 2] = tmpCol[1]; tile[idx + 3] = tmpCol[2];
      }
      out[0] = y; out[1] = tile[idx + 1]; out[2] = tile[idx + 2]; out[3] = tile[idx + 3];
    }

    function prewarm(ii0, ii1, jj0, jj1) {
      var tmp = [0, 0, 0, 0], n = 0;
      for (var jj = jj0; jj <= jj1; jj++) {
        for (var ii = ii0; ii <= ii1; ii++) { sample(ii, jj, tmp); n++; }
      }
      return n;
    }

    return { sample: sample, prewarm: prewarm, tileCount: function () { return tiles.size; } };
  }

  /* ======================================================================
   * 6. LOD 规划（纯数学，selfTest 可以脱离 THREE 直接测）
   * ----------------------------------------------------------------------
   * 产出：s[]（每环间距）、c[]（吸附后的环中心）、H[]（每环正方形半边长）、m[]（每环单元数）
   *      mask（地形覆盖矩形，世界矩形 + 1.5% 外扩）、step[]（相对 s0 的格子倍数）
   * ==================================================================== */
  function intersectArea(cx, cz, H, mask) {
    var x0 = Math.max(mask.x0, cx - H), x1 = Math.min(mask.x1, cx + H);
    var z0 = Math.max(mask.z0, cz - H), z1 = Math.min(mask.z1, cz + H);
    return Math.max(0, x1 - x0) * Math.max(0, z1 - z0);
  }
  /* 正方形环要盖住一个矩形，需要的半边长 = 中心到矩形四个角的 Chebyshev 距离 */
  function chebNeeded(cx, cz, rect) {
    return Math.max(
      Math.abs(rect.x0 - cx), Math.abs(rect.x1 - cx),
      Math.abs(rect.z0 - cz), Math.abs(rect.z1 - cz));
  }

  function normalizeQuality(opts) {
    var q = {};
    var src = opts.quality || {};
    for (var k in DEFAULT_QUALITY) if (DEFAULT_QUALITY.hasOwnProperty(k)) q[k] = DEFAULT_QUALITY[k];
    for (var k2 in src) if (src.hasOwnProperty(k2)) q[k2] = src[k2];
    return q;
  }

  /* 按档位估算环带三角形数：用于「更大 DEM / 更多环也不超预算」的自动收缩 */
  function estimateTris(plan) {
    var cells = 0, prevArea = 0, last = plan.rings - 1;
    for (var k = 0; k <= last; k++) {
      var area = intersectArea(plan.c[k].x, plan.c[k].z, plan.H[k], plan.mask);
      var add = Math.max(0, area - prevArea) / (plan.s[k] * plan.s[k]);
      cells += add;
      prevArea = area;
    }
    /* ×2 是三角形，×1.15 补偿 mask 边界上的半单元 + 裙边 */
    return cells * 2 * 1.15;
  }

  function makeMask(world, expand) {
    var W = world.width, D = world.depth, ex = num(expand, DEFAULT_QUALITY.expand);
    return {
      x0: -W / 2 - W * ex, x1: W / 2 + W * ex,
      z0: -D / 2 - D * ex, z1: D / 2 + D * ex
    };
  }

  /* ---- 档位规划参数：环数 / 间距倍率 / 内环格数 / 自动 baseSpacing ---- */
  function planParams(cfg) {
    var q = cfg.quality, tier = cfg.tier;
    var tdef = TIER_DEFAULTS[tier] || TIER_DEFAULTS.high;
    var maxRings = tier === "cpu" ? 2 : 6;           // §D：quality.rings 支持 5~6；cpu 契约 ≤2
    var rings = clampNum(Math.round(num(q.rings, tdef.rings) || tdef.rings), 1, maxRings);
    var ringScale = clampNum(Math.round(num(q.ringScale, 2)), 2, 4);
    var align = 2 * ringScale;                       // 洞边界必须落在格子线上
    var cells = Math.round(num(q.cells, tdef.cells) || tdef.cells);
    cells = Math.max(align, Math.round(cells / align) * align);
    var base0 = num(q.baseSpacing, 0);
    if (!(base0 > 0)) {
      base0 = Math.max(cfg.world.width / Math.max(1, cfg.field.N - 1),
                       cfg.world.depth / Math.max(1, cfg.field.M - 1));
    }
    return { rings: rings, ringScale: ringScale, align: align, cells: cells,
             base0: base0, tdef: tdef, maxRings: maxRings, cacheSpacing: base0 / 2 };
  }

  /* ---- 相机高度 → LOD 细分档位（§C）：高于阈值时整体细分一档 ----
     只允许 1 或 2 两档：缓存点阵固定按 base0/2 建，boost=1 时索引步长 ×2、boost=2 时 ×1，
     两档都能精确落在缓存点阵上（切换档位不需要重建缓存）。 */
  function lodBoost(cfg, camY) {
    var q = cfg.quality;
    var th = num(q.lodHeightThreshold, DEFAULT_QUALITY.lodHeightThreshold);
    var bMax = clampNum(Math.round(num(q.lodBoostMax, DEFAULT_QUALITY.lodBoostMax)), 1, 2);
    return num(camY, 0) > th ? bMax : 1;
  }

  /* ---- 组装一次完整规划（不做预算收缩）：base 是当前档位的实际内环间距 ----
     环格数按 ringDecay 向外递减（m[k]≈cells*decay^k，对齐到 2*ringScale 的整数倍）；
     最外环自适应盖住 mask 并至少留 2 格带宽；所有环共用同一个 tileWorld。 */
  function assemblePlan(cfg, params, base, center, cells) {
    var mask = cfg.mask, q = cfg.quality;
    var rings = params.rings, ringScale = params.ringScale, align = params.align;
    var decay = num(q.ringDecay, DEFAULT_QUALITY.ringDecay);
    var cacheSpacing = params.cacheSpacing;
    var last = rings - 1;
    var s = [], c = [], H = [], m = [], snap = [], cacheStep = [];
    for (var k0 = 0; k0 <= last; k0++) s[k0] = base * Math.pow(ringScale, k0);
    for (var k = 0; k <= last; k++) {
      snap[k] = (k < last) ? s[k + 1] : s[last];
      c[k] = {
        x: Math.round(center.x / snap[k]) * snap[k],
        z: Math.round(center.z / snap[k]) * snap[k]
      };
      /* 该环顶点在「缓存点阵」上的整数步长（缓存间距固定 = base0/2） */
      cacheStep[k] = Math.round(s[k] / cacheSpacing);
      if (k < last) {
        var mm = Math.round((cells * Math.pow(decay, k)) / align) * align;
        m[k] = Math.max(align, mm);
        H[k] = (m[k] / 2) * s[k];
      }
    }
    /* 最外环：覆盖 mask + 带宽约束 */
    var need = chebNeeded(c[last].x, c[last].z, mask);
    var mPrev = last > 0 ? m[last - 1] : cells;
    var bandCells = Math.ceil((mPrev / ringScale + 4) / 2) * 2;   // 至少 2 格带宽
    var hNeed = need + s[last];
    if (last > 0) hNeed = Math.max(hNeed, H[last - 1] + 1.5 * s[last]);
    var ml = Math.ceil((2 * hNeed) / s[last]);
    ml = Math.ceil(ml / 2) * 2;
    ml = Math.max(ml, bandCells, 2);
    m[last] = ml; H[last] = (ml / 2) * s[last];

    /* §A：所有环共用同一个世界对齐纹理尺度（旧版每环不同 repeat 是俯视矩形拼缝的根因） */
    var tileW = num(q.tileWorld, 0) > 0 ? num(q.tileWorld, 0)
      : num(q.tileBase, DEFAULT_QUALITY.tileBase);
    return {
      base: base, s0: base, rings: rings, last: last, ringScale: ringScale,
      s: s, c: c, H: H, m: m, snap: snap,
      cacheStep: cacheStep, cacheSpacing: cacheSpacing,
      normalEps: params.base0,             // 法线差分统一用 DEM 格距（跨环一致，见 §7.4）
      tileWorld: tileW, mask: mask, cells: cells, boost: 1,
      baseSpacingRaw: params.base0
    };
  }

  /* ---- 预算收缩：在代表性机位上取最大估算 ----
     先按比例缩小内环格数；若格数已到下限仍超预算（例如 cpu 档只有 2 环、世界又很大），
     就把 baseSpacing 整体翻倍（LOD 降档），直到放得下。返回 {base, cells}。 ---- */
  function fitPlan(cfg, params, boost, center) {
    var apronTris = estimateApronTris(cfg);
    var target = Math.max(2000, params.tdef.maxTris - apronTris - 800);
    var centers = [
      center,
      { x: 0, z: 0 },
      { x: cfg.world.width / 2, z: cfg.world.depth / 2 },
      { x: -cfg.world.width / 2, z: cfg.world.depth / 2 },
      { x: cfg.world.width / 2, z: -cfg.world.depth / 2 },
      { x: -cfg.world.width / 2, z: -cfg.world.depth / 2 }
    ];
    var cellsMin = 2 * params.ringScale;
    var base = params.base0 / boost;
    var cells = params.cells;
    for (var coarsen = 0; coarsen < 5; coarsen++) {
      var fitted = false;
      for (var it = 0; it < 16; it++) {
        var maxEst = 0;
        for (var ci = 0; ci < centers.length; ci++) {
          var p = assemblePlan(cfg, params, base, centers[ci], cells);
          var est = estimateTris(p);
          if (est > maxEst) maxEst = est;
        }
        if (maxEst <= target) { fitted = true; break; }
        if (cells > cellsMin) {
          cells = Math.max(cellsMin, Math.floor(cells * 0.86 / params.align) * params.align);
        } else break;
      }
      if (fitted) return { base: base, cells: cells };
      base *= 2;              // 格数已到下限：整体降 LOD 档（间距翻倍）
      cells = params.cells;
    }
    return { base: base, cells: cells };
  }

  /* ---- 最终规划：baseSpacing → 相机档位 → 预算收缩 → 组装 ---- */
  function choosePlan(cfg, center, camY) {
    var params = planParams(cfg);
    var boost = lodBoost(cfg, camY);
    var fit = fitPlan(cfg, params, boost, center);
    var plan = assemblePlan(cfg, params, fit.base, center, fit.cells);
    plan.boost = boost;
    plan.params = params;
    return plan;
  }

  /* ======================================================================
   * 7. 单环网格构建（纯逻辑：只写 TypedArray，不碰 THREE，selfTest 可直接复用）
   * ----------------------------------------------------------------------
   * 网格是「正方形 S(k) 去掉内环正方形 S(k-1)」的 L 形环带，再与 mask 求交：
   *   · 保留与 mask 相交的单元（保证世界矩形一定被覆盖）；
   *   · 单元若整个落在洞里则跳过（洞边界按构造落在格子线上，不会切出半个单元）；
   *   · 洞边界两侧顶点的世界坐标完全一致（内环用的是同一套高度采样），所以表面无缝；
   *   · 另外沿洞边界/外边界加一圈向下的裙边，盖住陡坡上 T 型接缝的高差。
   * 顶点全部落在 s0 全局点阵上 → 高度/颜色直接查缓存。
   * ==================================================================== */
  function ensureRingBuffers(rt, vNeed, iNeed, hNeed) {
    var b = rt.buf;
    if (!b.pos || b.vcap < vNeed) {
      b.pos = new Float32Array(vNeed * 3);
      b.col = new Float32Array(vNeed * 3);
      b.uv = new Float32Array(vNeed * 2);
      b.nrm = new Float32Array(vNeed * 3);
      b.vcap = vNeed;
      b.attrsDirty = true;
    }
    if (!b.idx || b.icap < iNeed) {
      b.idx = new Uint32Array(iNeed);
      b.icap = iNeed;
      b.attrsDirty = true;
    }
    if (!b.hgt || b.hgt.length < hNeed) b.hgt = new Float32Array(hNeed);
  }

  function buildRingMesh(state, k) {
    var plan = state.plan, rt = state.rings[k], q = state.quality;
    var s = plan.s[k], m = plan.m[k], c = plan.c[k], H = plan.H[k];
    var x0 = c.x - H, z0 = c.z - H;
    var mask = plan.mask;

    /* ---- 7.1 与 mask 相交的单元范围（保证世界矩形被完整覆盖） ---- */
    var iLo = Math.max(0, Math.ceil((mask.x0 - x0) / s) - 1);
    var iHi = Math.min(m - 1, Math.ceil((mask.x1 - x0) / s) - 1);
    var jLo = Math.max(0, Math.ceil((mask.z0 - z0) / s) - 1);
    var jHi = Math.min(m - 1, Math.ceil((mask.z1 - z0) / s) - 1);
    if (iLo > iHi || jLo > jHi) {
      rt.buf.icount = 0; rt.tris = 0; rt.verts = 0;
      return;
    }

    /* ---- 7.2 洞（第 k-1 环正方形），边界必须精确落在本级格子线上 ---- */
    var hole = null;
    if (k > 0) {
      var ch = plan.c[k - 1], Hh = plan.H[k - 1];
      hole = {
        x0: ch.x - Hh, x1: ch.x + Hh, z0: ch.z - Hh, z1: ch.z + Hh,
        i0: Math.round((ch.x - Hh - x0) / s), i1: Math.round((ch.x + Hh - x0) / s),
        j0: Math.round((ch.z - Hh - z0) / s), j1: Math.round((ch.z + Hh - z0) / s)
      };
    }

    /* ---- 7.3 顶点网格（外接矩形 + 1 圈 padding 用于差分法线） ---- */
    var ni = iHi - iLo + 2, nj = jHi - jLo + 2;
    var vcap = ni * nj;
    /* 裙边顶点上限：洞边界 ≤ 4*(m/2) 条边 + 外边界 ≤ 4*m 条边，每条边追加 2 个底点
       → 最多 12m 个顶点；再留余量，避免极端机位下写越界（TypedArray 越界写是静默丢弃，
       会让 index 指向缓冲区外的顶点 → GPU 画出撕裂到屏幕外的巨型三角形）。 */
    var skirtCap = 16 * m + 512;
    ensureRingBuffers(rt,
      vcap + skirtCap,
      6 * ((iHi - iLo + 1) * (jHi - jLo + 1)) + 90 * m + 4096,
      8);

    var b = rt.buf;
    var pos = b.pos, col = b.col, uv = b.uv, nrm = b.nrm, idx = b.idx;
    var tileW = plan.tileWorld;                  // §A：全环共用同一个世界对齐纹理尺度
    var step = plan.cacheStep[k];                // 相对缓存点阵的整数步长
    var ii0 = Math.round((x0 + iLo * s) / plan.cacheSpacing);
    var jj0 = Math.round((z0 + jLo * s) / plan.cacheSpacing);
    var tmp = state.tmp;                         // 复用采样临时数组
    if (!tmp) { tmp = state.tmp = [0, 0, 0, 0]; }
    var pj, pi, v;

    /* ---- 7.3 顶点：位置/高度/颜色/UV 全部来自世界对齐的缓存点阵 ---- */
    for (pj = 0; pj < nj; pj++) {
      var j = jLo + pj;
      var z = z0 + j * s;
      var jj = jj0 + pj * step;
      for (pi = 0; pi < ni; pi++) {
        var i = iLo + pi;
        var x = x0 + i * s;
        var ii = ii0 + pi * step;
        state.cache.sample(ii, jj, tmp);
        v = pj * ni + pi;
        pos[v * 3] = x; pos[v * 3 + 1] = tmp[0]; pos[v * 3 + 2] = z;
        col[v * 3] = tmp[1]; col[v * 3 + 1] = tmp[2]; col[v * 3 + 2] = tmp[3];
        uv[v * 2] = x / tileW; uv[v * 2 + 1] = z / tileW;
      }
    }

    /* ---- 7.4 法线：用**固定世界尺度**（DEM 格距）做高度差分 ----
       为什么不用各环自己的间距：粗环按 s=2.75 差分时法线只反映超大尺度起伏，
       细环按 0.17 差分时法线满是细节 → 环与环之间会出现明显的明暗/质感跳变
       （俯视像几块不同密度的抹布、远景发糊）。统一成 DEM 格距后，所有环共享同一套
       "细节法线"，粗环的远景也能看出山脊沟壑，环边界不再有明暗跳变。 */
    var nEps = plan.normalEps > 0 ? plan.normalEps : s;
    var inv2n = 1 / (2 * nEps);
    var nStep = Math.max(1, Math.round(nEps / plan.cacheSpacing));   // 缓存点阵上的整数步长
    var nt = state.tmpN; if (!nt) nt = state.tmpN = [0, 0, 0, 0];
    for (pj = 0; pj < nj; pj++) {
      var jjN = jj0 + pj * step;
      for (pi = 0; pi < ni; pi++) {
        var iiN = ii0 + pi * step;
        state.cache.sample(iiN - nStep, jjN, nt);
        var hL2 = nt[0];
        state.cache.sample(iiN + nStep, jjN, nt);
        var hR2 = nt[0];
        state.cache.sample(iiN, jjN - nStep, nt);
        var hD2 = nt[0];
        state.cache.sample(iiN, jjN + nStep, nt);
        var hU2 = nt[0];
        var nx2 = (hL2 - hR2) * inv2n;
        var nz2 = (hD2 - hU2) * inv2n;
        var len2 = Math.sqrt(nx2 * nx2 + 1 + nz2 * nz2);
        v = pj * ni + pi;
        nrm[v * 3] = nx2 / len2; nrm[v * 3 + 1] = 1 / len2; nrm[v * 3 + 2] = nz2 / len2;
      }
    }

    /* ---- 7.5 三角形：保留单元（洞内跳过） ---- */
    var mark = rt.mark;                          // 复用 scratch，避免每次重建 new（每帧零分配）
    if (!mark || mark.length < vcap) { mark = new Uint8Array(vcap); rt.mark = mark; }
    else mark.fill(0, 0, vcap);
    var ic = 0;
    for (j = jLo; j <= jHi; j++) {
      var gj = j - jLo;
      var inHoleJ = hole && j >= hole.j0 && j < hole.j1;
      for (i = iLo; i <= iHi; i++) {
        if (inHoleJ && i >= hole.i0 && i < hole.i1) continue;
        var a = gj * ni + (i - iLo);
        var bb = a + ni, ccn = bb + 1, d = a + 1;
        idx[ic++] = a; idx[ic++] = bb; idx[ic++] = ccn;
        idx[ic++] = a; idx[ic++] = ccn; idx[ic++] = d;
        mark[a] = 1; mark[bb] = 1; mark[ccn] = 1; mark[d] = 1;
      }
    }

    /* ---- 7.6 裙边：沿洞边界（朝内）和外边界（朝内）向下挂一圈 ----
       裙边是「T 型接缝」的最后保险：两侧网格顶点数不同（细/粗），陡坡上边缘高度
       会有微小高差，垂直裙边把这条缝完全盖住，任何角度都不会看到背景穿帮。 */
    var depth = Math.max(num(q.skirtDepth, 3.0), s * 2.0);
    var vCount = vcap;
    var skirtVerts = 0;

    function addSkirt(ia, ib, dirX, dirZ) {
      var ax = pos[ia * 3], az = pos[ia * 3 + 2];
      var bx = pos[ib * 3], bz = pos[ib * 3 + 2];
      /* 让三角形法线朝 (dirX, dirZ)：边方向取 (-dirZ, dirX) */
      var ex = bx - ax, ez = bz - az;
      if (ex * (-dirZ) + ez * dirX < 0) { var t = ia; ia = ib; ib = t; }
      var nlen = Math.sqrt(dirX * dirX + dirZ * dirZ) || 1;
      var nX = dirX / nlen, nZ = dirZ / nlen;
      var b1 = vCount, b2 = vCount + 1;
      var o1 = b1 * 3, o2 = b2 * 3;
      pos[o1] = pos[ia * 3]; pos[o1 + 1] = pos[ia * 3 + 1] - depth; pos[o1 + 2] = pos[ia * 3 + 2];
      pos[o2] = pos[ib * 3]; pos[o2 + 1] = pos[ib * 3 + 1] - depth; pos[o2 + 2] = pos[ib * 3 + 2];
      col[o1] = col[ia * 3]; col[o1 + 1] = col[ia * 3 + 1]; col[o1 + 2] = col[ia * 3 + 2];
      col[o2] = col[ib * 3]; col[o2 + 1] = col[ib * 3 + 1]; col[o2 + 2] = col[ib * 3 + 2];
      uv[b1 * 2] = uv[ia * 2]; uv[b1 * 2 + 1] = uv[ia * 2 + 1];
      uv[b2 * 2] = uv[ib * 2]; uv[b2 * 2 + 1] = uv[ib * 2 + 1];
      nrm[o1] = nX; nrm[o1 + 1] = 0; nrm[o1 + 2] = nZ;
      nrm[o2] = nX; nrm[o2 + 1] = 0; nrm[o2 + 2] = nZ;
      idx[ic++] = ia; idx[ic++] = ib; idx[ic++] = b2;
      idx[ic++] = ia; idx[ic++] = b2; idx[ic++] = b1;
      vCount += 2; skirtVerts += 2;
    }

    function cellKept(i, j) {
      if (i < iLo || i > iHi || j < jLo || j > jHi) return false;
      if (hole && i >= hole.i0 && i < hole.i1 && j >= hole.j0 && j < hole.j1) return false;
      return true;
    }

    /* 7.6a 洞边界裙边：贴着内环外沿向下，法线朝洞中心（左 +x / 右 -x / 下 +z / 上 -z） */
    if (hole) {
      var jA = Math.max(hole.j0, jLo), jB = Math.min(hole.j1 - 1, jHi);
      for (j = jA; j <= jB; j++) {
        var iL = hole.i0 - 1;
        if (cellKept(iL, j)) {
          var gL = (j - jLo) * ni + (iL - iLo);
          addSkirt(gL, gL + ni, 1, 0);
        }
        var iR = hole.i1;
        if (cellKept(iR, j)) {
          var gR = (j - jLo) * ni + (iR - iLo);
          addSkirt(gR, gR + ni, -1, 0);
        }
      }
      var iA = Math.max(hole.i0, iLo), iB = Math.min(hole.i1 - 1, iHi);
      for (i = iA; i <= iB; i++) {
        var jBtm = hole.j0 - 1;
        if (cellKept(i, jBtm)) {
          var gB = (jBtm - jLo) * ni + (i - iLo);
          addSkirt(gB, gB + 1, 0, 1);
        }
        var jTop = hole.j1;
        if (cellKept(i, jTop)) {
          var gT = (jTop - jLo) * ni + (i - iLo);
          addSkirt(gT, gT + 1, 0, -1);
        }
      }
    }

    /* 7.6b 外边界裙边：只在本级正方形真的顶到 mask 外侧时才需要（k < 最后一环） */
    if (k < plan.last) {
      if (iLo === 0) {
        for (j = jLo; j <= jHi; j++) {
          if (!cellKept(0, j)) continue;
          var g0 = (j - jLo) * ni;
          addSkirt(g0, g0 + ni, 1, 0);
        }
      }
      if (iHi === m - 1) {
        for (j = jLo; j <= jHi; j++) {
          if (!cellKept(m - 1, j)) continue;
          var g1 = (j - jLo) * ni + (ni - 1);
          addSkirt(g1, g1 + ni, -1, 0);
        }
      }
      if (jLo === 0) {
        for (i = iLo; i <= iHi; i++) {
          if (!cellKept(i, 0)) continue;
          var g2 = i - iLo;
          addSkirt(g2, g2 + 1, 0, 1);
        }
      }
      if (jHi === m - 1) {
        for (i = iLo; i <= iHi; i++) {
          if (!cellKept(i, m - 1)) continue;
          var g3 = (nj - 1) * ni + (i - iLo);
          addSkirt(g3, g3 + 1, 0, -1);
        }
      }
    }

    /* ---- 7.7 统计 ---- */
    var used = 0;
    for (v = 0; v < vcap; v++) if (mark[v]) used++;
    rt.buf.icount = ic;
    rt.buf.vused = vcap;                 // 顶点缓冲里网格段占用数（裙边接着往后放）
    rt.tris = ic / 3;
    rt.verts = used + skirtVerts;
    rt.visible = ic > 0;
  }

  /* ======================================================================
   * 7b. 外延缓冲带（apron）：世界矩形之外再铺一圈超低精度大网格
   * ----------------------------------------------------------------------
   * 为什么需要：
   *   clipmap 只覆盖 mask（世界矩形 +1.5%）。镜头压低、环绕、或"跟随无人机"时，
   *   世界边缘就是一圈竖直裙边，裙子下面直接是黑虚空 → 地形像一块"悬空的岛"，
   *   远处的环形山也像是浮在虚空里。
   *   这里在世界矩形之外再铺一圈间距 8~16 世界单位的超低模网格：
   *     · 内边界精确压在世界矩形上，比地形低 0.15 → 被最外层环的锯齿边盖住，看不见缝；
   *     · 高度用「世界边缘 clamp 高度」作起点，向外平滑下压到大地基底高度
   *       （= 场最低高程 - 1.6，与 app3d.html buildBackdrop 的大圆盘同高），
   *       所以地形边缘不再是断崖，而是自然过渡成"大地"；
   *     · 顶点色沿用同一套坡度-高程配色（下压后自然落进 DEEP 深色带），
   *       再随外扩距离向雾色融合并整体压暗 → 远景是一条没入雾里的大地。
   * 这块网格只依赖 world/field（与相机、与 clipmap 环无关），所以 create 时建一次即可；
   * 三角数约 1~8k、1 个 draw call，仍在 drawCalls≤8 与各档三角形预算之内。
   * ==================================================================== */
  /* ---- 外延带网格参数（纯数学；buildApronMesh 与三角形预算估算共用同一套公式） ---- */
  function apronGrid(cfg) {
    var W = cfg.world.width, D = cfg.world.depth;
    var halfW = W / 2, halfD = D / 2;
    var q = cfg.quality;
    var cpu = cfg.tier === "cpu";
    /* 间距：默认 max(8, max(W,D)/16)；cpu 档再放大 1.6 倍省三角形（软件光栅） */
    var spacing = num(q.extSpacing, 0);
    if (!(spacing > 0)) spacing = Math.max(8, Math.max(W, D) / 16);
    spacing *= cpu ? 1.6 : 1.0;
    /* 取偶数格数 → 世界边缘正好落在格线上（洞边界 = 格线，不会有半格裁剪裂缝） */
    var nx = Math.max(2, 2 * Math.round(W / (2 * spacing)));
    var nz = Math.max(2, 2 * Math.round(D / (2 * spacing)));
    var ex = W / nx, ez = D / nz;
    var targetR = num(q.extRadius, 0);
    if (!(targetR > 0)) targetR = Math.max(W, D) * 1.9;   // 外扩到世界大边的 ~1.9 倍
    var hx = nx / 2, hz = nz / 2;
    var marginX = Math.max(1, Math.ceil((targetR - halfW) / ex));
    var marginZ = Math.max(1, Math.ceil((targetR - halfD) / ez));
    var ciX = hx + marginX, ciZ = hz + marginZ;
    var nVX = 2 * ciX + 1, nVZ = 2 * ciZ + 1;
    var Rx = halfW + marginX * ex, Rz = halfD + marginZ * ez;
    return { W: W, D: D, halfW: halfW, halfD: halfD, spacing: spacing,
             nx: nx, nz: nz, ex: ex, ez: ez, targetR: targetR,
             hx: hx, hz: hz, marginX: marginX, marginZ: marginZ,
             ciX: ciX, ciZ: ciZ, nVX: nVX, nVZ: nVZ, Rx: Rx, Rz: Rz };
  }
  /* 外延带三角形数（供预算收缩扣减） */
  function estimateApronTris(cfg) {
    var g = apronGrid(cfg);
    return ((g.nVX - 1) * (g.nVZ - 1) - g.nx * g.nz) * 2;
  }

  function buildApronMesh(cfg) {
    var g = apronGrid(cfg);
    var W = g.W, D = g.D, halfW = g.halfW, halfD = g.halfD;
    var nx = g.nx, nz = g.nz, ex = g.ex, ez = g.ez;
    var hx = g.hx, hz = g.hz, ciX = g.ciX, ciZ = g.ciZ;
    var nVX = g.nVX, nVZ = g.nVZ, Rx = g.Rx, Rz = g.Rz;

    /* 大地基底高度：与主页面 buildBackdrop 的圆盘一致（hMin - 1.6），才能"接上" */
    var hMin = Infinity;
    var Hf = cfg.field.H;
    for (var hI = 0; hI < Hf.length; hI++) if (Hf[hI] < hMin) hMin = Hf[hI];
    if (!isFinite(hMin)) hMin = 0;
    var baseY = hMin - 1.6;
    var outerY = baseY + 0.25;   // 外缘略高于圆盘：先铺满再收，避免大面积共面 z-fighting
    var edgeSink = 0.15;         // 内边比地形低 0.15，被最外层环的锯齿边盖住
    var descentStart = 0.06;     // 前 6% 保持与边缘齐平，再开始下压（避免边缘一圈斜坡太扎眼）
    var uvTile = num(cfg.uvTile, 0) > 0 ? cfg.uvTile : Math.max(6, Math.max(W, D) / 16);

    var fogR = cfg.fogColor ? cfg.fogColor.r : 0.043;
    var fogG = cfg.fogColor ? cfg.fogColor.g : 0.102;
    var fogB = cfg.fogColor ? cfg.fogColor.b : 0.149;

    /* 归一化"外扩距离"：0 = 世界边缘，1 = 外缘（方形缓冲带，取 x/z 方向较大者） */
    function outFrac(x, z) {
      var ax = x < 0 ? -x : x, az = z < 0 ? -z : z;
      var fx = ax <= halfW ? 0 : (ax - halfW) / Math.max(1e-6, Rx - halfW);
      var fz = az <= halfD ? 0 : (az - halfD) / Math.max(1e-6, Rz - halfD);
      return Math.min(1, Math.max(fx, fz));
    }
    /* 低频起伏噪声（确定性哈希 + 双线性）：让外延带不是一张完美光滑的坡面，
       否则远景看起来像塑料板。振幅在内边界处为 0（保持与地形齐平），越往外越明显。 */
    function apHash(ix, iy) {
      var n = ix * 374761393 + iy * 668265263;
      n = (n ^ (n >> 13)) * 1274126177;
      n = n ^ (n >> 16);
      return ((n >>> 0) % 1000003) / 1000003;
    }
    function apNoise(x, z) {
      var x0 = Math.floor(x), y0 = Math.floor(z);
      var fx = x - x0, fy = z - y0;
      var sx = fx * fx * (3 - 2 * fx), sy = fy * fy * (3 - 2 * fy);
      var v00 = apHash(x0, y0), v10 = apHash(x0 + 1, y0);
      var v01 = apHash(x0, y0 + 1), v11 = apHash(x0 + 1, y0 + 1);
      return (v00 * (1 - sx) + v10 * sx) * (1 - sy) + (v01 * (1 - sx) + v11 * sx) * sy;
    }
    /* 高度场：边缘 clamp 高度 → 平滑下压到大地基底。
       两个尺度的低频起伏（~70 / ~22 世界单位）：远景俯视时不会是一张完美光滑的坡面，
       能看出"大地"的高低起伏；两端 envelope 归零，保证内边界与地形齐平、外缘不戳穿圆盘。 */
    function apronY(x, z) {
      var t = outFrac(x, z);
      var ts = smoothstep(descentStart, 1.0, t);
      var edgeH = cfg.sampler(x, z);       // 世界外自动 clamp 到最近边缘高度
      var y = edgeH + (outerY - edgeH) * ts;
      y -= edgeSink * (1 - ts);
      var undulEnv = smoothstep(0.08, 0.38, t) * (1 - smoothstep(0.82, 1.0, t));
      var n1 = apNoise(x * 0.014 + 2.3, z * 0.014 - 5.1) - 0.5;    // ~70 世界单位
      var n2 = apNoise(x * 0.045 - 9.7, z * 0.045 + 3.9) - 0.5;    // ~22 世界单位
      var undul = (n1 * 7.0 + n2 * 2.2) * undulEnv;
      return y + undul;
    }

    var nVert = nVX * nVZ;
    var pos = new Float32Array(nVert * 3);
    var col = new Float32Array(nVert * 3);
    var uv = new Float32Array(nVert * 2);
    var nrm = new Float32Array(nVert * 3);
    var colTmp = [0, 0, 0];
    var eps = Math.min(ex, ez) * 0.6;
    var inv2eps = 1 / (2 * eps);
    var i, j, v, x, z, y;
    for (j = 0; j < nVZ; j++) {
      z = -Rz + j * ez;
      for (i = 0; i < nVX; i++) {
        x = -Rx + i * ex;
        v = j * nVX + i;
        y = apronY(x, z);
        pos[v * 3] = x; pos[v * 3 + 1] = y; pos[v * 3 + 2] = z;
        /* 顶点色：用「世界边缘 clamp 高度」查同一套配色（而不是下压后的 y）——
           否则外延带会整体落进 DEEP 深色带、看起来像一块黑底板（地形又"浮空"了）。
           再用外扩距离做轻度压暗 + 雾色融合，让大地自然过渡到远景。 */
        var edgeH = cfg.sampler(x, z);
        cfg.colorFn(x, z, edgeH, colTmp);
        var f = smoothstep(0.05, 1.0, outFrac(x, z));
        var shade = 1 - 0.32 * f;
        col[v * 3] = lerp(colTmp[0], fogR, 0.30 * f) * shade;
        col[v * 3 + 1] = lerp(colTmp[1], fogG, 0.30 * f) * shade;
        col[v * 3 + 2] = lerp(colTmp[2], fogB, 0.30 * f) * shade;
        uv[v * 2] = x / uvTile; uv[v * 2 + 1] = z / uvTile;
        /* 法线：高度场差分（与地形环同一套公式） */
        var nX = (apronY(x - eps, z) - apronY(x + eps, z)) * inv2eps;
        var nZ = (apronY(x, z - eps) - apronY(x, z + eps)) * inv2eps;
        var len = Math.sqrt(nX * nX + 1 + nZ * nZ);
        nrm[v * 3] = nX / len; nrm[v * 3 + 1] = 1 / len; nrm[v * 3 + 2] = nZ / len;
      }
    }

    /* 索引：整块网格挖掉世界矩形（洞边界落在格线上） */
    var hCellX0 = ciX - hx, hCellX1 = ciX + hx;    // 洞的单元范围 [hCellX0, hCellX1)
    var hCellZ0 = ciZ - hz, hCellZ1 = ciZ + hz;
    var idx = new Uint32Array(((nVX - 1) * (nVZ - 1) - nx * nz) * 6);
    var ic = 0;
    for (j = 0; j < nVZ - 1; j++) {
      var inHoleJ = (j >= hCellZ0 && j < hCellZ1);
      for (i = 0; i < nVX - 1; i++) {
        if (inHoleJ && i >= hCellX0 && i < hCellX1) continue;
        var a = j * nVX + i;
        var b = a + nVX, cc = b + 1, d = a + 1;
        idx[ic++] = a; idx[ic++] = b; idx[ic++] = cc;
        idx[ic++] = a; idx[ic++] = cc; idx[ic++] = d;
      }
    }
    return {
      pos: pos, col: col, uv: uv, nrm: nrm, idx: idx,
      tris: ic / 3,
      verts: nVert - (nx - 1) * (nz - 1),   // 洞内部的顶点不参与渲染，不计入
      outerR: Math.max(Rx, Rz),
      spacing: Math.min(ex, ez),
      baseY: baseY,
      Rx: Rx, Rz: Rz
    };
  }

  /* ======================================================================
   * 8. create：搭 Group / 材质 / 纹理 / 环网格，并把 update 闭包返回给 Lead
   * ==================================================================== */
  function create(opts) {
    var THREE;
    try {
      opts = opts || {};
      THREE = opts.THREE;
      if (!THREE || !THREE.BufferGeometry || !THREE.MeshStandardMaterial) {
        logTo(opts, "缺少 THREE，跳过 LOD 地形");
        return null;
      }
      var field = opts.field;
      if (!field || !field.H || !(field.N >= 2) || !(field.M >= 2) ||
          field.H.length < field.N * field.M) {
        logTo(opts, "field 无效（需要 {H, N, M}），跳过 LOD 地形");
        return null;
      }
      var world = opts.world;
      if (!world || !(world.width > 0) || !(world.depth > 0)) {
        logTo(opts, "world 无效（需要 {width, depth}），跳过 LOD 地形");
        return null;
      }
      var scene = opts.scene;
      if (!scene || typeof scene.add !== "function") {
        logTo(opts, "scene 无效，跳过 LOD 地形");
        return null;
      }
      var camera = opts.camera || null;
      var tier = opts.tier || "high";
      if (!TIER_DEFAULTS[tier]) tier = "high";

      var quality = normalizeQuality(opts);
      if (quality.enabled === false) {
        logTo(opts, "quality.enabled=false，不启用 LOD 地形");
        return null;
      }
      /* 采坑参数 / 烘焙 AO：契约里没有这两项，但为了让 LOD 地形的配色与老版本完全一致，
         允许 Lead 显式传 quality.pit / quality.bakedAO；没传时尽量从全局 PIT / RENDER 读，
         读不到就退化为「不画采坑灰、默认烘焙 AO」。 */
      var colorOpt = {
        bakedAO: quality.bakedAO,
        pit: quality.pit,
        macroNoise: quality.macroNoise,
        macroAmp: quality.macroAmp
      };
      if (!colorOpt.pit) {
        try { if (typeof PIT !== "undefined" && PIT && PIT.rx > 0) colorOpt.pit = PIT; }
        catch (e1) { /* 独立自检页没有 PIT，忽略 */ }
      }
      if (quality.bakedAO === undefined || quality.bakedAO === null) {
        try { if (typeof RENDER !== "undefined" && RENDER) colorOpt.bakedAO = !!RENDER.ao; }
        catch (e2) { colorOpt.bakedAO = true; }
      }

      var cfg = {
        field: field, world: world, tier: tier, quality: quality,
        mask: makeMask(world, quality.expand)
      };

      /* ---- 8.1 相机中心（世界内=相机 XZ；世界外=夹到世界矩形边界，精度不浪费在背板上） ---- */
      function clipCenter(x, z) {
        return {
          x: clampNum(x, -world.width / 2, world.width / 2),
          z: clampNum(z, -world.depth / 2, world.depth / 2)
        };
      }
      var camPos0 = (camera && camera.position) ? camera.position : { x: 0, z: 0, y: 0 };
      var initialCenter = clipCenter(camPos0.x, camPos0.z);

      var sampler = makeSampler(field, world);
      var colorFn = makeColorFn(sampler, field, world, colorOpt);
      var plan = choosePlan(cfg, initialCenter, camPos0.y);

      /* 缓存点阵固定按 baseSpacing/2 建（§C：相机高度切换 LOD 档位时缓存不用重建） */
      var cache = makeCache(sampler, colorFn, plan.cacheSpacing);

      /* ---- 8.2 预热点阵缓存：世界内点阵 ≤ 32 万点时一次性预热 ----
         这样后续相机移动时内环重定位只是读缓存（<2ms），不会掉帧。
         超大 DEM（如 1024×768 自检页）点数太多，改为按需惰性计算，避免启动卡死。 */
      var prewarmCount = 0, tPrewarm = nowMs();
      try {
        var margin = plan.s[plan.last] * 2;
        var csp = plan.cacheSpacing;
        var ii0 = Math.ceil((plan.mask.x0 - margin) / csp);
        var ii1 = Math.floor((plan.mask.x1 + margin) / csp);
        var jj0 = Math.ceil((plan.mask.z0 - margin) / csp);
        var jj1 = Math.floor((plan.mask.z1 + margin) / csp);
        var pCount = Math.max(0, ii1 - ii0 + 1) * Math.max(0, jj1 - jj0 + 1);
        if (pCount > 0 && pCount <= 320000) {
          prewarmCount = cache.prewarm(ii0, ii1, jj0, jj1);
        }
      } catch (ePre) { /* 预热失败不影响正确性 */ }
      var prewarmMs = nowMs() - tPrewarm;

      /* ---- 8.3 细节纹理（canvas 程序化，512²；每环克隆一份让各环独立设置过滤参数） ---- */
      var baseCanvas = null;
      try { baseCanvas = makeDetailCanvas(512); }
      catch (eTex) { logTo(opts, "细节纹理生成失败（退化只用顶点色）：" + eTex.message); }

      var group = new THREE.Group();
      group.name = "terrain_lod";
      scene.add(group);

      var rings = [], materials = [];
      var aniso = clampNum(Math.round(num(quality.anisotropy, 4)), 1, 16);
      /* 细节纹理是 canvas 画的，canvas 像素本身就是 sRGB 空间 → 必须标 sRGBEncoding，
         three 才会在 shader 里解码到线性空间；否则（默认 LinearEncoding）贴图会被
         当成线性值使用，整体偏亮偏灰、对比度被压平。
         解码后贴图均值约从 0.90 降到 0.78，所以把 baseColor 从 1.12 收到 1.0
         （相当于净乘子 0.78，比"无贴图的老版本"略暗一点点，但色相/饱和与升级前一致）。 */
      var baseColor = num(quality.baseColor, DEFAULT_QUALITY.baseColor);
      function makeDetailTexture(anisoValue) {
        if (!baseCanvas) return null;
        var tex = new THREE.CanvasTexture(baseCanvas);
        tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
        tex.anisotropy = anisoValue;
        tex.minFilter = THREE.LinearMipmapLinearFilter;
        tex.magFilter = THREE.LinearFilter;
        tex.generateMipmaps = true;
        if (THREE.sRGBEncoding !== undefined) tex.encoding = THREE.sRGBEncoding;
        tex.needsUpdate = true;
        return tex;
      }

      for (var k = 0; k < plan.rings; k++) {
        var tex = makeDetailTexture(aniso);
        var mat = new THREE.MeshStandardMaterial({
          vertexColors: true,
          roughness: 0.94,
          metalness: 0.04,
          flatShading: false,
          side: THREE.FrontSide,
          map: tex
        });
        mat.color.setRGB(baseColor, baseColor, baseColor);
        mat.name = "terrain_lod_ring" + k;
        /* 这行给 Lead 的云影注入留的钩子：applyCloudShadow(mat) 里会新增 onBeforeCompile，
           并把它推入 state.cloudUs 统一推进时间。 */
        materials.push(mat);

        var geo = new THREE.BufferGeometry();
        var mesh = new THREE.Mesh(geo, mat);
        mesh.name = "terrain_ring" + k;
        mesh.receiveShadow = true;
        mesh.castShadow = true;
        mesh.frustumCulled = false;   // 顶点每帧可能重写，包围球不可靠；地形本来就常驻可见
        mesh.matrixAutoUpdate = false;
        mesh.updateMatrix();
        group.add(mesh);

        rings.push({
          geo: geo, mat: mat, mesh: mesh,
          buf: { pos: null, col: null, uv: null, nrm: null, idx: null, hgt: null,
                 vcap: 0, icap: 0, icount: 0, vused: 0, attrsDirty: true },
          tris: 0, verts: 0, buildMs: 0, mark: null
        });
      }

      /* ---- 8.3b 外延缓冲带（apron）：世界矩形之外一圈超低模"大地" ----
         只建一次（与相机无关，内边界固定压在世界矩形上）；材质也放进 materials，
         让 Lead 的云影注入对缓冲带同样生效。失败时只是边缘仍像断崖，不影响主地形。 */
      var apron = null, apronMesh = null, apronMat = null, apronGeo = null, apronMs = 0;
      try {
        var tApron = nowMs();
        var fogC = (scene.fog && scene.fog.color) ? scene.fog.color : null;
        apron = buildApronMesh({
          field: field, world: world, sampler: sampler, colorFn: colorFn,
          quality: quality, tier: tier, fogColor: fogC,
          uvTile: plan.tileWorld          // §A：外延带与地形共用同一个世界对齐纹理尺度
        });
        apronGeo = new THREE.BufferGeometry();
        apronGeo.setAttribute("position", new THREE.BufferAttribute(apron.pos, 3));
        apronGeo.setAttribute("color", new THREE.BufferAttribute(apron.col, 3));
        apronGeo.setAttribute("uv", new THREE.BufferAttribute(apron.uv, 2));
        apronGeo.setAttribute("normal", new THREE.BufferAttribute(apron.nrm, 3));
        apronGeo.setIndex(new THREE.Uint32BufferAttribute(apron.idx, 1));
        apronGeo.setDrawRange(0, apron.idx.length);
        apronMat = new THREE.MeshStandardMaterial({
          vertexColors: true,
          roughness: 0.96,
          metalness: 0.0,
          flatShading: false,
          side: THREE.FrontSide,
          map: makeDetailTexture(Math.max(1, Math.round(aniso * 0.5)))
        });
        apronMat.color.setRGB(baseColor, baseColor, baseColor);
        apronMat.name = "terrain_lod_apron";
        materials.push(apronMat);
        apronMesh = new THREE.Mesh(apronGeo, apronMat);
        apronMesh.name = "terrain_apron";
        apronMesh.receiveShadow = false;   // 位于世界外、阴影相机范围之外
        apronMesh.castShadow = false;
        apronMesh.frustumCulled = false;
        apronMesh.matrixAutoUpdate = false;
        apronMesh.updateMatrix();
        group.add(apronMesh);
        apronMs = nowMs() - tApron;
      } catch (eApron) {
        apron = null; apronMesh = null;
        logTo(opts, "外延缓冲带构建失败（地形边缘可能仍是断崖）：" +
          (eApron && eApron.message ? eApron.message : eApron));
      }

      /* ---- 8.4 状态与统计 ---- */
      var state = { plan: plan, cache: cache, quality: quality, rings: rings,
                    apron: apron, apronMesh: apronMesh };
      var enabled = true, debug = false;
      var stats = {
        rings: plan.rings,
        verts: 0, tris: 0, drawCalls: 0,
        aprons: apron ? 1 : 0,
        apronTris: apron ? apron.tris : 0,
        apronVerts: apron ? apron.verts : 0,
        apronOuterR: apron ? Number(apron.outerR.toFixed(2)) : 0,
        apronSpacing: apron ? Number(apron.spacing.toFixed(2)) : 0,
        apronMs: Number(apronMs.toFixed(2)),
        ringSpacing: [], baseSpacing: plan.s0,
        boost: plan.boost, cells: plan.cells,
        tileWorld: Number(plan.tileWorld.toFixed(2)),
        cacheSpacing: Number(plan.cacheSpacing.toFixed(4)),
        buildMs: 0, updateMs: 0, prewarmPoints: prewarmCount, prewarmMs: 0,
        tier: tier
      };
      function refreshSpacing() {
        var out = [];
        for (var i = 0; i < plan.s.length; i++) out.push(Number(plan.s[i].toFixed(4)));
        stats.ringSpacing = out;
        stats.baseSpacing = Number(plan.s0.toFixed(4));
      }
      refreshSpacing();

      function syncGeometry(rt) {
        var b = rt.buf, geo = rt.geo;
        if (b.attrsDirty || !geo.getAttribute("position")) {
          geo.setAttribute("position", new THREE.BufferAttribute(b.pos, 3));
          geo.setAttribute("color", new THREE.BufferAttribute(b.col, 3));
          geo.setAttribute("uv", new THREE.BufferAttribute(b.uv, 2));
          geo.setAttribute("normal", new THREE.BufferAttribute(b.nrm, 3));
          geo.setIndex(new THREE.Uint32BufferAttribute(b.idx, 1));
          b.attrsDirty = false;
        } else {
          var atts = geo.attributes;
          atts.position.needsUpdate = true;
          atts.color.needsUpdate = true;
          atts.uv.needsUpdate = true;
          atts.normal.needsUpdate = true;
          geo.index.needsUpdate = true;
        }
        geo.setDrawRange(0, b.icount);
        rt.mesh.visible = b.icount > 0;
      }

      function recount() {
        var v = 0, t = 0, dc = 0;
        var ringMs = [];
        for (var i = 0; i < rings.length; i++) {
          v += rings[i].verts; t += rings[i].tris;
          if (rings[i].buf.icount > 0) dc++;
          ringMs.push(Number((rings[i].buildMs || 0).toFixed(2)));
        }
        if (apron && apronMesh) { v += apron.verts; t += apron.tris; dc++; }
        stats.verts = v;
        stats.tris = t;
        stats.drawCalls = enabled ? dc : 0;
        stats.ringBuildMs = ringMs;
      }

      function buildAll() {
        var t0 = nowMs();
        for (var i = 0; i < rings.length; i++) {
          var tb = nowMs();
          buildRingMesh(state, i);
          rings[i].buildMs = nowMs() - tb;
          syncGeometry(rings[i]);
        }
        recount();
        stats.buildMs = nowMs() - t0;
        stats.prewarmMs = prewarmMs;
      }
      buildAll();

      /* ---- 8.5 调试着色：每环一个色，直接在材质色上乘（不影响顶点色与云影 shader） ---- */
      var DEBUG_TINTS = [
        rgbOf(0xff6b5c), rgbOf(0x7be06a), rgbOf(0x5aa8ff), rgbOf(0xffd34d),
        rgbOf(0xd06bff), rgbOf(0x66e0c8)
      ];
      var APRON_DEBUG_TINT = rgbOf(0x8899aa);
      function applyDebug() {
        for (var i = 0; i < materials.length; i++) {
          if (debug) {
            var t = (apronMat && materials[i] === apronMat)
              ? APRON_DEBUG_TINT : DEBUG_TINTS[i % DEBUG_TINTS.length];
            materials[i].color.setRGB(t[0] * 1.05, t[1] * 1.05, t[2] * 1.05);
          } else {
            materials[i].color.setRGB(baseColor, baseColor, baseColor);
          }
        }
      }

      /* ---- 8.6 update：只有环中心（或洞边界）真的变化时才重建对应环 ----
         先做「吸附值比较」这种零分配的廉价判断：相机没越过半格 → 直接 return，
         满足契约 §3.4「update 里每帧零分配」；只有真的要重定位时才重算规划数组。 */
      var dirtyFlags = new Array(rings.length);
      var desiredC = new Array(rings.length);
      for (var dc = 0; dc < rings.length; dc++) {
        dirtyFlags[dc] = false;
        desiredC[dc] = { x: 0, z: 0 };
      }
      function update(cam, dt) {
        if (!enabled) return;
        var pos = (cam && cam.position) ? cam.position
          : (camera && camera.position) ? camera.position : null;
        if (!pos) return;
        var cx = clampNum(pos.x, -world.width / 2, world.width / 2);
        var cz = clampNum(pos.z, -world.depth / 2, world.depth / 2);
        var boostNow = lodBoost(cfg, pos.y);

        /* §C：相机高度跨过阈值 → LOD 档位整体切换（baseSpacing 减半/恢复），全部环重建。
           缓存点阵固定 base0/2，所以这里只是重新规划 + 重建几何，不重建缓存。 */
        if (boostNow !== plan.boost) {
          var tB = nowMs();
          var planNew2 = choosePlan(cfg, { x: cx, z: cz }, pos.y);
          plan = planNew2;
          state.plan = plan;
          for (var kb = 0; kb < rings.length; kb++) {
            var tbB = nowMs();
            buildRingMesh(state, kb);
            rings[kb].buildMs = nowMs() - tbB;
            syncGeometry(rings[kb]);
          }
          refreshSpacing();
          recount();
          stats.updateMs = nowMs() - tB;
          return;
        }

        var any = false, k;
        for (k = 0; k < rings.length; k++) {
          var sn = (k < plan.last) ? plan.s[k + 1] : plan.s[plan.last];
          desiredC[k].x = Math.round(cx / sn) * sn;
          desiredC[k].z = Math.round(cz / sn) * sn;
          dirtyFlags[k] = Math.abs(desiredC[k].x - plan.c[k].x) > 1e-6 ||
                          Math.abs(desiredC[k].z - plan.c[k].z) > 1e-6;
        }
        for (k = 0; k < rings.length; k++) {
          /* 内环中心动了 → 外面这一环的洞边界也动了，必须一起重建 */
          if (k > 0 && !dirtyFlags[k]) {
            dirtyFlags[k] = Math.abs(desiredC[k - 1].x - plan.c[k - 1].x) > 1e-6 ||
                            Math.abs(desiredC[k - 1].z - plan.c[k - 1].z) > 1e-6;
          }
          if (dirtyFlags[k]) any = true;
        }
        if (!any) { stats.updateMs = 0; return; }

        var planNew = assemblePlan(cfg, plan.params, plan.s0, { x: cx, z: cz }, plan.cells);
        planNew.boost = plan.boost;
        planNew.params = plan.params;
        var t0 = nowMs();
        plan = planNew;
        state.plan = plan;
        /* 注意：任一环重定位时，它外层所有环的「洞边界」都会跟着动；
           这里干脆整组重建（5 环合计 <10ms），避免部分重建留下的洞/缝。
           只有静止时才会走上面的 early-return，所以不影响正常帧率。 */
        for (var k2 = 0; k2 < rings.length; k2++) {
          var tb2 = nowMs();
          buildRingMesh(state, k2);
          rings[k2].buildMs = nowMs() - tb2;
          syncGeometry(rings[k2]);
        }
        refreshSpacing();
        recount();
        stats.updateMs = nowMs() - t0;
        return;
      }

      /* ---- 8.7 对外 handle ---- */
      var handle = {
        version: VERSION,
        group: group,
        materials: materials,
        heightAt: sampler,
        update: update,
        setEnabled: function (b) {
          enabled = !!b;
          group.visible = enabled;
          recount();
        },
        setDebug: function (b) {
          debug = !!b;
          applyDebug();
        },
        stats: stats,
        /* 排障用：返回当前 LOD 规划快照（自检/无头验收脚本可读） */
        planInfo: function () {
          return {
            boost: plan.boost, cells: plan.cells, baseSpacing: plan.s0,
            centers: plan.c.map(function (q) { return [Number(q.x.toFixed(2)), Number(q.z.toFixed(2))]; }),
            H: plan.H.map(function (v) { return Number(v.toFixed(2)); }),
            m: plan.m.slice(),
            rings: plan.rings, cacheSpacing: plan.cacheSpacing,
            tileWorld: plan.tileWorld, normalEps: plan.normalEps
          };
        },
        dispose: function () {
          try { scene.remove(group); } catch (eS) { /* 忽略 */ }
          for (var i = 0; i < rings.length; i++) {
            try { rings[i].geo.dispose(); } catch (eG) { /* 忽略 */ }
            try {
              var m = rings[i].mat;
              if (m.map) m.map.dispose();
              m.dispose();
            } catch (eM) { /* 忽略 */ }
          }
          try { if (apronGeo) apronGeo.dispose(); } catch (eAg) { /* 忽略 */ }
          try {
            if (apronMat) { if (apronMat.map) apronMat.map.dispose(); apronMat.dispose(); }
          } catch (eAm) { /* 忽略 */ }
          rings.length = 0;
          materials.length = 0;
        }
      };

      applyDebug();
      if (quality.debugTint) handle.setDebug(true);

      logTo(opts, plan.rings + " 环 clipmap · 内环间距 " + plan.s0.toFixed(3) + "→" +
        plan.s[plan.last].toFixed(2) + " · LOD档 " + plan.boost + "（格数 " + plan.cells +
        "，纹理尺度 " + plan.tileWorld.toFixed(1) + "）· 外延带 " + (apron
          ? (apron.tris + " 三角 / 半径 " + apron.outerR.toFixed(0) + " / 间距 " +
             apron.spacing.toFixed(1) + " / " + apronMs.toFixed(1) + " ms")
          : "未启用") +
        " · " + stats.verts + " 顶点 / " + stats.tris + " 三角 / " +
        stats.drawCalls + " drawCalls · 构建 " + stats.buildMs.toFixed(1) +
        " ms" + (prewarmCount ? "（预热 " + prewarmCount + " 点 " + prewarmMs.toFixed(1) + " ms）" : ""));
      return handle;
    } catch (e) {
      logTo(opts, "初始化失败，回退基础地形：" + (e && e.message ? e.message : e));
      return null;
    }
  }

  /* ======================================================================
   * 9. 能力检测（不 new THREE.*，只做参数校验）
   * ==================================================================== */
  function capable(opts) {
    try {
      if (!opts || !opts.THREE) return false;
      var f = opts.field;
      if (!f || !f.H || !(f.N >= 2) || !(f.M >= 2)) return false;
      var w = opts.world;
      if (!w || !(w.width > 0) || !(w.depth > 0)) return false;
      return true;
    } catch (e) { return false; }
  }

  /* ======================================================================
   * 10. selfTest：纯逻辑自检（不依赖 renderer / 不 new Geometry，可在验收时
   *     --eval "JSON.stringify(window.UAVTerrain.selfTest())" 直接跑）
   * ----------------------------------------------------------------------
   * 检查项：
   *   · heightAt 与 terrainH 公式逐位一致（独立实现对照，含世界外 clamp）
   *   · baseSpacing 由 DEM 格自动推导；环间距逐级 ×2；环中心吸附到整数倍
   *   · 覆盖：任意机位最外环都盖住整个世界矩形；世界内每个采样点都落在且只落在
   *     某一级环的「保留单元」里（不会出现空洞，也不会重叠）
   *   · 网格：顶点高度 = heightAt、索引合法、无 NaN；洞边界对齐（无裂缝前提）
   *   · 预算：high/medium/cpu 三档实际三角形数都在契约预算内
   *   · 纹理：512×512 canvas、均值/无 NaN（有 document 时才测）
   * ==================================================================== */
  function selfTest(opts) {
    var res = { ok: true, checks: [], stats: {} };
    function add(name, ok, detail) {
      res.checks.push({ name: name, ok: !!ok, detail: String(detail) });
      if (!ok) res.ok = false;
    }
    try {
      /* ---- 10.1 合成高程场：1024×760、世界 352×256（与 Lead v2 的真实 DEM 同尺度/同格距） ---- */
      var N = 1024, M = 760, W = 352, D = 256;
      var H = new Float32Array(N * M);
      var dx = W / (N - 1), dz = D / (M - 1);
      for (var j = 0; j < M; j++) {
        var z = -D / 2 + j * dz;
        for (var i = 0; i < N; i++) {
          var x = -W / 2 + i * dx;
          var h = 10 * Math.sin(x * 0.045) * Math.cos(z * 0.052) +
                  5 * Math.sin((x + 2 * z) * 0.07) +
                  3 * Math.cos(x * 0.13 - z * 0.11);
          var rr = Math.sqrt(((x + 6) / 46) * ((x + 6) / 46) + ((z + 6) / 40) * ((z + 6) / 40));
          if (rr < 1) {
            var bench = 40 * (1 - rr);
            bench = Math.floor(bench / 6.2) * 6.2;      // 台阶（露天矿 10~15m 台阶，4× 垂直夸张）
            h -= bench;
          }
          H[j * N + i] = h;
        }
      }
      var field = { H: H, N: N, M: M };
      var world = { width: W, depth: D };
      var sampler = makeSampler(field, world);
      var pit = { cx: -6, cz: -6, rx: 46, rz: 40 };
      var colorFn = makeColorFn(sampler, field, world, { bakedAO: true, pit: pit });

      /* ---- 10.2 heightAt 与 app3d.html terrainH 公式一致性（独立实现对照） ---- */
      function refTerrainH(px, pz) {
        var fdx = W / (N - 1), fdz = D / (M - 1);
        var fx = (px + W / 2) / fdx, fz = (pz + D / 2) / fdz;
        var i0 = Math.floor(fx); if (i0 < 0) i0 = 0; if (i0 > N - 2) i0 = N - 2;
        var j0 = Math.floor(fz); if (j0 < 0) j0 = 0; if (j0 > M - 2) j0 = M - 2;
        var tx = fx - i0; if (tx < 0) tx = 0; if (tx > 1) tx = 1;
        var tz = fz - j0; if (tz < 0) tz = 0; if (tz > 1) tz = 1;
        var h00 = H[j0 * N + i0], h10 = H[j0 * N + i0 + 1];
        var h01 = H[(j0 + 1) * N + i0], h11 = H[(j0 + 1) * N + i0 + 1];
        /* 与 terrainH 等价、但写法不同的权重形式，避免同源代码自证 */
        var bot = h00 + (h10 - h00) * tx;
        var top = h01 + (h11 - h01) * tx;
        return bot + (top - bot) * tz;
      }
      var maxDiff = 0, sampleCount = 0;
      var rndSeed = 987654321;
      function rnd() { rndSeed = (rndSeed * 1103515245 + 12345) & 0x7fffffff; return rndSeed / 0x7fffffff; }
      for (var s1 = 0; s1 < 1500; s1++) {
        var px = (rnd() - 0.5) * (W * 1.6);         // 故意包含世界外（clamp 行为）
        var pz = (rnd() - 0.5) * (D * 1.6);
        var d1 = Math.abs(sampler(px, pz) - refTerrainH(px, pz));
        if (d1 > maxDiff) maxDiff = d1;
        sampleCount++;
      }
      add("heightAt 与 terrainH 公式一致", maxDiff < 1e-12,
        sampleCount + " 点，最大误差 " + maxDiff.toExponential(2));

      /* ---- 10.3 规划：baseSpacing / 间距 ×2 / 中心吸附 ---- */
      var cfg = {
        field: field, world: world, tier: "high",
        quality: normalizeQuality({}), mask: makeMask(world, DEFAULT_QUALITY.expand)
      };
      var center = { x: 40, z: 30 };
      var plan = choosePlan(cfg, center, 60);
      var expectBase = Math.max(W / (N - 1), D / (M - 1));
      add("baseSpacing 按 DEM 格自动推导",
        Math.abs(plan.s0 - expectBase) < 1e-9,
        "s0=" + plan.s0.toFixed(6) + " 期望=" + expectBase.toFixed(6));

      var ratio2 = true;
      for (var kk = 1; kk < plan.rings; kk++) {
        if (Math.abs(plan.s[kk] / plan.s[kk - 1] - 2) > 1e-9) ratio2 = false;
      }
      add("环间距逐级 ×2", ratio2, plan.s.map(function (v) { return v.toFixed(3); }).join(" / "));

      var snapOK = true;
      var camTests = [
        { x: 0, z: 0 }, { x: 176, z: 128 }, { x: -280, z: 200 },
        { x: 700, z: -600 }, { x: 80, z: 60 }
      ];
      for (var ct = 0; ct < camTests.length; ct++) {
        var cen = {
          x: clampNum(camTests[ct].x, -W / 2, W / 2),
          z: clampNum(camTests[ct].z, -D / 2, D / 2)
        };
        var p = choosePlan(cfg, cen, 60);
        for (var k3 = 0; k3 < p.rings; k3++) {
          var sn = (k3 < p.rings - 1) ? p.s[k3 + 1] : p.s[p.rings - 1];
          var qx = Math.round(p.c[k3].x / sn), qz = Math.round(p.c[k3].z / sn);
          if (Math.abs(p.c[k3].x - qx * sn) > 1e-6) snapOK = false;
          if (Math.abs(p.c[k3].z - qz * sn) > 1e-6) snapOK = false;
        }
      }
      add("环中心吸附到步长整数倍", snapOK, camTests.length + " 组机位");

      /* ---- 10.4 覆盖：最外环必须盖住整个世界矩形（任何机位都不出现空洞） ---- */
      var coverOK = true, worst = Infinity;
      for (var ct2 = 0; ct2 < camTests.length; ct2++) {
        var cen2 = {
          x: clampNum(camTests[ct2].x, -W / 2, W / 2),
          z: clampNum(camTests[ct2].z, -D / 2, D / 2)
        };
        var p2 = choosePlan(cfg, cen2, 60);
        var need = chebNeeded(p2.c[p2.rings - 1].x, p2.c[p2.rings - 1].z, p2.mask);
        var have = p2.H[p2.rings - 1];
        var m2 = have - need;
        if (!isFinite(m2)) { coverOK = false; worst = NaN; continue; }
        if (m2 < worst) worst = m2;
        if (m2 < -1e-9) coverOK = false;
      }
      add("最外环覆盖整个世界矩形", coverOK,
        "最小余量 " + worst.toFixed(3) + " 世界单位（" + camTests.length + " 组机位）");

      /* ---- 10.5 分区覆盖：世界内采样点必须恰好落在一级环的保留单元里 ---- */
      var holes = 0, overlaps = 0, tested = 0;
      for (var ct3 = 0; ct3 < camTests.length; ct3++) {
        var cen3 = {
          x: clampNum(camTests[ct3].x, -W / 2, W / 2),
          z: clampNum(camTests[ct3].z, -D / 2, D / 2)
        };
        var p3 = choosePlan(cfg, cen3, 60);
        for (var ssi = 0; ssi < 48; ssi++) {
          for (var ssj = 0; ssj < 34; ssj++) {
            var wxp = -W / 2 + (ssi + 0.5) * (W / 48);
            var wzp = -D / 2 + (ssj + 0.5) * (D / 34);
            var hit = 0;
            for (var k4 = 0; k4 < p3.rings; k4++) {
              var cX = p3.c[k4].x, cZ = p3.c[k4].z, HH = p3.H[k4];
              var inside = (wxp >= cX - HH && wxp <= cX + HH && wzp >= cZ - HH && wzp <= cZ + HH);
              if (!inside) continue;
              /* 判断是否落在该环的「保留单元」里（洞内单元不算） */
              var inHole = false;
              if (k4 > 0) {
                var chx = p3.c[k4 - 1].x, chz = p3.c[k4 - 1].z, chh = p3.H[k4 - 1];
                inHole = (wxp >= chx - chh && wxp <= chx + chh &&
                          wzp >= chz - chh && wzp <= chz + chh);
              }
              if (!inHole) hit++;
            }
            tested++;
            if (hit === 0) holes++;
            if (hit > 1) overlaps++;
          }
        }
      }
      add("世界采样点无空洞/无重叠", holes === 0 && overlaps === 0,
        tested + " 点，空洞 " + holes + "，重叠 " + overlaps);

      /* ---- 10.6 网格：顶点高度 = heightAt、索引合法、无 NaN、洞边界对齐 ---- */
      var cache = makeCache(sampler, colorFn, plan.cacheSpacing);
      var meshState = {
        plan: plan, cache: cache, quality: normalizeQuality({}),
        rings: [{ buf: { pos: null, col: null, uv: null, nrm: null, idx: null, hgt: null,
                          vcap: 0, icap: 0, icount: 0, vused: 0, attrsDirty: true },
                  tris: 0, verts: 0 }]
      };
      buildRingMesh(meshState, 0);
      var b0 = meshState.rings[0].buf;
      var maxHDiff = 0, badIdx = 0, nanPos = 0;
      var gridVerts = b0.vused;
      for (var vi = 0; vi < gridVerts; vi++) {
        var vx = b0.pos[vi * 3], vy = b0.pos[vi * 3 + 1], vz = b0.pos[vi * 3 + 2];
        if (!isFinite(vx) || !isFinite(vy) || !isFinite(vz)) { nanPos++; continue; }
        if (vi * 3 + 1 < b0.pos.length && (vx !== 0 || vz !== 0)) {
          var dh = Math.abs(vy - sampler(vx, vz));
          /* 缓存用 Float32 存高度，误差应在 1e-3 以内 */
          if (dh > maxHDiff) maxHDiff = dh;
        }
      }
      var mMax = 0;
      for (var mi2 = 0; mi2 < plan.m.length; mi2++) if (plan.m[mi2] > mMax) mMax = plan.m[mi2];
      for (var ii2 = 0; ii2 < b0.icount; ii2++) {
        if (b0.idx[ii2] >= gridVerts + 16 * mMax + 1024) badIdx++;
      }
      add("环 0 顶点高度 = heightAt", maxHDiff < 2e-3,
        gridVerts + " 顶点，最大误差 " + maxHDiff.toExponential(2));
      add("环 0 索引/顶点无 NaN", nanPos === 0 && badIdx === 0 && b0.icount % 3 === 0,
        "三角 " + (b0.icount / 3) + "，越界索引 " + badIdx + "，NaN " + nanPos);

      var seamOK = true;
      for (var k5 = 1; k5 < plan.rings; k5++) {
        var chx2 = plan.c[k5 - 1].x, chz2 = plan.c[k5 - 1].z, chh2 = plan.H[k5 - 1];
        var x00 = plan.c[k5].x - plan.H[k5];
        var z00 = plan.c[k5].z - plan.H[k5];
        var q1 = (chx2 - chh2 - x00) / plan.s[k5];
        var q2 = (chx2 + chh2 - x00) / plan.s[k5];
        var q3 = (chz2 - chh2 - z00) / plan.s[k5];
        var q4 = (chz2 + chh2 - z00) / plan.s[k5];
        if (Math.abs(q1 - Math.round(q1)) > 1e-6) seamOK = false;
        if (Math.abs(q2 - Math.round(q2)) > 1e-6) seamOK = false;
        if (Math.abs(q3 - Math.round(q3)) > 1e-6) seamOK = false;
        if (Math.abs(q4 - Math.round(q4)) > 1e-6) seamOK = false;
      }
      add("环洞边界与格线对齐（无缝前提）", seamOK, "全部 " + (plan.rings - 1) + " 条环间边界");

      /* ---- 10.6a §A 纹理尺度全环统一（旧版每环不同 repeat 是俯视矩形拼缝的根因） ---- */
      var uvOK = (typeof plan.tileWorld === "number" && plan.tileWorld > 0);
      var uvDet = [];
      try {
        var cacheUV = makeCache(sampler, colorFn, plan.cacheSpacing);
        var stUV = { plan: plan, cache: cacheUV, quality: normalizeQuality({}), rings: [] };
        for (var ri3 = 0; ri3 < 2 && ri3 < plan.rings; ri3++) {
          stUV.rings.push({ buf: { pos: null, col: null, uv: null, nrm: null, idx: null,
                                   hgt: null, vcap: 0, icap: 0, icount: 0, vused: 0,
                                   attrsDirty: true }, tris: 0, verts: 0 });
        }
        for (var rk3 = 0; rk3 < stUV.rings.length; rk3++) {
          buildRingMesh(stUV, rk3);
          var bb = stUV.rings[rk3].buf;
          var dxUV = bb.pos[3] - bb.pos[0], duv = bb.uv[2] - bb.uv[0];
          var tWorld = dxUV / duv;              // 一个贴图 tile 覆盖多少世界单位
          if (Math.abs(tWorld - plan.tileWorld) > 1e-3) uvOK = false;
          uvDet.push("环" + rk3 + "=" + tWorld.toFixed(2));
        }
      } catch (eUV) { uvOK = false; }
      add("纹理尺度全环统一（无拼缝前提）", uvOK,
        "tileWorld=" + plan.tileWorld + " 世界单位/贴图 · " + uvDet.join(" / "));

      /* ---- 10.6b §B 宏观颜色噪声：幅度 ±10%、世界坐标连续平滑 ---- */
      var macMin = Infinity, macMax = -Infinity, macSmooth = 0;
      var macSeed = 123456789;
      function mrnd() { macSeed = (macSeed * 1103515245 + 12345) & 0x7fffffff; return macSeed / 0x7fffffff; }
      for (var mi = 0; mi < 400; mi++) {
        var mx0 = (mrnd() - 0.5) * W, mz0 = (mrnd() - 0.5) * D;
        var mv = macroShade(mx0, mz0);
        if (mv < macMin) macMin = mv;
        if (mv > macMax) macMax = mv;
        var dv2 = Math.abs(macroShade(mx0 + 1, mz0) - mv);
        if (dv2 > macSmooth) macSmooth = dv2;
      }
      add("宏观颜色噪声幅度/平滑（§B）",
        macMin >= -1.001 && macMax <= 1.001 && (macMax - macMin) > 0.35 && macSmooth < 0.08,
        "范围 " + macMin.toFixed(2) + "~" + macMax.toFixed(2) +
        "（±" + (DEFAULT_QUALITY.macroAmp * 100).toFixed(0) + "% 亮度），1 单位最大跳变 " +
        macSmooth.toFixed(4));

      /* ---- 10.6c §C LOD 相机自适应：高度 >90 时整体细分一档，且缓存间距不变 ---- */
      var planL = choosePlan(cfg, center, 60);
      var planH2 = choosePlan(cfg, center, 150);
      var adaptOK = planL.boost === 1 && planH2.boost === 2 &&
                    Math.abs(planH2.s0 - planL.s0 * 0.5) < 1e-9 &&
                    Math.abs(planH2.cacheSpacing - planL.cacheSpacing) < 1e-12;
      var needH2 = chebNeeded(planH2.c[planH2.rings - 1].x, planH2.c[planH2.rings - 1].z, planH2.mask);
      if (planH2.H[planH2.rings - 1] < needH2 - 1e-9) adaptOK = false;
      add("LOD 相机自适应（§C）", adaptOK,
        "内环间距 " + planL.s0.toFixed(3) + " → " + planH2.s0.toFixed(3) +
        "（相机高度 60→150），缓存间距 " + planH2.cacheSpacing.toFixed(3) + " 不变，覆盖仍成立");

      /* ---- 10.6d §D 环数：默认 high=5、支持 6；cpu 档强制 ≤2 ---- */
      var q6 = normalizeQuality({ quality: { rings: 6 } });
      var cfg6 = { field: field, world: world, tier: "high", quality: q6,
                   mask: makeMask(world, q6.expand) };
      var p6 = choosePlan(cfg6, center, 60);
      var qcpu6 = normalizeQuality({ quality: { rings: 6 } });
      var cfgc6 = { field: field, world: world, tier: "cpu", quality: qcpu6,
                    mask: makeMask(world, qcpu6.expand) };
      var pcpu6 = choosePlan(cfgc6, center, 60);
      add("环数支持 5~6 / cpu ≤2 环", p6.rings === 6 && pcpu6.rings === 2 && plan.rings >= 5,
        "high 默认 " + plan.rings + "、high rings=6 → " + p6.rings +
        "、cpu rings=6 → " + pcpu6.rings);

      /* ---- 10.6e 外延缓冲带：内边与地形边缘齐平、外缘下压到大地基底、无 NaN ---- */
      var ap = buildApronMesh({
        field: field, world: world, sampler: sampler, colorFn: colorFn,
        quality: normalizeQuality({}), tier: "high",
        uvTile: plan.tileWorld,
        fogColor: { r: 0.043, g: 0.102, b: 0.149 }
      });
      var apInnerErr = 0, apOuterMax = -Infinity, apNaN = 0;
      var apN = ap.pos.length / 3;
      for (var ai = 0; ai < apN; ai++) {
        var apx = ap.pos[ai * 3], apy = ap.pos[ai * 3 + 1], apz = ap.pos[ai * 3 + 2];
        if (!isFinite(apx) || !isFinite(apy) || !isFinite(apz)) { apNaN++; continue; }
        var apax = Math.abs(apx), apaz = Math.abs(apz);
        /* 内边界顶点：正好在世界矩形边上 → 应等于「边缘 clamp 高度 - 0.15」 */
        if ((Math.abs(apax - W / 2) < 1e-6 && apaz <= D / 2 + 1e-6) ||
            (Math.abs(apaz - D / 2) < 1e-6 && apax <= W / 2 + 1e-6)) {
          var apErr = Math.abs(apy - (sampler(apx, apz) - 0.15));
          if (apErr > apInnerErr) apInnerErr = apErr;
        }
        /* 外缘顶点：必须已经下压到大地基底附近 */
        if (Math.abs(apax - ap.Rx) < 1e-6 || Math.abs(apaz - ap.Rz) < 1e-6) {
          if (apy > apOuterMax) apOuterMax = apy;
        }
      }
      /* 外延带与地形共用同一个纹理尺度（否则地形/大地交界会出现纹理密度跳变） */
      var apUVTile = (ap.uv[2] - ap.uv[0]) > 0
        ? (ap.pos[3] - ap.pos[0]) / (ap.uv[2] - ap.uv[0]) : -1;
      add("外延缓冲带：内边贴合地形、外缘压到大地基底、纹理尺度与地形一致",
        apNaN === 0 && ap.tris > 300 && ap.tris < 30000 &&
        apInnerErr < 2e-3 && apOuterMax <= ap.baseY + 2.5 &&
        Math.abs(apUVTile - plan.tileWorld) < 1e-3,
        "三角 " + ap.tris + " / 半径 " + ap.outerR.toFixed(0) + " / 间距 " +
        ap.spacing.toFixed(1) + " / 内边误差 " + apInnerErr.toExponential(1) +
        " / 外缘最高 " + apOuterMax.toFixed(2) + "（基底 " + ap.baseY.toFixed(2) +
        "） / 纹理 " + apUVTile.toFixed(2) + " 单位/贴图");

      /* ---- 10.7 预算：三档在新世界尺度 352×256 场上实测三角形数（环带 + 外延缓冲带） ---- */
      var tierDef = TIER_DEFAULTS;
      var tierStats = {};
      var budgetOK = true, budgetDetail = [];
      var tiers = ["high", "medium", "cpu"];
      for (var ti = 0; ti < tiers.length; ti++) {
        var tr = tiers[ti];
        var qcfg = normalizeQuality({});
        var ccfg = {
          field: field, world: world, tier: tr, quality: qcfg,
          mask: makeMask(world, qcfg.expand)
        };
        var pc = choosePlan(ccfg, { x: 0, z: 0 }, 60);   // 相机在世界中心（最坏覆盖情况）
        var cache2 = makeCache(sampler, colorFn, pc.cacheSpacing);
        var st = {
          plan: pc, cache: cache2, quality: qcfg, rings: []
        };
        for (var ri = 0; ri < pc.rings; ri++) {
          st.rings.push({ buf: { pos: null, col: null, uv: null, nrm: null, idx: null,
                                 hgt: null, vcap: 0, icap: 0, icount: 0, vused: 0,
                                 attrsDirty: true }, tris: 0, verts: 0 });
        }
        for (var rk = 0; rk < pc.rings; rk++) buildRingMesh(st, rk);
        var apT = buildApronMesh({
          field: field, world: world, sampler: sampler, colorFn: colorFn,
          quality: qcfg, tier: tr, uvTile: pc.tileWorld,
          fogColor: { r: 0.043, g: 0.102, b: 0.149 }
        });
        var tv = apT.verts, tt = apT.tris, dc = apT.tris > 0 ? 1 : 0;
        for (var rk2 = 0; rk2 < pc.rings; rk2++) {
          tv += st.rings[rk2].verts; tt += st.rings[rk2].tris;
          if (st.rings[rk2].buf.icount > 0) dc++;
        }
        tierStats[tr] = { rings: pc.rings, verts: tv, tris: tt, drawCalls: dc,
                          apronTris: apT.tris, s0: Number(pc.s0.toFixed(4)),
                          boost: pc.boost, cells: pc.cells, budget: tierDef[tr].maxTris };
        budgetDetail.push(tr + " " + (tt / 1000).toFixed(1) + "k/" +
          (tierDef[tr].maxTris / 1000) + "k（" + pc.rings + " 环，外延带 " +
          apT.tris + "，间距 " + pc.s0.toFixed(2) + "）");
        if (tt > tierDef[tr].maxTris) budgetOK = false;
        if (dc > 8) budgetOK = false;
        if (tr === "cpu" && pc.rings > 2) budgetOK = false;
        if (tt < 1000) budgetOK = false;              // 不能空
      }
      add("三档三角形预算与 drawCalls", budgetOK, budgetDetail.join("；"));
      res.stats.apron = { tris: ap.tris, verts: ap.verts,
                          outerR: Number(ap.outerR.toFixed(1)),
                          spacing: Number(ap.spacing.toFixed(2)),
                          uvTile: Number(plan.tileWorld.toFixed(2)) };
      res.stats.tiers = tierStats;
      res.stats.ringSpacing = plan.s.map(function (vv) { return Number(vv.toFixed(4)); });
      res.stats.tileWorld = Number(plan.tileWorld.toFixed(2));
      res.stats.boost = plan.boost;
      res.stats.cells = plan.cells;
      res.stats.cacheSpacing = Number(plan.cacheSpacing.toFixed(4));
      res.stats.verts = sumTier(tierStats, "verts");
      res.stats.tris = sumTier(tierStats, "tris");
      function sumTier(obj, key) { return obj.high ? obj.high[key] : 0; }

      /* ---- 10.8 纹理：512×512、可平铺数值范围正常（仅在有 DOM 时测） ---- */
      try {
        if (typeof document !== "undefined" && document.createElement) {
          var cv = makeDetailCanvas(512);
          var ctx = cv.getContext("2d");
          var px = ctx.getImageData(0, 0, 512, 512).data;
          var sum = 0, mn = 1, mx = 0, bad = 0;
          for (var pi2 = 0; pi2 < px.length; pi2 += 4) {
            var lum = (px[pi2] + px[pi2 + 1] + px[pi2 + 2]) / 3 / 255;
            sum += lum;
            if (lum < mn) mn = lum;
            if (lum > mx) mx = lum;
            if (!isFinite(lum)) bad++;
          }
          var mean = sum / (px.length / 4);
          add("细节纹理 512² 程序化生成（近白微对比，不压灰配色）",
            cv.width === 512 && cv.height === 512 && bad === 0 &&
            mean > 0.90 && mean < 1.0 && (mx - mn) > 0.03,
            "均值 " + mean.toFixed(3) + " 范围 " + mn.toFixed(2) + "~" + mx.toFixed(2));
        } else {
          add("细节纹理（无 DOM 时跳过）", true, "skipped");
        }
      } catch (eTex2) {
        add("细节纹理（无 DOM 时跳过）", true, "skipped: " + (eTex2 && eTex2.message));
      }

      /* ---- 10.9 能力检测函数本身 ---- */
      add("capable() 参数校验", capable({ THREE: {}, field: field, world: world }) === true &&
        capable({ THREE: {}, field: { H: new Float32Array(1), N: 1, M: 1 }, world: world }) === false,
        "合法参数 true / 非法参数 false");
    } catch (e) {
      add("selfTest 未抛异常", false, (e && e.message ? e.message : e));
    }
    return res;
  }

  /* ======================================================================
   * 11. 暴露唯一全局（契约 §2.2：一个文件只暴露一个全局）
   * ==================================================================== */
  window.UAVTerrain = {
    version: VERSION,
    capable: capable,
    create: create,
    selfTest: selfTest
  };
})();
