/* =============================================================================
 * ssao.js —— 真·屏幕空间环境光遮蔽（SSAO）后处理 · v1.0.0
 * 无人机集群调度仿真系统 / 3D 渲染升级模块 ①
 * -----------------------------------------------------------------------------
 * 为什么需要它：
 *   升级前的地形/山体 AO 是「顶点烘焙」的——只在建模阶段算一次，所以无人机贴地
 *   飞过、箱子落地、树根扎进土里这些**动态关系**完全没有接触阴影，画面发"飘"。
 *   真 SSAO 每帧从深度缓冲反投影出视空间位置，在屏幕空间里查"附近有没有东西挡住
 *   天光"，于是任意动态物体一接触就自动出现暗边。
 *
 * 本模块是一个**经典脚本**（非 ES Module），运行环境 three.js r128（全局 THREE）：
 *   <script src="/static/CopyShader.js"></script>        ... 后处理基础脚本
 *   <script src="/static/EffectComposer.js"></script>    ← 全局脚本版（提供 THREE.Pass /
 *                                                          THREE.FullScreenQuad）
 *   <script src="/static/ssao.js"></script>              ← 本文件
 *   ...
 *   composer = new THREE.EffectComposer(renderer);       // Lead 侧（app3d.html）
 *   composer.addPass(new THREE.RenderPass(scene, camera));
 *   composer.addPass(new THREE.UnrealBloomPass(...));
 *   var ssao = window.UAVSSAO.install({ THREE, renderer, scene, camera, composer,
 *                                       tier, quality, log });   // → handle | null
 *   // resize 时：ssao && ssao.setSize(innerWidth, innerHeight);
 *   // 每帧（可选）：ssao.update(dt)
 *
 * 实现要点（每一条都对应一个真实踩过的坑）：
 *   ① 法线+深度预渲染只用**一次** scene.overrideMaterial = MeshNormalMaterial，
 *      渲完**立刻**清空。忘清 → 主场景整幅变"法线色"，是最典型的事故。
 *   ② MeshNormalMaterial 输出的是**视空间**法线（normalMatrix 变换），打包在 [0,1]；
 *      而法线必须线性，所以预渲染期间临时把 renderer.outputEncoding 设成 Linear，
 *      否则 sRGB 编码会把法线扭成错误方向 → AO 斑驳。
 *   ③ 深度值是非线性的，必须用「uv+窗口深度」构 clip，乘 projectionMatrixInverse
 *      再齐次除，才是视空间坐标；直接当线性深度用会得到弯曲的 AO。
 *   ④ 深度=1（天空/远平面）的像素 AO 必须为 1，否则屏幕上会出现整片黑斑；
 *      另外超过 maxViewZ 的远景也强制 AO=1（屏幕空间 AO 在远处误差大）。
 *   ⑤ 半分辨率算 AO + 双边（深度感知）模糊 + 双线性上采样：16~24 个样本在 1080p
 *      全分辨率算太贵，半分辨率省 4 倍填充率，模糊把逐像素随机旋转的噪声抹平成
 *      连续的接触阴影，且深度权重保证前景/背景不会互相糊。
 *   ⑥ 透明的/加色的点线精灵（粒子、光锥、标签牌）如果被 overrideMaterial 渲进
 *      深度缓冲，会在 AO 里变成"幽灵遮挡"（空中黑块）。预渲染前把这类对象临时
 *      visible=false，渲完恢复——每帧现扫一遍场景，才不会和页面的显隐切换打架。
 *   ⑦ 阴影贴图在本帧 RenderPass 里已经渲过了，预渲染时临时关掉
 *      shadowMap.autoUpdate，避免 30 架机 + 地形的阴影再白白渲一遍。
 *   ⑧ 异常安全：render() 全程 try/catch/finally，无论出什么事都恢复
 *      scene.overrideMaterial / visible / renderTarget / autoClear；
 *      连续异常自动禁用本 pass，最多打 3 行日志，绝不把主场景搞崩。
 *   ⑨ 每帧零分配：所有 uniform / RT / 矩阵 / 数组在 install 期建好，render 里
 *      只做赋值和 set()；对象隐藏列表复用预分配数组。
 *
 * 对外只暴露一个全局：window.UAVSSAO = { version, capable, install, selfTest }
 * ========================================================================== */
(function () {
  "use strict";

  var THREE = window.THREE;
  if (!THREE) {
    /* 脚本加载期不许抛异常，也不许 new THREE.*；这里只做一次安全告警 */
    try { console.warn("[SSAO] 未找到全局 THREE（three.min.js 未加载），模块不启用"); } catch (e) {}
    return;
  }

  /* ==========================================================================
   * 0. 常量 / 模块级状态
   * ======================================================================== */
  var VERSION = "1.0.0";
  var PREFIX = "[SSAO]";
  var NOISE_SIZE = 4;              // 4×4 逐像素随机旋转贴图
  var MAX_SAMPLES = 24;            // 半球核上限（契约：16~24）
  var MIN_SAMPLES = 16;            // 半球核下限
  var DEFAULT_SAMPLES_HIGH = 24;   // high 档没指定 samples 时用 24
  var DEFAULT_SAMPLES_MEDIUM = 16; // medium 档 16
  var MAX_HIDDEN = 512;            // 预渲染时需要临时隐藏的对象数组容量
  var BLUR_RADIUS = 2;             // 双边模糊核 [-2..2] 共 5 tap（可分离：横竖各一次）

  var lastPass = null;             // 最近一次 install 的 pass，仅供 selfTest() 读运行期 stats

  /* ==========================================================================
   * 1. 小工具（全部在 install 期调用；render 路径不产生新对象）
   * ======================================================================== */

  /** mulberry32：确定性随机数，保证每次装出来的半球核/噪声贴图一致，自检可复现 */
  function makeRng(seed) {
    var s = seed >>> 0;
    return function () {
      s = (s + 0x6D2B79F5) >>> 0;
      var t = s;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function clampNum(v, lo, hi, fallback) {
    var n = Number(v);
    if (!isFinite(n)) n = fallback;
    return Math.max(lo, Math.min(hi, n));
  }

  /** 日志：opts.log 优先；任何情况下都不让日志把主场景带崩 */
  function makeLogger(custom) {
    var fn = (typeof custom === "function") ? custom : function (msg) {
      try { if (window.console && console.log) console.log(msg); } catch (e) {}
    };
    return function (msg) {
      try { fn(PREFIX + " " + msg); } catch (e) {}
    };
  }

  /**
   * 生成半球采样核。
   * 样本 z ∈ [0,1] 保证在法线正半球内；长度按 i² 从 0.12 渐变到 1.0，
   * 让样本更集中在接触点附近（经典做法，接触阴影更锐、远处更均匀）。
   */
  function buildKernel(count, rng) {
    var out = [];
    var n = Math.max(1, count | 0);
    for (var i = 0; i < n; i++) {
      var v = new THREE.Vector3(rng() * 2 - 1, rng() * 2 - 1, rng());
      v.normalize();                                  // 单位半球方向
      var t = (i + 1) / n;
      var scale = 0.12 + 0.88 * t * t;                // 内密外疏
      v.multiplyScalar(scale * (0.82 + rng() * 0.18));
      out.push(v);
    }
    return out;
  }

  /**
   * 4×4 随机旋转贴图：把固定半球核按像素随机旋转，防止出现规则的条带/摩尔纹。
   * xy 存单位圆内的随机方向，z=0（切平面内），shader 里会剔除法线分量再正交化。
   */
  function buildNoiseData(rng) {
    var n = NOISE_SIZE * NOISE_SIZE;
    var data = new Uint8Array(n * 4);
    for (var i = 0; i < n; i++) {
      var x = rng() * 2 - 1;
      var y = rng() * 2 - 1;
      var len = Math.sqrt(x * x + y * y) || 1;
      x /= len;
      y /= len;
      data[i * 4 + 0] = Math.floor((x * 0.5 + 0.5) * 255);
      data[i * 4 + 1] = Math.floor((y * 0.5 + 0.5) * 255);
      data[i * 4 + 2] = 128;                          // z ≈ 0
      data[i * 4 + 3] = 255;
    }
    return data;
  }

  function makeNoiseTexture(data) {
    var tex = new THREE.DataTexture(data, NOISE_SIZE, NOISE_SIZE,
      THREE.RGBAFormat, THREE.UnsignedByteType);
    tex.wrapS = THREE.RepeatWrapping;
    tex.wrapT = THREE.RepeatWrapping;
    tex.magFilter = THREE.NearestFilter;
    tex.minFilter = THREE.NearestFilter;
    tex.generateMipmaps = false;
    tex.needsUpdate = true;
    return tex;
  }

  /** 取 drawing buffer 尺寸（= 输出画面的真实像素尺寸，后处理 RT 都按它建） */
  function drawingBufferSize(renderer) {
    try {
      if (renderer.getDrawingBufferSize) {
        var v = renderer.getDrawingBufferSize(new THREE.Vector2());
        return { w: Math.max(2, Math.round(v.x)), h: Math.max(2, Math.round(v.y)) };
      }
    } catch (e) { /* 继续走兜底 */ }
    var s = renderer.getSize(new THREE.Vector2());
    var pr = renderer.getPixelRatio ? renderer.getPixelRatio() : 1;
    return { w: Math.max(2, Math.round(s.x * pr)), h: Math.max(2, Math.round(s.y * pr)) };
  }

  /** 法线 RT 精度：优先半浮点（法线在 [0,1] 打包，8bit 会有可见阶梯） */
  function pickNormalType(renderer) {
    try {
      var caps = renderer.capabilities || {};
      var gl = renderer.getContext();
      if (caps.isWebGL2) {
        if (gl.getExtension("EXT_color_buffer_float") ||
            gl.getExtension("EXT_color_buffer_half_float")) {
          return THREE.HalfFloatType;
        }
      } else if (gl.getExtension("OES_texture_half_float") &&
                 gl.getExtension("EXT_color_buffer_half_float")) {
        return THREE.HalfFloatType;
      }
    } catch (e) { /* 老设备退回 8bit */ }
    return THREE.UnsignedByteType;
  }

  /**
   * 深度纹理类型：WebGL2 上用 24bit（DEPTH_COMPONENT24）。
   * 16bit 在 main 页面 near=0.5 / far=2600 的配置下，100 单位外量化误差就到
   * 0.15 世界单位量级，AO 半径（~1 单位）会被深度噪声淹掉。
   */
  function pickDepthType(renderer) {
    try {
      if (renderer.capabilities && renderer.capabilities.isWebGL2) {
        return THREE.UnsignedIntType;
      }
    } catch (e) {}
    return THREE.UnsignedShortType;
  }

  /* ==========================================================================
   * 2. 着色器（GLSL ES 1.00；three r128 的 WebGL2 后端兼容这套写法）
   *    注意：fragment 里不要声明 projectionMatrix / viewMatrix / cameraPosition，
   *    three 的 program prefix 里可能已有同名内建 uniform（本文件统一用 ao* 前缀）。
   * ======================================================================== */

  /** 全屏四边形顶点着色器：直接铺满屏幕，把 uv 传给片元 */
  var GLSL_VERT = [
    "varying vec2 vUv;",
    "void main() {",
    "    vUv = uv;",
    "    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);",
    "}"
  ].join("\n");

  /** AO 计算：从深度反投影视空间位置，半球核采样做深度比较 */
  var GLSL_AO = [
    "uniform sampler2D tNormal;",
    "uniform sampler2D tDepth;",
    "uniform sampler2D tNoise;",
    "uniform mat4 aoProjMatrix;",       // camera.projectionMatrix（视 → 裁剪）
    "uniform mat4 aoProjInv;",          // camera.projectionMatrixInverse（裁剪 → 视）
    "uniform vec2 noiseScale;",         // AO 分辨率 / 4，噪声贴图的平铺次数
    "uniform float cameraNear;",
    "uniform float cameraFar;",
    "uniform float radius;",            // 视空间采样半径（世界单位）
    "uniform float bias;",              // 自遮挡容差（视空间 z 偏移）
    "uniform float minViewZ;",          // 太近的像素不算（避免相机贴脸时全黑）
    "uniform float maxViewZ;",          // 太远的像素不算（远景 AO 误差大，直接跳过）
    "uniform float contrast;",          // AO 对比度曲线（pow），把浅灰的接触带压深
    "uniform vec3 kernel[KERNEL_SIZE];",// 半球采样核（install 期生成，逐像素随机旋转）
    "varying vec2 vUv;",
    "",
    "// 由【uv + 窗口深度】反投影回视空间坐标。",
    "// 深度缓冲里的 z 是透视除法后的非线性值，必须先还原成 clip 空间，",
    "// 再乘投影逆矩阵、做齐次除法，才能得到真正的视空间坐标。",
    "vec4 viewFromDepth(vec2 uv, float depth) {",
    "    vec4 clip = vec4(uv * 2.0 - 1.0, depth * 2.0 - 1.0, 1.0);",
    "    vec4 view = aoProjInv * clip;",
    "    return view / view.w;",
    "}",
    "",
    "void main() {",
    "    float depth = texture2D(tDepth, vUv).x;",
    "",
    "    // 【坑 ④】天空/远平面：depth==1 表示该像素没有几何体。",
    "    // 这里必须直接输出 AO=1，否则天花板或天空会被当成【被遮挡】刷成黑斑。",
    "    if (depth >= 0.9999999) { gl_FragColor = vec4(1.0); return; }",
    "",
    "    vec3 viewPos = viewFromDepth(vUv, depth).xyz;",
    "    float viewDepth = -viewPos.z;                    // 视空间 z 是负的，取正表示距离",
    "    if (viewDepth < minViewZ || viewDepth > maxViewZ) { gl_FragColor = vec4(1.0); return; }",
    "",
    "    // MeshNormalMaterial 输出的是【视空间法线】打包到 [0,1]，解包成 [-1,1]",
    "    vec3 viewNormal = texture2D(tNormal, vUv).xyz * 2.0 - 1.0;",
    "    float nLen2 = dot(viewNormal, viewNormal);",
    "    // 法线长度异常（缺 normal 属性的对象 / NaN）视为无效像素，AO 置 1",
    "    if (nLen2 < 0.25) { gl_FragColor = vec4(1.0); return; }",
    "    viewNormal *= inversesqrt(nLen2);",
    "",
    "    // 逐像素随机旋转：取噪声里的切向随机向量，剔除法线分量后正交化，构造 TBN",
    "    vec3 randomVec = texture2D(tNoise, vUv * noiseScale).xyz * 2.0 - 1.0;",
    "    vec3 tangent = randomVec - viewNormal * dot(randomVec, viewNormal);",
    "    float tLen2 = dot(tangent, tangent);",
    "    if (tLen2 < 1e-4) { gl_FragColor = vec4(1.0); return; }",
    "    tangent *= inversesqrt(tLen2);",
    "    vec3 bitangent = cross(viewNormal, tangent);",
    "    mat3 tbn = mat3(tangent, bitangent, viewNormal);",
    "",
    "    float occlusion = 0.0;",
    "    float valid = 0.0;",
    "    for (int i = 0; i < KERNEL_SIZE; i++) {",
    "        vec3 samplePos = viewPos + tbn * kernel[i] * radius;",
    "        vec4 offset = aoProjMatrix * vec4(samplePos, 1.0);",
    "        if (offset.w <= 0.0) { continue; }",
    "        vec2 sampleUv = (offset.xy / offset.w) * 0.5 + 0.5;",
    "        if (sampleUv.x < 0.0 || sampleUv.x > 1.0 ||",
    "            sampleUv.y < 0.0 || sampleUv.y > 1.0) { continue; }",
    "        float sampleDepth = texture2D(tDepth, sampleUv).x;",
    "        // 采样点落在天空上 → 那里没有几何体，不算遮挡，但计入有效样本数",
    "        if (sampleDepth >= 0.9999999) { valid += 1.0; continue; }",
    "        float sampleViewZ = viewFromDepth(sampleUv, sampleDepth).z;",
    "        // 视空间 z 是负的：比采样点更靠近相机的表面 sampleViewZ 更大",
    "        float delta = sampleViewZ - samplePos.z;",
    "        // range check：深度差过大的不是同一个遮挡体，",
    "        // 避免【前景物体的深度把远景压黑】的经典黑边",
    "        float rangeCheck = smoothstep(0.0, 1.0, radius / max(0.0001, abs(sampleViewZ - viewPos.z)));",
    "        occlusion += (delta > bias ? 1.0 : 0.0) * rangeCheck;",
    "        valid += 1.0;",
    "    }",
    "",
    "    float ao = 1.0 - occlusion / max(1.0, valid);",
    "    ao = clamp(ao, 0.0, 1.0);",
    "    // 对比度曲线：屏幕空间 AO 的接触带通常只有 10%~20% 的遮挡量（灰得很浅），",
    "    // 直接混合几乎看不出来；pow 只压深浅灰、几乎不动干净区域，接触边立刻清晰。",
    "    ao = pow(ao, contrast);",
    "    // 远处淡出：远端像素的屏幕空间采样误差大，且本来就被雾/泛光压住",
    "    float fade = 1.0 - smoothstep(maxViewZ * 0.55, maxViewZ, viewDepth);",
    "    ao = mix(1.0, ao, fade);",
    "    gl_FragColor = vec4(ao, ao, ao, 1.0);",
    "}"
  ].join("\n");

  /** 双边模糊（可分离）：空间高斯 × 深度高斯，抹平随机噪声但不糊掉物体边界 */
  var GLSL_BLUR = [
    "uniform sampler2D tAO;",
    "uniform sampler2D tDepth;",
    "uniform mat4 aoProjInv;",
    "uniform vec2 blurStep;",        // 单像素的 uv 步长：水平 (1/w, 0) 或垂直 (0, 1/h)
    "uniform float depthSigma;",     // 视空间 z 容差（世界单位）
    "uniform float maxViewZ;",
    "varying vec2 vUv;",
    "",
    "float viewZAt(vec2 uv) {",
    "    float d = texture2D(tDepth, uv).x;",
    "    // 天空给一个【极远】的固定深度，保证它不会和前景像素互相混合",
    "    if (d >= 0.9999999) { return -maxViewZ * 4.0; }",
    "    vec4 clip = vec4(uv * 2.0 - 1.0, d * 2.0 - 1.0, 1.0);",
    "    vec4 view = aoProjInv * clip;",
    "    return view.z / view.w;",
    "}",
    "",
    "void main() {",
    "    float centerZ = viewZAt(vUv);",
    "    // 天空像素的 AO 本来就是 1，直接输出，省得除一个接近 0 的权重和",
    "    if (centerZ <= -maxViewZ * 2.0) { gl_FragColor = vec4(1.0); return; }",
    "",
    "    float sum = 0.0;",
    "    float wsum = 0.0;",
    "    for (int i = -" + BLUR_RADIUS + "; i <= " + BLUR_RADIUS + "; i++) {",
    "        float fi = float(i);",
    "        vec2 uv = clamp(vUv + blurStep * fi, vec2(0.0), vec2(1.0));",
    "        float z = viewZAt(uv);",
    "        float dz = (z - centerZ) / depthSigma;",
    "        float wDepth = exp(-0.5 * dz * dz);",           // 深度差越大权重越小（双边）
    "        float wSpatial = exp(-0.5 * fi * fi / 1.2);",    // 空间高斯
    "        float w = wDepth * wSpatial;",
    "        sum += texture2D(tAO, uv).x * w;",
    "        wsum += w;",
    "    }",
    "    gl_FragColor = vec4(sum / max(wsum, 1e-5));",
    "}"
  ].join("\n");

  /** 合成：outColor = beauty * mix(1.0, ao, strength) */
  var GLSL_COMPOSITE = [
    "uniform sampler2D tDiffuse;",   // RenderPass 已经渲染好的画面（beauty）
    "uniform sampler2D tAO;",        // 半分辨率 AO，LinearFilter 自动双线性上采样
    "uniform float strength;",
    "varying vec2 vUv;",
    "",
    "void main() {",
    "    vec4 color = texture2D(tDiffuse, vUv);",
    "    float ao = texture2D(tAO, vUv).x;",
    "    color.rgb *= mix(1.0, ao, strength);",
    "    gl_FragColor = color;",
    "}"
  ].join("\n");

  /* ==========================================================================
   * 3. SSAOPass —— 自己写，不依赖 examples/jsm/SSAOPass
   *    类在 ensurePassClass() 里才真正创建：脚本加载期连 class extends 都不执行，
   *    这样即使后处理脚本顺序意外（THREE.Pass 尚未定义）也不会在加载期抛异常。
   * ======================================================================== */
  var SSAOPassClass = null;

  function ensurePassClass() {
    if (SSAOPassClass) return SSAOPassClass;
    if (typeof THREE.Pass !== "function") return null;
    if (typeof THREE.FullScreenQuad !== "function") return null;

    SSAOPassClass = class SSAOPass extends THREE.Pass {
      /**
       * @param {THREE.Scene} scene
       * @param {THREE.Camera} camera
       * @param {THREE.WebGLRenderer} renderer
       * @param {object} params 已归一化的参数（见 install 里的 normalizeQuality）
       */
      constructor(scene, camera, renderer, params) {
        super();
        var self = this;

        this.isSSAOPass = true;              // 标记：install 幂等判断 / 调试面板识别
        this.scene = scene;
        this.camera = camera;
        this.renderer = renderer;

        /* ---- 参数 ---- */
        this.samples = params.samples;
        this.halfRes = params.halfRes;
        this.blur = params.blur;
        this.strength = params.strength;
        this.radius = params.radius;
        this.bias = params.bias;
        this.minViewZ = params.minViewZ;
        this.maxViewZ = params.maxViewZ;
        this.contrast = params.contrast;
        this._log = params.log || function () {};
        this._failCount = 0;
        this.onFail = null;                  // 连续异常时由 install 挂回调（同步 handle.enabled）

        /* ---- 输出尺寸 ---- */
        var dbs = drawingBufferSize(renderer);
        this.width = dbs.w;
        this.height = dbs.h;

        /* ---- GPU 资源格式 ---- */
        this.normalType = pickNormalType(renderer);
        this.depthType = pickDepthType(renderer);
        this.rtFormatName = (this.normalType === THREE.HalfFloatType) ? "RGBA16F" : "RGBA8";
        this.depthFormatName = (this.depthType === THREE.UnsignedIntType) ? "DEPTH24" : "DEPTH16";

        /* ---- 半球核 + 噪声贴图（install 期一次性生成）---- */
        this.kernel = buildKernel(this.samples, makeRng(0x5EED));
        this.noiseTexture = makeNoiseTexture(buildNoiseData(makeRng(0x51A0)));

        /* ---- 可复用的临时对象（保证 render 每帧零分配）---- */
        this._tmpInv = new THREE.Matrix4();
        this._hiddenObjs = new Array(MAX_HIDDEN);
        this._hiddenPrev = new Array(MAX_HIDDEN);
        this._hiddenCount = 0;
        this._traverseFn = function (obj) { self._collectHidden(obj); };

        /* ---- uniform（全部预建、render 里只赋值）---- */
        this.aoUniforms = {
          tNormal: { value: null },
          tDepth: { value: null },
          tNoise: { value: this.noiseTexture },
          aoProjMatrix: { value: camera.projectionMatrix },
          aoProjInv: { value: camera.projectionMatrixInverse || this._tmpInv },
          noiseScale: { value: new THREE.Vector2(1, 1) },
          cameraNear: { value: camera.near },
          cameraFar: { value: camera.far },
          radius: { value: this.radius },
          bias: { value: this.bias },
          minViewZ: { value: this.minViewZ },
          maxViewZ: { value: this.maxViewZ },
          contrast: { value: this.contrast },
          kernel: { value: this.kernel }
        };
        this.blurUniforms = {
          tAO: { value: null },
          tDepth: { value: null },
          aoProjInv: { value: this.aoUniforms.aoProjInv.value },
          blurStep: { value: new THREE.Vector2(1 / 512, 0) },
          depthSigma: { value: Math.max(0.05, this.radius) },
          maxViewZ: { value: this.maxViewZ }
        };
        this.compositeUniforms = {
          tDiffuse: { value: null },
          tAO: { value: null },
          strength: { value: this.strength }
        };

        /* ---- 材质 ---- */
        this.normalMaterial = new THREE.MeshNormalMaterial();   // 预渲染专用，绝不挂到物体上
        this.aoMaterial = new THREE.ShaderMaterial({
          uniforms: this.aoUniforms,
          vertexShader: GLSL_VERT,
          fragmentShader: GLSL_AO,
          defines: { KERNEL_SIZE: this.samples },
          depthTest: false,
          depthWrite: false,
          blending: THREE.NoBlending
        });
        this.blurMaterial = new THREE.ShaderMaterial({
          uniforms: this.blurUniforms,
          vertexShader: GLSL_VERT,
          fragmentShader: GLSL_BLUR,
          depthTest: false,
          depthWrite: false,
          blending: THREE.NoBlending
        });
        this.compositeMaterial = new THREE.ShaderMaterial({
          uniforms: this.compositeUniforms,
          vertexShader: GLSL_VERT,
          fragmentShader: GLSL_COMPOSITE,
          depthTest: false,
          depthWrite: false,
          blending: THREE.NoBlending
        });
        this.aoMaterial.name = "SSAO.ao";
        this.blurMaterial.name = "SSAO.blur";
        this.compositeMaterial.name = "SSAO.composite";

        /* ---- 全屏四边形（composer 提供的 THREE.FullScreenQuad）---- */
        this.quad = new THREE.FullScreenQuad(this.compositeMaterial);

        /* ---- 内部 RT ---- */
        this.normalRT = null;
        this.aoRT = null;
        this.blurRT = null;
        this._aoTexelX = 1 / 512;
        this._aoTexelY = 1 / 512;
        this._createTargets(this.width, this.height);

        /* ---- 对外可观测统计 ---- */
        this.stats = {
          samples: this.samples,
          kernelSize: MAX_SAMPLES,       // 采样池上限
          rtFormat: this.rtFormatName,
          depthFormat: this.depthFormatName,
          ms: 0,                         // SSAO 这一 pass 的平滑耗时（ms）
          frames: 0,
          aoWidth: this.aoWidth,
          aoHeight: this.aoHeight
        };
        this.enabled = true;             // composer 会跳过 enabled===false 的 pass
        this.needsSwap = true;           // 读 readBuffer 的画面，写 writeBuffer
        this.clear = false;              // 自己管理清屏
      }

      /* ------------------------------------------------------------------ */
      /* 内部 RT：normalRT 挂 DepthTexture（法线 + 深度一次预渲染）          */
      /* ------------------------------------------------------------------ */
      _createTargets(w, h) {
        this._disposeTargets();
        w = Math.max(2, Math.floor(w));
        h = Math.max(2, Math.floor(h));

        var depthTex = new THREE.DepthTexture(w, h, this.depthType);
        depthTex.name = "SSAO.depth";
        depthTex.format = THREE.DepthFormat;
        depthTex.minFilter = THREE.NearestFilter;
        depthTex.magFilter = THREE.NearestFilter;
        depthTex.generateMipmaps = false;

        var normalRT = new THREE.WebGLRenderTarget(w, h, {
          minFilter: THREE.NearestFilter,
          magFilter: THREE.NearestFilter,
          format: THREE.RGBAFormat,
          type: this.normalType,
          depthBuffer: true,
          stencilBuffer: false,
          depthTexture: depthTex
        });
        normalRT.texture.name = "SSAO.normal";
        this.normalRT = normalRT;

        var aoW = this.halfRes ? Math.max(1, Math.floor(w / 2)) : w;
        var aoH = this.halfRes ? Math.max(1, Math.floor(h / 2)) : h;
        this.aoWidth = aoW;
        this.aoHeight = aoH;

        // AO 用 8bit 足够（值域 0~1），LinearFilter 让合成时双线性上采样
        var aoRT = new THREE.WebGLRenderTarget(aoW, aoH, {
          minFilter: THREE.LinearFilter,
          magFilter: THREE.LinearFilter,
          format: THREE.RGBAFormat,
          type: THREE.UnsignedByteType,
          depthBuffer: false,
          stencilBuffer: false
        });
        aoRT.texture.name = "SSAO.ao";
        this.aoRT = aoRT;

        var blurRT = new THREE.WebGLRenderTarget(aoW, aoH, {
          minFilter: THREE.LinearFilter,
          magFilter: THREE.LinearFilter,
          format: THREE.RGBAFormat,
          type: THREE.UnsignedByteType,
          depthBuffer: false,
          stencilBuffer: false
        });
        blurRT.texture.name = "SSAO.aoBlur";
        this.blurRT = blurRT;

        this._aoTexelX = 1 / aoW;
        this._aoTexelY = 1 / aoH;
        this.aoUniforms.noiseScale.value.set(aoW / NOISE_SIZE, aoH / NOISE_SIZE);
        this.blurUniforms.depthSigma.value = Math.max(0.05, this.radius * 1.2);

        if (this.stats) {                       // setSize 时同步对外统计
          this.stats.aoWidth = aoW;
          this.stats.aoHeight = aoH;
        }
      }

      _disposeTargets() {
        try { if (this.normalRT) { this.normalRT.dispose(); this.normalRT = null; } } catch (e) {}
        try { if (this.aoRT) { this.aoRT.dispose(); this.aoRT = null; } } catch (e) {}
        try { if (this.blurRT) { this.blurRT.dispose(); this.blurRT = null; } } catch (e) {}
      }

      /* ------------------------------------------------------------------ */
      /* 预渲染前收集"不该进深度缓冲"的对象（透明/点线精灵/depthWrite=false）*/
      /* 每帧现扫：页面中途改 visible 也能立刻跟上，避免把对象"复活"。        */
      /* 只用预分配数组，不产生垃圾。                                         */
      /* ------------------------------------------------------------------ */
      _collectHidden(obj) {
        if (obj === this.scene) return;
        if (!obj.visible) return;
        if (obj.isPoints || obj.isLine || obj.isLineSegments || obj.isSprite) {
          this._addHidden(obj);                       // 点/线/精灵不构成有效遮挡面
          return;
        }
        var mat = obj.material;
        if (!mat) return;
        if (Array.isArray(mat)) {
          for (var i = 0; i < mat.length; i++) {
            if (this._isGhost(mat[i])) { this._addHidden(obj); return; }
          }
          return;
        }
        if (this._isGhost(mat)) this._addHidden(obj);
      }

      _isGhost(m) {
        if (!m) return false;
        if (m.transparent === true) return true;        // 透明/加色（光锥、粒子、HUD）
        if (m.depthWrite === false) return true;        // 不写深度的（天空球、贴花）
        if (typeof m.opacity === "number" && m.opacity < 1) return true;
        if (m.visible === false) return true;           // 材质自己被关掉
        return false;
      }

      _addHidden(obj) {
        var i = this._hiddenCount;
        if (i >= MAX_HIDDEN) return;                    // 超容量宁可不管，也不每帧扩容
        this._hiddenObjs[i] = obj;
        this._hiddenPrev[i] = obj.visible;
        this._hiddenCount = i + 1;
        obj.visible = false;
      }

      _hideGhostObjects() {
        this._hiddenCount = 0;
        this.scene.traverse(this._traverseFn);
      }

      _restoreHidden() {
        for (var i = 0; i < this._hiddenCount; i++) {
          var o = this._hiddenObjs[i];
          if (o) o.visible = this._hiddenPrev[i];
          this._hiddenObjs[i] = null;
        }
        this._hiddenCount = 0;
      }

      /* ------------------------------------------------------------------ */
      /* 尺寸变化：重建内部 RT（composer.setSize 传入的是 drawing buffer 像素）*/
      /* ------------------------------------------------------------------ */
      setSize(width, height) {
        var w = Math.max(2, Math.floor(width || 2));
        var h = Math.max(2, Math.floor(height || 2));
        if (this.width === w && this.height === h && this.normalRT) return;
        this.width = w;
        this.height = h;
        this._createTargets(w, h);
      }

      /** 相机参数同步（near/far 可能被页面改；矩阵是引用，会跟着 updateProjectionMatrix 变）*/
      syncCamera() {
        var cam = this.camera;
        if (!cam) return;
        this.aoUniforms.cameraNear.value = cam.near;
        this.aoUniforms.cameraFar.value = cam.far;
        this.aoUniforms.aoProjMatrix.value = cam.projectionMatrix;
        if (cam.projectionMatrixInverse && cam.projectionMatrixInverse.isMatrix4) {
          this.aoUniforms.aoProjInv.value = cam.projectionMatrixInverse;
        } else {
          this._tmpInv.copy(cam.projectionMatrix).invert();
          this.aoUniforms.aoProjInv.value = this._tmpInv;
        }
        this.blurUniforms.aoProjInv.value = this.aoUniforms.aoProjInv.value;
      }

      setStrength(v) {
        this.strength = Math.max(0, Math.min(1, Number(v) || 0));
        this.compositeUniforms.strength.value = this.strength;
      }

      /* ------------------------------------------------------------------ */
      /* 核心：一次法线+深度预渲染 → AO → 双边模糊 → 合成                    */
      /* ------------------------------------------------------------------ */
      render(renderer, writeBuffer, readBuffer /*, deltaTime, maskActive */) {
        if (!this.normalRT || !this.aoRT) return;       // dispose 后被误调：直接跳过
        if (!readBuffer || !readBuffer.texture) return;

        var t0 = (window.performance && performance.now) ? performance.now() : Date.now();

        /* ---- 保存渲染器现场（finally 里无条件恢复）---- */
        var savedRT = renderer.getRenderTarget();
        var savedAutoClear = renderer.autoClear;
        var savedOverride = this.scene.overrideMaterial || null;
        var savedShadowAuto = (renderer.shadowMap && renderer.shadowMap.autoUpdate !== undefined)
          ? renderer.shadowMap.autoUpdate : undefined;
        var savedEncoding = renderer.outputEncoding;

        try {
          this.syncCamera();

          /* ========== ① 法线 + 深度预渲染（只渲一次） ========== */
          this._hideGhostObjects();                       // 透明/点线精灵不进深度，避免幽灵遮挡
          this.scene.overrideMaterial = this.normalMaterial;
          renderer.autoClear = true;
          if (renderer.shadowMap && savedShadowAuto !== undefined) {
            renderer.shadowMap.autoUpdate = false;        // 本帧阴影贴图已经由 RenderPass 渲过
          }
          // 法线必须是线性数据：临时关掉 sRGB 输出编码，否则法线被 gamma 扭曲 → AO 斑驳
          if (savedEncoding !== undefined && THREE.LinearEncoding !== undefined) {
            renderer.outputEncoding = THREE.LinearEncoding;
          }
          renderer.setRenderTarget(this.normalRT);
          renderer.render(this.scene, this.camera);
          // ★★ 最典型事故点：overrideMaterial 必须【立刻】清掉，
          //    忘了清 → 主场景整幅变成法线色（RGB 法线图），看起来像渲染坏了。
          this.scene.overrideMaterial = null;
          this._restoreHidden();

          /* ========== ② AO（半分辨率） ========== */
          this.aoUniforms.tNormal.value = this.normalRT.texture;
          this.aoUniforms.tDepth.value = this.normalRT.depthTexture;
          renderer.setRenderTarget(this.aoRT);
          this.quad.material = this.aoMaterial;
          this.quad.render(renderer);

          /* ========== ③ 双边模糊（可分离：横 + 竖各一次 5 tap） ========== */
          if (this.blur) {
            this.blurUniforms.tDepth.value = this.normalRT.depthTexture;
            this.blurUniforms.depthSigma.value = Math.max(0.05, this.radius * 1.2);
            this.quad.material = this.blurMaterial;

            /* 横向：AO → blurRT */
            this.blurUniforms.tAO.value = this.aoRT.texture;
            renderer.setRenderTarget(this.blurRT);
            this.blurUniforms.blurStep.value.set(this._aoTexelX, 0);
            this.quad.render(renderer);

            /* 纵向：blurRT → aoRT。
             * ★ 这里必须把 tAO 换成 blurRT.texture：读和写不能是同一张纹理，
             *   否则触发 WebGL 反馈环（framebuffer feedback loop），输出会变成全黑。 */
            this.blurUniforms.tAO.value = this.blurRT.texture;
            renderer.setRenderTarget(this.aoRT);
            this.blurUniforms.blurStep.value.set(0, this._aoTexelY);
            this.quad.render(renderer);
          }

          /* ========== ④ 合成到 writeBuffer / 屏幕 ========== */
          this.compositeUniforms.tDiffuse.value = readBuffer.texture;
          this.compositeUniforms.tAO.value = this.aoRT.texture;
          renderer.autoClear = true;
          // 必须尊重 composer 设置的 renderToScreen（最后一个启用的 pass 要直接上屏）
          renderer.setRenderTarget(this.renderToScreen ? null : writeBuffer);
          this.quad.material = this.compositeMaterial;
          this.quad.render(renderer);
        } catch (err) {
          this._failCount++;
          if (this._failCount <= 3) {
            this._log("渲染异常（第 " + this._failCount + " 次）：" +
              (err && err.message ? err.message : String(err)));
          }
          if (this._failCount >= 3) {
            this.enabled = false;                    // 连续失败自动关闭，主场景继续跑
            if (typeof this.onFail === "function") { try { this.onFail(); } catch (e2) {} }
          }
        } finally {
          /* ---- 无条件恢复现场：这是"不把主场景搞崩"的底线 ---- */
          try { this.scene.overrideMaterial = savedOverride; } catch (e) {}
          try { this._restoreHidden(); } catch (e) {}
          try {
            if (renderer.shadowMap && savedShadowAuto !== undefined) {
              renderer.shadowMap.autoUpdate = savedShadowAuto;
            }
          } catch (e) {}
          try { if (savedEncoding !== undefined) renderer.outputEncoding = savedEncoding; } catch (e) {}
          try { renderer.autoClear = savedAutoClear; } catch (e) {}
          try { renderer.setRenderTarget(savedRT); } catch (e) {}

          /* ---- 平滑耗时统计（20 帧 EMA，≈ alpha 0.095）---- */
          var t1 = (window.performance && performance.now) ? performance.now() : Date.now();
          var dt = t1 - t0;
          if (!isFinite(dt) || dt < 0) dt = 0;
          this.stats.ms = this.stats.ms * 0.905 + dt * 0.095;
          this.stats.frames++;
        }
      }

      dispose() {
        try { this._restoreHidden(); } catch (e) {}
        this._disposeTargets();
        try { if (this.noiseTexture) this.noiseTexture.dispose(); } catch (e) {}
        try { if (this.aoMaterial) this.aoMaterial.dispose(); } catch (e) {}
        try { if (this.blurMaterial) this.blurMaterial.dispose(); } catch (e) {}
        try { if (this.compositeMaterial) this.compositeMaterial.dispose(); } catch (e) {}
        try { if (this.normalMaterial) this.normalMaterial.dispose(); } catch (e) {}
        try { if (this.quad && this.quad.dispose) this.quad.dispose(); } catch (e) {}
        this.enabled = false;
      }
    };

    return SSAOPassClass;
  }

  /* ==========================================================================
   * 4. 能力检测
   * ======================================================================== */
  function capable(opts) {
    try {
      var o = opts || {};
      if (o.tier === "cpu") return false;                       // CPU 软件光栅档：必须自动关闭
      if (typeof THREE.Pass !== "function") return false;
      if (typeof THREE.FullScreenQuad !== "function") return false;
      if (!THREE.WebGLRenderTarget || !THREE.DepthTexture) return false;
      var renderer = o.renderer;
      if (!renderer || typeof renderer.getContext !== "function") return false;
      var caps = renderer.capabilities;
      if (!caps) return false;
      var gl = renderer.getContext();
      if (!gl) return false;
      // 契约：无 WebGL2 → false（WebGL1 的 depth texture 组合坑太多，不冒风险）
      if (caps.isWebGL2 !== true) return false;
      // 浮点/半浮点颜色缓冲：法线用 RGBA16F 存，8bit 会让 AO 出阶梯
      if (!gl.getExtension("EXT_color_buffer_float") &&
          !gl.getExtension("EXT_color_buffer_half_float")) return false;
      return true;
    } catch (e) {
      return false;
    }
  }

  /* ==========================================================================
   * 5. install —— 挂到既有 EffectComposer 上（RenderPass 之后、泛光之前）
   * ======================================================================== */
  function normalizeQuality(opts) {
    var q = (opts && opts.quality) || {};
    var tier = (opts && opts.tier) || "high";
    var defSamples = (tier === "medium") ? DEFAULT_SAMPLES_MEDIUM : DEFAULT_SAMPLES_HIGH;
    var samples = Math.round(clampNum(q.samples, MIN_SAMPLES, MAX_SAMPLES, defSamples));
    return {
      samples: samples,
      halfRes: q.halfRes !== undefined ? !!q.halfRes : true,
      blur: q.blur !== undefined ? !!q.blur : true,
      strength: clampNum(q.strength, 0, 1, 0.9),
      radius: clampNum(q.radius, 0.05, 20, 2.2),
      bias: clampNum(q.bias, 0, 0.5, 0.025),
      minViewZ: clampNum(q.minViewZ, 0.01, 10, 0.5),
      maxViewZ: clampNum(q.maxViewZ, 10, 4000, 420),
      contrast: clampNum(q.contrast, 1, 3, 1.6)
    };
  }

  function install(opts) {
    var log = makeLogger(opts && opts.log);
    try {
      if (!opts || typeof opts !== "object") { log("关闭：未传 opts"); return null; }
      if (!opts.renderer) { log("关闭：缺少 renderer"); return null; }
      if (!opts.scene || !opts.camera) { log("关闭：缺少 scene/camera"); return null; }
      if (!opts.composer || typeof opts.composer.insertPass !== "function" ||
          !Array.isArray(opts.composer.passes)) {
        log("关闭：composer 不可用（需要有 insertPass 和 passes 数组）");
        return null;
      }
      if (opts.tier === "cpu") { log("关闭：cpu 档不启用 SSAO"); return null; }
      var q = (opts.quality || {});
      if (q.enabled === false) { log("关闭：quality.enabled=false"); return null; }
      if (!capable(opts)) { log("关闭：能力检测未通过（需要 WebGL2 + 浮点渲染目标）"); return null; }

      var PassClass = ensurePassClass();
      if (!PassClass) { log("关闭：THREE.Pass / THREE.FullScreenQuad 缺失（EffectComposer.js 未加载？）"); return null; }

      // 幂等：同一个 composer 上重复 install 直接拒绝，避免两个 SSAO 叠加
      var existing = opts.composer.passes;
      for (var k = 0; k < existing.length; k++) {
        if (existing[k] && existing[k].isSSAOPass) {
          log("已安装过，跳过重复 install");
          return null;
        }
      }

      var params = normalizeQuality(opts);
      params.log = log;

      var pass = new PassClass(opts.scene, opts.camera, opts.renderer, params);

      /* 插入位置：RenderPass 之后、UnrealBloomPass 之前 */
      var passes = opts.composer.passes;
      var insertAt = 1;                                  // 契约：RenderPass 固定 index 0
      for (var i = 0; i < passes.length; i++) {
        var nm = (passes[i] && passes[i].constructor && passes[i].constructor.name) || "";
        if (nm.indexOf("RenderPass") >= 0) insertAt = Math.max(insertAt, i + 1);
      }
      for (var j = 0; j < passes.length; j++) {
        var nm2 = (passes[j] && passes[j].constructor && passes[j].constructor.name) || "";
        if (nm2.indexOf("Bloom") >= 0 && j < insertAt) insertAt = j;  // 一定在泛光之前
      }
      opts.composer.insertPass(pass, insertAt);

      /* 句柄：给 Lead 的稳定 API（内部 pass 可随时访问） */
      var handle = {
        pass: pass,
        enabled: true,
        stats: pass.stats,
        setEnabled: function (on) {
          var v = !!on;
          pass.enabled = v;
          handle.enabled = v;
        },
        setStrength: function (v) {
          try { pass.setStrength(v); } catch (e) {}
        },
        setSize: function (w, h) {
          try {
            var pr = opts.renderer.getPixelRatio ? opts.renderer.getPixelRatio() : 1;
            if (typeof w === "number" && typeof h === "number") {
              pass.setSize(w * pr, h * pr);               // 传 CSS 像素，内部换算 drawing buffer
            } else {
              var dbs = drawingBufferSize(opts.renderer);
              pass.setSize(dbs.w, dbs.h);
            }
          } catch (e) { log("setSize 失败：" + (e && e.message ? e.message : e)); }
        },
        update: function (/* dt */) {
          try { pass.syncCamera(); } catch (e) {}
        },
        dispose: function () {
          try { opts.composer.removePass(pass); } catch (e) {}
          try { pass.dispose(); } catch (e) {}
          if (lastPass === pass) lastPass = null;
          handle.enabled = false;
          log("已卸载");
        }
      };
      pass.onFail = function () { handle.enabled = false; log("连续异常，已自动关闭 SSAO"); };

      lastPass = pass;
      log("已启用 " + params.samples + " 样本 · " +
          (params.halfRes ? ("半分辨率 " + pass.aoWidth + "×" + pass.aoHeight) : ("全分辨率 " + pass.aoWidth + "×" + pass.aoHeight)) +
          " · 法线 " + pass.rtFormatName + " · 深度 " + pass.depthFormatName +
          " · radius=" + params.radius + " strength=" + params.strength +
          " · 插入 composer[" + insertAt + "]");
      return handle;
    } catch (e) {
      log("关闭：install 异常 " + (e && e.message ? e.message : String(e)));
      return null;
    }
  }

  /* ==========================================================================
   * 6. selfTest —— 纯逻辑自检（不依赖 renderer，验收会用无头浏览器 --eval 抓取）
   * ======================================================================== */
  function selfTest() {
    var checks = [];
    function add(name, fn) {
      var ok = false;
      var detail = "";
      try {
        var r = fn();
        if (r && typeof r === "object") { ok = !!r.ok; detail = String(r.detail === undefined ? "" : r.detail); }
        else { ok = !!r; detail = String(r); }
      } catch (e) {
        ok = false;
        detail = "异常: " + (e && e.message ? e.message : String(e));
      }
      checks.push({ name: name, ok: ok, detail: detail });
    }

    /* --- 6.1 环境 --- */
    add("three.js 版本为 r128", function () {
      return { ok: String(THREE.REVISION) === "128", detail: "THREE.REVISION=" + THREE.REVISION };
    });
    add("THREE.Pass / FullScreenQuad 可用", function () {
      return {
        ok: typeof THREE.Pass === "function" && typeof THREE.FullScreenQuad === "function",
        detail: "Pass=" + typeof THREE.Pass + " FullScreenQuad=" + typeof THREE.FullScreenQuad
      };
    });
    add("SSAOPass 可创建（自写，非 examples/jsm）", function () {
      return { ok: typeof ensurePassClass() === "function", detail: "class extends THREE.Pass" };
    });

    /* --- 6.2 采样核 --- */
    var kernel16 = buildKernel(MIN_SAMPLES, makeRng(0x5EED));
    var kernel24 = buildKernel(MAX_SAMPLES, makeRng(0x5EED));
    add("半球采样核 16~24 个样本", function () {
      return {
        ok: kernel16.length === MIN_SAMPLES && kernel24.length === MAX_SAMPLES && MIN_SAMPLES >= 16 && MAX_SAMPLES <= 24,
        detail: kernel16.length + " / " + kernel24.length + "（16~24）"
      };
    });
    add("核样本全部位于法线正半球 (z>=0)", function () {
      var bad = 0;
      for (var i = 0; i < kernel24.length; i++) if (kernel24[i].z < -1e-6) bad++;
      return { ok: bad === 0, detail: bad + " 个越界样本" };
    });
    add("核样本长度归一化 (0<len<=1)", function () {
      var bad = 0; var maxLen = 0;
      for (var i = 0; i < kernel24.length; i++) {
        var L = kernel24[i].length();
        if (L > maxLen) maxLen = L;
        if (!(L > 0 && L <= 1.0001)) bad++;
      }
      return { ok: bad === 0, detail: "max=" + maxLen.toFixed(3) + "，异常 " + bad };
    });

    /* --- 6.3 噪声贴图 --- */
    add("4×4 逐像素随机旋转贴图", function () {
      var data = buildNoiseData(makeRng(0x51A0));
      return {
        ok: data.length === NOISE_SIZE * NOISE_SIZE * 4,
        detail: NOISE_SIZE + "×" + NOISE_SIZE + "×4 = " + data.length + " 字节"
      };
    });
    add("随机旋转向量为单位长度（切平面）", function () {
      var data = buildNoiseData(makeRng(0x51A0));
      var bad = 0;
      for (var i = 0; i < NOISE_SIZE * NOISE_SIZE; i++) {
        var x = data[i * 4] / 255 * 2 - 1;
        var y = data[i * 4 + 1] / 255 * 2 - 1;
        var L = Math.sqrt(x * x + y * y);
        if (L < 0.85 || L > 1.05) bad++;
      }
      return { ok: bad === 0, detail: bad + " 个非单位向量" };
    });

    /* --- 6.4 着色器关键实现（源码字符串检查，防止误删关键分支）--- */
    add("AO 着色器含深度反投影（投影逆矩阵 + 齐次除）", function () {
      var ok = GLSL_AO.indexOf("aoProjInv * clip") >= 0 &&
               GLSL_AO.indexOf("view / view.w") >= 0 &&
               GLSL_AO.indexOf("texture2D(tDepth") >= 0;
      return { ok: ok, detail: "uv+windowDepth → clip → view/w" };
    });
    add("天空/远平面像素 AO=1（无黑斑）", function () {
      var ok = GLSL_AO.indexOf("depth >= 0.9999999") >= 0 &&
               GLSL_AO.indexOf("maxViewZ") >= 0 &&
               GLSL_AO.indexOf("gl_FragColor = vec4(1.0)") >= 0;
      return { ok: ok, detail: "depth==1 与 viewDepth>maxViewZ 都输出 1" };
    });
    add("半球核循环 + range check + bias", function () {
      var ok = GLSL_AO.indexOf("for (int i = 0; i < KERNEL_SIZE; i++)") >= 0 &&
               GLSL_AO.indexOf("rangeCheck") >= 0 &&
               GLSL_AO.indexOf("delta > bias") >= 0;
      return { ok: ok, detail: "KERNEL_SIZE 循环 / rangeCheck / bias" };
    });
    add("合成公式 beauty * mix(1.0, ao, strength)", function () {
      var ok = GLSL_COMPOSITE.indexOf("mix(1.0, ao, strength)") >= 0 &&
               GLSL_COMPOSITE.indexOf("texture2D(tDiffuse") >= 0;
      return { ok: ok, detail: "outColor = beauty.rgb * mix(1,ao,strength)" };
    });
    add("双边模糊按深度加权（可分离）", function () {
      var ok = GLSL_BLUR.indexOf("blurStep") >= 0 &&
               GLSL_BLUR.indexOf("depthSigma") >= 0 &&
               GLSL_BLUR.indexOf("wDepth") >= 0;
      return { ok: ok, detail: "5 tap × 深度高斯 × 空间高斯" };
    });

    /* --- 6.5 深度反投影数学自检（与着色器完全同一套公式，纯 CPU 验证）--- */
    add("深度反投影数学正确（着色器同公式）", function () {
      var cam = new THREE.PerspectiveCamera(50, 1.6, 0.5, 2600);
      cam.position.set(3, 4, 5);
      cam.lookAt(0, 0, 0);
      cam.updateMatrixWorld(true);
      cam.updateProjectionMatrix();
      // 在视空间取一个已知点，正投影到 ndc/深度，再按着色器公式反投影回来
      var p = new THREE.Vector3(1.5, -2.0, -12.3);
      var clip = new THREE.Vector4(p.x, p.y, p.z, 1).applyMatrix4(cam.projectionMatrix);
      var ndc = new THREE.Vector3(clip.x / clip.w, clip.y / clip.w, clip.z / clip.w);
      var depth = ndc.z * 0.5 + 0.5;
      // 反投影：clip = (ndc.x, ndc.y, depth*2-1, 1) → P^-1 → 齐次除
      var back4 = new THREE.Vector4(ndc.x, ndc.y, depth * 2 - 1, 1)
        .applyMatrix4(cam.projectionMatrixInverse);
      var back = new THREE.Vector3(back4.x / back4.w, back4.y / back4.w, back4.z / back4.w);
      var err = back.distanceTo(p);
      // 天空像素（深度=1）应反投影到远平面附近
      var sky4 = new THREE.Vector4(0, 0, 1, 1).applyMatrix4(cam.projectionMatrixInverse);
      var skyZ = sky4.z / sky4.w;
      var skyOk = Math.abs(skyZ + cam.far) < 1e-2;
      return {
        ok: err < 1e-4 && skyOk,
        detail: "误差=" + err.toExponential(2) + " 天空反投影 z=" + skyZ.toFixed(2) + "(期望 -" + cam.far + ")"
      };
    });

    /* --- 6.6 能力检测与异常安全 --- */
    add("cpu 档 capable() 返回 false", function () {
      var r = capable({ tier: "cpu", renderer: {} });
      return { ok: r === false, detail: "capable({tier:'cpu'})=" + r };
    });
    add("缺 renderer 时 capable() 返回 false", function () {
      var r = capable({ tier: "high" });
      return { ok: r === false, detail: "capable({})= " + r };
    });
    add("install({}) 异常安全返回 null（不抛异常）", function () {
      var r = null;
      try { r = install({}); } catch (e) { return { ok: false, detail: "抛异常: " + e.message }; }
      return { ok: r === null, detail: "返回 " + String(r) };
    });
    add("cpu 档 install 返回 null 并自动关闭", function () {
      var r = null;
      try {
        r = install({ tier: "cpu", renderer: {}, scene: {}, camera: {}, composer: { passes: [], insertPass: function () {} } });
      } catch (e) { return { ok: false, detail: "抛异常: " + e.message }; }
      return { ok: r === null, detail: "返回 null" };
    });

    /* --- 6.7 运行期 --- */
    if (lastPass) {
      add("运行期已安装并出帧（stats.ms 有效）", function () {
        var s = lastPass.stats;
        return {
          ok: s.frames > 0 && isFinite(s.ms) && s.ms >= 0 && s.samples >= MIN_SAMPLES && s.samples <= MAX_SAMPLES,
          detail: "帧数=" + s.frames + " ms=" + s.ms.toFixed(3) +
                  " 样本=" + s.samples + " AO=" + s.aoWidth + "×" + s.aoHeight
        };
      });
      add("运行期法线/深度 RT 已建立", function () {
        var ok = !!(lastPass.normalRT && lastPass.normalRT.depthTexture &&
                    lastPass.aoRT && lastPass.aoRT.texture);
        return { ok: ok, detail: lastPass.rtFormatName + " + " + lastPass.depthFormatName };
      });
    }

    var okAll = true;
    for (var c = 0; c < checks.length; c++) if (!checks[c].ok) okAll = false;

    var s = lastPass ? lastPass.stats : null;
    return {
      ok: okAll,
      checks: checks,
      stats: {
        samples: s ? s.samples : DEFAULT_SAMPLES_HIGH,
        kernelSize: MAX_SAMPLES,
        rtFormat: s ? s.rtFormat : (THREE.HalfFloatType ? "RGBA16F" : "RGBA8"),
        ms: s ? s.ms : 0,
        aoWidth: s ? s.aoWidth : 0,
        aoHeight: s ? s.aoHeight : 0,
        frames: s ? s.frames : 0,
        installed: !!s,
        halfRes: s ? lastPass.halfRes : true,
        version: VERSION
      }
    };
  }

  /* ==========================================================================
   * 7. 唯一的全局导出
   * ======================================================================== */
  window.UAVSSAO = {
    version: VERSION,
    capable: capable,
    install: install,
    selfTest: selfTest
  };

})();
