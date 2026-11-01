/* 复现用户视角：拉远 + 抬高（世界已放大 2 倍，这里用新的远机位） */
(function () {
  var c = window.__DBG.camera, ctl = window.__DBG.controls;
  c.position.set(200, 270, 430);     // 高空斜视（比默认 iso 更远更高）
  ctl.target.set(-8, -12, -12);
  ctl.update();
  return "cam=far-oblique " + c.position.toArray().map(function (v) { return v.toFixed(0); }).join(",");
})()
