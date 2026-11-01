/* =============================================================================
 * uav_model.js —— 无人机模型细化（window.UAVModel）
 * -----------------------------------------------------------------------------
 * 对应 docs/07-3D渲染升级接口契约.md §4.3。
 *
 * 升级前 app3d.html 里的 makeDrone()：机身 + 四臂 + 4 个**实心圆片**当旋翼。
 * 旋翼永远像"停了"，没有云台，起落架是 4 根焊死的柱子 —— 这是本模块要修的三件事。
 *
 * 三件真实飞行器的可动件 → 代码落点：
 *   1) 桨叶运动模糊：
 *      相机快门时间内，桨叶扫过的角度越大，画面上的桨叶越"糊"、越像一个半透明圆盘。
 *      所以低转速画 2 片细长薄桨叶（0.9 不透明），转速升高时桨叶淡出到 0.25，
 *      同时把一片"加色混合的运动模糊盘"淡入（待机 0.06 → 巡航 0.45 → 高负载 0.62）。
 *      盘的 fragment shader 用"双叶扫掠瓣 + 细密桨影 + 桨尖涡流亮边"叠加，
 *      相位随时间推进 —— 所以静止截图里也看得出它在转，而不是一个实心圆片。
 *   2) 二轴云台跟随：
 *      真实吊舱是 yaw + pitch 两级增稳，有最大角速度、有阻尼、机身滚转不会传给镜头。
 *      这里用"阻尼 lerp + 角速度限幅（≈120°/s）"逼近目标，并叠加 ±0.5° 稳像抖动。
 *      pitch 限幅 [-85°, +20°]（低头最多 85°，抬头被机身挡住只有 20°）。
 *      PAD/LAND 回正朝前下方，SURVEY 俯视扫描点（或缓慢左右扫），其余阶段朝前。
 *   3) 起落架收放：
 *      4 条腿分 2 组（前/后）挂在铰链 Group 上，PAD/LAND 放下、其余收起，
 *      0.35 s 线性进度 + smoothstep 缓动；只旋转腿部，机身/机臂高度不动。
 *
 * 性能（契约 §4.2 性能条）：
 *   - 几何体全部在**模块级资产缓存**里，30 架机共享同一批 BufferGeometry；
 *   - 静态机壳在构建时就合并成 1~2 个 BufferGeometry（机臂/电机不再各占一个 draw call），
 *     每架机 Mesh 数：high/medium 17 个、cpu 10 个（cpu 关模糊盘、合并机壳、减段数）；
 *   - anim() 里**不做任何 new**：目标角/临时量都用复用对象或数值变量。
 *
 * three.js **r128** 经典脚本：<script src="/static/uav_model.js"></script>，
 * 全局 THREE。加载期只做 IIFE + 常量（契约 §2.3：脚本加载期不 new THREE 对象），
 * 所有几何/材质在第一次 create() 时才构建。
 *
 * 对外 API：
 *   window.UAVModel = { version, create(opts), selfTest() }
 *   create() 返回的 THREE.Group 上：
 *     userData = {
 *       rotors, led, ledMat, cone, coneMat,   // ← 与旧 makeDrone() 完全兼容
 *       anim(dt, ctx),                        // ← 主循环每帧调用
 *       gimbal: {yaw, pitch, group} | null,
 *       gear: [铰链 Group, ...],
 *       dispose(), tier, stats
 *     }
 * ========================================================================== */
(function () {
  "use strict";

  var VERSION = "1.0.0";

  /* ---------------------------------------------------------------- 常量 */
  var TWO_PI = Math.PI * 2;
  var DEG = Math.PI / 180;

  var ROTOR_XZ = 2.2;                  // 电机/旋翼中心到机身中心（与原 makeDrone 一致：±2.2）
  var ROTOR_Y = 0.35;                  // 旋翼平面高度（原值 0.35）
  var ROTOR_CORNERS = [[1, 1], [1, -1], [-1, 1], [-1, -1]];

  var GEAR_FOLD = 1.42;                // 起落架收起时腿绕 X 轴转过的角度（≈81°）
  var GEAR_EASE_S = 0.35;              // 契约要求：0.35 s 缓动

  var GIMBAL_MAX_RATE = 2.1;           // 云台最大角速度 rad/s（≈120°/s，真实吊舱量级）
  var PITCH_MIN = -85 * DEG;           // 低头最大 85°（契约）
  var PITCH_MAX = 20 * DEG;            // 抬头最大 20°（契约）

  var EMPTY_CTX = {};                  // anim(dt) 缺 ctx 时的兜底（模块级，不分配）
  var _tgt = { yaw: 0, pitch: 0 };     // 云台目标角复用对象（零分配）
  var THREE_G = null;                  // 延迟捕获全局 THREE（脚本加载期不碰它）

  /* 三个档位的默认参数。Lead 的 opts.quality 可以逐项覆盖。
     high/medium：模糊盘开、机壳分 2 个材质（浅色金属 + 深色件）、云台带支架和镜头。
     cpu：关模糊盘、合并成一个机壳 mesh、删掉云台支架/镜头发光片，Mesh 压到 10 个。 */
  var TIER_PRESETS = {
    high: {
      rotorDisc: true, discSeg: 40, bladeSeg: 3, lensGlass: true, yoke: true,
      mergedHull: false, gimbal: true, gear: true, led: true, cone: true,
      shadows: true, coneSeg: 18, ledSeg: 14
    },
    medium: {
      rotorDisc: true, discSeg: 24, bladeSeg: 2, lensGlass: true, yoke: true,
      mergedHull: false, gimbal: true, gear: true, led: true, cone: true,
      shadows: true, coneSeg: 14, ledSeg: 10
    },
    cpu: {
      rotorDisc: false, discSeg: 8, bladeSeg: 1, lensGlass: false, yoke: false,
      mergedHull: true, gimbal: true, gear: true, led: true, cone: true,
      shadows: false, coneSeg: 10, ledSeg: 8
    }
  };

  /* ================================================================ 纯数学工具
     下面这些小函数都是"纯逻辑"，selfTest() 可以脱离 renderer 直接验证。 */

  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

  /** 经典 smoothstep：0→0，1→1，中点 0.5；用来做起落架缓动 */
  function smoothstep01(x) {
    x = clamp(x, 0, 1);
    return x * x * (3 - 2 * x);
  }

  /** 把角度差折到 [-π, π]，否则云台会绕远路转 */
  function wrapPi(a) {
    a = (a + Math.PI) % TWO_PI;
    if (a < 0) a += TWO_PI;
    return a - Math.PI;
  }

  /** 一阶指数趋近系数：rate 越大跟得越紧；dt 越小步长越小 —— 帧率无关的阻尼 */
  function dampK(rate, dt) { return 1 - Math.exp(-rate * dt); }

  /**
   * 运动模糊盘不透明度（核心曲线）。
   * 真实现象：转速越高，同一像素在一帧内被桨叶扫过的次数越多 → 盘越"实"。
   * 标定：待机 spin=9 ≈0.06（几乎看不见）；巡航 spin=27 ≈0.45；高负载 spin=38 ≈0.62。
   */
  function discOpacityForSpin(spin) {
    var t = smoothstep01((clamp(spin, 9, 38) - 9) / 29);
    return 0.06 + 0.56 * t;
  }

  /**
   * 桨叶本体透明度：低转速 0.9（能看清 2 片桨叶）→ 高转速 0.25（和模糊盘交叉淡出）。
   * 两条曲线加起来保持"总视觉能量"大致守恒，切换时不会忽明忽暗。
   */
  function bladeOpacityForSpin(spin) {
    var t = smoothstep01((clamp(spin, 9, 38) - 9) / 29);
    return 0.9 - 0.65 * t;
  }

  /** 架次阶段 → 起落架目标量（1 = 放下，0 = 收起）。CLIMB 的"过渡"由 0.35s 缓动体现 */
  function gearTargetForPhase(phase) {
    return (phase === "PAD" || phase === "LAND") ? 1 : 0;
  }

  /** 云台俯仰限幅：正 = 抬头，负 = 低头 */
  function clampPitch(p) { return clamp(p, PITCH_MIN, PITCH_MAX); }

  /** 局部方向 (lx,ly,lz) → 云台 yaw/pitch，结果写进复用对象 out（零分配） */
  function aimLocal(out, lx, ly, lz) {
    var h = Math.sqrt(lx * lx + lz * lz);
    out.yaw = Math.atan2(lx, lz);
    out.pitch = clampPitch(Math.atan2(ly, h < 1e-4 ? 1e-4 : h));
    return out;
  }

  /**
   * 计算云台两轴的目标角（纯函数，结果写进 out）。
   * 世界坐标 → 机体系时只按"机头偏航"旋转，忽略压坡 bank：
   * 真实云台有增稳，机身横滚本来就不该传给镜头。
   *
   * @param out      {yaw, pitch} 复用对象
   * @param phase    架次阶段
   * @param lookTarget 可选 {x,y,z} 世界坐标
   * @param px,py,pz 无人机世界坐标（机体系原点）
   * @param droneYaw 机头偏航（弧度）
   * @param t        累计时间（无注视点时的扫视用）
   * @param seed     每架机相位种子
   */
  function computeGimbalTarget(out, phase, lookTarget, px, py, pz, droneYaw, t, seed) {
    out.yaw = 0;
    out.pitch = -0.18;                    // 默认：朝前略低头（约 -10°）

    if (phase === "PAD" || phase === "LAND") {
      out.pitch = -0.38;                  // 待机/降落：回正朝前下方（约 -22°）
      return out;
    }
    if (phase === "SURVEY") {
      if (lookTarget &&
          typeof lookTarget.x === "number" && isFinite(lookTarget.x) &&
          typeof lookTarget.y === "number" && isFinite(lookTarget.y) &&
          typeof lookTarget.z === "number" && isFinite(lookTarget.z)) {
        var dx = lookTarget.x - px;
        var dy = lookTarget.y - py;
        var dz = lookTarget.z - pz;
        var c = Math.cos(droneYaw), s = Math.sin(droneYaw);
        // 世界增量绕 Y 轴转 -yaw → 机体局部（+Z 为机头）
        aimLocal(out, dx * c - dz * s, dy, dx * s + dz * c);
      } else {
        out.pitch = -1.35;                                     // 无注视点：近似垂直向下 -77°
        out.yaw = Math.sin(t * 0.55 + seed) * 0.28;            // 像真机一样缓慢左右扫
      }
      return out;
    }
    /* CLIMB / TRANSIT / RETURN：镜头朝前，保持"看向航向"的作业感 */
    out.pitch = -0.14;
    return out;
  }

  /** 架次阶段 → 默认转速标度（与 app3d.html 旧逻辑一致） */
  function defaultSpinForPhase(phase, busy) {
    if (phase === "PAD") return 9;
    if (phase === "LAND" || phase === "CLIMB") return 18;
    return busy ? 38 : 27;
  }

  /* ================================================================ 日志
     约定：模块把"正文"交给 opts.log，由调用方决定前缀渲染方式
     （app3d.html 的接法是 console.log("[UAV]", msg)）；没有传 log 时默认带 [UAV] 前缀。 */
  function logLine(opts, msg) {
    var log = opts && typeof opts.log === "function" ? opts.log : null;
    if (log) { log(msg); return; }
    if (typeof console !== "undefined" && console.log) console.log("[UAV] " + msg);
  }

  /* ================================================================ 几何合并
     把若干 {geo, m} 合并成一个 BufferGeometry（m 为 Matrix4，可为空）。
     目的：机臂/电机/机盖这些"永远一起动"的小零件不该各占一个 draw call。
     只在构建模块级资产时调用一次，不在乎分配。 */
  function mergeParts(parts) {
    var i, k, g, list = [], totalV = 0, totalI = 0;
    for (i = 0; i < parts.length; i++) {
      g = parts[i].geo.clone();
      if (parts[i].m) g.applyMatrix4(parts[i].m);
      if (!g.index) g = g.toNonIndexed();        // 兜底：本模块图元都带索引
      totalV += g.attributes.position.count;
      totalI += g.index ? g.index.count : g.attributes.position.count;
      list.push(g);
    }
    var pos = new Float32Array(totalV * 3);
    var nrm = new Float32Array(totalV * 3);
    var uv = new Float32Array(totalV * 2);
    var idx = totalV > 65535 ? new Uint32Array(totalI) : new Uint16Array(totalI);
    var vo = 0, io = 0;
    for (i = 0; i < list.length; i++) {
      g = list[i];
      var n = g.attributes.position.count;
      pos.set(g.attributes.position.array, vo * 3);
      if (g.attributes.normal) nrm.set(g.attributes.normal.array, vo * 3);
      if (g.attributes.uv) uv.set(g.attributes.uv.array, vo * 2);
      if (g.index) {
        var ia = g.index.array;
        for (k = 0; k < ia.length; k++) idx[io++] = ia[k] + vo;
      } else {
        for (k = 0; k < n; k++) idx[io++] = k + vo;
      }
      vo += n;
      g.dispose();
    }
    var out = new THREE_G.BufferGeometry();
    out.setAttribute("position", new THREE_G.BufferAttribute(pos, 3));
    out.setAttribute("normal", new THREE_G.BufferAttribute(nrm, 3));
    out.setAttribute("uv", new THREE_G.BufferAttribute(uv, 2));
    out.setIndex(new THREE_G.BufferAttribute(idx, 1));
    out.computeBoundingSphere();
    return out;
  }

  /** 建资产用的矩阵：先按欧拉角旋转（度数为弧度），再平移到 (px,py,pz) */
  function mat4(px, py, pz, rx, ry, rz) {
    var m = new THREE_G.Matrix4();
    if (rx || ry || rz) {
      m.makeRotationFromEuler(new THREE_G.Euler(rx || 0, ry || 0, rz || 0, "XYZ"));
    }
    m.setPosition(px || 0, py || 0, pz || 0);
    return m;
  }

  /* ================================================================ 运动模糊盘 shader
     为什么不用普通贴图：需要"双叶扫掠瓣 + 细密桨影"两层频率叠加，且相位每帧推进；
     程序化 shader 不需要生成 canvas 贴图，也不受 UV 分辨率限制（契约允许 ShaderMaterial）。
     加色混合 + depthWrite:false：盘只是"光的叠加"，不遮挡后面的东西，30 架叠在一起也不发黑。 */
  var DISC_VERT = [
    "varying vec2 vUv;",
    "void main() {",
    "  vUv = uv;",
    "  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);",
    "}"
  ].join("\n");

  var DISC_FRAG = [
    "precision mediump float;",
    "uniform float uOpacity;   // 随转速 0.06 → 0.62",
    "uniform float uBlur;      // 0 = 低转速（结构清晰） 1 = 高转速（扫掠瓣被曝光平均掉）",
    "uniform float uPhase;     // 盘面旋转相位，让静止画面也看得出在转",
    "uniform vec3  uTint;      // 桨盘冷青色（和 LED / 扫描锥的颜色区分开）",
    "varying vec2  vUv;",
    "void main() {",
    "  vec2 p = vUv * 2.0 - 1.0;",
    "  float r = length(p);",
    "  if (r > 1.0) discard;                       // 圆盘裁剪，省掉方角像素",
    "  float a = atan(p.y, p.x);",
    // 双叶桨扫掠瓣：低转速时能看出两片桨扫过的亮区；转速越高，曝光积分越接近均匀圆盘
    "  float lobe = 0.5 + 0.5 * cos(2.0 * (a - uPhase));",
    // 细密桨影：每圈扫过留下的细条纹，高转速时反而更明显（高频结构比低频结构更"耐模糊"）
    "  float streak = 0.5 + 0.5 * cos(13.0 * (a - uPhase * 0.8));",
    "  float lobeAmp = mix(0.24, 0.05, uBlur);",
    "  float streakAmp = mix(0.05, 0.12, uBlur);",
    // 径向权重：桨毂与外缘淡出，保持"半透明圆盘"的边界感
    "  float ring = smoothstep(0.05, 0.30, r) * (1.0 - smoothstep(0.86, 1.0, r));",
    "  float alpha = uOpacity * ring * (0.46 + lobeAmp * lobe + streakAmp * streak * lobe);",
    // 桨尖涡流亮边：真实照片里桨尖最亮
    "  alpha += uOpacity * 0.07 * smoothstep(0.80, 1.0, r) * (1.0 - smoothstep(0.92, 1.0, r));",
    "  vec3 col = uTint * (0.66 + mix(0.28, 0.10, uBlur) * lobe + streakAmp * 0.6 * streak);",
    "  gl_FragColor = vec4(col, alpha);",
    "}"
  ].join("\n");

  /* ================================================================ 模块级资产缓存
     key = 档位（high/medium/cpu）。第一次 create() 时构建，之后 30 架机全部共享。
     per-drone 材质只有 4 个（led/cone/blade/disc），因为它们各自要变颜色/透明度。 */
  var ASSETS = {};

  function buildAssets(tier) {
    var P = TIER_PRESETS[tier] || TIER_PRESETS.high;
    var THREE = THREE_G;
    var geo = {};
    var mat = {};
    var i, sx, sz;

    /* ---- 机壳（浅色金属件）：机身 2.1×0.42×2.1（与旧模型同尺寸）+ 4 条机臂 + 顶部机盖 ----
       机臂按"机身中心 → 电机"的真实方向摆放（旧版 45° 朝向算错了对角，臂和电机接不上）。 */
    var metalParts = [];
    metalParts.push({ geo: new THREE.BoxGeometry(2.1, 0.42, 2.1), m: null });
    for (i = 0; i < 4; i++) {
      sx = ROTOR_CORNERS[i][0];
      sz = ROTOR_CORNERS[i][1];
      var armAng = Math.atan2(-sz, sx);
      // 盒子的长轴是 +X；绕 Y 转 atan2(-sz, sx) 后正好指向该角上的电机
      metalParts.push({
        geo: new THREE.BoxGeometry(2.75, 0.12, 0.2),
        m: mat4(sx * 1.149, 0, sz * 1.149, 0, armAng, 0)
      });
      // 机臂根部整流罩：盖住"方盒接方盒"的直角，让机身和机臂过渡自然（合并进同一 mesh，零额外 draw call）
      metalParts.push({
        geo: new THREE.BoxGeometry(0.34, 0.17, 0.28),
        m: mat4(sx * 0.98, 0, sz * 0.98, 0, armAng, 0)
      });
    }
    // 机尾天线（细杆）：航测机常见的外置数传天线
    metalParts.push({ geo: new THREE.CylinderGeometry(0.015, 0.022, 0.5, 5), m: mat4(0, 0.44, -0.92) });

    /* ---- 深色件：机头 + 顶部机盖 + 4 个电机 + 电机座 + 机腹板 + 云台底座 ----
       注意：这些盒子与机身有交叠，但**不能共面**（共面会在远处视角出现 z-fighting 条纹）。 */
    var darkParts = [];
    darkParts.push({ geo: new THREE.BoxGeometry(0.85, 0.3, 0.9), m: mat4(0, 0.02, 1.2) });      // 机头（顶面 0.17 < 机身顶 0.21）
    darkParts.push({ geo: new THREE.BoxGeometry(1.15, 0.1, 1.25), m: mat4(0, 0.255, -0.05) }); // 低矮的深色机盖（底面沉进机身 0.005）
    darkParts.push({ geo: new THREE.CylinderGeometry(0.2, 0.2, 0.07, 12), m: mat4(0, 0.338, -0.38) }); // 顶部 GPS 圆盘
    darkParts.push({ geo: new THREE.BoxGeometry(1.7, 0.1, 1.7), m: mat4(0, -0.22, 0) });
    darkParts.push({ geo: new THREE.BoxGeometry(0.42, 0.12, 0.42), m: mat4(0, -0.3, 0.55) });
    for (i = 0; i < 4; i++) {
      sx = ROTOR_CORNERS[i][0];
      sz = ROTOR_CORNERS[i][1];
      darkParts.push({ geo: new THREE.CylinderGeometry(0.23, 0.23, 0.34, 10), m: mat4(sx * ROTOR_XZ, 0.16, sz * ROTOR_XZ) });
      darkParts.push({ geo: new THREE.BoxGeometry(0.44, 0.09, 0.44), m: mat4(sx * ROTOR_XZ, 0.02, sz * ROTOR_XZ) });
    }

    if (P.mergedHull) {
      // cpu 档：机壳全部合成 1 个 mesh（少一次 draw call 比重色差更重要）
      geo.hull = mergeParts(metalParts.concat(darkParts));
      geo.hullMetal = null;
      geo.hullDark = null;
    } else {
      geo.hullMetal = mergeParts(metalParts);
      geo.hullDark = mergeParts(darkParts);
    }

    /* ---- 4 条腿 × 2 组的起落架：铰链 Group 在机腹前后各一个，一组带 2 条腿 + 滑橇 ---- */
    var gearParts = [];
    for (i = 0; i < 2; i++) {
      sx = i === 0 ? -1 : 1;
      gearParts.push({ geo: new THREE.CylinderGeometry(0.055, 0.045, 0.72, 6), m: mat4(sx * 0.88, -0.36, 0) });
      gearParts.push({ geo: new THREE.BoxGeometry(0.2, 0.05, 0.2), m: mat4(sx * 0.88, -0.695, 0) });
    }
    gearParts.push({ geo: new THREE.BoxGeometry(1.88, 0.055, 0.13), m: mat4(0, -0.695, 0) });  // 滑橇横杆
    geo.gearPair = mergeParts(gearParts);

    /* ---- 旋翼桨叶：2 片"根宽尖窄"的薄桨（把 BoxGeometry 顶点按展向收窄，像真桨叶）+ 桨毂 ---- */
    function makeBladeGeo() {
      var bg = new THREE.BoxGeometry(0.8, 0.016, 0.13);
      var pa = bg.attributes.position;
      for (var v = 0; v < pa.count; v++) {
        var bx = pa.getX(v);                                    // -0.4 → +0.4（展向）
        var f = 1 - 0.55 * (bx + 0.4) / 0.8;                    // 根部 1.0 → 桨尖 0.45（缩窄）
        pa.setZ(v, pa.getZ(v) * f);                             // 弦长收窄
        pa.setY(v, pa.getY(v) * (0.55 + 0.45 * f));             // 厚度也收薄
      }
      pa.needsUpdate = true;
      bg.computeVertexNormals();
      return bg;
    }
    var bladeParts = [];
    bladeParts.push({ geo: makeBladeGeo(), m: mat4(0.5, 0, 0, 0.22, 0, 0) });    // 桨距角让薄片有迎角
    bladeParts.push({ geo: makeBladeGeo(), m: mat4(-0.5, 0, 0, -0.22, 0, 0) });
    bladeParts.push({ geo: new THREE.CylinderGeometry(0.07, 0.06, 0.09, 8), m: null });
    geo.blade = mergeParts(bladeParts);

    /* ---- 运动模糊盘：半径 0.95（旧圆片同尺寸），段数按档位降 ---- */
    geo.disc = P.rotorDisc
      ? new THREE.CircleGeometry(0.95, Math.max(8, P.discSeg | 0))
      : null;

    /* ---- 云台：支架（yaw 段）+ 相机机身/镜头（pitch 段）+ 镜头发光片 ---- */
    if (P.gimbal) {
      if (P.yoke) {
        geo.gimbalYoke = mergeParts([
          { geo: new THREE.BoxGeometry(0.06, 0.2, 0.26), m: mat4(0.2, -0.06, 0) },
          { geo: new THREE.BoxGeometry(0.06, 0.2, 0.26), m: mat4(-0.2, -0.06, 0) },
          { geo: new THREE.BoxGeometry(0.44, 0.06, 0.2), m: mat4(0, 0.04, 0) }
        ]);
      } else {
        geo.gimbalYoke = null;
      }
      var camParts = [];
      camParts.push({ geo: new THREE.BoxGeometry(0.32, 0.28, 0.42), m: null });
      camParts.push({ geo: new THREE.CylinderGeometry(0.1, 0.115, 0.2, 12), m: mat4(0, -0.01, 0.31, Math.PI / 2, 0, 0) });
      geo.gimbalCam = mergeParts(camParts);
      geo.lensGlass = P.lensGlass
        ? new THREE.CylinderGeometry(0.082, 0.082, 0.016, 12)
        : null;
    } else {
      geo.gimbalYoke = null;
      geo.gimbalCam = null;
      geo.lensGlass = null;
    }

    /* ---- LED 指示球 / 扫描锥（尺寸与旧 makeDrone 完全一致） ---- */
    geo.led = new THREE.SphereGeometry(0.24, Math.max(6, P.ledSeg | 0), Math.max(6, (P.ledSeg - 2) | 0));
    geo.cone = new THREE.ConeGeometry(4.2, 13, Math.max(6, P.coneSeg | 0), 1, true);

    /* ---- 共享材质（不随实例变化的部分） ---- */
    mat.metal = new THREE.MeshStandardMaterial({ color: 0x9aa9b6, roughness: 0.4, metalness: 0.6 });
    mat.dark = new THREE.MeshStandardMaterial({ color: 0x232b33, roughness: 0.55, metalness: 0.65 });
    mat.glass = new THREE.MeshBasicMaterial({ color: 0x37e2d5 });   // 镜头发光片（共享，不变色）

    return { tier: tier, geo: geo, mat: mat, preset: P };
  }

  function ensureAssets(tier, opts) {
    if (!ASSETS[tier]) {
      ASSETS[tier] = buildAssets(tier);
      logLine(opts, "几何资产构建完成（tier=" + tier + "，全机群共享同一批 BufferGeometry）");
    }
    return ASSETS[tier];
  }

  /* ================================================================ 工具：THREE 延迟捕获 */
  function pickThree(opts) {
    if (!THREE_G) {
      if (opts && opts.THREE) THREE_G = opts.THREE;
      else if (typeof window !== "undefined" && window.THREE) THREE_G = window.THREE;
      else if (typeof THREE !== "undefined") THREE_G = THREE;     // node / 其他全局兜底
    }
    return THREE_G;
  }

  function normalizeTier(tier) {
    return (tier === "medium" || tier === "cpu") ? tier : "high";
  }

  /** 把契约里的 opts.quality 覆盖项合并进档位默认值 */
  function resolveQuality(P, q) {
    q = q || {};
    return {
      rotorDisc: q.rotorDisc !== undefined ? !!q.rotorDisc : P.rotorDisc,
      gimbal: q.gimbal !== undefined ? !!q.gimbal : P.gimbal,
      gear: q.gear !== undefined ? !!q.gear : P.gear,
      led: q.led !== undefined ? !!q.led : P.led,
      cone: q.cone !== undefined ? !!q.cone : P.cone,
      lensGlass: q.lensGlass !== undefined ? !!q.lensGlass : P.lensGlass,
      shadows: q.shadows !== undefined ? !!q.shadows : P.shadows
    };
  }

  /** 统计 Group 下 Mesh 数量与三角面数（构建期算一次，给 Lead 的 debug 面板用） */
  function countStats(root) {
    var meshes = 0, tris = 0;
    root.traverse(function (o) {
      if (o.isMesh && o.geometry) {
        meshes++;
        var gi = o.geometry.index;
        tris += (gi ? gi.count : o.geometry.attributes.position.count) / 3;
      }
    });
    return { meshes: meshes, tris: Math.round(tris) };
  }

  /* ================================================================ create()
     入参：{ THREE, tier, quality:{rotorDisc,bladeSeg,gimbal,gear,led,cone,...}, log }
     返回：THREE.Group（失败返回 null，绝不抛异常把主场景搞崩 —— 契约 §3.3） */
  function create(opts) {
    try {
      opts = opts || {};
      var THREE = pickThree(opts);
      if (!THREE) {
        logLine(opts, "create 失败：全局 THREE 不存在（three.js 未加载？）");
        return null;
      }
      var tier = normalizeTier(opts.tier);
      var P = TIER_PRESETS[tier];
      var A = ensureAssets(tier, opts);
      var Q = resolveQuality(P, opts.quality);
      var hasDisc = !!(Q.rotorDisc && A.geo.disc);
      var hasGimbal = !!(Q.gimbal && A.geo.gimbalCam);
      var hasGear = !!(Q.gear && A.geo.gearPair);
      var hasLed = !!(Q.led && A.geo.led);
      var hasCone = !!(Q.cone && A.geo.cone);

      var g = new THREE.Group();
      g.name = "UAVModel";
      var sx, sz;

      /* 每个实例的可变状态都放在闭包里（不挂在 userData 上，避免 Lead 误改） */
      var st = {
        tier: tier,
        time: 0,
        spin: 0,
        angle: 0,
        bladeOp: 0.9,
        discOp: 0.0,
        discPhase: 0,
        gearAmt: 1,                 // 1 = 放下（初始在地面待机）
        gimbalYaw: 0,
        gimbalPitch: -0.38,
        sweepSeed: Math.random() * TWO_PI,
        phasePrev: "",
        phaseT: 0,                  // 当前阶段已持续时长（起落架"离地后再收"用）
        ever: false,
        disposed: false,
        hasDisc: hasDisc,
        rotors: [],
        discs: [],
        gears: [],
        gearSigns: [],
        bladeMat: null,
        discMat: null,
        ledMat: null,
        coneMat: null,
        gYaw: null,
        gPitch: null,
        gRoot: null
      };

      /* ---- 1. 机壳（静态件，全部合并过，最多 2 个 mesh） ---- */
      function addMesh(geo, mat, cast) {
        var m = new THREE.Mesh(geo, mat);
        m.castShadow = !!(cast && Q.shadows);
        m.receiveShadow = false;
        g.add(m);
        return m;
      }
      if (A.geo.hull) {
        addMesh(A.geo.hull, A.mat.metal, true);
      } else {
        addMesh(A.geo.hullMetal, A.mat.metal, true);
        addMesh(A.geo.hullDark, A.mat.dark, true);
      }

      /* ---- 2. 旋翼：4 个 Group（主循环旧代码直接改 rotation.y 也能转） ---- */
      st.bladeMat = new THREE.MeshStandardMaterial({
        color: 0xc2d4e2, roughness: 0.25, metalness: 0.8,
        transparent: true, opacity: 0.9, depthWrite: false
      });
      if (hasDisc) {
        st.discMat = new THREE.ShaderMaterial({
          uniforms: {
            uOpacity: { value: 0.06 },
            uBlur: { value: 0 },
            uPhase: { value: 0 },
            uTint: { value: new THREE.Color(0x9dc6e6) }
          },
          vertexShader: DISC_VERT,
          fragmentShader: DISC_FRAG,
          transparent: true,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
          depthTest: true,
          side: THREE.DoubleSide
        });
      }
      for (var i = 0; i < 4; i++) {
        sx = ROTOR_CORNERS[i][0];
        sz = ROTOR_CORNERS[i][1];
        var rg = new THREE.Group();
        rg.position.set(sx * ROTOR_XZ, ROTOR_Y, sz * ROTOR_XZ);
        var blades = new THREE.Mesh(A.geo.blade, st.bladeMat);
        blades.castShadow = false;            // 薄桨的阴影没有意义，还容易出锯齿
        blades.renderOrder = 2;               // 透明排序：桨叶画在模糊盘之后
        rg.add(blades);
        if (hasDisc) {
          var disc = new THREE.Mesh(A.geo.disc, st.discMat);
          disc.rotation.x = -Math.PI / 2;     // 圆片从 XY 平面放平到 XZ 平面
          disc.renderOrder = 1;
          disc.visible = false;               // 待机时几乎看不见，由 anim 按转速打开
          rg.add(disc);
          st.discs.push(disc);
        }
        g.add(rg);
        st.rotors.push(rg);
      }

      /* ---- 3. 机腹二轴云台：gimbalRoot → yaw Group → pitch Group → 相机 ---- */
      if (hasGimbal) {
        var gRoot = new THREE.Group();
        gRoot.position.set(0, -0.34, 0.55);
        var gYaw = new THREE.Group();
        var gPitch = new THREE.Group();
        gPitch.position.set(0, -0.16, 0);
        if (A.geo.gimbalYoke) {
          var yoke = new THREE.Mesh(A.geo.gimbalYoke, A.mat.dark);
          yoke.castShadow = Q.shadows;
          gYaw.add(yoke);
        }
        var cam = new THREE.Mesh(A.geo.gimbalCam, A.mat.dark);
        cam.castShadow = Q.shadows;
        gPitch.add(cam);
        if (A.geo.lensGlass) {
          var lens = new THREE.Mesh(A.geo.lensGlass, A.mat.glass);
          lens.position.set(0, -0.01, 0.423);   // 贴在镜头前端
          lens.rotation.x = Math.PI / 2;
          gPitch.add(lens);
        }
        gYaw.add(gPitch);
        gRoot.add(gYaw);
        g.add(gRoot);
        st.gRoot = gRoot;
        st.gYaw = gYaw;
        st.gPitch = gPitch;
      }

      /* ---- 4. 起落架：前后两个铰链 Group，各带 2 条腿 ---- */
      if (hasGear) {
        var fh = new THREE.Group();
        fh.position.set(0, -0.2, 0.88);        // 前铰链（+Z 机头方向）
        fh.add(new THREE.Mesh(A.geo.gearPair, A.mat.dark));
        fh.children[0].castShadow = Q.shadows;
        var rh = new THREE.Group();
        rh.position.set(0, -0.2, -0.88);       // 后铰链
        rh.add(new THREE.Mesh(A.geo.gearPair, A.mat.dark));
        rh.children[0].castShadow = Q.shadows;
        g.add(fh);
        g.add(rh);
        st.gears.push(fh, rh);
        // 收起时：前组转 +，后组转 -，两条腿都向机腹中心折（不会飞到机臂外面）
        st.gearSigns.push(1, -1);
      }

      /* ---- 5. LED / 扫描锥：材质引用必须给主循环留着（旧代码语义不变） ---- */
      st.ledMat = new THREE.MeshBasicMaterial({ color: 0x3ddc97 });
      var led = new THREE.Mesh(A.geo.led, st.ledMat);
      led.position.set(0, -0.26, -0.95);       // 机腹后下方，侧视/俯视都能看到
      g.add(led);

      st.coneMat = new THREE.MeshBasicMaterial({
        color: 0x37e2d5, transparent: true, opacity: 0,
        blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide
      });
      var cone = new THREE.Mesh(A.geo.cone, st.coneMat);
      cone.position.y = -6.9;
      cone.visible = false;
      g.add(cone);

      /* ============================================================== anim()
         主循环每帧调用一次：anim(dt, ctx)。
         ctx = { t, phase, spin, busy, priority, scanning, health, online,
                 bank, yawRate, lookTarget }（契约 §4.3；gearOverride 仅自检页用）
         约定：调用前 Lead 已经把 g.position / g.rotation 设置为本帧姿态。
         本函数内部零 new：所有临时量都是数字/闭包里的复用对象。 */
      function anim(dt, ctx) {
        if (st.disposed) return;
        ctx = ctx || EMPTY_CTX;
        dt = +dt || 0;
        if (dt < 0) dt = 0;
        if (dt > 0.1) dt = 0.1;                 // 切后台回来时 dt 会很大，钳一下防止"瞬移"

        var phase = typeof ctx.phase === "string" ? ctx.phase : "PAD";
        var busy = !!ctx.busy;
        var t = typeof ctx.t === "number" ? ctx.t : (st.time += dt);
        var spinT = typeof ctx.spin === "number"
          ? ctx.spin
          : defaultSpinForPhase(phase, busy);

        /* ---- 1) 旋翼：转速一阶平滑 + 桨叶/模糊盘交叉淡入淡出 ---- */
        if (!st.ever) { st.spin = spinT; st.ever = true; }      // 首帧直接对齐，避免从 0 起飞
        st.spin += (spinT - st.spin) * dampK(4.5, dt);
        st.angle += dt * st.spin * 1.85;                        // 1.85 是"视觉倍率"，见文件头注释
        if (st.angle > TWO_PI) st.angle %= TWO_PI;              // 防止长时间运行后浮点精度掉光
        // 对角旋翼同向（真实四旋翼的反扭矩配对）
        st.rotors[0].rotation.y = st.angle;
        st.rotors[1].rotation.y = -st.angle;
        st.rotors[2].rotation.y = -st.angle;
        st.rotors[3].rotation.y = st.angle;

        var bladeT = hasDisc ? bladeOpacityForSpin(st.spin) : 0.92;
        st.bladeOp += (bladeT - st.bladeOp) * dampK(6, dt);
        st.bladeMat.opacity = st.bladeOp;

        if (hasDisc) {
          var dOpT = discOpacityForSpin(st.spin);
          st.discOp += (dOpT - st.discOp) * dampK(7, dt);
          st.discMat.uniforms.uOpacity.value = st.discOp;
          // uBlur：转速越高，双叶扫掠瓣越被"曝光积分"平均掉，盘面越接近均匀半透明圆盘
          st.discMat.uniforms.uBlur.value = clamp((st.spin - 9) / 29, 0, 1);
          st.discPhase += dt * st.spin * 0.85;
          if (st.discPhase > TWO_PI) st.discPhase %= TWO_PI;
          st.discMat.uniforms.uPhase.value = st.discPhase;
          var dv = st.discOp > 0.008;
          st.discs[0].visible = dv;
          st.discs[1].visible = dv;
          st.discs[2].visible = dv;
          st.discs[3].visible = dv;
        }

        /* ---- 2) 起落架：PAD/LAND 放下，其余收起，0.35s 缓动（只转腿） ---- */
        if (st.gears.length) {
          if (phase !== st.phasePrev) { st.phasePrev = phase; st.phaseT = 0; }
          st.phaseT += dt;
          var gearT = gearTargetForPhase(phase);
          // 真机是离地之后才收腿：CLIMB 前 0.45s 保持放下，再按 0.35s 缓动收起
          if (phase === "CLIMB" && st.phaseT < 0.45) gearT = 1;
          if (ctx.gearOverride === 0 || ctx.gearOverride === 1) gearT = ctx.gearOverride;
          var step = dt / GEAR_EASE_S;
          if (st.gearAmt < gearT) st.gearAmt = Math.min(gearT, st.gearAmt + step);
          else if (st.gearAmt > gearT) st.gearAmt = Math.max(gearT, st.gearAmt - step);
          var eased = smoothstep01(st.gearAmt);                 // 缓入缓出
          var fold = (1 - eased) * GEAR_FOLD;
          st.gears[0].rotation.x = st.gearSigns[0] * fold;
          st.gears[1].rotation.x = st.gearSigns[1] * fold;
        }

        /* ---- 3) 云台：阻尼跟随 + 角速度限幅 + ±0.5° 稳像抖动 ---- */
        if (st.gRoot) {
          // Lead 的机群 Group 直接 add 在 scene 下，g.position 就是世界坐标；
          // 若 offline 就按待机姿态回正。
          var pose = (ctx.online === false) ? "PAD" : phase;
          computeGimbalTarget(_tgt, pose, ctx.lookTarget,
            g.position.x, g.position.y, g.position.z,
            g.rotation.y, t, st.sweepSeed);

          var dyaw = wrapPi(_tgt.yaw - st.gimbalYaw);
          var maxStep = GIMBAL_MAX_RATE * dt;
          var stepY = clamp(dyaw * dampK(6.5, dt), -maxStep, maxStep);
          st.gimbalYaw = wrapPi(st.gimbalYaw + stepY);

          var dpit = clampPitch(_tgt.pitch) - st.gimbalPitch;
          var stepP = clamp(dpit * dampK(7.5, dt), -maxStep, maxStep);
          st.gimbalPitch = clampPitch(st.gimbalPitch + stepP);

          // 稳像残余抖动：±0.5° ≈ 0.0087 rad（两个不同频率正弦叠加，看起来像伺服微颤）
          var jit = 0.00435 * (Math.sin(t * 23.1 + st.sweepSeed * 5.3) +
                               Math.sin(t * 14.7 + st.sweepSeed * 2.1));
          st.gYaw.rotation.y = st.gimbalYaw + jit;
          st.gPitch.rotation.x = -(st.gimbalPitch + jit * 0.6);
        }
      }

      /* ---- dispose：只释放本实例独有的 4 个材质；共享几何/共享材质不动 ---- */
      function dispose() {
        if (st.disposed) return;
        st.disposed = true;
        try {
          st.bladeMat.dispose();
          if (st.discMat) st.discMat.dispose();
          st.ledMat.dispose();
          st.coneMat.dispose();
        } catch (e) { /* 释放失败不影响主场景 */ }
        if (g.parent) g.parent.remove(g);
        while (g.children.length) g.remove(g.children[0]);
      }

      /* ---- 对外挂载（契约 §4.3） ---- */
      var cs = countStats(g);
      g.userData = {
        // 与旧 makeDrone() 完全兼容的 5 个字段
        rotors: st.rotors,
        led: led,
        ledMat: st.ledMat,
        cone: cone,
        coneMat: st.coneMat,
        // 新增
        anim: anim,
        gimbal: (st.gRoot ? { yaw: st.gYaw, pitch: st.gPitch, group: st.gRoot } : null),
        gear: st.gears,
        dispose: dispose,
        // 自描述，Lead 的 ?debug=1 面板可直接读
        tier: tier,
        stats: { meshes: cs.meshes, tris: cs.tris, tier: tier }
      };

      // 初始姿态：起落架放下、云台朝前下方
      if (st.gears.length) {
        st.gears[0].rotation.x = 0;
        st.gears[1].rotation.x = 0;
      }
      if (st.gRoot) {
        st.gYaw.rotation.y = 0;
        st.gPitch.rotation.x = -st.gimbalPitch;
      }
      return g;
    } catch (e) {
      logLine(opts, "create 异常（已兜住，返回 null）：" + (e && e.message ? e.message : e));
      return null;
    }
  }

  /* 小工具：仅为了让"构建顺序"注释块读起来连贯的 no-op（不产生任何副作用） */
  function byteSafeNoop() { }
  /* ================================================================ selfTest()
     纯逻辑 + 可选结构自检（页面里有 THREE 时顺便 create 两档模型数 Mesh）。
     返回 { ok, checks:[{name, ok, detail}], stats:{...} }（契约 §3.7）。 */
  function selfTest() {
    var checks = [];
    function ck(name, ok, detail) {
      checks.push({ name: name, ok: !!ok, detail: String(detail) });
    }

    /* ---- 1) 缓动 / 曲线（纯数学） ---- */
    ck("smoothstep01(0)=0", smoothstep01(0) === 0, smoothstep01(0));
    ck("smoothstep01(1)=1", smoothstep01(1) === 1, smoothstep01(1));
    ck("smoothstep01(0.5)=0.5", Math.abs(smoothstep01(0.5) - 0.5) < 1e-12, smoothstep01(0.5));
    var d9 = discOpacityForSpin(9), d18 = discOpacityForSpin(18),
        d27 = discOpacityForSpin(27), d38 = discOpacityForSpin(38);
    ck("模糊盘不透明度随转速单调递增",
      d9 < d18 && d18 < d27 && d27 < d38,
      d9.toFixed(3) + " → " + d18.toFixed(3) + " → " + d27.toFixed(3) + " → " + d38.toFixed(3));
    ck("待机模糊盘 ≈0.06", Math.abs(d9 - 0.06) < 0.015, d9.toFixed(3));
    ck("巡航模糊盘 ≈0.45", Math.abs(d27 - 0.45) < 0.06, d27.toFixed(3));
    ck("高负载模糊盘 ≈0.62", Math.abs(d38 - 0.62) < 0.02, d38.toFixed(3));
    var b9 = bladeOpacityForSpin(9), b27 = bladeOpacityForSpin(27), b38 = bladeOpacityForSpin(38);
    ck("桨叶低转速清晰（0.9）", b9 > 0.85, b9.toFixed(3));
    ck("桨叶高转速淡出（0.25）", Math.abs(b38 - 0.25) < 0.03, b38.toFixed(3));
    ck("桨叶与模糊盘交叉淡入淡出", b9 > b27 && b27 > b38 && d9 < d27 && d27 < d38,
      "blade " + b9.toFixed(2) + "→" + b38.toFixed(2) + " / disc " + d9.toFixed(2) + "→" + d38.toFixed(2));

    /* ---- 2) 起落架目标量 ---- */
    ck("PAD 起落架放下", gearTargetForPhase("PAD") === 1, gearTargetForPhase("PAD"));
    ck("LAND 起落架放下", gearTargetForPhase("LAND") === 1, gearTargetForPhase("LAND"));
    ck("CLIMB 起落架收起（0.35s 缓动过渡）", gearTargetForPhase("CLIMB") === 0, 0);
    ck("TRANSIT/SURVEY/RETURN 收起",
      gearTargetForPhase("TRANSIT") === 0 && gearTargetForPhase("SURVEY") === 0 &&
      gearTargetForPhase("RETURN") === 0, "0/0/0");

    /* ---- 3) 云台限幅与指向（纯函数） ---- */
    var o = { yaw: 0, pitch: 0 };
    computeGimbalTarget(o, "PAD", null, 0, 10, 0, 0, 0, 0);
    ck("PAD 云台回正朝前下方", Math.abs(o.yaw) < 1e-9 && o.pitch < -0.3 && o.pitch > -0.5,
      "yaw=" + o.yaw.toFixed(3) + " pitch=" + o.pitch.toFixed(3));
    computeGimbalTarget(o, "SURVEY", { x: 0, y: -1000, z: 0 }, 0, 10, 0, 0, 0, 0);
    ck("云台 pitch 下限 -85°", Math.abs(o.pitch - (-85 * DEG)) < 1e-9,
      (o.pitch / DEG).toFixed(2) + "°");
    computeGimbalTarget(o, "SURVEY", { x: 0, y: 1000, z: 0 }, 0, 10, 0, 0, 0, 0);
    ck("云台 pitch 上限 +20°", Math.abs(o.pitch - (20 * DEG)) < 1e-9,
      (o.pitch / DEG).toFixed(2) + "°");
    computeGimbalTarget(o, "SURVEY", { x: 5, y: 0, z: 5 }, 0, 10, 0, 0, 0, 0);
    ck("SURVEY 云台 yaw 指向目标（机头 +Z）",
      Math.abs(o.yaw - Math.atan2(5, 5)) < 1e-9,
      (o.yaw / DEG).toFixed(1) + "°（期望 45°）");
    var tgt = { x: 0, y: 0, z: 10 };
    computeGimbalTarget(o, "SURVEY", tgt, 0, 10, 0, Math.PI, 0, 0);
    ck("云台世界坐标→机体系（机头转 180° 后目标在身后）", Math.abs(Math.abs(o.yaw) - Math.PI) < 1e-9,
      (o.yaw / DEG).toFixed(1) + "°");
    computeGimbalTarget(o, "TRANSIT", { x: 0, y: -1000, z: 0 }, 0, 10, 0, 0, 0, 0);
    ck("TRANSIT 忽略注视点、镜头朝前",
      Math.abs(o.yaw) < 1e-9 && Math.abs(o.pitch + 0.14) < 1e-9,
      "yaw=" + o.yaw.toFixed(2) + " pitch=" + (o.pitch / DEG).toFixed(1) + "°");

    /* ---- 4) 结构自检（需要页面里有 THREE；自带 try/catch，失败也不抛） ---- */
    var stats = {
      version: VERSION,
      tiers: Object.keys(TIER_PRESETS),
      discOpacity: { pad: +d9.toFixed(3), cruise: +d27.toFixed(3), busy: +d38.toFixed(3) },
      bladeOpacity: { pad: +b9.toFixed(3), busy: +b38.toFixed(3) },
      meshes: null,
      tris: null,
      cacheTiers: Object.keys(ASSETS)
    };
    var THREE = pickThree(null);
    if (!THREE) {
      ck("结构自检（无 THREE，跳过）", true, "页面未加载 three.min.js，仅跑纯逻辑检查");
    } else {
      var hi = null, cpu = null;
      try {
        hi = create({ tier: "high", quality: { shadows: false }, log: byteSafeNoop });
        cpu = create({ tier: "cpu", quality: { shadows: false }, log: byteSafeNoop });
        var hiStats = hi ? hi.userData.stats : null;
        var cpuStats = cpu ? cpu.userData.stats : null;
        stats.meshes = { high: hiStats ? hiStats.meshes : -1, cpu: cpuStats ? cpuStats.meshes : -1 };
        stats.tris = { high: hiStats ? hiStats.tris : -1, cpu: cpuStats ? cpuStats.tris : -1 };
        stats.cacheTiers = Object.keys(ASSETS);

        ck("create(high) 返回 Group", !!hi && hi.isGroup, hi ? hi.type : "null");
        ck("create(cpu) 返回 Group", !!cpu && cpu.isGroup, cpu ? cpu.type : "null");
        ck("兼容字段 rotors/led/ledMat/cone/coneMat 齐全",
          !!(hi && hi.userData.rotors && hi.userData.rotors.length === 4 &&
             hi.userData.led && hi.userData.ledMat && hi.userData.cone && hi.userData.coneMat),
          "rotors=" + (hi && hi.userData.rotors ? hi.userData.rotors.length : 0));
        ck("新增字段 anim/gimbal/gear/dispose 齐全",
          !!(hi && typeof hi.userData.anim === "function" &&
             hi.userData.gimbal && hi.userData.gimbal.yaw && hi.userData.gimbal.pitch &&
             hi.userData.gimbal.group && Array.isArray(hi.userData.gear) &&
             typeof hi.userData.dispose === "function"),
          "gear=" + (hi && hi.userData.gear ? hi.userData.gear.length : 0) + " 组");
        ck("high 档 Mesh ≤18", hiStats && hiStats.meshes <= 18, hiStats ? hiStats.meshes : -1);
        ck("cpu 档 Mesh ≤10", cpuStats && cpuStats.meshes <= 10, cpuStats ? cpuStats.meshes : -1);
        ck("机身仍是旧 makeDrone 的 2.1×0.42×2.1（顶点级校验）", (function () {
          if (!hi) return false;
          var hull = hi.children[0];           // high 档第一个 mesh = 机身+机臂合并几何
          if (!hull || !hull.geometry) return false;
          var pa = hull.geometry.attributes.position;
          var found = 0;
          for (var v = 0; v < pa.count; v++) {
            var x = pa.getX(v), y = pa.getY(v), z = pa.getZ(v);
            // BoxGeometry(2.1, 0.42, 2.1) 的角点：x/z=±1.05，y=±0.21
            if (Math.abs(x - 1.05) < 1e-4 && Math.abs(y - 0.21) < 1e-4 && Math.abs(z - 1.05) < 1e-4) found |= 1;
            if (Math.abs(x + 1.05) < 1e-4 && Math.abs(y + 0.21) < 1e-4 && Math.abs(z + 1.05) < 1e-4) found |= 2;
          }
          return found === 3;
        })(), "机身角点 (±1.05, ±0.21, ±1.05) 存在");
        ck("旋翼包络与旧模型一致（桨盘半径 0.95 @ ±2.2）", (function () {
          if (!hi) return false;
          try {
            hi.updateMatrixWorld(true);
            var box = new THREE.Box3(), tmp = new THREE.Box3();
            hi.traverse(function (o) {
              if (!o.isMesh || o === hi.userData.cone || !o.geometry) return;
              if (!o.geometry.boundingBox) o.geometry.computeBoundingBox();
              tmp.copy(o.geometry.boundingBox).applyMatrix4(o.matrixWorld);
              box.union(tmp);
            });
            // 不含扫描锥时的整机包络：X/Z 到桨尖 ±3.15，Y 从起落架 -0.92 到机盖 0.41
            return Math.abs(box.max.x - 3.15) < 0.02 && Math.abs(box.max.z - 3.15) < 0.02 &&
                   box.min.y < -0.85 && box.max.y > 0.3;
          } catch (e) { return false; }
        })(), "桨尖半径 3.15 = 2.2 + 0.95");
        /* ---- anim() 功能自检：真的跑动画时间线，验证三个可动件的行为 ---- */
        ck("anim：PAD 起落架展开 / TRANSIT 收起", (function () {
          if (!hi || !hi.userData.gear || hi.userData.gear.length < 2) return false;
          var u = hi.userData, f;
          for (f = 0; f < 60; f++) u.anim(1 / 60, { t: f / 60, phase: "PAD", spin: 9 });
          var deployed = Math.abs(u.gear[0].rotation.x) < 0.02 && Math.abs(u.gear[1].rotation.x) < 0.02;
          for (f = 0; f < 72; f++) u.anim(1 / 60, { t: 1 + f / 60, phase: "TRANSIT", spin: 38, busy: true });
          var folded = Math.abs(u.gear[0].rotation.x) > 1.2 && Math.abs(u.gear[1].rotation.x) > 1.2;
          return deployed && folded;
        })(), "PAD 铰链 ≈0rad → TRANSIT 铰链 ≈±1.42rad");
        ck("anim：高转速桨叶淡出、模糊盘淡入", (function () {
          if (!hi) return false;
          var r0 = hi.userData.rotors[0];
          if (!r0 || r0.children.length < 2) return false;
          var bladeOp = r0.children[0].material.opacity;
          var discOp = r0.children[1].material.uniforms.uOpacity.value;
          return bladeOp < 0.35 && discOp > 0.55;
        })(), "spin=38 时桨叶≈0.25 / 盘≈0.62");
        ck("anim：SURVEY 云台俯视目标（pitch 收敛到 -85° 限幅）", (function () {
          if (!hi || !hi.userData.gimbal) return false;
          var g = hi.userData.gimbal, f;
          var target = { x: 0, y: -1000, z: 0 };
          for (f = 0; f < 180; f++) {
            hi.userData.anim(1 / 60, {
              t: 5 + f / 60, phase: "SURVEY", spin: 38, busy: true,
              scanning: true, lookTarget: target
            });
          }
          var pitch = -g.pitch.rotation.x;     // 正 = 抬头
          return Math.abs(pitch - (-85 * DEG)) < 0.02 && g.pitch.rotation.x > 1.4;
        })(), "pitch 收敛到 -85.0°（正的 rotation.x 表示低头）");
      } catch (e) {
        ck("结构自检异常", false, e && e.message ? e.message : String(e));
      } finally {
        try { if (hi) hi.userData.dispose(); } catch (e2) { /* ignore */ }
        try { if (cpu) cpu.userData.dispose(); } catch (e3) { /* ignore */ }
      }
    }

    var ok = checks.every(function (c) { return c.ok; });
    return { ok: ok, checks: checks, stats: stats };
  }

  /* ================================================================ 导出（唯一全局） */
  window.UAVModel = {
    version: VERSION,
    create: create,
    selfTest: selfTest
  };
})();
