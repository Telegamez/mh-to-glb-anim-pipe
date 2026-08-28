#!/usr/bin/env node
/**
 * Telegames MetaHuman Bake Console
 *
 * Turns a MetaHuman saved in Unreal into a finished, publishable package: the pipeline's
 * five stages, the mandatory FBX reweight, and the preview capture, in one run.
 *
 * It shells out to the same launchers RUN.md dispatches and the same capture script the
 * monorepo ships. Nothing is reimplemented here, so anything this does can be done without
 * it and the two cannot drift.
 *
 * The stages have filesystem dependencies on each other and must not overlap, so one bake
 * runs at a time — the console refuses a second rather than interleaving two characters.
 *
 * Binds to 127.0.0.1. There is no credential anywhere in this tool: baking is local work,
 * and publishing — the part that needs a cookie — is a different console.
 */

import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

const argv = process.argv.slice(2);
const flag = (name, dflt) => {
  const i = argv.indexOf(`--${name}`);
  return i === -1 ? dflt : argv[i + 1];
};

const PORT = Number(flag('port', 7312));
const SETTINGS = path.join(os.homedir(), '.telegames-bake.json');

const remember = (key, value) => {
  let all = {};
  try { all = JSON.parse(fs.readFileSync(SETTINGS, 'utf8')); } catch { /* first run */ }
  all[key] = value;
  try { fs.writeFileSync(SETTINGS, JSON.stringify(all, null, 2)); } catch { /* read-only home */ }
};
const recall = (key) => {
  try { return JSON.parse(fs.readFileSync(SETTINGS, 'utf8'))[key]; } catch { return undefined; }
};

/**
 * Find the pipeline root, whether this is running from the repo or from a flat bundle.
 * One implementation rather than a copy per layout, so a bundle cannot drift.
 */
const PIPELINE_ROOT = [
  path.resolve(HERE, '..', '..'),      // repo: 5.7/native-glb/tools/bake -> 5.7/native-glb
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
const PACKAGES = path.resolve(flag('packages', path.resolve(REPO_ROOT, '..', 'characters')));

/**
 * What the export leaves behind in `docs/`, and why. Two groups, for two different reasons.
 *
 * The viewer site's own files. `preview.png` is a 1280x800 Playwright screenshot taken to
 * prove the character loads in a browser — render validation, not artwork, and on the site's
 * dark ground rather than the one previews are shown against. `index.html` is that site's page
 * for the character. Nothing in the product reads either from a published directory.
 *
 * And the three previews, which belong to capture and not to the pipeline. Stage 04 copies a
 * thumbnail in for its gallery, and older runs left previews in `docs/` too, so carrying them
 * across would hand a re-bake its PREDECESSOR's previews. That is exactly what happened on the
 * first run through this console: capture failed, the export had already supplied stale
 * previews, and the package looked complete while depicting a different bake. Excluded here, a
 * failed capture leaves a package visibly missing its previews — which the ready list reports
 * and offers a button to fix.
 */
const NOT_EXPORTED = new Set([
  'preview.png', 'index.html',
  'thumbnail.png', 'imagePreview.jpg', 'videoPreview.mp4',
]);

/** Where the local stack serves the capture page. Detected, never started — see the UI. */
const STACK = flag('stack', 'http://localhost:3000');

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
const PREVIEW_BG = flag('bg', '18385d');

/**
 * The monorepo, for the capture script. A sibling of this repo in the normal layout; the
 * console asks for it once and remembers, so an unusual checkout is a one-time answer
 * rather than a command-line flag nobody remembers to pass.
 */
const guessMonorepo = () => {
  const guesses = [
    recall('monorepo'),
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

function send(res, code, body, type = 'application/json') {
  const data = type === 'application/json' ? JSON.stringify(body) : body;
  res.writeHead(code, { 'content-type': type, 'cache-control': 'no-store' });
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let s = '';
    req.on('data', (d) => { s += d; if (s.length > 1e6) req.destroy(); });
    req.on('end', () => { try { resolve(s ? JSON.parse(s) : {}); } catch (e) { reject(e); } });
    req.on('error', reject);
  });
}

/** Is the local stack up? Capture needs it; this only ever reports, never starts it. */
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

async function bake(res, { asset, body, force, capture }) {
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
      '--asset', asset, '--body', body, ...(force ? ['--force'] : [])],
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
  try {
    fs.mkdirSync(PACKAGES, { recursive: true });
    // Cleared first so a re-bake cannot leave a file from the previous one behind — a
    // stale texture nothing references still gets published with the character.
    fs.rmSync(dir, { recursive: true, force: true });
    fs.cpSync(built, dir, { recursive: true, filter: (from) => !NOT_EXPORTED.has(path.basename(from)) });
  } catch (e) {
    return stop(`Export failed: ${e.message}`);
  }
  res.write(`${fs.readdirSync(dir).length} entries copied\n`);
  res.write(`\n[bake] model ready: ${path.join(dir, `${id}.glb`)}\n`);

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
    if (url.pathname === '/' || url.pathname === '/index.html') {
      const page = path.join(HERE, 'console.html');
      if (!fs.existsSync(page)) return send(res, 500, 'console.html is missing', 'text/plain');
      return send(res, 200, fs.readFileSync(page, 'utf8'), 'text/html; charset=utf-8');
    }

    if (url.pathname === '/api/config') {
      return send(res, 200, {
        pipelineRoot: PIPELINE_ROOT,
        stageOut: STAGE_OUT,
        packages: PACKAGES,
        ueContent: ueContentDir(),
        monorepo: guessMonorepo() ?? null,
        stack: STACK,
        stackUp: await stackUp(),
        busy: Boolean(busy),
      });
    }

    /** Browse for the .uasset. Directories and .uasset files only — nothing else is pickable. */
    if (url.pathname === '/api/browse') {
      const raw = url.searchParams.get('path') || recall('lastBrowse') || ueContentDir() || os.homedir();
      const here = path.resolve(raw);
      if (!fs.existsSync(here)) return send(res, 400, { error: `no such folder: ${here}` });
      remember('lastBrowse', here);
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

    /** What has already been baked, and whether each package is complete. */
    if (url.pathname === '/api/baked') {
      const rows = [];
      if (fs.existsSync(PACKAGES)) {
        for (const e of fs.readdirSync(PACKAGES, { withFileTypes: true })) {
          if (!e.isDirectory()) continue;
          const dir = path.join(PACKAGES, e.name);
          const files = fs.readdirSync(dir);
          // The body archetype a character was last baked with. Carried so a re-bake can
          // start from what it already is: silently flipping it is exactly the defect that
          // made the flag mandatory (telegamez#3285), and nobody re-reads a manifest first.
          let body = null;
          try {
            body = JSON.parse(fs.readFileSync(path.join(PIPELINE_ROOT, 'characters', e.name, 'manifest.json'), 'utf8')).body ?? null;
          } catch { /* never bootstrapped here, or an older manifest */ }

          rows.push({
            id: e.name,
            body,
            model: files.some((f) => f.toLowerCase().endsWith('.glb')),
            previews: ['thumbnail.png', 'imagePreview.jpg', 'videoPreview.mp4'].every((f) => files.includes(f)),
          });
        }
      }
      return send(res, 200, { packages: PACKAGES, rows });
    }

    if (url.pathname === '/api/bake' && req.method === 'POST') {
      const { asset, body, force, capture } = await readBody(req);
      if (!asset) return send(res, 400, { error: 'pick a MetaHuman first' });
      // Refused rather than defaulted: the value decides the animation library, and a wrong
      // or missing one ships silently as female (telegamez#3285).
      if (body !== 'female' && body !== 'male') return send(res, 400, { error: 'choose female or male first' });
      if (busy) return send(res, 409, { error: `already baking ${busy} — one at a time` });
      busy = asset;
      res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
      return bake(res, { asset, body, force: Boolean(force), capture: capture !== false });
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

    return send(res, 404, { error: 'not found' });
  } catch (err) {
    busy = null;
    if (!res.headersSent) return send(res, 500, { error: err.message });
    res.end(`\n[bake] ${err.message}\n`);
    return undefined;
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`  MetaHuman bake console  http://127.0.0.1:${PORT}`);
  console.log(`  pipeline: ${PIPELINE_ROOT}`);
});
