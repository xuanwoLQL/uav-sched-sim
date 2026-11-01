/* 诊断：隐藏"扫描覆盖热力图"叠加层（additive、512 canvas、爹是 Scene），看画面是否恢复 */
(function () {
  var n = 0;
  window.__DBG.scene.children.forEach(function (o) {
    var m = o.material;
    if (o.isMesh && m && m.transparent && m.map && m.map.image && m.map.image.width === 512) {
      o.visible = false; n++;
    }
  });
  document.querySelectorAll("[data-cam]")[2].click();
  return "coverage-hidden=" + n;
})()
