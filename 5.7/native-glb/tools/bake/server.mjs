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
const CHARACTERS_OUT = path.join(PIPELINE_ROOT, 'docs', 'characters');

/** Where the local stack serves the capture page. Detected, never started — see the UI. */
const STACK = flag('stack', 'http://localhost:3000');

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

async function bake(res, { asset, force, capture }) {
  const stop = (msg) => { res.write(`\n[bake] ${msg}\n`); res.end(); busy = null; };

  // Bootstrap first: it derives the character id, and everything after is keyed on it.
  const boot = await runStep(res, {
    label: 'Bootstrap',
    cmd: 'python',
    args: [path.join(PIPELINE_ROOT, 'tools', 'bootstrap_character.py'), '--asset', asset, ...(force ? ['--force'] : [])],
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

  const dir = path.join(CHARACTERS_OUT, id);
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
        args: [path.join(monorepo, 'scripts', 'media', 'capture-companion-previews.mjs'), '--dir', dir, '--base', STACK],
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
        charactersOut: CHARACTERS_OUT,
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
        else if (e.name.toLowerCase().endsWith('.uasset')) entries.push({ name: e.name, dir: false });
      }
      entries.sort((a, b) => (a.dir === b.dir ? a.name.localeCompare(b.name) : a.dir ? -1 : 1));
      return send(res, 200, { here, up: path.dirname(here) === here ? null : path.dirname(here), entries });
    }

    /** What has already been baked, and whether each package is complete. */
    if (url.pathname === '/api/baked') {
      const rows = [];
      if (fs.existsSync(CHARACTERS_OUT)) {
        for (const e of fs.readdirSync(CHARACTERS_OUT, { withFileTypes: true })) {
          if (!e.isDirectory()) continue;
          const dir = path.join(CHARACTERS_OUT, e.name);
          const files = fs.readdirSync(dir);
          rows.push({
            id: e.name,
            model: files.some((f) => f.toLowerCase().endsWith('.glb')),
            previews: ['thumbnail.png', 'imagePreview.jpg', 'videoPreview.mp4'].every((f) => files.includes(f)),
          });
        }
      }
      return send(res, 200, { charactersOut: CHARACTERS_OUT, rows });
    }

    if (url.pathname === '/api/bake' && req.method === 'POST') {
      const { asset, force, capture } = await readBody(req);
      if (!asset) return send(res, 400, { error: 'pick a MetaHuman first' });
      if (busy) return send(res, 409, { error: `already baking ${busy} — one at a time` });
      busy = asset;
      res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
      return bake(res, { asset, force: Boolean(force), capture: capture !== false });
    }

    /** Previews on their own, for a model that baked while the stack was down. */
    if (url.pathname === '/api/capture' && req.method === 'POST') {
      const { id } = await readBody(req);
      const dir = path.join(CHARACTERS_OUT, String(id ?? ''));
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
        args: [path.join(monorepo, 'scripts', 'media', 'capture-companion-previews.mjs'), '--dir', dir, '--base', STACK],
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
