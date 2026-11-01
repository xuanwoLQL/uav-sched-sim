(function(){
  const rc = new THREE.Raycaster();
  const out = {};
  // 屏幕中心 + 下方 40% + 左中，各打一条射线，看命中的是什么
  [[0,0,"center"],[0,-0.45,"lower"],[0.35,0.1,"right"]].forEach(function(p){
    rc.setFromCamera(new THREE.Vector2(p[0],p[1]), __DBG.camera);
    const hits = rc.intersectObjects(__DBG.scene.children, true).slice(0,4).map(function(h){
      const o=h.object, m=o.material;
      return {name:o.name||o.type, mat: m? (m.name||m.type):null,
              mapKey: m&&m.map? (m.map.image&&m.map.image.width?m.map.image.width+"px":"tex"):null,
              col: m&&m.color? m.color.getHexString():null, dist: +h.distance.toFixed(1)};
    });
    out[p[2]] = hits;
  });
  out.terrainTris = __DBG.terrain? __DBG.terrain.stats.tris : null;
  return JSON.stringify(out);
})()
