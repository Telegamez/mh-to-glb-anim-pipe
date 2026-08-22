/**
 * Assert that a published GLB's COLOR_0 actually carries the scalp darkening.
 *
 * Why this exists: stage 02 bakes proximity darkening into the face mesh's
 * vertex colours and logs a healthy count, and for the whole life of the
 * project every shipped GLB carried COLOR_0 at pure white anyway. Stage 03 was
 * exporting with Blender's defaults, which fabricate a white COLOR_0 when the
 * material converter cannot trace the attribute — so the presence check that
 * anyone would naturally write ("does COLOR_0 exist?") passed the entire time.
 *
 * The values are the assertion. Presence is not.
 *
 * Draco is why this is JavaScript rather than a few lines of Python: the
 * meshes are compressed, so reading the attribute means running Draco, and the
 * decoder that ships with three is the one already on disk.
 *
 *   node tools/verify_vertex_colours.cjs [<glb> ...]
 *
 * With no arguments it checks the six published companions. Exits non-zero if
 * any head mesh comes back undarkened.
 */
const fs = require('node:fs');
const path = require('node:path');

const THREE_ROOT =
  's:/Avatars/metahuman/telegamez/node_modules/.pnpm/three@0.176.0/node_modules/three';
const DECODER = path.join(THREE_ROOT, 'examples/jsm/libs/draco/draco_decoder.js');
const PUBLISHED = 's:/Avatars/metahuman/telegamez/packages/web-next/public/models';
const ROSTER = ['telly', 'tele', 'telie', 'telee', 'tele1', 'tele2'];

// Meshes that must show darkening, and the floor they must beat. Teeth are
// legitimately all-white — the source bakes no occlusion there — so asserting
// on every mesh would just teach people to ignore the check.
const REQUIRE_DARK = { Head_Mesh: 0.05 };

// three's package.json is `"type": "module"`, so require()-ing the emscripten
// glue returns an empty namespace and `module.exports = ...` never runs.
// Evaluate it in a real CommonJS scope instead.
const DracoDecoderModule = (() => {
  const src = fs.readFileSync(DECODER, 'utf8');
  const mod = { exports: {} };
  new Function('module', 'exports', 'require', '__dirname', src)(
    mod, mod.exports, require, path.dirname(DECODER),
  );
  return mod.exports;
})();

function parseGlb(buf) {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  let off = 12;
  let json = null;
  let bin = null;
  while (off < buf.byteLength) {
    const len = dv.getUint32(off, true);
    const type = dv.getUint32(off + 4, true);
    const start = off + 8;
    if (type === 0x4e4f534a) json = JSON.parse(buf.slice(start, start + len).toString('utf8'));
    if (type === 0x004e4942) bin = buf.slice(start, start + len);
    off = start + len + ((4 - (len % 4)) % 4);
  }
  return { json, bin };
}

function readColor0(draco, json, bin, mesh) {
  const prim = mesh.primitives[0];
  const ext = prim.extensions && prim.extensions.KHR_draco_mesh_compression;
  if (!ext) return null;                      // uncompressed path not needed here
  const id = ext.attributes.COLOR_0;
  if (id === undefined) return null;

  const bv = json.bufferViews[ext.bufferView];
  const start = bv.byteOffset || 0;
  const slice = bin.slice(start, start + bv.byteLength);

  const decoder = new draco.Decoder();
  const dbuf = new draco.DecoderBuffer();
  dbuf.Init(new Int8Array(slice), slice.byteLength);
  const geom = new draco.Mesh();
  const status = decoder.DecodeBufferToMesh(dbuf, geom);
  if (!status.ok()) {
    draco.destroy(geom); draco.destroy(dbuf); draco.destroy(decoder);
    return null;
  }
  const attr = decoder.GetAttributeByUniqueId(geom, id);
  const n = geom.num_points();
  const nc = attr.num_components();
  const out = new draco.DracoFloat32Array();
  decoder.GetAttributeFloatForAllPoints(geom, attr, out);

  let min = Infinity; let sum = 0; let dark = 0;
  for (let i = 0; i < n; i++) {
    const l = (out.GetValue(i * nc) + out.GetValue(i * nc + 1) + out.GetValue(i * nc + 2)) / 3;
    if (l < min) min = l;
    sum += l;
    if (l < 0.95) dark++;
  }
  draco.destroy(out); draco.destroy(geom); draco.destroy(dbuf); draco.destroy(decoder);
  return { count: n, min, mean: sum / n, darkFrac: dark / n };
}

DracoDecoderModule().then((draco) => {
  const targets = process.argv.length > 2
    ? process.argv.slice(2)
    : ROSTER.map((id) => path.join(PUBLISHED, id, `${id}.glb`));

  let failures = 0;
  for (const file of targets) {
    const label = path.basename(path.dirname(file));
    if (!fs.existsSync(file)) {
      console.log(`${label.padEnd(8)} MISSING ${file}`);
      failures++;
      continue;
    }
    const { json, bin } = parseGlb(fs.readFileSync(file));
    for (const [name, floor] of Object.entries(REQUIRE_DARK)) {
      const mesh = json.meshes.find((m) => m.name === name);
      if (!mesh) { console.log(`${label.padEnd(8)} ${name}: absent`); failures++; continue; }
      const s = readColor0(draco, json, bin, mesh);
      if (!s) { console.log(`${label.padEnd(8)} ${name}: no COLOR_0`); failures++; continue; }
      const ok = s.darkFrac >= floor;
      if (!ok) failures++;
      console.log(
        `${label.padEnd(8)} ${name}  ${String(s.count).padStart(6)} verts  ` +
        `min=${s.min.toFixed(3)} mean=${s.mean.toFixed(3)} ` +
        `darkened=${(s.darkFrac * 100).toFixed(1)}%  ${ok ? 'OK' : 'FAIL (white COLOR_0)'}`,
      );
    }
  }
  console.log(failures === 0
    ? '\nall checked meshes carry real vertex colours'
    : `\n${failures} check(s) failed`);
  process.exit(failures === 0 ? 0 : 1);
});
