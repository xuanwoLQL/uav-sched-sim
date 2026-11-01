(function(){
  const D = window.__DBG;
  const out = { weather: D.weather ? { current: D.weather.current, stats: D.weather.stats } : null,
                 hud: D.hud ? D.hud.stats : null };
  // 列出场景里"带 canvas 贴图的大面片"和粒子/线，定位到底是什么在场景里发白
  const big = [];
  D.scene.traverse(function(o){
    const m = o.material;
    if (!m) return;
    const map = m.map;
    const w = map && map.image && map.image.width ? map.image.width : 0;
    if (o.type === "Points" || o.type === "LineSegments" || (w >= 256 && o.type === "Mesh")) {
      big.push({ type: o.type, name: o.name || "", parent: o.parent ? (o.parent.name || o.parent.type) : "",
                 map: w, color: m.color ? m.color.getHexString() : null,
                 opacity: m.opacity, transparent: !!m.transparent, visible: o.visible,
                 blending: m.blending, depthWrite: m.depthWrite });
    }
  });
  // 只保留有可疑的大面积透明物
  out.suspects = big.filter(function(x){ return x.type !== "Points" || x.map >= 32; }).slice(0, 25);
  out.counts = { points: big.filter(function(x){return x.type==="Points";}).length,
                 lines: big.filter(function(x){return x.type==="LineSegments";}).length,
                 bigMeshes: big.filter(function(x){return x.type==="Mesh";}).length };
  return JSON.stringify(out);
})()
