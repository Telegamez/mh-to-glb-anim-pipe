/**
 * MetaHuman to Avatar — one console for the whole route.
 *
 * Two phases in one page, over one state. Phase 1 makes a package on this machine: Unreal and
 * Blender produce the model, the local stack photographs it. Phase 2 puts that package into an
 * environment: bytes to the CDN, previews to the avatar's asset slots, then the row pointed at
 * the model. The grid across the top says where every character is at every step, so nobody has
 * to remember which of two tools was run last.
 *
 * ## Why a server and not a static page
 *
 * The work is on this host: reading bake directories, launching Unreal and Blender, driving a
 * browser, holding an admin cookie for the length of a run. A page can do none of those. So this
 * is the smallest thing that can — one file, Node built-ins only, nothing to install.
 *
 * ## It does not reimplement the pipeline
 *
 * Every button shells out to the script that already does the job: the stage launchers RUN.md
 * dispatches, `publish_to_cdn.ps1`, `publish_previews.ps1`, `Test-CompanionsLive.ps1`, and the
 * monorepo's capture script. The scripts stay the source of truth and stay runnable without
 * this. A console that grew its own copy of the publish logic would drift, and the drift would
 * surface as "it worked from the console but not from the script".
 *
 * ## The token
 *
 * Typed in the browser, held in memory for the process lifetime, passed to children through the
 * environment. Never written to disk, never logged, never echoed back. It is a live admin
 * session, so this binds to 127.0.0.1 only.
 *
 * Usage:  node server.mjs [--port 7311] [--root <packages dir>] [--stack <url>]
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
 * In the repo the publisher sits one level up in `tools/`; in the zip everything is flat beside
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

const PUBLISHER = findBeside('publish_to_cdn.ps1');
const VERIFIER = findBeside('Test-CompanionsLive.ps1');
const PREVIEWS = findBeside('publish_previews.ps1');

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

/** Environments this console will talk to. Adding one is a line here, not a code change. */
const ENVIRONMENTS = {
  panda: { apiBase: 'https://api.panda.telegames.ai', label: 'panda' },
  beta: { apiBase: 'https://api.beta.telegames.ai', label: 'beta — PRODUCTION', danger: true },
  local: { apiBase: 'http://localhost:3005', label: 'local' },
};

/**
 * The shipping roster, from packages/shared/db/src/seed-metahuman-roster.ts.
 *
 * Used ONLY to decide what is pre-selected. Everything found is still listed and can still be
 * ticked by hand — the scan found `telly-research` sitting beside the six, which is a research
 * variant that must not reach a picker. Listing it and leaving it unticked is the useful
 * behaviour; hiding it would make the directory's contents unanswerable from the console, and
 * pre-ticking it would publish it on the first careless run.
 */
const ROSTER = ['telly', 'tele', 'telie', 'telee', 'tele1', 'tele2'];

/** Held for the process lifetime only. Never persisted. */
const session = { token: null, prior: {} };

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
    const dir = path.join(root, name);
    if (!fs.statSync(dir).isDirectory()) continue;

    const glb = path.join(dir, `${name}.glb`);
    if (!fs.existsSync(glb)) continue; // not a character directory

    const sidecar = fs.existsSync(path.join(dir, 'mh_materials.json'));
    const texDir = path.join(dir, 'textures');
    const textures = fs.existsSync(texDir) ? fs.readdirSync(texDir).length : 0;
    const strayManifest = fs.existsSync(path.join(dir, 'manifest.json'));
    const mb = Math.round(fs.statSync(glb).size / 1048576);

    // The three assets that fill avatars' preview columns. Not required to publish the model
    // directory — they go through a different route — so their absence is reported, never a
    // problem that blocks the publish.
    const PREVIEW_FILES = ['thumbnail.png', 'imagePreview.jpg', 'videoPreview.mp4'];
    const previews = PREVIEW_FILES.every((f) => fs.existsSync(path.join(dir, f)));

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
      id: name, dir, mb, sidecar, textures, strayManifest, previews, body,
      ready: problems.length === 0, problems, inRoster: ROSTER.includes(name),
    });
  }
  return { root, characters };
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
// ---------------------------------------------------------------------------------------
// The publish journal
//
// A publish streams to the browser and is then gone. That is survivable for the model key,
// which can be read back off the CDN, and not survivable at all for the *previous* model_path:
// nothing else records it, and it is the entire rollback. So every publish — including a failed
// one — appends a record to disk.
//
// Records are grouped by bake suffix rather than by date, because the suffix is the batch's own
// identity: one run's characters land in one file however long the run takes or how near midnight
// it starts.
//
// No token is ever written here. Avatar ids and CDN keys are not credentials.

const JOURNAL_DIR = path.join(os.homedir(), '.telegames-publisher', 'publishes');

function recordPublish({ env, apiBase, char, avatarId, bakeSuffix, bakeHash, previous, output, code }) {
  const grab = (re) => { const m = output.match(re); return m ? m[1] : null; };
  const modelKey = grab(/modelKey:\s*(\S+)/);
  const files = grab(/OK - (\d+ files published)/);
  const size = grab(/\d+ files, ([\d.]+ MB)/);

  const out = [`## ${char}`, ''];
  out.push(`- when    ${new Date().toISOString()}`);
  out.push(`- avatar  \`${avatarId}\``);
  out.push(`- bake    \`${bakeHash}\``);
  if (files || size) out.push(`- upload  ${[files, size].filter(Boolean).join(', ')}`);
  const wasPath = previous?.modelPath ?? null;
  const wasStatus = previous?.status ?? null;
  out.push(`- was     ${wasPath ? `\`${wasPath}\`` : '(no model on the row)'}${wasStatus ? `, status \`${wasStatus}\`` : ''}`);
  out.push('');

  if (code !== 0 || !modelKey) {
    // Worth a record precisely because it failed — this is the evidence that a character in a
    // batch was attempted and did not land, which the absence of a record would not show.
    out.push(`**FAILED** — exit ${code}. Nothing was published; the row is untouched.`, '');
  } else {
    out.push('```sql');
    out.push('-- go live');
    // status is set alongside the path because a row created by the admin API starts as
    // 'processing', and the companion form only offers avatars that are isActive AND active.
    // Without this the character publishes correctly and cannot be picked.
    out.push(`update avatars set model_path = '${modelKey}', status = 'active' where id = '${avatarId}';`);
    out.push('');
    out.push('-- roll back');
    out.push(`update avatars set model_path = ${wasPath ? `'${wasPath}'` : 'NULL'}`
      + `${wasStatus ? `, status = '${wasStatus}'` : ''} where id = '${avatarId}';`);
    if (!wasPath) out.push('-- (the row carried no model before this publish)');
    if (!wasStatus) out.push('-- (previous status unknown — left as it is, rather than guessed)');
    out.push('```', '');
  }

  try {
    fs.mkdirSync(JOURNAL_DIR, { recursive: true });
    const file = path.join(JOURNAL_DIR, `${env}-${bakeSuffix}.md`);
    if (!fs.existsSync(file)) {
      fs.writeFileSync(file, [
        `# Publish to ${env}`, '',
        `${apiBase}`,
        `bake suffix \`${bakeSuffix}\``, '',
        'Publishing moved bytes only. Nothing below is live until its "go live" statement runs.',
        '', '---', '',
      ].join('\n'));
    }
    fs.appendFileSync(file, out.join('\n') + '\n');
    return file;
  } catch {
    // A journal that cannot be written must not take the publish down with it — the publish is
    // the thing that matters, and it has already succeeded by the time we get here.
    return null;
  }
}

/**
 * Record a character going live, in the same file as the publish that produced it.
 *
 * The undo is the point. A publish is content-addressed and immutable, so rolling back is
 * putting the previous key back — and the moment this call returns, the previous key exists
 * nowhere else. Written here rather than printed, because a value that only appears in a
 * browser pane is a value nobody has when they need it.
 */
function recordGoLive({ env, apiBase, char, avatarId, bakeSuffix, modelKey, previous }) {
  const out = [`## ${char} — live`, ''];
  out.push(`- when    ${new Date().toISOString()}`);
  out.push(`- avatar  \`${avatarId}\``);
  out.push(`- now     \`${modelKey}\``);
  out.push(`- was     ${previous ? `\`${previous}\`` : '(no model on the row)'}`);
  out.push('');
  // Only a published key can be restored through the API — the route refuses anything that is
  // not a content-addressed object under this avatar, which is what makes it safe. A seeded
  // placeholder is not one, so the FIRST time a character goes live there is nothing to go back
  // to through this door. That is a real limit, and saying so here is better than printing a
  // call that would be refused at the moment somebody needed it to work.
  const restorable = previous && /avatars\/[0-9a-f-]{36}\/[A-Za-z0-9._-]+\/[^/]+\.glb$/i.test(previous);
  out.push('To put it back:');
  out.push('```');
  if (restorable) {
    out.push(`PUT ${apiBase}/api/admin/avatars/${avatarId}/model-path  { "modelKey": "${previous}" }`);
  } else if (previous) {
    out.push(`the previous value is not a published key, so the API will not accept it:`);
    out.push(`  ${previous}`);
    out.push(`restoring it needs a database update, or a re-publish of that bake`);
  } else {
    out.push('the row carried no model before this, so there is nothing to restore');
  }
  out.push('```', '');

  try {
    fs.mkdirSync(JOURNAL_DIR, { recursive: true });
    const file = path.join(JOURNAL_DIR, `${env}-${bakeSuffix}.md`);
    fs.appendFileSync(file, out.join('\n') + '\n');
    return file;
  } catch {
    return null;
  }
}

function stream(res, file, args, extraEnv = {}, onClose) {
  res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
  const child = spawn('pwsh', ['-NoProfile', '-File', file, ...args], {
    env: { ...process.env, ...extraEnv },
    windowsHide: true,
  });
  // Held only to lift the key and counts out at the end, and capped: the browser is the live
  // view, this is a fixed-size tail, and a chatty child cannot grow it without bound.
  let seen = '';
  const keep = (d) => { if (seen.length < 64_000) seen += d.toString(); };
  child.stdout.on('data', (d) => { keep(d); res.write(d); });
  child.stderr.on('data', (d) => { keep(d); res.write(d); });
  child.on('error', (e) => { res.write(`\n[console] failed to start: ${e.message}\n`); res.end(); });
  child.on('close', (code) => {
    const recorded = onClose ? onClose(seen, code) : null;
    if (recorded) res.write(`\n[console] recorded: ${recorded}\n`);
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
    if (url.pathname === '/') {
      return send(res, 200, fs.readFileSync(path.join(HERE, 'console.html'), 'utf8'), 'text/html; charset=utf-8');
    }

    if (url.pathname === '/api/config') {
      return send(res, 200, {
        defaultRoot: DEFAULT_ROOT,
        environments: Object.entries(ENVIRONMENTS).map(([k, v]) => ({ key: k, ...v })),
        hasToken: Boolean(session.token),
        publisherFound: fs.existsSync(PUBLISHER),
        verifierFound: fs.existsSync(VERIFIER),
        previewsFound: fs.existsSync(PREVIEWS),
        // The create half. Reported rather than enforced: a machine without Unreal can still
        // publish packages somebody else baked, and should be told which half it can do.
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

    if (url.pathname === '/api/token' && req.method === 'POST') {
      const { token } = await readBody(req);
      session.token = token || null;
      // Deliberately does not echo the value back.
      return send(res, 200, { hasToken: Boolean(session.token) });
    }

    /**
     * Resolve each character to its avatar row in the TARGET environment.
     *
     * The console asked the operator to type uuids. That was wrong in a way worth naming: the
     * ids differ per environment — panda seeded its own rows — so a remembered uuid is not
     * merely inconvenient, it is a way to publish a correct directory under a stranger's
     * avatar. The environment is the only authority on its own ids, so ask it.
     *
     * A character with no row comes back `avatarId: null` rather than as an error: that is a
     * real and expected state for a character that has never been published here, and the page
     * offers to create it.
     */
    if (url.pathname === '/api/resolve' && req.method === 'POST') {
      const { env, characters } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });

      const r = await fetch(`${target.apiBase}/api/admin/avatars?limit=200`, {
        headers: { Authorization: `Bearer ${session.token}` },
      });
      if (!r.ok) return send(res, 502, { error: `admin API ${r.status} — is the cookie for ${env}?` });
      const body = await r.json();
      const rows = body.avatars || body.data || [];

      const resolved = (characters || []).map((char) => {
        const slug = `mh-${char}`;
        const row = rows.find((x) => x.slug === slug);
        return {
          char, slug,
          avatarId: row ? row.id : null,
          name: row ? row.name : null,
          active: row ? Boolean(row.isActive) : null,
          modelPath: row ? row.modelPath : null,
          // A new row is created with status 'processing', and the admin companion form only
          // offers avatars matching isActive=true AND status=active. Left alone, a freshly
          // published character is simply absent from that dropdown with nothing to explain
          // why — so the status is carried here and the go-live SQL sets it.
          status: row ? (row.status ?? null) : null,
          // Whether the three preview columns are filled. A row created through the admin API
          // gets placeholder URLs pointing at objects that do not exist, so presence alone
          // says nothing — only a real uploaded asset key does.
          previewsPublished: Boolean(
            row && ['thumbnailUrl', 'imagePreviewUrl', 'videoPreviewUrl']
              .every((f) => typeof row[f] === 'string' && row[f].includes('/avatars/') && !/\/(thumbnail|preview)\.(png|mp4|jpg)$/i.test(row[f])),
          ),
          // Not anchored, deliberately. The column stores the relative key that was written
          // to it, but the admin LIST endpoint formats it into a full CDN URL on the way out
          // — so a row that is genuinely live comes back as
          // `https://<bucket>.s3.../avatars/<id>/<hash>/<file>.glb`. Anchoring at `^avatars/`
          // reported every live avatar as unpublished. Searching for the published segment
          // anywhere still excludes the seeded placeholder, which is `models/<id>/<id>.glb`
          // in either form.
          published: /avatars\/[0-9a-f-]{36}\/[A-Za-z0-9._-]+\/[^/]+\.glb$/i.test(row?.modelPath || ''),
        };
      });
      // What each row points at *now*, kept for the journal. A publish overwrites this value,
      // and after that it exists nowhere else — so it is captured here, before it can be lost,
      // rather than asked for at rollback time when the answer is already gone.
      for (const row of resolved) {
        session.prior[`${env}/${row.char}`] = { modelPath: row.modelPath || null, status: row.status || null };
      }

      return send(res, 200, { environment: env, resolved });
    }

    /**
     * Create the avatar row for a character that has none.
     *
     * `modelPath` is deliberately not sent, and the route would not accept it: the registry is
     * pointed at bytes as a separate step, after they exist. So a row created here is a real
     * row with no model yet, which is exactly the state the publish then fills.
     */
    if (url.pathname === '/api/create-avatar' && req.method === 'POST') {
      const { env, char, name, gender, description, category } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      if (!char || !name) return send(res, 400, { error: 'char and name are required' });

      const r = await fetch(`${target.apiBase}/api/admin/avatars`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${session.token}`, 'content-type': 'application/json' },
        body: JSON.stringify({
          name,
          slug: `mh-${char}`,
          category: category || 'metahuman',
          gender: gender || undefined,
          description: description || undefined,
          tags: ['metahuman'],
        }),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) return send(res, 502, { error: out.error || `create failed (${r.status})` });
      return send(res, 200, { avatarId: out.id || out.avatar?.id || null, raw: out });
    }

    if (url.pathname === '/api/verify' && req.method === 'POST') {
      const { env } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      return stream(res, VERIFIER, ['-ApiBase', target.apiBase], { TG_ADMIN_TOKEN: session.token });
    }

    if (url.pathname === '/api/publish' && req.method === 'POST') {
      const { env, characters, bakeHashSuffix, avatarIds } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      if (!Array.isArray(characters) || characters.length === 0) {
        return send(res, 400, { error: 'no characters selected' });
      }
      if (!/^[A-Za-z0-9._-]+$/.test(bakeHashSuffix || '')) {
        return send(res, 400, { error: 'bake hash suffix must be [A-Za-z0-9._-]' });
      }
      // One character per request keeps the stream readable and lets a failure stop the batch
      // at a known point; the page drives the loop.
      const char = characters[0];
      const avatarId = (avatarIds || {})[char];
      if (!avatarId) return send(res, 400, { error: `no avatarId for ${char}` });

      return stream(res, PUBLISHER, [
        '-Char', char, '-AvatarId', avatarId, '-ApiBase', target.apiBase,
        '-Source', path.join(url.searchParams.get('root') || DEFAULT_ROOT, char),
        '-BakeHash', `${char}-${bakeHashSuffix}`,
      ], { TG_ADMIN_TOKEN: session.token }, (output, code) => recordPublish({
        env, apiBase: target.apiBase, char, avatarId,
        bakeSuffix: bakeHashSuffix, bakeHash: `${char}-${bakeHashSuffix}`,
        previous: session.prior[`${env}/${char}`] ?? null,
        output, code,
      }));
    }

    /**
     * The other half of publishing an avatar: its thumbnail, image and video.
     *
     * A separate route and a separate step because it is a separate contract. The model goes
     * through `companion-directory`, which writes bytes and deliberately touches no row; these
     * go through `smart-upload`, which owns the three preview columns and writes them itself.
     * So this one needs no SQL afterwards — and, as a side effect, takes the row out of the
     * 'processing' state a newly created avatar starts in.
     */
    if (url.pathname === '/api/publish-previews' && req.method === 'POST') {
      const { env, characters, avatarIds } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      if (!Array.isArray(characters) || characters.length === 0) {
        return send(res, 400, { error: 'no characters selected' });
      }

      // One per request, like the model publish: the page drives the loop so a failure stops
      // the batch at a character anyone can name.
      const char = characters[0];
      const avatarId = (avatarIds || {})[char];
      if (!avatarId) return send(res, 400, { error: `no avatarId for ${char}` });

      return stream(res, PREVIEWS, [
        '-Char', char, '-AvatarId', avatarId, '-ApiBase', target.apiBase,
        '-Source', path.join(url.searchParams.get('root') || DEFAULT_ROOT, char),
      ], { TG_ADMIN_TOKEN: session.token });
    }

    /**
     * Point a published directory at its avatar row — what the SQL block used to do by hand.
     *
     * Still a separate press from Publish, because it is still a separate decision: bytes go up
     * without changing anything a user sees, and this is the moment they do. What changes is
     * that it no longer needs database access, and the rollback value is written down by the
     * tool instead of depending on someone keeping a text file.
     */
    if (url.pathname === '/api/go-live' && req.method === 'POST') {
      const { env, char, avatarId, modelKey } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      if (!avatarId || !modelKey) return send(res, 400, { error: 'avatarId and modelKey are required' });

      const r = await fetch(`${target.apiBase}/api/admin/avatars/${avatarId}/model-path`, {
        method: 'PUT',
        headers: { Authorization: `Bearer ${session.token}`, 'content-type': 'application/json' },
        body: JSON.stringify({ modelKey }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        return send(res, 502, { error: body?.error ?? body?.message ?? `admin API ${r.status}` });
      }

      // The bake suffix is already in the key, so the record lands in the same file as the
      // publish it completes rather than needing the page to carry it back here.
      const suffix = String(modelKey).split('/')[2] ?? 'unknown';
      const previous = body?.data?.previousModelPath ?? null;
      const recorded = recordGoLive({
        env, apiBase: target.apiBase, char, avatarId,
        bakeSuffix: suffix, modelKey, previous,
      });

      return send(res, 200, { char, avatarId, modelKey, previous, recorded });
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

// 127.0.0.1 only. This holds a live admin session; it must not be reachable from the network.
server.listen(PORT, '127.0.0.1', () => {
  console.log(`[deploy-console] http://127.0.0.1:${PORT}`);
  console.log(`[deploy-console] models root: ${DEFAULT_ROOT}`);
  if (!fs.existsSync(PUBLISHER)) console.warn(`[deploy-console] WARNING publisher not found: ${PUBLISHER}`);
  if (!fs.existsSync(PREVIEWS)) console.warn(`[deploy-console] WARNING preview publisher not found: ${PREVIEWS}`);
});
