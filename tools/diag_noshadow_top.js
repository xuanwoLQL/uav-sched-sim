/* 诊断：关掉方向光阴影，看俯视的"矩形/斜条纹"是否来自 shadow map */
(function () {
  var n = 0;
  window.__DBG.scene.traverse(function (o) {
    if (o.isDirectionalLight && o.castShadow) { o.castShadow = false; n++; }
  });
  document.querySelectorAll("[data-cam]")[1].click();
  return "shadowLightsOff=" + n;
})()
