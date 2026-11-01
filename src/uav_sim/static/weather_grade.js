/* =============================================================================
   weather_grade.js —— 天气色彩分级 + 屏幕空间天气叠加（后处理 Pass）
   =============================================================================

   为什么需要它（2026-11 截图复盘）：
     旧版只做了"雾 + 粒子"，没有任何色彩/对比处理。雾色一盖，整幅画面的
     动态范围被压到很窄的一段 —— 雨/雪/沙尘三张截图看起来都是"同一块单色"，
     地形、山脊、采坑台阶全部糊掉。这不是粒子不够多，而是**没有分级**。

   这个 Pass 做四件事（全部是屏幕空间，代价很低）：
     1. 色彩分级：曝光 / 对比度 / 饱和度 / 阴影染色 —— 把被雾压扁的层次拉回来；
     2. 大气透视：地平线附近叠加雾色，但**只叠 55%**，保留远景轮廓；
     3. 天气叠加：雨丝 / 雪花光斑 / 沙尘流带，随天气强度淡入；
     4. 暗角 + 颗粒：压住边缘、消除大面积渐变的色带（banding）。

   契约：初始化失败返回 null，绝不把异常抛给主场景；每帧零分配。
   ============================================================================= */
(function (global) {
  "use strict";

  var VERSION = "1.0.0";

  /* 每种天气的"分级配方"。intensity 会把非 clear 的项按强度插值。 */
  var GRADE = {
    clear: { mode: 0, exposure: 1.00, contrast: 1.00, saturation: 1.00,
             tint: 0x9ec8ff, haze: 0.00, vignette: 0.18, grain: 0.006 },
    rain:  { mode: 1, exposure: 1.16, contrast: 1.14, saturation: 0.84,
             tint: 0x8299b0, haze: 0.24, vignette: 0.28, grain: 0.012 },
    snow:  { mode: 2, exposure: 1.00, contrast: 1.22, saturation: 0.72,
             tint: 0xc6d2de, haze: 0.26, vignette: 0.24, grain: 0.010 },
    dust:  { mode: 3, exposure: 1.02, contrast: 1.14, saturation: 0.94,
             tint: 0xa9865c, haze: 0.34, vignette: 0.30, grain: 0.016 }
  };

  var VERT = [
    "varying vec2 vUv;",
    "void main() {",
    "  vUv = uv;",
    "  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);",
    "}"
  ].join("\n");

  var FRAG = [
    "precision highp float;",
    "uniform sampler2D tDiffuse;",
    "uniform vec2  uResolution;",
    "uniform float uTime;",
    "uniform float uIntensity;",
    "uniform int   uMode;",
    "uniform vec3  uTint;",
    "uniform float uExposure;",
    "uniform float uContrast;",
    "uniform float uSaturation;",
    "uniform float uHaze;",
    "uniform float uVignette;",
    "uniform float uGrain;",
    "uniform vec2  uWind;",
    "varying vec2 vUv;",
    "",
    "float hash12(vec2 p) {",
    "  vec3 p3 = fract(vec3(p.xyx) * 0.1031);",
    "  p3 += dot(p3, p3.yzx + 33.33);",
    "  return fract((p3.x + p3.y) * p3.z);",
    "}",
    "float vnoise(vec2 p) {",
    "  vec2 i = floor(p), f = fract(p);",
    "  f = f * f * (3.0 - 2.0 * f);",
    "  float a = hash12(i), b = hash12(i + vec2(1.0, 0.0));",
    "  float c = hash12(i + vec2(0.0, 1.0)), d = hash12(i + vec2(1.0, 1.0));",
    "  return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);",
    "}",
    "float fbm(vec2 p) {",
    "  float v = 0.0, a = 0.5;",
    "  for (int i = 0; i < 4; i++) { v += a * vnoise(p); p *= 2.03; a *= 0.5; }",
    "  return v;",
    "}",
    "vec3 grade(vec3 c) {",
    "  c *= uExposure;",
    "  c = (c - 0.5) * uContrast + 0.5;",
    "  float l = dot(c, vec3(0.2126, 0.7152, 0.0722));",
    "  c = mix(vec3(l), c, uSaturation);",
    "  c = mix(c * uTint, c, smoothstep(0.0, 0.62, l));",
    "  return c;",
    "}",
    "float rainLayer(vec2 uv, float t) {",
    "  vec2 p = vec2(uv.x * uResolution.x / uResolution.y, uv.y);",
    "  vec2 dir = normalize(uWind + vec2(0.0, -1.0));",
    "  vec2 q = vec2(dot(p, vec2(dir.y, -dir.x)), dot(p, dir));",
    "  vec2 g = vec2(q.x * 52.0, q.y * 4.5 + t * 5.5);",
    "  vec2 id = floor(g); vec2 f = fract(g) - 0.5;",
    "  float h = hash12(id);",
    "  float w = smoothstep(0.5, 0.0, abs(f.x) * 7.5);",
    "  float l = smoothstep(0.5, 0.0, abs(f.y));",
    "  float streak = w * l * step(0.74, h);",
    "  vec2 g2 = vec2(p.x * 11.0 + t * 0.10, p.y * 8.0 + t * 0.45);",
    "  vec2 id2 = floor(g2); vec2 f2 = fract(g2) - 0.5;",
    "  float drop = smoothstep(0.22, 0.0, length(f2 * vec2(1.0, 0.72))) * step(0.96, hash12(id2 + 7.0));",
    "  return clamp(streak * 0.55 + drop * 0.85, 0.0, 1.0);",
    "}",
    "float snowLayer(vec2 uv, float t) {",
    "  vec2 p = vec2(uv.x * uResolution.x / uResolution.y, uv.y);",
    "  float acc = 0.0;",
    "  for (int i = 0; i < 3; i++) {",
    "    float s = 1.0 + float(i) * 1.7;",
    "    vec2 g = p * (9.0 * s) + vec2(t * 0.22 * s, t * 0.85 * s);",
    "    vec2 id = floor(g); vec2 f = fract(g) - 0.5;",
    "    float h = hash12(id + float(i) * 13.0);",
    "    acc += smoothstep(0.26 / s, 0.0, length(f)) * step(0.88, h) * (0.58 - 0.13 * float(i));",
    "  }",
    "  return clamp(acc, 0.0, 1.0);",
    "}",
    "float dustLayer(vec2 uv, float t) {",
    "  vec2 p = vec2(uv.x * uResolution.x / uResolution.y, uv.y);",
    "  float n = fbm(vec2(p.x * 2.2 + t * 0.06, p.y * 3.4 + t * 0.03));",
    "  return smoothstep(0.36, 0.86, n);",
    "}",
    "void main() {",
    "  vec3 c = texture2D(tDiffuse, vUv).rgb;",
    "  c = grade(c);",
    "  float horizon = exp(-pow((vUv.y - 0.52) / 0.26, 2.0));",
    "  float haze = uHaze * (0.35 + 0.65 * horizon);",
    "  c = mix(c, uTint * (0.78 + 0.22 * vUv.y), haze * 0.55);",
    "  if (uMode == 1) {",
    "    float r = rainLayer(vUv, uTime) * uIntensity;",
    "    c = mix(c, vec3(0.78, 0.86, 0.96), r * 0.30);",
    "  } else if (uMode == 2) {",
    "    float s = snowLayer(vUv, uTime) * uIntensity;",
    "    c = mix(c, vec3(0.98, 0.99, 1.0), s * 0.45);",
    "  } else if (uMode == 3) {",
    "    float d = dustLayer(vUv, uTime) * uIntensity;",
    "    c = mix(c, uTint * 1.08, d * 0.35);",
    "  }",
    "  float vig = smoothstep(0.35, 1.15, length((vUv - 0.5) * vec2(1.0, 1.1)) * 1.35);",
    "  c *= 1.0 - uVignette * vig;",
    "  float g = hash12(vUv * uResolution + uTime * 37.0) - 0.5;",
    "  c += g * uGrain;",
    "  gl_FragColor = vec4(clamp(c, 0.0, 1.0), 1.0);",
    "}"
  ].join("\n");

  function num(v, d) { var n = Number(v); return isFinite(n) ? n : d; }
  function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }

  /**
   * @param {object} opts { THREE, composer, renderer, width, height }
   * @returns {null | { pass, setWeather(preset,intensity,windDir), update(dt), setSize(w,h), stats }}
   */
  function create(opts) {
    try {
      opts = opts || {};
      var T = opts.THREE || (typeof THREE !== "undefined" ? THREE : null);
      if (!T) throw new Error("THREE 未加载");
      if (!opts.composer) throw new Error("缺少 composer");
      if (!T.ShaderPass) throw new Error("缺少 ShaderPass");

      var w = num(opts.width, 1600), h = num(opts.height, 1000);
      var shader = {
        uniforms: {
          tDiffuse: { value: null },
          uResolution: { value: new T.Vector2(w, h) },
          uTime: { value: 0 },
          uIntensity: { value: 0 },
          uMode: { value: 0 },
          uTint: { value: new T.Color(0x9ec8ff) },
          uExposure: { value: 1 },
          uContrast: { value: 1 },
          uSaturation: { value: 1 },
          uHaze: { value: 0 },
          uVignette: { value: 0.18 },
          uGrain: { value: 0.006 },
          uWind: { value: new T.Vector2(0, -1) }
        },
        vertexShader: VERT,
        fragmentShader: FRAG
      };

      var pass = new T.ShaderPass(shader);
      pass.renderToScreen = true;      // 作为后处理链最后一环
      var u = pass.uniforms;

      var cur = { preset: "clear", intensity: 0, mode: 0 };
      var tmp = new T.Color();
      var stats = { ms: 0, preset: "clear", intensity: 0, mode: 0 };
      // 预建颜色对象：setWeather 每帧都会被调用，绝不能在里面 new Color（会制造 GC 抖动）
      var colorCache = {};
      for (var ck in GRADE) {
        if (Object.prototype.hasOwnProperty.call(GRADE, ck)) {
          colorCache[ck] = new T.Color(GRADE[ck].tint);
        }
      }

      /** 设置天气（每帧调用也安全：只有变化时才写 uniform） */
      function setWeather(preset, intensity, windDirDeg) {
        var p = String(preset || "clear").toLowerCase();
        if (!GRADE[p]) p = "clear";
        var g = GRADE[p];
        var k = clamp01(num(intensity, p === "clear" ? 0 : 0.6));
        if (p === "clear") k = 0;
        cur.preset = p; cur.intensity = k; cur.mode = g.mode;

        // clear 直接吃 clear 配方；非 clear 按强度在 clear 与目标配方之间插值
        var c0 = GRADE.clear;
        u.uMode.value = g.mode;
        u.uIntensity.value = k;
        u.uExposure.value = c0.exposure + (g.exposure - c0.exposure) * k;
        u.uContrast.value = c0.contrast + (g.contrast - c0.contrast) * k;
        u.uSaturation.value = c0.saturation + (g.saturation - c0.saturation) * k;
        u.uHaze.value = c0.haze + (g.haze - c0.haze) * k;
        u.uVignette.value = c0.vignette + (g.vignette - c0.vignette) * k;
        u.uGrain.value = c0.grain + (g.grain - c0.grain) * k;

        tmp.copy(colorCache.clear).lerp(colorCache[p], k);
        u.uTint.value.copy(tmp);

        // 屏幕空间风向：把"世界风向角"粗略映射成 2D（只求观感一致）
        var a = num(windDirDeg, 90) * Math.PI / 180;
        u.uWind.value.set(Math.sin(a) * 0.55, -1.0).normalize();
      }

      function update(dt) {
        var d = clamp01(num(dt, 0));
        u.uTime.value += d;
        stats.preset = cur.preset;
        stats.intensity = cur.intensity;
        stats.mode = cur.mode;
      }

      function setSize(nw, nh) {
        var a = Math.max(1, num(nw, 1)), b = Math.max(1, num(nh, 1));
        u.uResolution.value.set(a, b);
      }

      function dispose() {
        try { if (pass.material) pass.material.dispose(); } catch (e) { /* 忽略 */ }
      }

      return { pass: pass, setWeather: setWeather, update: update,
               setSize: setSize, dispose: dispose, stats: stats, version: VERSION };
    } catch (e) {
      if (typeof console !== "undefined" && console.warn) {
        console.warn("[WeatherGrade] 初始化失败（已跳过色彩分级）：" + (e && e.message ? e.message : e));
      }
      return null;
    }
  }

  global.UAVWeatherGrade = { create: create, version: VERSION, GRADE: GRADE };
})(typeof window !== "undefined" ? window : this);
