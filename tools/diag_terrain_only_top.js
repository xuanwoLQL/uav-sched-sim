/* 诊断：只留地形 —— 隐藏所有 InstancedMesh（树/碎石）、点云、HUD、天气、阴影，再俯视 */
(function () {
  var hidden = 0;
  window.__DBG.scene.traverse(function (o) {
    if (o.isInstancedMesh) { o.visible = false; hidden++; }
    if (o.isPoints || o.isLineSegments || o.isSprite) { o.visible = false; }
    if (o.isDirectionalLight && o.castShadow) { o.castShadow = false; }
    if (o.name && (o.name.indexOf("UAVHUD") >= 0)) { o.visible = false; }
  });
  document.querySelectorAll("[data-cam]")[1].click();
  return "instancedHidden=" + hidden;
})()
