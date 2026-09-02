/**
 * Compress one companion directory for the web. Ticket #3662.
 *
 * Operates on a PUBLISHABLE character directory — the same shape the publisher uploads:
 * `<name>.glb`, `mh_materials.json`, `textures/`. Deliberately not on the pipeline's
 * `characters/<id>/03-glb` layout, so the publisher, stage 06 and CI all drive one
 * implementation instead of three.
 *
 *   node compress_companion.mjs --src <charDir> --out <outDir> [--mode meshopt|full]
 *                               [--texture-px N] [--ktx-bin <dir>]
 *
 * ## Why there are two modes
 *
 * `meshopt` (default) is SAFE TO PUBLISH, and that is a measured claim rather than an
 * assumption. It IS lossy on geometry — 0.85 mm median RMS on the largest morph targets, and
 * base vertex positions are quantised too, which turns out to be ~87% of the visible effect.
 * The differences land on hair- and eyelash-card edges, where a sub-pixel shift becomes a
 * whole pixel.
 *
 * Judged where it counts: both models loaded through the real runtime side by side
 * (`/dev/model-ab` in the telegamez repo) with the MetaHuman material profile, per-character
 * hair calibration and clothing textures all live. Indistinguishable. A Blender render said
 * the same thing but could not be trusted on its own, because it does not run the hair shader
 * — which is exactly where the differences are. drei's `useGLTF` defaults to
 * `useMeshopt = true` and wires a MeshoptDecoder, so every surface that
 * loads a companion through it already accepts `EXT_meshopt_compression`. This is also
 * where nearly all the win is: ~73% of a MetaHuman GLB is uncompressed FLOAT32 morph-target
 * data, and meshopt takes the model from 47.5 MB to 16.6 MB on its own.
 *
 * `full` adds KTX2 textures and is NOT loadable yet. Nothing in web-next registers a
 * `KTX2Loader`, and `KHR_texture_basisu` is listed in `extensionsRequired`, so a companion
 * published this way uploads fine, reports success, and then fails to render — the same
 * shape of silent failure as beta's G13. It exists so the bytes can be measured and handed
 * to whoever wires the loader.
 *
 * ## Order matters
 *
 * Texture codecs run BEFORE meshopt. gltf-transform decodes `EXT_meshopt_compression`
 * whenever it has to touch a file and does not re-apply it, so a texture pass afterwards
 * produces a file larger than its input — 16.59 MB became 37 MB when tried the wrong way.
 *
 * ## No `dedup`
 *
 * It is the obvious next step and it is wrong here: zero bytes saved on this asset while
 * merging 17 materials into 10. `metaHumanMaterials.ts` matches materials BY NAME to decide
 * what is skin, hair or eye, so merging them renders a companion with no hair tint and no
 * clothing textures — indistinguishable from a bad bake.
 */

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const GLTF_TRANSFORM = '@gltf-transform/cli@4.5.0';

const argv = process.argv.slice(2);
const flag = (n, d) => { const i = argv.indexOf(`--${n}`); return i === -1 ? d : argv[i + 1]; };

const src = flag('src');
const out = flag('out');
const mode = flag('mode', 'meshopt');
const texturePx = Number(flag('texture-px', 0));
const ktxBin = flag('ktx-bin', '');
// The model is `<name>.glb`. In a publishable directory that is the folder name, but the
// pipeline's stage 06 passes `characters/<id>/03-glb`, where it is not.
const explicitName = flag('name', '');

if (!src || !out) { console.error('--src and --out are required'); process.exit(2); }
if (!['none', 'meshopt', 'full'].includes(mode)) { console.error(`unknown --mode ${mode}`); process.exit(2); }
if (!fs.existsSync(src)) { console.error(`source not found: ${src}`); process.exit(1); }

const name = explicitName || path.basename(path.resolve(src));
const srcGlb = path.join(src, `${name}.glb`);
const srcSidecar = path.join(src, 'mh_materials.json');

if (!fs.existsSync(srcGlb)) { console.error(`model not found: ${srcGlb}`); process.exit(1); }
if (!fs.existsSync(srcSidecar)) {
  // Same check the publisher makes, and for the same reason: without it the companion
  // publishes and renders untextured, which looks exactly like a bad bake.
  console.error(`mh_materials.json not found in ${src} — the runtime finds it beside the model`);
  process.exit(1);
}

const env = { ...process.env };
if (ktxBin) env.PATH = `${ktxBin}${path.delimiter}${env.PATH}`;

const MB = (b) => (b / 1048576).toFixed(2);
const sizeOf = (p) => (fs.existsSync(p) ? fs.statSync(p).size : 0);
const dirBytes = (d) => (fs.existsSync(d)
  ? fs.readdirSync(d).reduce((a, f) => a + sizeOf(path.join(d, f)), 0) : 0);

function requireKtx() {
  try { execFileSync('ktx', ['--version'], { env, stdio: 'pipe' }); } catch {
    console.error('mode=full needs the `ktx` CLI (KTX-Software 4.4+). Put it on PATH or pass --ktx-bin.');
    process.exit(1);
  }
}

function gt(args, label) {
  process.stdout.write(`  ${label} ... `);
  const t0 = Date.now();
  try {
    execFileSync('npx', ['--yes', GLTF_TRANSFORM, ...args], { env, stdio: 'pipe', shell: true });
  } catch (e) {
    console.log('FAILED');
    console.error(String(e.stderr || e.stdout || e.message).slice(0, 2000));
    process.exit(1);
  }
  console.log(`${((Date.now() - t0) / 1000).toFixed(0)}s`);
}

/** Counts that must survive compression. A change here is a rewrite, not a compression. */
function structure(glbPath) {
  const buf = fs.readFileSync(glbPath);
  const json = JSON.parse(buf.subarray(20, 20 + buf.readUInt32LE(12)).toString());
  const meshes = json.meshes || [];
  return {
    meshes: meshes.length,
    primitives: meshes.reduce((a, m) => a + (m.primitives || []).length, 0),
    morphTargets: meshes.reduce(
      (a, m) => a + Math.max(0, ...(m.primitives || []).map((p) => (p.targets || []).length)), 0),
    materials: (json.materials || []).length,
    images: (json.images || []).length,
  };
}

if (mode === 'full') requireKtx();
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });

console.log(`companion : ${name}`);
const MODE_NOTE = {
  none: '  (lossless — model copied unchanged)',
  meshopt: '  (default — verified visually, see below)',
  full: '  (NOT loadable by the runtime yet)',
};
console.log(`mode      : ${mode}${MODE_NOTE[mode]}`);
if (mode === 'full') {
  console.log('WARNING: mode=full carries KTX2, which no surface can load. The publisher will');
  console.log('         refuse it. Build it for measurement only.');
}

// ------------------------------------------------------------------------------- model
const before = structure(srcGlb);
const outGlb = path.join(out, `${name}.glb`);
const tmp = [];

if (mode === 'none') {
  // Nothing to do to the model. The bake is already the lean artifact: stage 03 caps and
  // de-alphas the sidecar textures, which took one companion's set from 168 MB to 22 MB
  // without a lossy step anywhere.
  fs.copyFileSync(srcGlb, outGlb);
  console.log('  model copied unchanged (no lossy step)');
} else if (mode === 'full') {
  const a = path.join(out, '.tmp-a.glb');
  const b = path.join(out, '.tmp-b.glb');
  tmp.push(a, b);
  gt(['uastc', srcGlb, a, '--slots', '{normalTexture,occlusionTexture,metallicRoughnessTexture}'],
     'uastc   (normal/ORM)');
  gt(['etc1s', a, b], 'etc1s   (albedo)');
  gt(['meshopt', b, outGlb], 'meshopt (geometry + morphs)');
} else {
  gt(['meshopt', srcGlb, outGlb], 'meshopt (geometry + morphs)');
}
for (const t of tmp) fs.rmSync(t, { force: true });

const after = structure(outGlb);
for (const k of ['meshes', 'primitives', 'morphTargets', 'materials']) {
  if (before[k] !== after[k]) {
    console.error(`\nABORT: ${k} changed ${before[k]} -> ${after[k]}. That is not a compression `
      + 'result — geometry or the material set was rewritten, and the material profile '
      + 'matches materials by name.');
    process.exit(1);
  }
}
console.log(`  model ${MB(sizeOf(srcGlb))} MB -> ${MB(outGlb ? sizeOf(outGlb) : 0)} MB`);

// ---------------------------------------------------------------------------- textures
const srcTex = path.join(src, 'textures');
const outTex = path.join(out, 'textures');
let texBefore = dirBytes(srcTex);
let texAfter = 0;
let stemRefs = 0;

if (fs.existsSync(srcTex)) {
  fs.mkdirSync(outTex, { recursive: true });
  const files = fs.readdirSync(srcTex);

  if (mode !== 'full') {
    // Copied verbatim. The runtime resolves these by name against the model's directory, and
    // outside `full` nothing about them changes here — stage 03 has already capped and
    // de-alphaed them at bake time, which is where that work belongs.
    for (const f of files) fs.copyFileSync(path.join(srcTex, f), path.join(outTex, f));
    texAfter = texBefore;
    console.log(`  textures ${files.length} files copied unchanged (${MB(texBefore)} MB)`);
  } else {
    for (const f of files.filter((x) => /\.png$/i.test(x))) {
      const stem = f.replace(/\.png$/i, '');
      const dst = path.join(outTex, `${stem}.ktx2`);
      // Normal/ORM/Tangent are linear, non-colour data. ETC1S fits per-block endpoints across
      // channels that are independent, which mangles them, and without --assign-tf linear the
      // encoder applies an sRGB transfer the data never had.
      const linear = /_N$|_ORM$|_Tangent$/i.test(stem);
      const args = linear
        ? ['create', '--format', 'R8G8B8A8_UNORM', '--assign-tf', 'linear',
           '--encode', 'uastc', '--uastc-quality', '2', '--zstd', '18']
        // BasisLZ carries its own entropy coding; the encoder refuses --zstd alongside it.
        : ['create', '--format', 'R8G8B8A8_SRGB', '--encode', 'basis-lz',
           '--clevel', '4', '--qlevel', '190'];
      // `ktx create` has no --resize; --width/--height resample.
      if (texturePx > 0) args.push('--width', String(texturePx), '--height', String(texturePx));
      args.push('--generate-mipmap', path.join(srcTex, f), dst);
      try { execFileSync('ktx', args, { env, stdio: 'pipe' }); } catch (e) {
        console.error(`  FAILED ${f}: ${String(e.stderr || e.message).slice(0, 300)}`);
        process.exit(1);
      }
      texAfter += sizeOf(dst);
    }
    console.log(`  textures ${MB(texBefore)} MB -> ${MB(texAfter)} MB`);
  }
}

// ----------------------------------------------------------------------------- sidecar
const sidecar = JSON.parse(fs.readFileSync(srcSidecar, 'utf8'));
for (const m of sidecar.materials || []) {
  for (const key of ['alpha_stem', 'tangent_stem']) {
    if (typeof m.params?.[key] === 'string') stemRefs++;
  }
  if (mode === 'full') {
    for (const [k, v] of Object.entries(m.textures || {})) {
      if (typeof v === 'string') m.textures[k] = v.replace(/\.png$/i, '.ktx2');
    }
  }
}
if (mode === 'full') sidecar.texture_format = 'ktx2';
fs.writeFileSync(path.join(out, 'mh_materials.json'), JSON.stringify(sidecar, null, 2));

const totalBefore = sizeOf(srcGlb) + texBefore;
const totalAfter = sizeOf(outGlb) + texAfter;

fs.writeFileSync(path.join(out, 'compression_manifest.json'), JSON.stringify({
  companion: name,
  ticket: 3662,
  mode,
  generated_at: new Date().toISOString(),
  source: path.resolve(src),
  texture_px: texturePx || 'unchanged',
  model: { before_bytes: sizeOf(srcGlb), after_bytes: sizeOf(outGlb), structure: after },
  textures: { before_bytes: texBefore, after_bytes: texAfter },
  total: { before_bytes: totalBefore, after_bytes: totalAfter },
  runtime: mode === 'meshopt'
    ? ['Loadable: drei useGLTF registers a meshopt decoder; the other surfaces go through '
        + 'the shared companionGltfLoader (telegamez#3682).']
    : [
        'NOT loadable: nothing in web-next registers a KTX2Loader, and KHR_texture_basisu is required.',
        `${stemRefs} alpha_stem/tangent_stem refs still resolve to .png in metaHumanMaterials.ts.`,
      ],
}, null, 2));

console.log(`\nTOTAL ${MB(totalBefore)} MB -> ${MB(totalAfter)} MB  (${((1 - totalAfter / totalBefore) * 100).toFixed(0)}% off)`);
console.log(`out   ${path.resolve(out)}`);
if (mode === 'full') {
  console.log('\nWARNING: mode=full is not loadable by the runtime yet. Publishing it would');
  console.log('upload cleanly, report success, and fail to render. See CONTEXT.md.');
}
