/* 诊断：地形顶点色关掉（vertexColors=false），只看光照/法线对亮度块的影响 */
(function () {
  var n = 0;
  window.__DBG.scene.traverse(function (o) {
    var m = o.material;
    if (m && m.vertexColors && o.name && o.name.indexOf("terrain") >= 0) {
      m.vertexColors = false; m.needsUpdate = true; n++;
    }
  });
  document.querySelectorAll("[data-cam]")[1].click();
  return "vertexColors off: " + n;
})()
