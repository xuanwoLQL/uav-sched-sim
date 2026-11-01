/* 诊断：地形纹理贴图全部关掉（map=null），看俯视斜条纹/亮度块是否消失 */
(function () {
  var n = 0;
  window.__DBG.scene.traverse(function (o) {
    var m = o.material;
    if (m && m.map && o.name && o.name.indexOf("terrain") >= 0) {
      m.map = null; m.needsUpdate = true; n++;
    }
  });
  document.querySelectorAll("[data-cam]")[1].click();
  return "terrain maps disabled: " + n;
})()
