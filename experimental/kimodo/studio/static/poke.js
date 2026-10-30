// Skin poking through the garment, measured in three.js - the same skinning the runtime uses.
//
// (A Blender version of this measured nothing: an action imported from one glTF onto another
// file's armature half-applies, because Blender stores rotations relative to each armature's own
// imported rest axes. three.js plays glTF local rotations as-is, like the runtime.)
//
// covered (decided at bind) = body vertices inside a garment (signed distance < -1mm), within 3cm
// of it, whose nearest cloth point is > RIM_FREE from any garment opening (cuff, hem, collar).
// poke at a frame = a covered vertex whose signed distance to the posed garment is > POKE_EPS.
import * as THREE from 'three';
import { MeshBVH, StaticGeometryGenerator } from 'three-mesh-bvh';

const POKE_EPS = 0.001, RIM_FREE = 0.03, NEAR = 0.03;
const GARMENT = /Outfit|cloth|shirt|chino|trouser|pant/i;

// Posed, world-space geometry via three's own per-vertex skinning (getVertexPosition applies
// morphs + bone transforms exactly as the shader does), then matrixWorld. StaticGeometryGenerator
// was dropped: on the meshopt/KHR_mesh_quantization GLBs it returned some meshes in dequantized
// [-1,1] space, so body and garment were compared in different frames (flags at y = -1.00).
function posed(meshes) {
  let total = 0; for (const m of meshes) total += m.geometry.attributes.position.count;
  const pos = new Float32Array(total * 3), idx = [];
  const v = new THREE.Vector3();
  let base = 0;
  for (const m of meshes) {
    m.updateMatrixWorld(true);
    if (m.skeleton) m.skeleton.update();
    const n = m.geometry.attributes.position.count;
    for (let i = 0; i < n; i++) {
      m.getVertexPosition(i, v).applyMatrix4(m.matrixWorld);
      pos[(base + i) * 3] = v.x; pos[(base + i) * 3 + 1] = v.y; pos[(base + i) * 3 + 2] = v.z;
    }
    const src = m.geometry.index ? m.geometry.index.array : [...Array(n).keys()];
    for (let i = 0; i < src.length; i++) idx.push(src[i] + base);
    base += n;
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setIndex(idx);
  return g;
}

function boundaryPoints(geo) {
  // Weld by position first: glTF splits vertices along UV seams, which would read as openings.
  const pos = geo.attributes.position, key = (i) => `${pos.getX(i).toFixed(4)},${pos.getY(i).toFixed(4)},${pos.getZ(i).toFixed(4)}`;
  const id = new Map(), weld = new Int32Array(pos.count);
  for (let i = 0; i < pos.count; i++) { const k = key(i); if (!id.has(k)) id.set(k, i); weld[i] = id.get(k); }
  const idx = geo.index.array, edges = new Map();
  for (let t = 0; t < idx.length; t += 3) for (let e = 0; e < 3; e++) {
    let a = weld[idx[t + e]], b = weld[idx[t + (e + 1) % 3]]; if (a > b) [a, b] = [b, a];
    if (a === b) continue;
    const k = a * 4194304 + b; edges.set(k, (edges.get(k) || 0) + 1);
  }
  const pts = [];
  for (const [k, n] of edges) if (n === 1) {
    const a = Math.floor(k / 4194304), b = k % 4194304;
    pts.push(new THREE.Vector3().fromBufferAttribute(pos, a), new THREE.Vector3().fromBufferAttribute(pos, b));
  }
  return pts;
}

function signedTo(bvh, geo, p, hit, tri) {
  bvh.closestPointToPoint(p, hit);
  const idx = geo.index.array, pos = geo.attributes.position, f = hit.faceIndex * 3;
  tri.setFromAttributeAndIndices(pos, idx[f], idx[f + 1], idx[f + 2]);
  const n = tri.getNormal(new THREE.Vector3());
  const d = hit.point.distanceTo(p);
  return p.clone().sub(hit.point).dot(n) > 0 ? d : -d;
}

export function makePokeMeter(characterRoot) {
  const body = [], garments = [];
  characterRoot.traverse((o) => {
    if (!o.isSkinnedMesh) return;
    if (/^Body_Mesh/.test(o.name) || (o.material && /MI_Body/i.test(o.material.name))) body.push(o);
    else if (GARMENT.test(o.name) || (o.material && /MI_WI_|MI_HS_|Outfit/i.test(o.material.name))) garments.push(o);
  });
  if (!body.length || !garments.length) throw new Error(`poke meter needs body+garment, got ${body.length}/${garments.length}`);
  characterRoot.updateMatrixWorld(true);
  const bgeo = posed(body), ggeo = posed(garments);
  const bvh = new MeshBVH(ggeo);
  const rim = boundaryPoints(ggeo);
  const hit = {}, tri = new THREE.Triangle(), p = new THREE.Vector3();
  const covered = [];
  const bpos = bgeo.attributes.position;
  for (let i = 0; i < bpos.count; i++) {
    p.fromBufferAttribute(bpos, i);
    const d = signedTo(bvh, ggeo, p, hit, tri);
    if (d < -POKE_EPS && d > -NEAR) {
      let rd = Infinity; for (const r of rim) { const q = r.distanceToSquared(hit.point); if (q < rd) rd = q; }
      if (Math.sqrt(rd) > RIM_FREE) covered.push(i);
    }
  }
  return {
    covered: covered.length, coveredIdx: covered, bodyMeshes: body.map((m) => m.name), garmentMeshes: garments.map((m) => m.name),
    // Measure the CURRENT pose (call after the mixer / twist driver have run).
    measure() {
      characterRoot.updateMatrixWorld(true);
      const b = posed(body), g = posed(garments), gb = new MeshBVH(g), out = new Float32Array(covered.length);
      const pa = b.attributes.position;
      covered.forEach((vi, k) => { p.fromBufferAttribute(pa, vi); out[k] = signedTo(gb, g, p, hit, tri); });
      let poking = 0, max = 0;
      for (const d of out) { if (d > POKE_EPS) poking++; if (d > max) max = d; }
      return { poking, maxMm: +(max * 1000).toFixed(1), worst: out };
    },
  };
}
