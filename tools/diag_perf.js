(function () {
  const D = window.__DBG, r = D.renderer;
  const info = r.info.render;
  return JSON.stringify({
    calls: info.calls, triangles: info.triangles,
    geoMem: r.info.memory.geometries, texMem: r.info.memory.textures,
    ssaoMs: D.ssao ? D.ssao.stats.ms : null,
    terrain: D.terrain ? { tris: D.terrain.stats.tris, updateMs: D.terrain.stats.updateMs } : null,
    hud: D.hud ? D.hud.stats : null,
    wx: D.weather ? D.weather.stats.ms : null,
    sprites: (function(){ let n=0; D.scene.traverse(function(o){ if(o.isSprite) n++; }); return n; })(),
    children: D.scene.children.length,
  });
})()
