/**
 * MetaHuman bake console — one grid for making a package on this machine.
 *
 * Unreal and Blender produce the model, the local stack photographs it, the compressor makes
 * the ktx2 build. The grid across the top says where every character is at every step, so
 * nobody has to remember which tool was run last.
 *
 * Publishing is not here. A finished package goes to an environment through the Publisher
 * (`Start-Publisher.ps1`, `tools/deploy/`), by the character id in its `character.json`, and
 * go-live and version removal live there too (telegamez#4632 phase 2). This console used to
 * carry a copy of that flow against the platform's avatar routes (`POST /api/admin/avatars`,
 * `PUT .../model-path`, the preview upload); those routes are retired and the copy went with
 * them, so there is no admin token here and nothing on this page can reach an environment.
 *
 * ## Why a server and not a static page
 *
 * The work is on this host: reading bake directories, launching Unreal and Blender, driving a
 * browser. A page can do none of those. So this is the smallest thing that can — one file, Node
 * built-ins only, nothing to install.
 *
 * ## It does not reimplement the pipeline
 *
 * Every button shells out to the script that already does the job: the stage launchers RUN.md
 * dispatches, `package_character.ps1`, `compress_companion.ps1` and the monorepo's capture
 * script. The scripts stay the source of truth and stay runnable without this. A console that
 * grew its own copy of the logic would drift, and the drift would surface as "it worked from
 * the console but not from the script".
 *
 * Usage:  node server.mjs [--port 7312] [--root <packages dir>] [--stack <url>]
 */

import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};

const PORT = Number(arg('port', 7311));

/**
 * Where the character folders live, remembered between runs.
 *
 * The built-in default is a path inside the monorepo, which whoever built this has and an
 * operator will not. So the folder they pick is remembered - in the user profile rather than
 * beside the code, so there is nothing to gitignore and nothing that follows the repo around.
 *
 * Order: --root wins, then the remembered one, then the developer default. A remembered path
 * that no longer exists is ignored rather than shown, so a moved drive degrades to "pick a
 * folder" instead of an error about somewhere this operator has never been.
 */
const SETTINGS = path.join(os.homedir(), '.telegames-publisher.json');

const readSetting = (key) => {
  try { return JSON.parse(fs.readFileSync(SETTINGS, 'utf8'))[key]; } catch { return undefined; }
};
const writeSetting = (key, value) => {
  let all = {};
  try { all = JSON.parse(fs.readFileSync(SETTINGS, 'utf8')); } catch { /* first run */ }
  all[key] = value;
  try { fs.writeFileSync(SETTINGS, JSON.stringify(all, null, 2)); } catch { /* read-only home */ }
};

const remembered = readSetting('root');

/**
 * Where the bake console leaves finished packages, when this is running from the repo.
 * Guarded by existence rather than assumed: from the shared bundle this resolves to
 * nonsense, and falling through is the correct outcome there.
 */
const bakedPackages = path.resolve(HERE, '..', '..', '..', '..', '..', 'characters');

const DEFAULT_ROOT = arg('root', null)
  || (remembered && fs.existsSync(remembered) ? remembered : null)
  || (fs.existsSync(bakedPackages) ? bakedPackages : null)
  // Last resort: the monorepo's local-development models. Kept so an operator with neither a
  // remembered folder nor a bake box still opens somewhere real rather than an empty picker.
  || 'S:\\Avatars\\metahuman\\telegamez\\packages\\web-next\\public\\models';
/**
 * Find the scripts whether this is running from the repo or from the shared bundle.
 *
 * In the repo the compressor sits one level up in `tools/`; in the zip everything is flat beside
 * this file. One search rather than two copies of the tool - a bundle that drifts from the repo
 * is a bundle that gets sent out stale.
 */
const findBeside = (name, ...extra) => {
  for (const dir of [HERE, path.resolve(HERE, '..'), ...extra]) {
    const candidate = path.join(dir, name);
    if (fs.existsSync(candidate)) return candidate;
  }
  return path.join(HERE, name);   // report the flat location when genuinely missing
};

const COMPRESSOR = findBeside('compress_companion.ps1');

/**
 * The one build: ktx2 (telegamez#3662). Owner decision 2026-10-08: the ktx2 build is the default
 * on panda and beta, and "to avoid confusion end to end" the meshopt build is removed. There is
 * no build selector and nothing here reads or writes `<root>/_compressed/` any more; an old one
 * on disk is inert. meshopt itself lives on as the geometry codec inside the ktx2 build.
 *
 * History, for the journals that still say otherwise: telegamez#3728 made meshopt the standard
 * and retired the TG_MESHOPT_RUNTIME_READY hold; telegamez#3662 added ktx2 beside it in its own
 * folder and made it panda's default; telegamez#3701's WebP textures (and their
 * TG_WEBP_RUNTIME_READY hold) applied to the meshopt build only and went with it.
 */
const BUILD = 'ktx2';
/** Refusal text for a page, script or habit that still asks for another build. */
const ONE_BUILD = 'ktx2 is the only build (owner decision 2026-10-08: meshopt, none and full were removed)';

/**
 * Where the ktx2 build of a character lives: `<root>/_compressed_ktx2/<char>/`.
 *
 * Beside the character folders rather than inside one, because the Publisher's
 * `publish_to_cdn.ps1` uploads its `-Source` recursively — a compressed copy nested in the
 * character directory would be published alongside the original and double the payload
 * instead of cutting it.
 */
const COMPRESSED_DIR = '_compressed_ktx2';
const compressedPath = (root, char) => path.join(root, COMPRESSED_DIR, char);

/**
 * The package's newest content time: the model, the sidecar and the clothing textures. The
 * compressed build is a derivative of all three, so a build older than any of them would
 * publish yesterday's companion while reporting success. Clothing colour and texture live in
 * the sidecar and textures/, so a shirt can change with the .glb untouched.
 */
function contentTimeOf(dir, name) {
  const sidecar = path.join(dir, 'mh_materials.json');
  const texDir = path.join(dir, 'textures');
  return [
    fs.statSync(path.join(dir, `${name}.glb`)).mtimeMs,
    ...(fs.existsSync(sidecar) ? [fs.statSync(sidecar).mtimeMs] : []),
    ...(fs.existsSync(texDir)
      ? fs.readdirSync(texDir).map((f) => fs.statSync(path.join(texDir, f)).mtimeMs)
      : []),
  ].reduce((a, b) => Math.max(a, b), 0);
}

/**
 * What is in `_compressed_ktx2/<name>/`, or null. `publishable` needs a ktx2 manifest whose
 * compositor variant exists: without it the compositor falls back to the KTX2 room model and
 * cannot decode it. `current` is what publish wants - that, no older than the package - and
 * anything else is compressed again before upload.
 */
function compressedInfo(root, name, contentTime) {
  const build = BUILD;
  const dir = compressedPath(root, name);
  const glb = path.join(dir, `${name}.glb`);
  if (!fs.existsSync(glb)) return null;
  let mode = 'unknown';
  let textureFormat = 'png';
  let variants = null;
  try {
    const cm = JSON.parse(fs.readFileSync(path.join(dir, 'compression_manifest.json'), 'utf8'));
    mode = cm.mode || 'unknown';
    textureFormat = cm.texture_format || 'png';
    variants = cm.variants || null;
  } catch { /* a build without a readable manifest still has a size worth showing */ }
  const stale = fs.statSync(glb).mtimeMs < contentTime;
  const publishable = mode === 'ktx2' && Boolean(variants?.room && variants?.compositor)
    && fs.existsSync(path.join(dir, variants.compositor));
  return {
    dir, build, mode, textureFormat, variants, stale, publishable,
    // Per browser visit, the figure the size column compares against the bake: the room model,
    // the sidecar and the .ktx2 textures. The compositor's model and PNGs sit beside them and
    // are never fetched by a browser, so counting them would make the build look bigger than
    // what a phone downloads.
    mb: Math.round(payloadBytes(dir, name, { browserKtx2: true }) / 1048576),
    current: mode === build && !stale && publishable,
  };
}

/**
 * Find the pipeline root, whether this is running from the repo or from a flat bundle.
 * One implementation rather than a copy per layout, so a bundle cannot drift.
 */
const PIPELINE_ROOT = [
  path.resolve(HERE, '..', '..'),      // repo: 5.7/native-glb/tools/console -> 5.7/native-glb
  HERE,                                 // bundle: everything flat
].find((d) => fs.existsSync(path.join(d, 'stages')) || fs.existsSync(path.join(d, 'RUN.md')))
  ?? path.resolve(HERE, '..', '..');

const REPO_ROOT = path.resolve(PIPELINE_ROOT, '..', '..');

/**
 * Where stage 04 leaves a character. This is inside the pipeline's `docs/` viewer site —
 * that site is what stage 04 exists to build, so it is read from and never redirected.
 */
const STAGE_OUT = path.join(PIPELINE_ROOT, 'docs', 'characters');

/**
 * Where finished packages are put for publishing, and what the publisher browses.
 *
 * Deliberately outside the repo: whoever publishes has no repo access, and asking them to
 * find a folder inside a pipeline's generated documentation site is asking them to learn a
 * layout that has nothing to do with their job. Derived rather than hard-coded so a
 * different checkout still resolves; --packages overrides it.
 */
const PACKAGES = path.resolve(arg('packages', bakedPackages));

/** Where the local stack serves the capture page. Detected, never started — see the UI. */
const STACK = arg('stack', 'http://localhost:3000');

/**
 * The surface these previews are shown on: the room settings avatar picker. Rendering on it
 * means a card reads as one image instead of a cut-out pasted onto the page.
 *
 * Flat, not a gradient — framing measures the subject against the top-left pixel, so a
 * gradient reads as subject at one end of the frame and clips the fit.
 *
 * Chosen by measurement rather than eye: against tele's garments the navy polo sits 72 away
 * from this on average where the threshold is 24, and further from it than from the grey it
 * replaces. A future character in a mid-navy shirt would want checking before it is trusted.
 */
const PREVIEW_BG = arg('bg', '18385d');

/**
 * The monorepo, for the capture script. A sibling of this repo in the normal layout; the
 * console asks for it once and remembers, so an unusual checkout is a one-time answer
 * rather than a command-line flag nobody remembers to pass.
 */
const guessMonorepo = () => {
  const guesses = [
    readSetting('monorepo'),
    path.resolve(REPO_ROOT, '..', 'telegamez'),
    path.resolve(REPO_ROOT, '..', '..', 'telegamez'),
  ].filter(Boolean);
  return guesses.find((d) => fs.existsSync(path.join(d, 'scripts', 'media', 'capture-companion-previews.mjs')));
};

/** The UE project's Content folder, read from the pipeline's own config. */
function ueContentDir() {
  for (const cfg of [
    path.join(REPO_ROOT, '_config', 'pipeline.yaml'),
    path.join(PIPELINE_ROOT, '_config', 'pipeline.yaml'),
  ]) {
    try {
      const text = fs.readFileSync(cfg, 'utf8');
      const m = text.match(/^\s*ue_project_path:\s*"?([^"\n]+?)"?\s*$/m);
      if (m && m[1] && !m[1].includes('<')) {
        const content = path.join(path.dirname(m[1].trim()), 'Content');
        if (fs.existsSync(content)) return content;
      }
    } catch { /* try the next one */ }
  }
  return null;
}

/**
 * What kind of asset a .uasset holds, from the FIRST recognised class name in its header.
 *
 * "Contains MetaHumanCharacter" is not the question — shoes, hair and clothing all reference
 * the class and match it, which is why a naive search offers an operator a list of boots. The
 * asset's own class appears in its header before the things it imports, so the earliest
 * recognised name wins and the references never do.
 *
 * This labels rather than filters. Baking a Blueprint is caught by the pipeline in about 20
 * seconds with `wrong class: Blueprint`, so a false positive is cheap; a false negative that
 * hid somebody's character behind a rule they cannot see would be expensive. Everything stays
 * pickable — this only says what each thing is.
 */
const UASSET_CLASSES = [
  'MetaHumanCharacter', 'BlueprintGeneratedClass', 'SkeletalMesh', 'StaticMesh',
  'AnimSequence', 'AnimMontage', 'GroomAsset', 'Texture2D', 'MaterialInstanceConstant',
  'LevelSequence', 'SoundWave', 'World',
];

function uassetKind(file) {
  let fd;
  try {
    fd = fs.openSync(file, 'r');
    const buf = Buffer.alloc(65536);
    const n = fs.readSync(fd, buf, 0, buf.length, 0);
    // latin1 so byte offsets survive: the header is length-prefixed names, not text, and a
    // utf8 decode would move or drop bytes and with them the ordering this depends on.
    const head = buf.subarray(0, n).toString('latin1');
    let best = null;
    let bestAt = Infinity;
    for (const cls of UASSET_CLASSES) {
      const at = head.indexOf(cls);
      if (at !== -1 && at < bestAt) { bestAt = at; best = cls; }
    }
    return best;
  } catch {
    return null;
  } finally {
    if (fd !== undefined) { try { fs.closeSync(fd); } catch { /* already gone */ } }
  }
}

// ---------------------------------------------------------------------------------------

/**
 * What a publishable character directory looks like, and why each check is here.
 *
 * `mh_materials.json` is the file the runtime keys "is this a MetaHuman" off, so a directory
 * without it publishes and then renders as plain glTF — no clothing textures, no hair tint,
 * looking exactly like a bad bake. A stray `manifest.json` is refused by the publish route
 * (MANIFEST_AS_FILE) because one sent as a file would publish unchecked, so it is caught here
 * rather than 400-ing mid-batch.
 */
function scanRoot(root) {
  if (!fs.existsSync(root)) return { root, error: `not found: ${root}`, characters: [] };

  const characters = [];
  for (const name of fs.readdirSync(root)) {
    // `_compressed_ktx2` holds character folders and is not one; a legacy `_compressed` is
    // skipped too rather than looked into (it would fail the .glb check below anyway).
    if (name.startsWith('_compressed')) continue;
    const dir = path.join(root, name);
    if (!fs.statSync(dir).isDirectory()) continue;

    const glb = path.join(dir, `${name}.glb`);
    if (!fs.existsSync(glb)) continue; // not a character directory

    const sidecar = fs.existsSync(path.join(dir, 'mh_materials.json'));
    const texDir = path.join(dir, 'textures');
    const textures = fs.existsSync(texDir) ? fs.readdirSync(texDir).length : 0;
    const strayManifest = fs.existsSync(path.join(dir, 'manifest.json'));
    const mb = Math.round(fs.statSync(glb).size / 1048576);
    // What the browser downloads for this companion, which is what #3662 is about and what a
    // compressed build has to be compared against.
    const payloadMb = Math.round(payloadBytes(dir, name) / 1048576);

    // The three preview assets. The Publisher sends them inside the version when they are here
    // and publishes the model without them when they are not, so their absence is reported,
    // never a problem that blocks anything.
    const PREVIEW_FILES = ['thumbnail.png', 'imagePreview.jpg', 'videoPreview.mp4'];
    const previews = PREVIEW_FILES.every((f) => fs.existsSync(path.join(dir, f)));

    // The ktx2 build, if one has been made. telegamez#3728/#3662: every bake makes one, and
    // publish remakes a missing or stale one before uploading, so the page reports it rather
    // than gating on it.
    const compressed = compressedInfo(root, name, contentTimeOf(dir, name));

    const problems = [];
    if (!sidecar) problems.push('no mh_materials.json — would render untextured');
    if (textures === 0) problems.push('no textures/');
    if (strayManifest) problems.push('stray manifest.json — publish would be refused');

    // Recorded at bootstrap, and the one setting a re-bake must not silently flip: a wrong
    // body ships as the female animation library whatever the character is (telegamez#3285).
    let body = null;
    try {
      body = JSON.parse(fs.readFileSync(path.join(PIPELINE_ROOT, 'characters', name, 'manifest.json'), 'utf8')).body ?? null;
    } catch { /* never bootstrapped here, or an older manifest */ }

    characters.push({
      id: name, dir, mb, payloadMb, sidecar, textures, strayManifest, previews, body, compressed,
      ready: problems.length === 0, problems,
    });
  }
  return { root, characters };
}

/**
 * What a companion actually costs to download: the model, the sidecar and its textures.
 *
 * Not the .glb alone. The clothing textures are half the payload and are fetched at runtime
 * alongside the model, so a GLB-only figure understates a companion by about 45% — and, worse,
 * cannot be compared against a compressed build, whose saving is spread across both. The
 * previous version of this compared a GLB-only "before" against a whole-package "after" and
 * quietly flattered the result.
 */
function payloadBytes(dir, name, { browserKtx2 = false } = {}) {
  let total = 0;
  for (const f of [`${name}.glb`, 'mh_materials.json']) {
    try { total += fs.statSync(path.join(dir, f)).size; } catch { /* optional */ }
  }
  const tex = path.join(dir, 'textures');
  if (fs.existsSync(tex)) {
    const files = fs.readdirSync(tex);
    const have = new Set(files.map((f) => f.toLowerCase()));
    for (const f of files) {
      // A ktx2 build keeps every PNG for the compositor; the browser fetches the sibling instead.
      if (browserKtx2 && /\.png$/i.test(f) && have.has(f.toLowerCase().replace(/\.png$/, '.ktx2'))) continue;
      try { total += fs.statSync(path.join(tex, f)).size; } catch { /* vanished mid-walk */ }
    }
  }
  return total;
}

/** Bytes under a directory, so a delete can report what it actually freed. */
function dirBytes(dir) {
  let total = 0;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    try { total += e.isDirectory() ? dirBytes(p) : fs.statSync(p).size; } catch { /* vanished mid-walk */ }
  }
  return total;
}

// ---------------------------------------------------------------------------------------

function send(res, code, body, type = 'application/json') {
  res.writeHead(code, { 'content-type': type, 'cache-control': 'no-store' });
  res.end(typeof body === 'string' ? body : JSON.stringify(body));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let s = '';
    req.on('data', (c) => { s += c; if (s.length > 1e6) reject(new Error('body too large')); });
    req.on('end', () => { try { resolve(s ? JSON.parse(s) : {}); } catch (e) { reject(e); } });
  });
}

/**
 * Run a child and stream its output to the response as it arrives.
 *
 * Plain chunked text rather than SSE: the consumer is one page showing a log, and a batch that
 * takes minutes should show its first line immediately rather than after the last one.
 */
function stream(res, file, args) {
  res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
  const child = spawn('pwsh', ['-NoProfile', '-File', file, ...args], { windowsHide: true });
  child.stdout.on('data', (d) => res.write(d));
  child.stderr.on('data', (d) => res.write(d));
  child.on('error', (e) => { res.write(`\n[console] failed to start: ${e.message}\n`); res.end(); });
  child.on('close', (code) => {
    res.write(`\n[console] exit ${code}\n`);
    res.end();
  });
}

async function stackUp() {
  try {
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), 2500);
    const r = await fetch(`${STACK}/dev/companion-capture`, { signal: c.signal });
    clearTimeout(t);
    return r.ok;
  } catch { return false; }
}

/**
 * One bake at a time.
 *
 * The stages read and write the same character directory and the contract says plainly not
 * to run them in parallel. A refused second bake is a clear message; two interleaved ones
 * are a corrupted character nobody can explain afterwards.
 */
let busy = null;

function runStep(res, { label, cmd, args, cwd }) {
  return new Promise((resolve) => {
    res.write(`\n=== ${label}\n`);
    const child = spawn(cmd, args, { cwd: cwd ?? PIPELINE_ROOT, windowsHide: true });
    let tail = '';
    const keep = (d) => { if (tail.length < 16_000) tail += d.toString(); };
    child.stdout.on('data', (d) => { keep(d); res.write(d); });
    child.stderr.on('data', (d) => { keep(d); res.write(d); });
    child.on('error', (e) => { res.write(`\n[bake] could not start ${cmd}: ${e.message}\n`); resolve({ code: -1, tail }); });
    child.on('close', (code) => resolve({ code, tail }));
  });
}

/**
 * The full sequence for one character.
 *
 * Stage 03 is deliberately absent: run_fbx_reweight.ps1 runs it. Unreal's glTF exporter
 * corrupts skin weights, the reweight replaces them from a parallel FBX export and then
 * exports the GLB itself. A character baked without it ships torn garments, so there is no
 * path here that skips it.
 */
const STAGES = [
  { label: 'Stage 00 — Unreal assemble', script: 'stages/00-unreal-assemble/tools/run_assemble.ps1' },
  { label: 'Stage 01 — Unreal GLB export', script: 'stages/01-unreal-glb-export/tools/run_export.ps1' },
  { label: 'Stage 02 — Blender assemble', script: 'stages/02-blender-assemble/tools/run_assemble.ps1' },
  { label: 'FBX reweight (also exports the GLB)', script: 'tools/run_fbx_reweight.ps1' },
  { label: 'Stage 04 — Webview build', script: 'stages/04-webview-build/tools/run_site.ps1' },
];

async function bake(res, { asset, body, force, capture, charId }) {
  const stop = (msg) => { res.write(`\n[bake] ${msg}\n`); res.end(); busy = null; };

  // Bootstrap first: it derives the character id, and everything after is keyed on it.
  //
  // --body is required by the script and is not defaulted here for the reason it is required
  // there: a wrong or missing value ships silently as female (telegamez#3285). Choosing one
  // on the operator's behalf would put that bug back, so the console refuses instead.
  const boot = await runStep(res, {
    label: 'Bootstrap',
    cmd: 'python',
    args: [path.join(PIPELINE_ROOT, 'tools', 'bootstrap_character.py'),
      '--asset', asset, '--body', body,
      // Optional: give this bake a different id from the asset's own name, so one UE asset
      // can produce two companions side by side.
      ...(charId ? ['--id', charId] : []),
      ...(force ? ['--force'] : [])],
  });

  if (boot.code !== 0) {
    // Exit 1 with an existing character is the one failure a person can act on directly,
    // so it is named rather than left as a code to look up.
    if (/already exists/i.test(boot.tail)) {
      return stop('That character has already been baked. Tick "re-bake" to replace it.');
    }
    return stop(`Bootstrap failed (exit ${boot.code}). Nothing was changed.`);
  }

  const idMatch = boot.tail.match(/\[bootstrap\]\s*char_id:\s*(\S+)/);
  if (!idMatch) return stop('Bootstrap did not report a character id — stopping rather than guessing one.');
  const id = idMatch[1];
  res.write(`\n[bake] character id: ${id}\n`);

  for (const stage of STAGES) {
    const script = path.join(PIPELINE_ROOT, ...stage.script.split('/'));
    if (!fs.existsSync(script)) return stop(`Missing launcher: ${stage.script}`);
    const { code } = await runStep(res, {
      label: stage.label,
      cmd: 'pwsh',
      args: ['-NoProfile', '-File', script, '-Char', id],
    });
    if (code !== 0) return stop(`${stage.label} failed (exit ${code}). Stopped here — later stages depend on it.`);
  }

  // Stage 04 writes into the pipeline's viewer site. Check the model is really there before
  // treating the run as a success: an exit code says a script finished, not that it produced
  // anything.
  const built = path.join(STAGE_OUT, id);
  if (!fs.existsSync(path.join(built, `${id}.glb`))) {
    return stop(`Stage 04 reported success but ${id}.glb is not in ${built}.`);
  }

  // Copy the finished character to where publishing looks for it. A copy, not a move: the
  // viewer site is built from its own copy and breaks if the character is taken out from
  // under it.
  const dir = path.join(PACKAGES, id);
  res.write(`\n=== Export to ${dir}\n`);
  // Shelled out like every other step rather than done inline. It used to be inline, which
  // meant a package could only be refreshed by baking the character again from Unreal --
  // ten minutes for a file copy -- after re-running any single stage.
  const packaged = await runStep(res, {
    label: `Package — ${id}`,
    cmd: 'pwsh',
    args: ['-NoProfile', '-File', path.join(PIPELINE_ROOT, 'tools', 'package_character.ps1'),
      '-Char', id, '-Packages', PACKAGES],
  });
  if (packaged.code !== 0) return stop(`Export failed (exit ${packaged.code}).`);
  res.write(`\n[bake] model ready: ${path.join(dir, `${id}.glb`)}\n`);

  // telegamez#3728, #3662: every bake ends with the ktx2 build (the only build since the owner
  // decision of 2026-10-08). Publish uploads this, not the bake, and would make it then anyway;
  // making it here means the size column is right the moment the character appears. Needs the
  // `ktx` CLI. Not fatal: the package is finished and correct without it.
  if (!fs.existsSync(COMPRESSOR)) {
    res.write('\n[bake] Compress skipped: compress_companion.ps1 is not beside this console.\n');
  } else {
    const { code } = await runStep(res, {
      label: `Compress — ${id} (${BUILD})`,
      cmd: 'pwsh',
      args: ['-NoProfile', '-File', COMPRESSOR, '-Char', id, '-Root', PACKAGES, '-Mode', BUILD],
    });
    if (code !== 0) {
      res.write('\n[bake] Compress failed. The package is finished and correct; publish will compress it again.\n');
    }
  }

  if (capture) {
    const monorepo = guessMonorepo();
    if (!monorepo) {
      res.write('\n[bake] Previews skipped: could not find the telegamez checkout.\n');
    } else if (!(await stackUp())) {
      res.write(`\n[bake] Previews skipped: the local stack is not answering at ${STACK}.\n`);
      res.write('[bake] The model is finished and correct — start the stack and press Capture.\n');
    } else {
      const { code } = await runStep(res, {
        label: 'Previews — thumbnail, image, video',
        cmd: 'node',
        args: [path.join(monorepo, 'scripts', 'media', 'capture-companion-previews.mjs'),
          '--dir', dir, '--base', STACK, '--bg', PREVIEW_BG],
        cwd: monorepo,
      });
      if (code !== 0) {
        res.write('\n[bake] Previews failed. The model is still finished and correct; previews can be retried.\n');
      }
    }
  }

  const have = fs.existsSync(dir) ? fs.readdirSync(dir) : [];
  res.write(`\n[bake] package: ${dir}\n[bake] contains: ${have.join(', ')}\n`);
  res.write('\n[bake] done\n');
  res.end();
  busy = null;
}

// ---------------------------------------------------------------------------------------

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);

  try {
    if (url.pathname === '/') {
      return send(res, 200, fs.readFileSync(path.join(HERE, 'console.html'), 'utf8'), 'text/html; charset=utf-8');
    }

    if (url.pathname === '/api/config') {
      return send(res, 200, {
        defaultRoot: DEFAULT_ROOT,
        // Where `bake` actually writes. Reported because it is chosen independently of the
        // browse root, and when the two diverge a bake succeeds and simply does not appear:
        // the grid rescans, correctly, in a folder nothing was written to. That is a silent
        // failure with a completed bake behind it, which is the worst kind to debug.
        packagesRoot: PACKAGES,
        compressorFound: fs.existsSync(COMPRESSOR),
        // The build every environment publishes. Fixed, reported so the page labels it rather
        // than offering a choice (owner decision 2026-10-08).
        build: BUILD,
        // Reported rather than enforced: a machine without Unreal can still photograph and
        // compress packages somebody else baked, and should be told what it can do.
        ueContent: ueContentDir(),
        monorepo: guessMonorepo() ?? null,
        stack: STACK,
        stackUp: await stackUp(),
        busy: Boolean(busy),
      });
    }

    /**
     * Browse the filesystem so the operator can pick a folder instead of typing a path.
     *
     * This is a local operator tool on the operator's own machine, so there is no path
     * restriction to enforce — the person running it can already open Explorer. What it does
     * do is fail softly: a directory it cannot read is listed and marked, not thrown, because
     * one unreadable system folder should not empty the whole listing.
     *
     * With no path it lists drives, which is where a Windows operator expects to start.
     */
    if (url.pathname === '/api/browse') {
      const raw = url.searchParams.get('path') || '';

      if (!raw) {
        const drives = [];
        for (const letter of 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('')) {
          const root = letter + ':' + path.sep;
          try { fs.accessSync(root); drives.push({ name: letter + ':', path: root, isCharacter: false }); } catch { /* absent */ }
        }
        return send(res, 200, { path: '', parent: null, dirs: drives, drives: true });
      }

      const here = path.resolve(raw);
      if (!fs.existsSync(here)) return send(res, 200, { path: here, parent: path.dirname(here), dirs: [], error: 'not found' });

      let entries = [];
      try {
        entries = fs.readdirSync(here, { withFileTypes: true }).filter((e) => e.isDirectory());
      } catch (e) {
        return send(res, 200, { path: here, parent: path.dirname(here), dirs: [], error: e.code || 'unreadable' });
      }

      const dirs = entries.map((e) => {
        const full = path.join(here, e.name);
        // A folder holding <name>.glb is a character; one holding character folders is a
        // models root. Marking both is what lets the picker say "this is the one".
        let isCharacter = false;
        let characterCount = 0;
        try {
          isCharacter = fs.existsSync(path.join(full, e.name + '.glb'));
          if (!isCharacter) {
            for (const sub of fs.readdirSync(full, { withFileTypes: true })) {
              if (sub.isDirectory() && fs.existsSync(path.join(full, sub.name, sub.name + '.glb'))) characterCount++;
            }
          }
        } catch { /* unreadable child; leave the flags false */ }
        return { name: e.name, path: full, isCharacter, characterCount };
      });

      const parent = path.dirname(here);
      return send(res, 200, { path: here, parent: parent === here ? '' : parent, dirs });
    }

    if (url.pathname === '/api/scan') {
      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      const result = scanRoot(root);
      // Remember a folder that actually held characters, not one that merely got typed.
      if (result.characters.length) writeSetting('root', root);
      return send(res, 200, result);
    }

    /**
     * Compress one companion before publishing it — #3662.
     *
     * A MetaHuman costs ~87 MB to download, of which ~73% of the model is uncompressed
     * FLOAT32 morph-target data. Beta serves Avaturn avatars at ~11.5 MB, so publishing
     * these as they come out of the bake multiplies the mobile payload roughly eightfold.
     *
     * Shells out like every other button. The output is a complete character directory, so
     * publishing a compressed build is the ordinary publish pointed at a different -Source
     * rather than a second publish path.
     */
    if (url.pathname === '/api/compress' && req.method === 'POST') {
      const { characters, mode, texturePx } = await readBody(req);
      if (!Array.isArray(characters) || characters.length === 0) {
        return send(res, 400, { error: 'no characters selected' });
      }
      // ktx2, always (owner decision 2026-10-08). `mode` is accepted only as `ktx2` so an older
      // page or script that still sends `meshopt` is told why, not silently given a ktx2 build.
      const build = mode || BUILD;
      if (build !== BUILD) {
        return send(res, 400, { error: `${ONE_BUILD}; asked for ${mode}` });
      }
      // This reads the character directory a bake is writing. Compressing mid-bake would
      // produce a build of a half-written character and report success, so it waits like a
      // second bake does.
      if (busy) return send(res, 409, { error: `${busy} is baking — one at a time` });
      // One per request, like a bake: the page drives the loop so a failure stops the batch
      // at a character anyone can name.
      const char = characters[0];
      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      const args = ['-Char', char, '-Root', root, '-Mode', build];
      if (Number(texturePx) > 0) args.push('-TexturePx', String(Number(texturePx)));
      return stream(res, COMPRESSOR, args);
    }

    if (url.pathname === '/api/assets') {
      const raw = url.searchParams.get('path') || readSetting('lastBrowse') || ueContentDir() || os.homedir();
      const here = path.resolve(raw);
      if (!fs.existsSync(here)) return send(res, 400, { error: `no such folder: ${here}` });
      writeSetting('lastBrowse', here);
      const entries = [];
      for (const e of fs.readdirSync(here, { withFileTypes: true })) {
        if (e.isDirectory()) entries.push({ name: e.name, dir: true });
        else if (e.name.toLowerCase().endsWith('.uasset')) {
          entries.push({ name: e.name, dir: false, kind: uassetKind(path.join(here, e.name)) });
        }
      }
      entries.sort((a, b) => (a.dir === b.dir ? a.name.localeCompare(b.name) : a.dir ? -1 : 1));
      return send(res, 200, { here, up: path.dirname(here) === here ? null : path.dirname(here), entries });
    }


    if (url.pathname === '/api/bake' && req.method === 'POST') {
      const { asset, body, force, capture, id } = await readBody(req);
      if (!asset) return send(res, 400, { error: 'pick a MetaHuman first' });
      // Same charset the publish path validates, and it becomes a directory name and a
      // filename, so it is checked here rather than trusted.
      if (id && !/^[A-Za-z0-9._-]{1,64}$/.test(id)) {
        return send(res, 400, { error: 'name must be 1-64 chars of [A-Za-z0-9._-]' });
      }
      // Refused rather than defaulted: the value decides the animation library, and a wrong
      // or missing one ships silently as female (telegamez#3285).
      if (body !== 'female' && body !== 'male') return send(res, 400, { error: 'choose female or male first' });
      if (busy) return send(res, 409, { error: `already baking ${busy} — one at a time` });
      busy = asset;
      res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
      return bake(res, { asset, body, force: Boolean(force), capture: capture !== false, charId: id });
    }

    /** Previews on their own, for a model that baked while the stack was down. */
    if (url.pathname === '/api/capture' && req.method === 'POST') {
      const { id } = await readBody(req);
      const dir = path.join(PACKAGES, String(id ?? ''));
      if (!id || !fs.existsSync(dir)) return send(res, 400, { error: 'unknown character' });
      const monorepo = guessMonorepo();
      if (!monorepo) return send(res, 400, { error: 'could not find the telegamez checkout' });
      if (busy) return send(res, 409, { error: 'a bake is running — one at a time' });
      if (!(await stackUp())) return send(res, 400, { error: `the local stack is not answering at ${STACK}` });
      busy = id;
      res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
      await runStep(res, {
        label: `Previews — ${id}`,
        cmd: 'node',
        args: [path.join(monorepo, 'scripts', 'media', 'capture-companion-previews.mjs'),
          '--dir', dir, '--base', STACK, '--bg', PREVIEW_BG],
        cwd: monorepo,
      });
      res.write('\n[bake] done\n');
      res.end();
      busy = null;
      return undefined;
    }


    /**
     * Remove a character from this machine, so a re-bake starts from nothing.
     *
     * Three directories hold one character, and leaving any of them is a way to get a bake that
     * looks fresh and is not:
     *
     *   <root>/<id>            the finished package — what the publisher browses
     *   characters/<id>        the pipeline working copy — what makes bootstrap refuse a re-bake
     *   docs/characters/<id>   stage 04's output, and what the package is COPIED FROM. Stage 04
     *                          writes into it without clearing it first, so a file the new bake
     *                          does not produce survives there and is copied forward — past the
     *                          package's own rm, which only clears what it is about to write.
     *
     * Local only, and deliberately so. A character that has been published stays published and
     * stays live: the bytes are on the CDN and the row still points at them. Nothing here can
     * reach an environment, and the page says so before it asks.
     */
    if (url.pathname === '/api/delete' && req.method === 'POST') {
      const { ids } = await readBody(req);
      if (!Array.isArray(ids) || !ids.length) return send(res, 400, { error: 'nothing selected' });
      // The stages read and write these same directories. Removing one mid-bake is how a
      // character becomes something nobody can explain afterwards.
      if (busy) return send(res, 409, { error: `${busy} is baking — one at a time` });

      const where = path.resolve(url.searchParams.get('root') || DEFAULT_ROOT);
      // Ids are checked against the folders that actually exist rather than pattern-matched, so
      // nothing here can be talked into removing a path of the caller's choosing.
      const known = new Set(scanRoot(where).characters.map((c) => c.id));
      const unknown = ids.filter((id) => !known.has(id));
      if (unknown.length) return send(res, 400, { error: `not characters in ${where}: ${unknown.join(', ')}` });

      // Belt and braces on top of the id check: a target that is not directly inside the parent
      // it was built from is not removed, whatever the id turned out to be.
      const contains = (parent, child) => {
        const rel = path.relative(parent, child);
        return rel !== '' && !rel.startsWith('..') && !path.isAbsolute(rel);
      };

      const removed = [];
      let freed = 0;
      for (const id of ids) {
        for (const [what, parent] of [
          ['package', where],
          // Left behind, this outlives the character it was built from and stays publishable:
          // the next bake under the same name would find a compressed build of the OLD one
          // sitting there, correctly named and reporting a size. The ktx2 folder only: a legacy
          // `_compressed/<id>` is no longer read by anything, so it is left for the operator.
          ['compressed', path.join(where, COMPRESSED_DIR)],
          ['working copy', path.join(PIPELINE_ROOT, 'characters')],
          ['viewer copy', STAGE_OUT],
        ]) {
          const dir = path.join(parent, id);
          if (!contains(parent, dir) || !fs.existsSync(dir)) continue;
          const bytes = dirBytes(dir);
          try {
            fs.rmSync(dir, { recursive: true, force: true });
          } catch (e) {
            // Report what did go before stopping: a half-finished delete the operator cannot
            // see is worse than one they can finish by hand.
            return send(res, 500, { error: `could not remove ${dir}: ${e.message}`, removed, freed });
          }
          removed.push({ id, what, dir, bytes });
          freed += bytes;
        }
      }
      return send(res, 200, { removed, freed });
    }

    /**
     * Serve a package's own preview files, so the grid can show who each character is.
     *
     * A row that says "tele, five ticks" does not tell an operator whether tele is the model
     * they meant, or male, or the one they baked an hour ago. The picture does, and it is
     * sitting in the package already — the same file that will be uploaded.
     *
     * Only the three preview assets, by exact name. The character id is checked against the
     * folders that actually exist rather than pattern-matched, so nothing here can be talked
     * into reading a path of the caller's choosing.
     */
    if (url.pathname === '/api/asset') {
      const char = String(url.searchParams.get('char') || '');
      const file = String(url.searchParams.get('file') || '');
      const TYPES = {
        'thumbnail.png': 'image/png',
        'imagePreview.jpg': 'image/jpeg',
        'videoPreview.mp4': 'video/mp4',
      };
      if (!TYPES[file]) return send(res, 400, { error: 'unknown asset' });

      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      const known = fs.existsSync(root)
        ? fs.readdirSync(root, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name)
        : [];
      if (!known.includes(char)) return send(res, 404, { error: 'unknown character' });

      const full = path.join(root, char, file);
      if (!fs.existsSync(full)) return send(res, 404, { error: 'not captured yet' });

      const stat = fs.statSync(full);
      res.writeHead(200, {
        'content-type': TYPES[file],
        'content-length': stat.size,
        // The file is replaced in place by a re-capture, so a cached copy is a stale answer to
        // exactly the question this endpoint exists to answer.
        'cache-control': 'no-store',
      });
      return fs.createReadStream(full).pipe(res);
    }

    /** The page's script, kept out of the HTML so it can be read and diffed on its own. */
    if (url.pathname === '/console.js') {
      return send(res, 200, fs.readFileSync(path.join(HERE, 'console.js'), 'utf8'),
        'application/javascript; charset=utf-8');
    }

    return send(res, 404, { error: 'not found' });
  } catch (e) {
    return send(res, 500, { error: e.message });
  }
});

// 127.0.0.1 only. It launches Unreal, Blender and a browser on this machine and reads and
// removes folders here; it must not be reachable from the network.
server.listen(PORT, '127.0.0.1', () => {
  console.log(`[bake-console] http://127.0.0.1:${PORT}`);
  console.log(`[bake-console] packages root: ${DEFAULT_ROOT}`);
  if (!fs.existsSync(COMPRESSOR)) console.warn(`[bake-console] WARNING compressor not found: ${COMPRESSOR}`);
  console.log('[bake-console] every bake ends with the ktx2 build (the only build); publishing is the Publisher\'s (Start-Publisher.ps1)');
});
