/**
 * Compress one companion directory for the web. Ticket #3662.
 *
 * Operates on a PUBLISHABLE character directory — the same shape the publisher uploads:
 * `<name>.glb`, `mh_materials.json`, `textures/`. Deliberately not on the pipeline's
 * `characters/<id>/03-glb` layout, so the publisher, stage 06 and CI all drive one
 * implementation instead of three.
 *
 *   node compress_companion.mjs --src <charDir> --out <outDir> [--mode ktx2]
 *                               [--texture-px N] [--ktx-bin <dir>] [--seat off]
 *
 * ## One build: ktx2
 *
 * Owner decision, 2026-10-08: the ktx2 build is the default on panda and beta, and "to avoid
 * confusion end to end" meshopt is removed as a build. `ktx2` is the only mode. `meshopt`,
 * `none` and `full` are refused with an error that says so, rather than quietly producing a
 * build nothing publishes any more; WebP sidecar textures (`--texture-format webp`,
 * telegamez#3701) went with them, because they only ever applied to those modes.
 *
 * `EXT_meshopt_compression` is NOT going anywhere. It is the geometry codec INSIDE the ktx2
 * build, on both of its models, and what follows is why that is safe.
 *
 * ## The geometry codec: meshopt
 *
 * meshopt is SAFE TO PUBLISH, and that is a measured claim rather than an
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
 * ## What the ktx2 build carries
 *
 * Only the browser can read KTX2 (telegamez#3662 gave web-next a `KTX2Loader`). The native
 * compositor reads the SAME published files and decodes textures with the `image` crate,
 * PNG/WebP only, which is why the old KTX2-only `full` mode was never publishable. So `ktx2`
 * carries both formats:
 *   - `<name>.glb`: the room model, KTX2 embedded textures + meshopt.
 *   - `<name>.compositor.glb`: PNG embedded textures + meshopt (model for model, what the
 *     retired `meshopt` build was).
 *   - `<name>.seat.glb`: the room model for small on-screen seats (telegamez#4863, the Hold'em
 *     pilot): morph normals dropped on every mesh and embedded textures capped at 1024 px.
 *     It shares the room model's sidecar; only the GLB differs. `--seat off` skips it.
 *   - `textures/`: every PNG kept, plus a `.ktx2` sibling for each.
 *   - `mh_materials.json`: paths still name the PNGs (the compositor reads them), plus
 *     `browser_texture_format: "ktx2"`, which tells the browser to fetch the siblings, and
 *     `texture_sha256` (telegamez#4863), a fingerprint per .ktx2 so identical maps share one
 *     GPU copy across characters.
 * `compression_manifest.json` records `variants: { room, compositor, seat }`. The publisher sends that
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
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const GLTF_TRANSFORM = '@gltf-transform/cli@4.5.0';

const argv = process.argv.slice(2);
const flag = (n, d) => { const i = argv.indexOf(`--${n}`); return i === -1 ? d : argv[i + 1]; };

const src = flag('src');
const out = flag('out');
const mode = flag('mode', 'ktx2');
const texturePx = Number(flag('texture-px', 0));
const ktxBin = flag('ktx-bin', '');
// The model is `<name>.glb`. In a publishable directory that is the folder name, but the
// pipeline's stage 06 passes `characters/<id>/03-glb`, where it is not.
const explicitName = flag('name', '');
// telegamez#3701's `--texture-format webp` applied to the `none`/`meshopt` modes only, and left
// with them (owner decision 2026-10-08). The manifest still records `texture_format: "png"`:
// the PNGs are what the compositor reads.
const textureFormat = 'png';
// telegamez#3662: drop each mesh's morph targets that the published build stores as zeros (see
// `prune-morphs` in gltf-transform.config.mjs). `ktx2` only, and only on the ROOM model: there
// the browser has its own file, and its face writer skips names a mesh lacks. The compositor
// model is left untouched apart from meshopt. On by default; `--prune-morphs off` for an A/B.
const pruneMorphs = flag('prune-morphs', 'on') !== 'off';
// telegamez#3662 step 2, room model only. Defaults in ktx2 mode are what the fixed-expression A/B
// on /dev/model-ab signed off (smile, frown, open mouth on tg-drake, 2026-10-05):
// `--morph-normals small` (default) drops morph NORMAL/TANGENT deltas on every mesh but the head
// (teeth, eyes, lashes, brows): no visible change on the face, the open mouth's teeth shade
// marginally differently. `drop` does the head too: 60 -> 34 MB of morph texture, but smile
// cheeks and frown creases shade visibly shallower up close. Opt-in, for small on-screen sizes.
// `keep` for an A/B. See strip-morph-normals in gltf-transform.config.mjs.
const morphNormals = flag('morph-normals', 'small');
// `--lean-textures` (default on, `off` for an A/B): teeth and eye maps to 512 px,
// and the body's specular and metallic-roughness maps to 128 px (the outfit covers most of the
// body; at 128 they are close to the constants they nearly are). Embedded textures only, before
// KTX2 encoding. Teeth are 512 rather than 256 because the tongue shares their material and
// loses its surface detail at 256 with the mouth open; 512 keeps it, for ~1 MB.
const leanTextures = flag('lean-textures', 'on') === 'on';
// telegamez#4863: `<name>.seat.glb`, a second browser model for seat-size slots (the Hold'em table
// draws heads at most 180 x 150 px). The room model's pipeline with morph normals dropped on every
// mesh (`drop`, see above: the shallower creases are invisible at that size) and every embedded
// texture capped at SEAT_TEXTURE_PX. Sidecar textures are shared with the room model. Routed by
// the avatar manifest's `variants.seat`; a surface without a seat file loads the room model.
// On by default; `--seat off` skips it.
const seat = flag('seat', 'on') !== 'off';
const SEAT_TEXTURE_PX = 1024;
const LEAN_TEXTURES = [
  ['MI_Teeth_*', 512],
  ['MI_Eye*', 512],
  ['MI_Body_*_Specular', 128],
  ['MI_Body_*_MetallicRoughness', 128],
];
const CONFIG = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')),
  'gltf-transform.config.mjs');

if (!src || !out) { console.error('--src and --out are required'); process.exit(2); }
if (['none', 'meshopt', 'full'].includes(mode)) {
  // Refused by name, not as an unknown word: a script or a habit that still asks for these
  // should learn why they went, not guess at a typo.
  console.error(`--mode ${mode} was removed: ktx2 is the only build (owner decision 2026-10-08: `
    + 'the ktx2 build is the default on panda and beta, and meshopt/none/full were removed to '
    + 'avoid confusion end to end). Use --mode ktx2, or leave --mode off.');
  process.exit(2);
}
if (mode !== 'ktx2') { console.error(`unknown --mode ${mode} (ktx2 is the only build)`); process.exit(2); }
if (argv.includes('--texture-format')) {
  console.error('--texture-format was removed with the meshopt/none builds (owner decision 2026-10-08); '
    + 'the ktx2 build carries PNG for the compositor and KTX2 for the browser.');
  process.exit(2);
}
if (!['keep', 'small', 'drop'].includes(morphNormals)) { console.error(`unknown --morph-normals ${morphNormals}`); process.exit(2); }
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

requireKtx();
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });

console.log(`companion : ${name}`);
console.log(`mode      : ${mode}  (KTX2 room model + PNG compositor model, both texture sets)`);

// ------------------------------------------------------------------------------- model
const before = structure(srcGlb);
const outGlb = path.join(out, `${name}.glb`);
// The compositor's own model, routed to it through the avatar manifest's variants.
const compositorGlb = path.join(out, `${name}.compositor.glb`);
const seatGlb = path.join(out, `${name}.seat.glb`);
const tmp = [];
/** `[{ mesh, kept, total }]` when the room model's morph targets were pruned. */
let pruned = null;

/**
 * One browser model from `from` (the source, pruned when pruning is on): morph normals, lean
 * textures, an optional texture cap, then the texture codecs and meshopt last (see "Order
 * matters" above). `tag` keeps each model's intermediate files apart.
 */
function browserModel(from, normals, capPx, dst, tag) {
  let cur = from;
  const a = path.join(out, `.tmp-${tag}a.glb`);
  const b = path.join(out, `.tmp-${tag}b.glb`);
  tmp.push(a, b);
  if (normals !== 'keep') {
    const n = path.join(out, `.tmp-${tag}n.glb`);
    tmp.push(n);
    const meshes = normals === 'small' ? '"^(?!Head_Mesh$)"' : '".*"';
    gt(['strip-morph-normals', cur, n, '--meshes', meshes, '--config', `"${CONFIG}"`],
       `strip   (morph normals: ${normals === 'small' ? 'all but the head' : 'every mesh'})`);
    cur = n;
  }
  if (leanTextures) {
    LEAN_TEXTURES.forEach(([pattern, px], i) => {
      const r = path.join(out, `.tmp-${tag}r${i}.glb`);
      tmp.push(r);
      gt(['resize', cur, r, '--pattern', `"${pattern}"`, '--width', px, '--height', px],
         `resize  (${pattern} -> ${px} px)`);
      cur = r;
    });
  }
  if (capPx > 0) {
    const c = path.join(out, `.tmp-${tag}c.glb`);
    tmp.push(c);
    // `resize` only shrinks: a texture already inside the box is left as it is.
    gt(['resize', cur, c, '--width', capPx, '--height', capPx], `resize  (every texture -> <= ${capPx} px)`);
    cur = c;
  }
  gt(['uastc', cur, a, '--slots', MODEL_DATA_SLOTS], 'uastc   (normal/ORM/specular)');
  gt(['etc1s', a, b], 'etc1s   (colour)');
  gt(['meshopt', b, dst], `meshopt (geometry + morphs${tag === 'seat-' ? ', seat model' : ''})`);
}

// A block only to keep the intermediate file names local. The compositor model is the source
// with meshopt alone.
{
  const p = path.join(out, '.tmp-p.glb');
  tmp.push(p);
  let roomSrc = srcGlb;
  if (pruneMorphs) {
    const log = gt(['prune-morphs', srcGlb, p, '--config', `"${CONFIG}"`], 'prune   (morph targets stored as zeros)');
    pruned = log.split(/\r?\n/).filter((l) => l.startsWith('PRUNE ')).map((l) => {
      const [mesh, kept, total] = l.slice(6).split('\t');
      return { mesh, kept: Number(kept), total: Number(total) };
    });
    roomSrc = p;
  }
  browserModel(roomSrc, morphNormals, 0, outGlb, '');
  gt(['meshopt', srcGlb, compositorGlb], 'meshopt (compositor model, PNG)');
  if (seat) browserModel(roomSrc, 'drop', SEAT_TEXTURE_PX, seatGlb, 'seat-');
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
for (const glb of seat ? [outGlb, compositorGlb, seatGlb] : [outGlb, compositorGlb]) {
  const got = glb === outGlb ? after : structure(glb);
  for (const k of ['meshes', 'primitives', 'morphTargets', 'materials']) {
    if (k === 'morphTargets' && glb !== compositorGlb && pruned) continue;
    if (before[k] !== got[k]) {
      console.error(`\nABORT: ${path.basename(glb)} ${k} changed ${before[k]} -> ${got[k]}. That `
        + 'is not a compression result — geometry or the material set was rewritten, and the '
        + 'material profile matches materials by name.');
      process.exit(1);
    }
  }
}
console.log(`  model ${MB(sizeOf(srcGlb))} MB -> ${MB(sizeOf(outGlb))} MB`);
console.log(`  compositor model ${MB(sizeOf(compositorGlb))} MB`);
if (seat) {
  // The seat model is pruned like the room model and must carry exactly its morph targets: the
  // lipsync drive and the face writer find them by name.
  const roomNames = targetNames(outGlb);
  const seatNames = targetNames(seatGlb);
  for (const [mesh, names] of Object.entries(roomNames)) {
    if ((seatNames[mesh] || []).join('\n') !== names.join('\n')) {
      console.error(`\nABORT: ${path.basename(seatGlb)} ${mesh} morph targets differ from the room model's.`);
      process.exit(1);
    }
  }
  console.log(`  seat model ${MB(sizeOf(seatGlb))} MB`);
}

// ---------------------------------------------------------------------------- textures
const srcTex = path.join(src, 'textures');
const outTex = path.join(out, 'textures');
let texBefore = dirBytes(srcTex);
let texAfter = 0;

if (fs.existsSync(srcTex)) {
  fs.mkdirSync(outTex, { recursive: true });
  const files = fs.readdirSync(srcTex);

  const codecs = { uastc: 0, etc1s: 0 };
  for (const f of files) {
    const isPng = /\.png$/i.test(f);
    // Every original stays beside its sibling: the compositor reads those.
    fs.copyFileSync(path.join(srcTex, f), path.join(outTex, f));
    if (!isPng) continue;
    codecs[encodeKtx2(path.join(srcTex, f), path.join(outTex, f.replace(/\.png$/i, '.ktx2')))]++;
  }
  texAfter = dirBytes(outTex);
  const ktx2Bytes = fs.readdirSync(outTex).filter((f) => /\.ktx2$/i.test(f))
    .reduce((acc, f) => acc + sizeOf(path.join(outTex, f)), 0);
  console.log(`  textures ${codecs.uastc} UASTC (data) + ${codecs.etc1s} ETC1S (colour): `
    + `${MB(texBefore)} MB PNG -> ${MB(ktx2Bytes)} MB KTX2, ${MB(texAfter)} MB with the PNGs kept`);
}

// ----------------------------------------------------------------------------- sidecar
const sidecar = JSON.parse(fs.readFileSync(srcSidecar, 'utf8'));
// No paths are rewritten: they keep naming the PNGs the compositor decodes, and
// `browser_texture_format` tells the browser to fetch each one's `.ktx2` sibling instead
// (`browserTextureUrl` in telegamez's metaHumanMaterials.ts).
sidecar.browser_texture_format = 'ktx2';
// telegamez#4863: a fingerprint per browser texture, keyed by its path beside the model. The
// browser keeps one GPU copy per fingerprint, so the maps every character shares (the outfit,
// the eyelash coverage) are uploaded once per page rather than once per head. Every .ktx2 counts,
// including the hair and brow card atlases the runtime derives rather than reads from here.
const sidecarTex = path.join(out, 'textures');
sidecar.texture_sha256 = Object.fromEntries(
  (fs.existsSync(sidecarTex) ? fs.readdirSync(sidecarTex) : [])
    .filter((f) => /\.ktx2$/i.test(f))
    .sort()
    .map((f) => [`textures/${f}`,
      createHash('sha256').update(fs.readFileSync(path.join(sidecarTex, f))).digest('hex')]),
);
fs.writeFileSync(path.join(out, 'mh_materials.json'), JSON.stringify(sidecar, null, 2));

// What one browser visit downloads: the room model and the KTX2 textures;
// the compositor model and the PNGs sit beside them for the compositor.
const ktx2Only = (d) => (fs.existsSync(d) ? fs.readdirSync(d) : [])
  .filter((f) => /\.ktx2$/i.test(f)).reduce((acc, f) => acc + sizeOf(path.join(d, f)), 0);
const totalBefore = sizeOf(srcGlb) + texBefore;
const totalAfter = sizeOf(outGlb) + ktx2Only(outTex);

fs.writeFileSync(path.join(out, 'compression_manifest.json'), JSON.stringify({
  companion: name,
  ticket: 3662,
  mode,
  generated_at: new Date().toISOString(),
  source: path.resolve(src),
  texture_px: texturePx || 'unchanged',
  texture_format: textureFormat,
  browser_texture_format: 'ktx2',
  // For the publisher: send as the avatar manifest's `variants`. Without `compositor` the
  // compositor falls back to `room` and cannot decode its KTX2 textures.
  // `seat` (telegamez#4863) is the room model for seat-size slots; absent with --seat off.
  variants: {
    room: path.basename(outGlb),
    compositor: path.basename(compositorGlb),
    ...(seat ? { seat: path.basename(seatGlb) } : {}),
  },
  compositor_model: { after_bytes: sizeOf(compositorGlb) },
  seat_model: seat
    ? { after_bytes: sizeOf(seatGlb), morph_normals: 'drop', texture_px_max: SEAT_TEXTURE_PX, structure: structure(seatGlb) }
    : null,
  // Room model only. Null when --prune-morphs off.
  pruned_morph_targets: pruned,
  morph_normals: morphNormals,
  lean_textures: leanTextures ? Object.fromEntries(LEAN_TEXTURES) : null,
  model: { before_bytes: sizeOf(srcGlb), after_bytes: sizeOf(outGlb), structure: after },
  textures: { before_bytes: texBefore, after_bytes: texAfter },
  total: { before_bytes: totalBefore, after_bytes: totalAfter },
  runtime: [
    'Browser: needs the KTX2Loader from telegamez#3662 (KHR_texture_basisu is required, and '
      + 'browser_texture_format selects the .ktx2 sidecar siblings).',
    'Compositor: only if the avatar manifest routes it to the compositor variant; otherwise '
      + 'it falls back to the room model and cannot decode it.',
  ],
}, null, 2));

console.log(`\nTOTAL ${MB(totalBefore)} MB -> ${MB(totalAfter)} MB  (${((1 - totalAfter / totalBefore) * 100).toFixed(0)}% off)`
  + '  per browser visit: room model + KTX2 textures');
console.log(`out   ${path.resolve(out)}`);
console.log('\nNOTE: publish with the avatar manifest variants from compression_manifest.json,');
console.log('or the compositor falls back to the KTX2 room model.');
