/**
 * Compress one companion directory for the web. Ticket #3662.
 *
 * Operates on a PUBLISHABLE character directory — the same shape the publisher uploads:
 * `<name>.glb`, `mh_materials.json`, `textures/`. Deliberately not on the pipeline's
 * `characters/<id>/03-glb` layout, so the publisher, stage 06 and CI all drive one
 * implementation instead of three.
 *
 *   node compress_companion.mjs --src <charDir> --out <outDir> [--mode meshopt|ktx2|full]
 *                               [--texture-px N] [--ktx-bin <dir>]
 *
 * ## The modes
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
 * `full` adds KTX2 textures, and only the browser can read them (telegamez#3662 gave web-next a
 * `KTX2Loader`). The native compositor reads the SAME published files and decodes textures with
 * the `image` crate, PNG/WebP only, so a `full` build renders in a room and fails on the TV and
 * program output. Not publishable for that reason; it stays for measurement.
 *
 * `ktx2` is the publishable KTX2 build, and carries both formats:
 *   - `<name>.glb`: the room model, KTX2 embedded textures + meshopt.
 *   - `<name>.compositor.glb`: PNG embedded textures + meshopt, the same as the `meshopt` build.
 *   - `textures/`: every PNG kept, plus a `.ktx2` sibling for each.
 *   - `mh_materials.json`: paths still name the PNGs (the compositor reads them), plus
 *     `browser_texture_format: "ktx2"`, which tells the browser to fetch the siblings.
 * `compression_manifest.json` records `variants: { room, compositor }`. The publisher sends that
 * as the avatar manifest's `variants`, which is how the compositor is routed to its own file.
 * Without that routing the compositor falls back to the room model and cannot decode it.
 *
 * ## Colour or data
 *
 * Every map that carries data rather than colour is encoded UASTC with a linear transfer
 * function. ETC1S fits endpoints across channels that are independent in a data map, and an
 * sRGB transfer the data never had bends every value. In the model that is normal, ORM and the
 * `KHR_materials_specular` intensity map; in the sidecar the normal, ORM and strand-tangent maps,
 * and the hair/brow `_CardsAtlas_Attribute` and eyelash `_Coverage` maps. The last two are
 * coverage and depth: exactly the hair-card edges where meshopt's only visible difference was.
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
const SHARP_CLI = 'sharp-cli@5.2.0';

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
// TELEGAMEZ (telegamez#3701): sidecar texture format. `png` is what every runtime reads
// today. `webp` is the measured lever on the hair atlases — 2048 PNG 5.9 MB vs 4096 WebP
// q95 4.7 MB vs 2048 WebP q95 1.5 MB on Julius — and every browser decodes it natively,
// unlike KTX2. It is opt-in because the runtime resolves atlas STEMS to `.png` itself; until
// it reads the sidecar's `texture_format`, a WebP build publishes cleanly and renders hair
// untextured. The publisher refuses such a build for the same reason it refuses `full`.
const textureFormat = flag('texture-format', 'png');
// telegamez#3662: drop each mesh's morph targets that the published build stores as zeros (see
// `prune-morphs` in gltf-transform.config.mjs). `ktx2` only, and only on the ROOM model: there
// the browser has its own file, and its face writer skips names a mesh lacks. The compositor
// model is left exactly as the `meshopt` build. On by default; `--prune-morphs off` for an A/B.
const pruneMorphs = flag('prune-morphs', 'on') !== 'off';
const CONFIG = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')),
  'gltf-transform.config.mjs');

if (!src || !out) { console.error('--src and --out are required'); process.exit(2); }
if (!['none', 'meshopt', 'ktx2', 'full'].includes(mode)) { console.error(`unknown --mode ${mode}`); process.exit(2); }
if (!['png', 'webp'].includes(textureFormat)) { console.error(`unknown --texture-format ${textureFormat}`); process.exit(2); }
const carriesKtx2 = mode === 'ktx2' || mode === 'full';
if (carriesKtx2 && textureFormat !== 'png') { console.error(`--texture-format applies to none/meshopt; ${mode} carries KTX2`); process.exit(2); }
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
    console.error(`mode=${mode} needs the \`ktx\` CLI (KTX-Software 4.4+). Put it on PATH or pass --ktx-bin.`);
    process.exit(1);
  }
}

function gt(args, label) {
  process.stdout.write(`  ${label} ... `);
  const t0 = Date.now();
  let stdout = '';
  try {
    stdout = execFileSync('npx', ['--yes', GLTF_TRANSFORM, ...args],
      { env, stdio: 'pipe', shell: true, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  } catch (e) {
    console.log('FAILED');
    console.error(String(e.stderr || e.stdout || e.message).slice(0, 2000));
    process.exit(1);
  }
  console.log(`${((Date.now() - t0) / 1000).toFixed(0)}s`);
  return stdout;
}

/** Morph target names per mesh, from `extras.targetNames`. */
function targetNames(glbPath) {
  const buf = fs.readFileSync(glbPath);
  const json = JSON.parse(buf.subarray(20, 20 + buf.readUInt32LE(12)).toString());
  return Object.fromEntries((json.meshes || []).map((m) => [m.name, m.extras?.targetNames || []]));
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

/**
 * Model slots holding data rather than colour, for `gltf-transform uastc --slots`. Everything
 * else goes ETC1S. `specularTexture` is `KHR_materials_specular`'s intensity, read from alpha;
 * MetaHuman bakes put one on skin, body and teeth.
 */
const MODEL_DATA_SLOTS = '{normalTexture,occlusionTexture,metallicRoughnessTexture,specularTexture}';

/**
 * Sidecar maps holding data rather than colour, by filename stem: `_N` normal, `_ORM` packed
 * occlusion/roughness/metal, `_Tangent` strand direction, `_Attribute` the card atlas's coverage
 * and depth channels, `_Coverage` the eyelash coverage.
 */
const SIDECAR_DATA_STEM = /_(N|ORM|Tangent|Attribute|Coverage)$/i;

/** Encode one sidecar PNG to KTX2: UASTC + linear for data maps, ETC1S + sRGB for colour. */
function encodeKtx2(srcPng, dst) {
  const stem = path.basename(srcPng).replace(/\.png$/i, '');
  const linear = SIDECAR_DATA_STEM.test(stem);
  const args = linear
    ? ['create', '--format', 'R8G8B8A8_UNORM', '--assign-tf', 'linear',
       '--encode', 'uastc', '--uastc-quality', '2', '--zstd', '18']
    // BasisLZ carries its own entropy coding; the encoder refuses --zstd alongside it.
    : ['create', '--format', 'R8G8B8A8_SRGB', '--encode', 'basis-lz',
       '--clevel', '4', '--qlevel', '190'];
  // `ktx create` has no --resize; --width/--height resample.
  if (texturePx > 0) args.push('--width', String(texturePx), '--height', String(texturePx));
  args.push('--generate-mipmap', srcPng, dst);
  try { execFileSync('ktx', args, { env, stdio: 'pipe' }); } catch (e) {
    console.error(`  FAILED ${path.basename(srcPng)}: ${String(e.stderr || e.message).slice(0, 300)}`);
    process.exit(1);
  }
  return linear ? 'uastc' : 'etc1s';
}

if (carriesKtx2) requireKtx();
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });

console.log(`companion : ${name}`);
const MODE_NOTE = {
  none: '  (lossless — model copied unchanged)',
  meshopt: '  (default — verified visually, see below)',
  ktx2: '  (KTX2 room model + PNG compositor model, both texture sets)',
  full: '  (browser only — the compositor cannot decode it)',
};
console.log(`mode      : ${mode}${MODE_NOTE[mode]}`);
if (!carriesKtx2) {
  console.log(`textures  : ${textureFormat}${textureFormat === 'webp'
    ? '  (NOT publishable until the runtime reads texture_format — telegamez#3701)' : ''}`);
}
if (mode === 'full') {
  console.log('WARNING: mode=full carries only KTX2, which the native compositor cannot decode.');
  console.log('         The publisher will refuse it. Build it for measurement, or use --mode ktx2.');
}

// ------------------------------------------------------------------------------- model
const before = structure(srcGlb);
const outGlb = path.join(out, `${name}.glb`);
// `ktx2` only: the compositor's own model, routed to it through the avatar manifest's variants.
const compositorGlb = mode === 'ktx2' ? path.join(out, `${name}.compositor.glb`) : null;
const tmp = [];
/** `[{ mesh, kept, total }]` when the room model's morph targets were pruned. */
let pruned = null;

if (mode === 'none') {
  // Nothing to do to the model. The bake is already the lean artifact: stage 03 caps and
  // de-alphas the sidecar textures, which took one companion's set from 168 MB to 22 MB
  // without a lossy step anywhere.
  fs.copyFileSync(srcGlb, outGlb);
  console.log('  model copied unchanged (no lossy step)');
} else if (carriesKtx2) {
  const p = path.join(out, '.tmp-p.glb');
  const a = path.join(out, '.tmp-a.glb');
  const b = path.join(out, '.tmp-b.glb');
  tmp.push(p, a, b);
  let roomSrc = srcGlb;
  if (mode === 'ktx2' && pruneMorphs) {
    const log = gt(['prune-morphs', srcGlb, p, '--config', `"${CONFIG}"`], 'prune   (morph targets stored as zeros)');
    pruned = log.split(/\r?\n/).filter((l) => l.startsWith('PRUNE ')).map((l) => {
      const [mesh, kept, total] = l.slice(6).split('\t');
      return { mesh, kept: Number(kept), total: Number(total) };
    });
    roomSrc = p;
  }
  gt(['uastc', roomSrc, a, '--slots', MODEL_DATA_SLOTS], 'uastc   (normal/ORM/specular)');
  gt(['etc1s', a, b], 'etc1s   (colour)');
  gt(['meshopt', b, outGlb], 'meshopt (geometry + morphs)');
  if (compositorGlb) gt(['meshopt', srcGlb, compositorGlb], 'meshopt (compositor model, PNG)');
} else {
  gt(['meshopt', srcGlb, outGlb], 'meshopt (geometry + morphs)');
}
for (const t of tmp) fs.rmSync(t, { force: true });

if (pruned) {
  // Pruning changes the room model's target counts on purpose, so check what it promised instead:
  // per mesh, the kept count it reported, and only names the source had.
  const srcNames = targetNames(srcGlb);
  const outNames = targetNames(outGlb);
  for (const r of pruned) {
    const got = outNames[r.mesh] || [];
    const foreign = got.filter((n) => !(srcNames[r.mesh] || []).includes(n));
    if (got.length !== r.kept || foreign.length) {
      console.error(`\nABORT: ${r.mesh} has ${got.length} targets after pruning, expected ${r.kept}`
        + (foreign.length ? `; names not in the source: ${foreign.join(', ')}` : '') + '.');
      process.exit(1);
    }
  }
  const total = pruned.reduce((acc, r) => acc + r.total, 0);
  const kept = pruned.reduce((acc, r) => acc + r.kept, 0);
  console.log(`  morph targets ${total} -> ${kept} across ${pruned.length} meshes `
    + `(${pruned.filter((r) => r.kept < r.total).map((r) => `${r.mesh} ${r.kept}/${r.total}`).join(', ')})`);
}

const after = structure(outGlb);
for (const glb of [outGlb, compositorGlb].filter(Boolean)) {
  const got = glb === outGlb ? after : structure(glb);
  for (const k of ['meshes', 'primitives', 'morphTargets', 'materials']) {
    if (k === 'morphTargets' && glb === outGlb && pruned) continue;
    if (before[k] !== got[k]) {
      console.error(`\nABORT: ${path.basename(glb)} ${k} changed ${before[k]} -> ${got[k]}. That `
        + 'is not a compression result — geometry or the material set was rewritten, and the '
        + 'material profile matches materials by name.');
      process.exit(1);
    }
  }
}
console.log(`  model ${MB(sizeOf(srcGlb))} MB -> ${MB(sizeOf(outGlb))} MB`);
if (compositorGlb) console.log(`  compositor model ${MB(sizeOf(compositorGlb))} MB`);

// ---------------------------------------------------------------------------- textures
const srcTex = path.join(src, 'textures');
const outTex = path.join(out, 'textures');
let texBefore = dirBytes(srcTex);
let texAfter = 0;
let stemRefs = 0;

if (fs.existsSync(srcTex)) {
  fs.mkdirSync(outTex, { recursive: true });
  const files = fs.readdirSync(srcTex);

  if (!carriesKtx2 && textureFormat === 'webp') {
    // WebP through sharp, fetched the same way gltf-transform is. Normal and tangent maps are
    // encoded LOSSLESS: they are vector data, and a lossy codec's chroma handling bends them.
    // Coverage atlases, base colour and ORM take q95, which on the hair atlas is invisible at
    // the density gain the runtime applies and a quarter of the PNG bytes.
    let converted = 0;
    for (const f of files) {
      if (!/\.png$/i.test(f)) { fs.copyFileSync(path.join(srcTex, f), path.join(outTex, f)); continue; }
      const stem = f.replace(/\.png$/i, '');
      const dst = path.join(outTex, `${stem}.webp`);
      const lossless = /_N$|_Tangent$/i.test(stem);
      const args = ['--yes', SHARP_CLI, '-i', path.join(srcTex, f), '-o', dst, '-f', 'webp',
        ...(lossless ? ['--lossless'] : ['-q', '95'])];
      try {
        execFileSync('npx', args, { env, stdio: 'pipe', shell: true });
      } catch (e) {
        console.error(`  FAILED ${f}: ${String(e.stderr || e.message).slice(0, 300)}`);
        process.exit(1);
      }
      if (!fs.existsSync(dst)) { console.error(`  FAILED ${f}: sharp wrote nothing at ${dst}`); process.exit(1); }
      converted++;
    }
    texAfter = dirBytes(outTex);
    console.log(`  textures ${converted} PNG -> WebP: ${MB(texBefore)} MB -> ${MB(texAfter)} MB`);
  } else if (!carriesKtx2) {
    // Copied verbatim. The runtime resolves these by name against the model's directory, and
    // outside the KTX2 modes nothing about them changes here — stage 03 has already capped and
    // de-alphaed them at bake time, which is where that work belongs.
    for (const f of files) fs.copyFileSync(path.join(srcTex, f), path.join(outTex, f));
    texAfter = texBefore;
    console.log(`  textures ${files.length} files copied unchanged (${MB(texBefore)} MB)`);
  } else {
    const codecs = { uastc: 0, etc1s: 0 };
    for (const f of files) {
      const isPng = /\.png$/i.test(f);
      // `ktx2` keeps every original beside its sibling: the compositor reads those.
      if (mode === 'ktx2' || !isPng) fs.copyFileSync(path.join(srcTex, f), path.join(outTex, f));
      if (!isPng) continue;
      codecs[encodeKtx2(path.join(srcTex, f), path.join(outTex, f.replace(/\.png$/i, '.ktx2')))]++;
    }
    texAfter = dirBytes(outTex);
    const ktx2Bytes = fs.readdirSync(outTex).filter((f) => /\.ktx2$/i.test(f))
      .reduce((acc, f) => acc + sizeOf(path.join(outTex, f)), 0);
    console.log(`  textures ${codecs.uastc} UASTC (data) + ${codecs.etc1s} ETC1S (colour): `
      + `${MB(texBefore)} MB PNG -> ${MB(ktx2Bytes)} MB KTX2`
      + (mode === 'ktx2' ? `, ${MB(texAfter)} MB with the PNGs kept` : ''));
  }
}

// ----------------------------------------------------------------------------- sidecar
const sidecar = JSON.parse(fs.readFileSync(srcSidecar, 'utf8'));
// Explicit texture paths are rewritten to the new extension; the atlas STEMS are not touched
// — the runtime appends the extension to those itself, from `texture_format`.
//
// `ktx2` rewrites nothing: the paths keep naming the PNGs the compositor decodes, and
// `browser_texture_format` tells the browser to fetch each one's `.ktx2` sibling instead
// (`browserTextureUrl` in telegamez's metaHumanMaterials.ts).
const outExt = mode === 'full' ? 'ktx2' : (textureFormat === 'webp' ? 'webp' : null);
for (const m of sidecar.materials || []) {
  for (const key of ['alpha_stem', 'tangent_stem']) {
    if (typeof m.params?.[key] === 'string') stemRefs++;
  }
  if (outExt) {
    for (const [k, v] of Object.entries(m.textures || {})) {
      if (typeof v === 'string') m.textures[k] = v.replace(/\.png$/i, `.${outExt}`);
    }
  }
}
if (outExt) sidecar.texture_format = outExt;
if (mode === 'ktx2') sidecar.browser_texture_format = 'ktx2';
fs.writeFileSync(path.join(out, 'mh_materials.json'), JSON.stringify(sidecar, null, 2));

// What one browser visit downloads. In `ktx2` that is the room model and the KTX2 textures;
// the compositor model and the PNGs sit beside them for the compositor.
const ktx2Only = (d) => (fs.existsSync(d) ? fs.readdirSync(d) : [])
  .filter((f) => /\.ktx2$/i.test(f)).reduce((acc, f) => acc + sizeOf(path.join(d, f)), 0);
const totalBefore = sizeOf(srcGlb) + texBefore;
const totalAfter = sizeOf(outGlb) + (mode === 'ktx2' ? ktx2Only(outTex) : texAfter);

fs.writeFileSync(path.join(out, 'compression_manifest.json'), JSON.stringify({
  companion: name,
  ticket: 3662,
  mode,
  generated_at: new Date().toISOString(),
  source: path.resolve(src),
  texture_px: texturePx || 'unchanged',
  texture_format: mode === 'full' ? 'ktx2' : textureFormat,
  ...(mode === 'ktx2' ? {
    browser_texture_format: 'ktx2',
    // For the publisher: send as the avatar manifest's `variants`. Without `compositor` the
    // compositor falls back to `room` and cannot decode its KTX2 textures.
    variants: { room: path.basename(outGlb), compositor: path.basename(compositorGlb) },
    compositor_model: { after_bytes: sizeOf(compositorGlb) },
    // Room model only. Null when --prune-morphs off.
    pruned_morph_targets: pruned,
  } : {}),
  model: { before_bytes: sizeOf(srcGlb), after_bytes: sizeOf(outGlb), structure: after },
  textures: { before_bytes: texBefore, after_bytes: texAfter },
  total: { before_bytes: totalBefore, after_bytes: totalAfter },
  runtime: textureFormat === 'webp' && !carriesKtx2
    ? [
        'NOT loadable until the runtime resolves atlas stems by texture_format (telegamez#3701); '
          + `${stemRefs} alpha_stem/tangent_stem refs currently resolve to .png in metaHumanMaterials.ts.`,
      ]
    : mode === 'meshopt'
    ? ['Loadable: drei useGLTF registers a meshopt decoder; the other surfaces go through '
        + 'the shared companionGltfLoader (telegamez#3682).']
    : mode === 'ktx2'
    ? [
        'Browser: needs the KTX2Loader from telegamez#3662 (KHR_texture_basisu is required, and '
          + 'browser_texture_format selects the .ktx2 sidecar siblings).',
        'Compositor: only if the avatar manifest routes it to the compositor variant; otherwise '
          + 'it falls back to the room model and cannot decode it.',
      ]
    : mode === 'full'
    ? [
        'Browser only (telegamez#3662). The native compositor decodes textures with the image '
          + 'crate and cannot read KTX2; use --mode ktx2 for a publishable build.',
      ]
    : [],
}, null, 2));

console.log(`\nTOTAL ${MB(totalBefore)} MB -> ${MB(totalAfter)} MB  (${((1 - totalAfter / totalBefore) * 100).toFixed(0)}% off)`
  + (mode === 'ktx2' ? '  per browser visit: room model + KTX2 textures' : ''));
console.log(`out   ${path.resolve(out)}`);
if (mode === 'full') {
  console.log('\nWARNING: mode=full is browser-only. The compositor reads the same files and cannot');
  console.log('decode KTX2, so a published full build fails on the TV and program output.');
} else if (mode === 'ktx2') {
  console.log('\nNOTE: publish with the avatar manifest variants from compression_manifest.json,');
  console.log('or the compositor falls back to the KTX2 room model.');
} else if (textureFormat === 'webp') {
  console.log('\nWARNING: WebP sidecar textures need a runtime that reads texture_format');
  console.log('(telegamez#3701). The publisher refuses this build until TG_WEBP_RUNTIME_READY=1.');
}
