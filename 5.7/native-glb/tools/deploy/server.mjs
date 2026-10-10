/**
 * Companion deploy console — a localhost GUI for publishing MetaHumans to an environment.
 *
 * Point it at a directory of character bakes, see at a glance which are ready, tick the ones
 * you want and run the batch. Streams the underlying script output live.
 *
 * ## Why a server and not a static page
 *
 * The work is on this host: reading bake directories, running PowerShell, holding an admin
 * cookie for the length of a run. A browser can do none of those. So this is the smallest
 * thing that can — one file, Node built-ins only, no package.json and nothing to install.
 *
 * ## It does not reimplement the pipeline
 *
 * Every button shells out to the script that already does the job — `publish_to_cdn.ps1`,
 * `Test-CompanionsLive.ps1`. That matters: the scripts stay the source of truth, stay
 * runnable without this, and stay usable from CI. A GUI that grew its own copy of the publish
 * logic would drift from them, and the drift would surface as "it worked from the console but
 * not from the script", which is the worst kind of bug to chase.
 *
 * ## The token
 *
 * Typed in the browser, held in memory for the process lifetime, passed to children through
 * the environment. Never written to disk, never logged, never echoed back to the page. It is a
 * live admin session, so the server binds to 127.0.0.1 only.
 *
 * Usage:  node server.mjs [--port 7311] [--root <models dir>]
 */

import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
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
const COMPRESSOR = findBeside('compress_companion.ps1');

/**
 * The one build this publisher makes and uploads: ktx2 (telegamez#3662) - a KTX2 room model, a
 * PNG `<char>.compositor.glb` for the native compositor, and the sidecar PNGs each with a
 * `.ktx2` beside it. Its geometry is still meshopt-compressed inside the file; what is gone is
 * the separate "meshopt" BUILD.
 *
 * Owner decision, 2026-10-08: ktx2 is the only build on panda and beta, and meshopt goes end to
 * end. Two builds of one character under one name is how a meshopt tg-drake reached beta when
 * ktx2 was meant (journal beta-20261008-6670), and nothing downstream could tell them apart.
 *
 * Lives beside the character folders rather than inside one, because `publish_to_cdn.ps1`
 * uploads its `-Source` recursively - a build nested in the character directory would be
 * published alongside the original.
 */
const BUILD = 'ktx2';
const BUILD_DIR = '_compressed_ktx2';
const compressedPath = (root, char) => path.join(root, BUILD_DIR, char);

/**
 * Which avatar a package belongs to, per environment, stored IN the package folder.
 *
 * The folder name used to be the only link: `tg-drake` meant the avatar with slug
 * `mh-tg-drake`, so renaming a folder silently became "Add as new" - a second avatar, with every
 * companion still on the first. The binding is the avatar's id, learned the first time the
 * package is matched or created in an environment and used from then on. The name is free to
 * change; the slug is only the fallback for a package that has never been bound there.
 *
 * Kept in the package because that is the one thing a rename carries along. It is never
 * uploaded: publish sends the ktx2 build folder, which the compressor fills with the model,
 * sidecar and textures only.
 */
const BINDING_FILE = 'avatar-binding.json';

/** Newest of the model, the sidecar and the textures - what a compressed build must postdate. */
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
 * The ktx2 build for `<name>`, or null; `current` is what Publish sends as it stands. Only
 * current if it records the `variants` the publish sends as the avatar manifest, naming this
 * character's files - without them the compositor would fall back to the KTX2 model, and
 * `publish_to_cdn.ps1` refuses a room variant that is not `<char>.glb`.
 */
function compressedInfo(root, name, contentTime) {
  const dir = compressedPath(root, name);
  const glb = path.join(dir, `${name}.glb`);
  if (!fs.existsSync(glb)) return null;
  let mode = 'unknown';
  let variants = null;
  try {
    const cm = JSON.parse(fs.readFileSync(path.join(dir, 'compression_manifest.json'), 'utf8'));
    mode = cm.mode || 'unknown';
    variants = cm.variants || null;
  } catch { /* a build without a readable manifest still has a size worth showing */ }
  const tex = path.join(dir, 'textures');
  const bytes = fs.statSync(glb).size + (fs.existsSync(tex)
    ? fs.readdirSync(tex).reduce((a, f) => a + fs.statSync(path.join(tex, f)).size, 0)
    : 0);
  const stale = fs.statSync(glb).mtimeMs < contentTime;
  const publishable = mode === BUILD && variants?.room === `${name}.glb` && Boolean(variants?.compositor)
    && fs.existsSync(path.join(dir, variants.compositor));
  return {
    dir, mode, variants, stale, publishable, mb: Math.round(bytes / 1048576),
    current: !stale && publishable,
  };
}

/** `{ environments: { <env>: { avatarId, apiBase, boundAt, how } } }`, or an empty one. */
function readBinding(dir) {
  try {
    const b = JSON.parse(fs.readFileSync(path.join(dir, BINDING_FILE), 'utf8'));
    return b && typeof b.environments === 'object' && b.environments ? b : { environments: {} };
  } catch { return { environments: {} }; }
}

function writeBinding(dir, env, apiBase, avatarId, how) {
  const b = readBinding(dir);
  b.note = 'Which avatar this package publishes to, per environment. Written by the publisher; '
    + 'the folder and model can be renamed freely. Delete an entry only to re-bind it.';
  b.environments[env] = { avatarId, apiBase, boundAt: new Date().toISOString(), how };
  fs.writeFileSync(path.join(dir, BINDING_FILE), JSON.stringify(b, null, 2) + '\n');
}

/**
 * The model in a package folder: `<folder>.glb`, or - when someone renamed the folder by hand -
 * the folder's one other room model. Returns the file name, or null when there is none or the
 * choice would be a guess.
 */
function modelFileOf(dir, name) {
  if (fs.existsSync(path.join(dir, `${name}.glb`))) return `${name}.glb`;
  let glbs = [];
  try {
    glbs = fs.readdirSync(dir).filter((f) => /\.glb$/i.test(f) && !/\.compositor\.glb$/i.test(f));
  } catch { return null; }
  return glbs.length === 1 ? glbs[0] : null;
}

/** One child, streamed into a response that stays open for the next step. */
function runInto(res, label, file, args) {
  return new Promise((resolve) => {
    res.write(`\n=== ${label}\n`);
    const child = spawn('pwsh', ['-NoProfile', '-File', file, ...args], { windowsHide: true });
    child.stdout.on('data', (d) => res.write(d));
    child.stderr.on('data', (d) => res.write(d));
    child.on('error', (e) => { res.write(`\n[console] could not start: ${e.message}\n`); resolve(-1); });
    child.on('close', (code) => resolve(code));
  });
}

/** Environments this console will talk to. Adding one is a line here, not a code change. */
const ENVIRONMENTS = {
  // Every environment runs the KTX2 web runtime (telegamez#4551; beta since 2.0.0-rc52.3).
  panda: { apiBase: 'https://api.panda.telegames.ai', label: 'panda' },
  beta: { apiBase: 'https://api.beta.telegames.ai', label: 'beta — PRODUCTION', danger: true },
  local: { apiBase: 'http://localhost:3005', label: 'local' },
};

/**
 * Model file names the runtime treats as a calibrated character: telegamez web-next takes a
 * companion's tuning id from its model FILE name (`metaHumanSurface.ts` characterIdOf) and looks
 * it up in `HAIR_PROFILES` (`hairProfile.ts`). Any other name renders on the bake's own embedded
 * values, so a rename is invisible - except to one of these, which would quietly pick up another
 * character's hair, scalp and eye tuning. Refused as rename targets.
 */
const PROFILE_NAMES = ['telly', 'tele', 'telie', 'telee', 'tele1', 'tele2', 'tele4'];
const NAME_OK = /^[a-z0-9][a-z0-9._-]{1,62}$/;

/**
 * The shipping roster, from packages/shared/db/src/seed-metahuman-roster.ts.
 *
 * Used ONLY to decide what is pre-selected. Everything found is still listed and can still be
 * ticked by hand — the scan found `telly-research` sitting beside the six, which is a research
 * variant that must not reach a picker. Listing it and leaving it unticked is the useful
 * behaviour; hiding it would make the directory's contents unanswerable from the console, and
 * pre-ticking it would publish it on the first careless run.
 */
const ROSTER = [
  // The tg-* roster (telegamez#3725 rebakes, the greenfield packages), the ids the avatar records use.
  'tg-drake', 'tg-jan', 'tg-jen', 'tg-jules', 'tg-ken', 'tg-mae', 'tg-rick', 'tg-tom',
  // The pre-tg ids, still pre-ticked when their folders are found.
  'telly', 'tele', 'telie', 'telee', 'tele1', 'tele2',
];

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
    // `_compressed_ktx2` holds builds, and `_compressed` / `_compressed_full` are what is left of
    // the retired builds: none of them is a character folder.
    if (name.startsWith('_compressed')) continue;
    const dir = path.join(root, name);
    if (!fs.statSync(dir).isDirectory()) continue;

    const modelFile = modelFileOf(dir, name);
    if (!modelFile) continue; // not a character directory
    const glb = path.join(dir, modelFile);
    // Folder renamed by hand, model not: listed, so the operator can see it, and held back until
    // Rename brings the two in line - every script downstream addresses `<folder>/<folder>.glb`.
    const modelMismatch = modelFile !== `${name}.glb` ? modelFile : null;

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

    // A compressed build, if one has been made. Reported rather than assumed current: it is a
    // derivative of this directory, so a build that predates the content it was made from
    // would publish yesterday's companion while reporting success.
    //
    // Compared against the whole package, not just the GLB. The model file carries the mesh
    // and almost nothing a person would recognise: clothing colour and texture live in
    // mh_materials.json and textures/, and are applied over the model at runtime. A shirt can
    // change with the .glb untouched, and a GLB-only comparison would call that build current.
    const contentTime = [
      fs.statSync(glb).mtimeMs,
      ...(sidecar ? [fs.statSync(path.join(dir, 'mh_materials.json')).mtimeMs] : []),
      ...(fs.existsSync(texDir)
        ? fs.readdirSync(texDir).map((f) => fs.statSync(path.join(texDir, f)).mtimeMs)
        : []),
    ].reduce((a, b) => Math.max(a, b), 0);

    // The ktx2 build, made on Publish when it is missing or older than the package.
    const compressed = modelMismatch ? null : compressedInfo(root, name, contentTime);
    const bindings = readBinding(dir).environments;

    const problems = [];
    if (modelMismatch) problems.push(`model is ${modelMismatch}, folder is ${name} — Rename to bring them in line`);
    if (!sidecar) problems.push('no mh_materials.json — would render untextured');
    if (textures === 0) problems.push('no textures/');
    if (strayManifest) problems.push('stray manifest.json — publish would be refused');

    characters.push({
      id: name, dir, mb, sidecar, textures, strayManifest, previews, compressed, modelMismatch,
      bindings,
      // When the previews were captured, for the Previews panel (and to tell a recapture apart).
      previewsAt: previews
        ? Math.max(...PREVIEW_FILES.map((f) => fs.statSync(path.join(dir, f)).mtimeMs)) : null,
      // Pre-ticked when it is a roster id or already bound to an avatar somewhere: a renamed
      // package stays ticked because its binding came with it.
      ready: problems.length === 0, problems,
      inRoster: ROSTER.includes(name) || Object.keys(bindings).length > 0,
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

function recordPublish({ env, apiBase, char, avatarId, bakeSuffix, bakeHash, previous, output, code, reason = null, previews = false }) {
  const grab = (re) => { const m = output.match(re); return m ? m[1] : null; };
  const modelKey = grab(/modelKey:\s*(\S+)/);
  const files = grab(/OK - (\d+ files published)/);
  const size = grab(/\d+ files, ([\d.]+ MB)/);

  const out = [`## ${char}`, ''];
  out.push(`- when    ${new Date().toISOString()}`);
  out.push(`- avatar  \`${avatarId}\``);
  out.push(`- bake    \`${bakeHash}\``);
  if (files || size) out.push(`- upload  ${[files, size].filter(Boolean).join(', ')}`);
  // Which bytes went up. Always the ktx2 build now; still written, because older records in the
  // same folder say meshopt or uncompressed, and Remove old versions reads this line back.
  out.push(`- source  COMPRESSED ${BUILD} build (${BUILD_DIR}/)`);
  // telegamez#3662: a ktx2 publish also writes manifest.json routing the compositor to its own
  // PNG model. Recorded because rolling back the row does not remove it from the old prefix.
  const manifest = grab(/avatar manifest:\s*(\{.*\})/);
  if (manifest) out.push(`- manifest \`${manifest}\``);
  // telegamez#4632 phase 1b: what the registry recorded. `ready` means every object was read
  // back from the bucket and matched its digest. No line = an older platform, no version row.
  const state = grab(/\] version: \S+ state (\w+)/);
  if (state) out.push(`- version \`${state}\``);
  out.push(`- previews ${previews ? 'inside the version (character.json)' : 'none in the version; the avatar keeps its current ones'}`);
  if (reason) out.push(`- reason  ${reason}`);
  const wasPath = previous?.modelPath ?? null;
  const wasStatus = previous?.status ?? null;
  out.push(`- was     ${wasPath ? `\`${wasPath}\`` : '(no model on the row)'}${wasStatus ? `, status \`${wasStatus}\`` : ''}`);
  out.push('');

  if (code !== 0 || !modelKey) {
    // Worth a record precisely because it failed — this is the evidence that a character in a
    // batch was attempted and did not land, which the absence of a record would not show.
    out.push(`**FAILED** — exit ${code}. Nothing was published; the row is untouched.`, '');
  } else {
    // Through the API, never SQL: since telegamez#4632 phase 1b going live is a state change on
    // the version, and a hand-run UPDATE leaves the registry behind (db:characters:verify
    // reports it as pointer drift).
    out.push('```');
    out.push('-- go live');
    out.push(`PUT ${apiBase}/api/admin/avatars/${avatarId}/model-path  { "modelKey": "${modelKey}", "reason": "..." }`);
    out.push('');
    out.push('-- roll back');
    // `was` came from the admin list, which formats the key into a full URL; the API wants the key.
    const wasKey = wasPath ? keyOf(wasPath) : null;
    if (wasKey && /^avatars\/[0-9a-f-]{36}\/[A-Za-z0-9._-]+\/.+\.glb$/i.test(wasKey)) {
      out.push(`PUT ${apiBase}/api/admin/avatars/${avatarId}/model-path  { "modelKey": "${wasKey}", "reason": "..." }`);
    } else if (wasPath) {
      out.push(`-- the previous value is not a published key (${wasPath}); restoring it needs a re-publish of that bake`);
    } else {
      out.push('-- the row carried no model before this publish, so there is nothing to restore');
    }
    if (wasStatus && wasStatus !== 'active') out.push(`-- status was \`${wasStatus}\`; going live does not change it, set it in admin with a reason`);
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
function recordGoLive({ env, apiBase, char, avatarId, bakeSuffix, modelKey, previous, reason = null, state = null, registry = null, status = null }) {
  const out = [`## ${char} — live`, ''];
  out.push(`- when    ${new Date().toISOString()}`);
  out.push(`- avatar  \`${avatarId}\``);
  out.push(`- now     \`${modelKey}\``);
  out.push(`- was     ${previous ? `\`${previous}\`` : '(no model on the row)'}`);
  // telegamez#4632 phase 1b: the registry read back after the call. `live` is the proof; a
  // platform without the versions route (beta before its rc) is written down as such.
  if (registry === false) out.push('- registry no versions route on this platform yet');
  else out.push(`- version ${state ? `\`${state}\`` : 'not found in the registry'}`);
  if (status) out.push(`- status  \`${status}\`${status !== 'active' ? ' (going live does not change it; set it in admin with a reason)' : ''}`);
  if (reason) out.push(`- reason  ${reason}`);
  out.push('');
  // Only a published key can be restored through the API — the route refuses anything that is
  // not a content-addressed object under this avatar, which is what makes it safe. A seeded
  // placeholder is not one, so the FIRST time a character goes live there is nothing to go back
  // to through this door. That is a real limit, and saying so here is better than printing a
  // call that would be refused at the moment somebody needed it to work.
  const previousKey = previous ? keyOf(previous) : null;
  const restorable = previousKey && /^avatars\/[0-9a-f-]{36}\/[A-Za-z0-9._-]+\/.+\.glb$/i.test(previousKey);
  out.push('To put it back:');
  out.push('```');
  if (restorable) {
    out.push(`PUT ${apiBase}/api/admin/avatars/${avatarId}/model-path  { "modelKey": "${previousKey}", "reason": "..." }`);
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

/**
 * Every published version the journals know of in one environment: bake hash -> { avatarId,
 * build }. The CDN cannot be listed through the admin API, so this is how Remove old versions
 * finds what to remove: the `bake` line of each publish, plus the `was` / `now` keys of
 * publishes and go-lives, which is where versions published before these journals existed
 * (the seeded `…-20260904-8044` bakes) turn up.
 *
 * Only files whose header names this environment's API are read; a journal is plain markdown
 * and a hand-copied file must not leak another environment's versions in.
 */
function journalVersions(env, apiBase) {
  const versions = new Map();
  const deleted = new Set();
  const note = (hash, avatarId, build) => {
    const v = versions.get(hash) ?? { avatarId, build: null };
    if (build && !v.build) v.build = build;
    versions.set(hash, v);
  };
  let files = [];
  try { files = fs.readdirSync(JOURNAL_DIR).filter((f) => f.startsWith(`${env}-`) && f.endsWith('.md')); } catch { return versions; }
  for (const f of files) {
    let text;
    try { text = fs.readFileSync(path.join(JOURNAL_DIR, f), 'utf8'); } catch { continue; }
    if (!text.split('\n', 4).some((l) => l.trim() === apiBase)) continue;
    let avatarId = null;
    let lastHash = null;
    for (const line of text.split('\n')) {
      if (line.startsWith('## ')) { avatarId = null; lastHash = null; }
      const a = line.match(/^- avatar\s+`([0-9a-f-]{36})`/i);
      if (a) avatarId = a[1];
      const b = line.match(/^- bake\s+`([A-Za-z0-9._-]+)`/);
      if (b && avatarId) { lastHash = b[1]; note(lastHash, avatarId, null); }
      const s = line.match(/^- source\s+(?:COMPRESSED (\w+) build|(uncompressed) bake)/);
      if (s && lastHash) note(lastHash, avatarId, s[1] || s[2]);
      for (const k of line.matchAll(/avatars\/([0-9a-f-]{36})\/([A-Za-z0-9._-]+)\/[^\s`']+\.glb/gi)) note(k[2], k[1], null);
      // What Remove old versions already took, so it is not offered again.
      const d = line.match(/ deleted avatars\/[0-9a-f-]{36}\/([A-Za-z0-9._-]+)\/\. No undo\./i);
      if (d) deleted.add(d[1]);
    }
  }
  for (const h of deleted) versions.delete(h);
  return versions;
}

/** What a version holds: from its name when it says (`-ktx2-` / `-meshopt-`), else the journal. */
function buildOf(hash, versions) {
  const m = hash.match(/-(ktx2|meshopt)-\d{8}-/);
  if (m) return m[1];
  return versions.get(hash)?.build ?? 'not recorded';
}

/** The bake hash a model path points into, whether stored relative or returned as a full URL. */
const bakeOfPath = (p) => (String(p || '').match(/avatars\/[0-9a-f-]{36}\/([A-Za-z0-9._-]+)\/[^/]+\.glb/i) || [])[1] ?? null;

/** The object key behind a model path: the admin list formats keys into full URLs on the way out,
 * and the API accepts only the key. */
const keyOf = (p) => String(p || '').replace(/^https?:\/\/[^/]+\//, '').replace(/^\/+/, '');

/** A reason for the character history (telegamez#4632 phase 1b): required, trimmed, bounded. */
const reasonOf = (r) => { const s = String(r ?? '').trim(); return s && s.length <= 500 ? s : null; };

/**
 * The registry's view of one avatar (telegamez#4632 phase 1b): every version with its state, and
 * the state of one version in particular. `registry: false` where the platform has no versions
 * route yet (beta before the rc that carries 1b), so callers fall back to the journals and say so.
 */
async function registryOf(target, avatarId, version = null) {
  const r = await adminCall(target, 'GET', `/api/admin/avatars/${avatarId}/versions`);
  if (r.status === 404) return { registry: false, versions: [], state: null };
  if (!r.ok) return { registry: true, versions: [], state: null, problem: `versions ${r.status}${r.error ? ` ${r.error}` : ''}` };
  const versions = Array.isArray(r.body?.versions) ? r.body.versions : [];
  return { registry: true, versions, state: version ? versions.find((v) => v.version === version)?.state ?? null : null };
}

/** Local renames, appended to one file - the history of what each folder used to be called. */
function recordRename(lines) {
  try {
    fs.mkdirSync(path.dirname(JOURNAL_DIR), { recursive: true });
    fs.appendFileSync(path.join(path.dirname(JOURNAL_DIR), 'renames.md'), lines.join('\n') + '\n\n');
  } catch { /* the rename itself already happened */ }
}

/** Rewrite a JSON file in place without changing its timestamps - so a build stays current. */
function rewriteJson(file, edit) {
  if (!fs.existsSync(file)) return;
  const st = fs.statSync(file);
  const j = JSON.parse(fs.readFileSync(file, 'utf8'));
  edit(j);
  fs.writeFileSync(file, JSON.stringify(j, null, 2) + '\n');
  fs.utimesSync(file, st.atime, st.mtime);
}

function stream(res, file, args, extraEnv = {}, onClose) {
  if (!res.headersSent) res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
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

// ---------------------------------------------------------------------------------------
// Previews (telegamez#3662, telegamez#4632): captured here and sent inside the version by
// Publish. A character folder is finished when its
// thumbnail.png, imagePreview.jpg and videoPreview.mp4 sit beside its model; the capture is
// the monorepo's own scripts/media/capture-companion-previews.mjs, which photographs the
// character through the real web app's /dev/companion-capture page, so the frames come from
// the renderer, material profile and animation the room uses.

const PREVIEW_FILES = ['thumbnail.png', 'imagePreview.jpg', 'videoPreview.mp4'];
/** The local web app the capture drives. Started by this console when nothing answers there. */
const STACK = arg('stack', 'http://localhost:3000');
/** The room-settings avatar picker's background; see the bake console for how it was chosen. */
const PREVIEW_BG = arg('bg', '18385d');

/**
 * The telegamez checkout, for the capture script and the web app it drives. `--monorepo` wins;
 * otherwise a remembered answer, then the sibling of this repo (S:\Avatars\metahuman\telegamez
 * on the bake box). A bundled publisher on a machine without the monorepo cannot capture, and
 * says so instead of publishing nothing.
 */
const guessMonorepo = () => [
  arg('monorepo', null),
  readSetting('monorepo'),
  path.resolve(HERE, '..', '..', '..', '..', '..', 'telegamez'),
].filter(Boolean).find((d) => fs.existsSync(path.join(d, 'scripts', 'media', 'capture-companion-previews.mjs')));

const hasPreviews = (dir) => PREVIEW_FILES.every((f) => fs.existsSync(path.join(dir, f)));

async function stackUp(timeoutMs = 2500) {
  try {
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), timeoutMs);
    const r = await fetch(`${STACK}/dev/companion-capture`, { signal: c.signal });
    clearTimeout(t);
    return r.ok;
  } catch { return false; }
}

/**
 * The web app this console started, if any. Kept for the console's lifetime so a batch of
 * characters pays the start-up once, and stopped when the console exits.
 *
 * `next dev` without `--turbopack`: Turbopack does not apply web-next's `.js` -> `.ts`
 * extension alias and resolves workspace packages to their built `dist/`, and the dev pages
 * 500 on it.
 */
let stackChild = null;
let stackTail = '';

function stopStack() {
  if (!stackChild) return;
  try {
    if (process.platform === 'win32') {
      spawnSync('taskkill', ['/pid', String(stackChild.pid), '/T', '/F'], { windowsHide: true });
    } else {
      stackChild.kill('SIGTERM');
    }
  } catch { /* already gone */ }
  stackChild = null;
}
process.on('exit', stopStack);
for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => { stopStack(); process.exit(0); });

async function ensureStack(res, monorepo) {
  if (await stackUp()) return true;
  if (!stackChild) {
    const port = new URL(STACK).port || '3000';
    res.write(`\n=== Starting the local web app at ${STACK} for preview capture (first start takes a minute or two)\n`);
    stackTail = '';
    stackChild = spawn('npx', ['next', 'dev', '-p', port], {
      cwd: path.join(monorepo, 'packages', 'web-next'), shell: true, windowsHide: true,
    });
    const keep = (d) => { stackTail = (stackTail + d.toString()).slice(-4000); };
    stackChild.stdout.on('data', keep);
    stackChild.stderr.on('data', keep);
    stackChild.on('exit', () => { stackChild = null; });
  }
  // The first request compiles the capture page, which takes longer than a probe's timeout.
  const deadline = Date.now() + 6 * 60_000;
  while (Date.now() < deadline) {
    if (await stackUp(60_000)) { res.write('\n[console] local web app is up\n'); return true; }
    if (!stackChild) break;
    res.write('.');
    await new Promise((r) => setTimeout(r, 3000));
  }
  res.write(`\n[console] the local web app did not come up at ${STACK}. Last output:\n${stackTail}\n`);
  return false;
}

function runCapture(res, monorepo, dir) {
  return new Promise((resolve) => {
    res.write(`\n=== Capture previews — ${path.basename(dir)}\n`);
    const child = spawn('node', [
      path.join(monorepo, 'scripts', 'media', 'capture-companion-previews.mjs'),
      '--dir', dir, '--base', STACK, '--bg', PREVIEW_BG,
    ], { cwd: monorepo, windowsHide: true });
    child.stdout.on('data', (d) => res.write(d));
    child.stderr.on('data', (d) => res.write(d));
    child.on('error', (e) => { res.write(`\n[console] could not start the capture: ${e.message}\n`); resolve(-1); });
    child.on('close', (code) => resolve(code));
  });
}

// ---------------------------------------------------------------------------------------
// Clean up old avatars (telegamez#3662).
//
// Retiring an avatar is two changes through the admin API, in this order:
//   1. every companion and talk-show slot using it is pointed at a replacement avatar, so
//      nothing a user sees loses its look (the companion keeps its name, voice and prompts);
//   2. the avatar is archived (status 'archived', isActive false, with a reason in its history):
//      hidden from pickers, files kept, reversible by putting the status back.
// There is no delete-for-good any more (telegamez#4648): the row and its history are permanent,
// and old versions go through Remove old versions, one at a time through the lifecycle.
// The page shows the full plan as a dry run first; production needs its name typed. Every
// change is written to the journal with how to undo it.

/** The pre-tg MetaHuman ids, retired 2026-10-05 (metahuman-to-glb #27). Pre-ticked, nothing more. */
const RETIRED_SLUGS = ['mh-tele', 'mh-telly', 'mh-telie', 'mh-telee', 'mh-tele1', 'mh-tele2'];

/** The admin API wraps some responses as { success, data } and returns others bare. */
const unwrapApi = (b) => (b && typeof b === 'object' && b.data && typeof b.data === 'object' ? b.data : b);

async function adminCall(target, method, apiPath, json) {
  const r = await fetch(`${target.apiBase}${apiPath}`, {
    method,
    headers: {
      Authorization: `Bearer ${session.token}`,
      ...(json === undefined ? {} : { 'content-type': 'application/json' }),
    },
    ...(json === undefined ? {} : { body: JSON.stringify(json) }),
  });
  const body = await r.json().catch(() => ({}));
  const err = body?.error ?? body?.message ?? null;
  const code = typeof body?.code === 'string' ? ` [${body.code}]` : '';
  return {
    ok: r.ok, status: r.status, body: unwrapApi(body),
    error: err === null ? null : (typeof err === 'string' ? err : JSON.stringify(err)) + code,
  };
}

async function adminGet(target, apiPath) {
  const r = await adminCall(target, 'GET', apiPath);
  if (!r.ok) throw new Error(`GET ${apiPath}: ${r.status}${r.error ? ` ${r.error}` : ''}`);
  return r.body;
}

async function adminGetAll(target, apiPath, key) {
  const out = [];
  for (let page = 1; page < 100; page++) {
    const sep = apiPath.includes('?') ? '&' : '?';
    const d = await adminGet(target, `${apiPath}${sep}limit=100&page=${page}`);
    const items = Array.isArray(d?.[key]) ? d[key] : [];
    out.push(...items);
    const pages = Number(d?.pagination?.totalPages ?? 1);
    if (items.length === 0 || page >= pages) break;
  }
  return out;
}

/**
 * avatarId -> package folder name, for every package under `root` bound in `env`. What the
 * cleanup table calls "your packages": the avatars this folder publishes to, whatever their
 * slugs or the folders' names happen to be.
 */
function boundAvatars(root, env) {
  const out = new Map();
  if (!root || !fs.existsSync(root)) return out;
  for (const name of fs.readdirSync(root)) {
    if (name.startsWith('_')) continue;
    const id = readBinding(path.join(root, name)).environments[env]?.avatarId;
    if (id) out.set(id, name);
  }
  return out;
}

/** Every avatar with what uses it. Read fresh for the page and again before applying. */
async function cleanupScan(target, bound = new Map()) {
  const avatars = await adminGetAll(target, '/api/admin/avatars', 'avatars');
  const companions = await adminGetAll(target, '/api/admin/companions?includeInactive=true', 'companions');
  const shows = (await adminGet(target, '/api/admin/talk-shows'))?.talkShows ?? [];
  const slots = [];
  for (const show of shows) {
    const d = await adminGet(target, `/api/admin/talk-shows/${show.id}/companions`);
    for (const c of d?.companions ?? []) {
      slots.push({
        showId: show.id, showName: show.name || show.title || show.slug || show.id,
        slotId: c.id, label: c.displayName || c.role || c.id, avatarId: c.avatarId ?? null,
      });
    }
  }
  return avatars.map((a) => ({
    id: a.id, slug: a.slug, name: a.name, status: a.status ?? null, isActive: Boolean(a.isActive),
    modelPath: a.modelPath ?? null,
    // Never pre-ticked, and the dry run says so if it is ticked: this is a current character.
    package: bound.get(a.id) ?? null,
    oldId: RETIRED_SLUGS.includes(a.slug) && !bound.has(a.id),
    companions: companions.filter((c) => c.avatarId === a.id).map((c) => ({ id: c.id, name: c.name, slug: c.slug })),
    talkShowSlots: slots.filter((s) => s.avatarId === a.id),
  }));
}

function recordCleanup(env, apiBase, lines) {
  try {
    fs.mkdirSync(JOURNAL_DIR, { recursive: true });
    // Milliseconds and a counter: two runs in one second (a refusal, then the real run) must
    // not share a file, or the second record overwrites the first.
    const stamp = new Date().toISOString().replace(/[-:.]/g, '').slice(0, 18);
    let file = path.join(JOURNAL_DIR, `${env}-cleanup-${stamp}.md`);
    for (let n = 2; fs.existsSync(file); n++) file = path.join(JOURNAL_DIR, `${env}-cleanup-${stamp}-${n}.md`);
    fs.writeFileSync(file, [`# Avatar cleanup on ${env}`, '', apiBase, '', ...lines, ''].join('\n'));
    return file;
  } catch { return null; }
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
      const root = url.searchParams.get('root') || DEFAULT_ROOT;

      let rows;
      try {
        rows = await adminGetAll(target, '/api/admin/avatars', 'avatars');
      } catch (e) {
        return send(res, 502, { error: `${e.message} — is the cookie for ${env}?` });
      }
      const versions = journalVersions(env, target.apiBase);

      const resolved = (characters || []).map((char) => {
        // The binding first: it is the avatar's id, and survives any rename. The slug only for a
        // package never bound here - and a slug match is written down as the binding, so the
        // NEXT rename is safe too. A binding to an avatar the environment no longer has is
        // reported and stops there; falling back to the slug would publish over whatever
        // happens to carry the name now.
        const dir = path.join(root, char);
        const bound = readBinding(dir).environments[env]?.avatarId ?? null;
        let row = null;
        let via = null;
        let bindingProblem = null;
        if (bound) {
          row = rows.find((x) => x.id === bound) ?? null;
          via = 'binding';
          if (!row) bindingProblem = `bound to avatar ${bound}, which ${env} does not have`;
        } else {
          row = rows.find((x) => x.slug === `mh-${char}`) ?? null;
          if (row && fs.existsSync(dir)) {
            via = 'slug';
            try { writeBinding(dir, env, target.apiBase, row.id, `matched slug ${row.slug}`); via = 'slug, now bound'; } catch { /* read-only folder: still resolved */ }
          }
        }
        const liveBake = bakeOfPath(row?.modelPath);
        return {
          char, slug: row ? row.slug : `mh-${char}`,
          via, bindingProblem,
          // What the row serves now, by name: the build is in every bake hash published since
          // #39, and the journal's source line says it for the ones before.
          liveBake, liveBuild: liveBake ? buildOf(liveBake, versions) : null,
          avatarId: row ? row.id : null,
          name: row ? row.name : null,
          active: row ? Boolean(row.isActive) : null,
          modelPath: row ? row.modelPath : null,
          // A new row is created with status 'processing', and the admin companion form only
          // offers avatars matching isActive=true AND status=active. Left alone, a freshly
          // published character is simply absent from that dropdown with nothing to explain
          // why — so the status is carried here and the go-live SQL sets it.
          status: row ? (row.status ?? null) : null,
          // Whether the three preview columns hold real uploaded assets (telegamez#3662: Go live
          // waits for them). A row created through the admin API gets placeholder URLs pointing at
          // objects that do not exist, so presence alone says nothing. Same rule as the bake
          // console.
          // A preview that lives inside a published version (`avatars/<id>/<bake>/thumbnail.png`,
          // telegamez#4632 phase 1b) is real whatever its file name; the placeholder test is only
          // for the flat legacy slots.
          previewsPublished: Boolean(
            row && ['thumbnailUrl', 'imagePreviewUrl', 'videoPreviewUrl']
              .every((f) => typeof row[f] === 'string' && (
                /avatars\/[0-9a-f-]{36}\/[A-Za-z0-9._-]{6,64}\/[^/]+$/i.test(row[f])
                || (row[f].includes('/avatars/') && !/\/(thumbnail|preview)\.(png|mp4|jpg)$/i.test(row[f])))),
          ),
          // Not anchored, deliberately. The column stores the relative key that was written
          // to it, but the admin LIST endpoint formats it into a full CDN URL on the way out
          // — so a row that is genuinely live comes back as
          // `https://<bucket>.s3.../avatars/<id>/<hash>/<file>.glb`. Anchoring at `^avatars/`
          // reported every live avatar as unpublished, which showed up as every row on the
          // page reading "Add its model" — the label for a character that has no files at all.
          // Searching for the published segment anywhere still excludes the seeded
          // placeholder, which is `models/<id>/<id>.glb` in either form.
          published: /avatars\/[0-9a-f-]{36}\/[A-Za-z0-9._-]+\/[^/]+\.glb$/i.test(row?.modelPath || ''),
        };
      });
      // What each row points at *now*, kept for the journal. A publish overwrites this value,
      // and after that it exists nowhere else — so it is captured here, before it can be lost,
      // rather than asked for at rollback time when the answer is already gone.
      for (const row of resolved) {
        session.prior[`${env}/${row.char}`] = { modelPath: row.modelPath || null, status: row.status || null };
      }

      // For a package with neither a binding nor a matching slug: the page offers "update an
      // existing avatar" from this list instead of creating a second one. Name-free by design.
      const avatars = rows
        .filter((x) => x.status !== 'archived')
        .map((x) => ({ id: x.id, slug: x.slug, name: x.name }))
        .sort((p, q) => String(p.name).localeCompare(String(q.name)));
      return send(res, 200, { environment: env, resolved, avatars });
    }

    /**
     * Bind a package to an avatar that already exists here - "update existing" on the page, for a
     * package whose name matches nothing (a re-bake under a new id, a folder renamed elsewhere).
     * Checked against the environment so a typo cannot bind to nothing.
     */
    if (url.pathname === '/api/bind' && req.method === 'POST') {
      const { env, char, avatarId } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      const dir = path.join(url.searchParams.get('root') || DEFAULT_ROOT, String(char || ''));
      if (!/^[A-Za-z0-9._-]+$/.test(String(char || '')) || !fs.existsSync(dir)) return send(res, 400, { error: `no package ${char}` });
      const r = await adminCall(target, 'GET', `/api/admin/avatars/${encodeURIComponent(String(avatarId || ''))}`);
      const a = r.body?.avatar ?? r.body;
      if (!r.ok || a?.id !== avatarId) return send(res, 400, { error: `avatar ${avatarId} not found on ${env}` });
      writeBinding(dir, env, target.apiBase, avatarId, `chosen on the page (${a.slug})`);
      return send(res, 200, { char, avatarId, slug: a.slug });
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

      // 'human', because the database says so. chk_avatar_category allows exactly
      // human | fantasy | robot | animal | abstract | other, and 'metahuman' is not one of
      // them - it violated the constraint on every attempt. The metahuman marker belongs in
      // tags, which carries no constraint and is what the six existing rows are found by.
      const payload = {
        name,
        slug: `mh-${char}`,
        category: category || 'human',
        gender: gender || undefined,
        description: description || undefined,
        tags: ['metahuman'],
      };

      const r = await fetch(`${target.apiBase}/api/admin/avatars`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${session.token}`, 'content-type': 'application/json' },
        body: JSON.stringify(payload),
      });

      // Read as text first: a failure that is not JSON still has something to say, and
      // .json() would swallow it.
      const raw = await r.text();
      let out = {};
      try { out = raw ? JSON.parse(raw) : {}; } catch { /* not JSON - the text is the message */ }

      if (!r.ok) {
        // The admin API special-cases a duplicate slug and collapses everything else into one
        // sentence, with the reason left in its own log. "Failed to create avatar" alone
        // cannot be told apart from a rejected field, a constraint violation or a database
        // that is down - so carry the status and the payload that caused it.
        const said = out.error || out.message || raw.slice(0, 300) || '(empty body)';
        return send(res, 502, { error: `admin API ${r.status}: ${said}`, sent: payload });
      }
      const avatarId = out.id || out.avatar?.id || out.data?.id || null;
      // Bound at birth, so this package keeps finding its avatar whatever it is renamed to.
      const dir = path.join(url.searchParams.get('root') || DEFAULT_ROOT, char);
      if (avatarId && fs.existsSync(dir)) {
        try { writeBinding(dir, env, target.apiBase, avatarId, `created as ${payload.slug}`); } catch { /* resolve binds it by slug */ }
      }
      return send(res, 200, { avatarId, raw: out });
    }

    if (url.pathname === '/api/verify' && req.method === 'POST') {
      const { env, slugs } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      // The avatars this page published, by the slugs they carry here. Without -Slugs the
      // verifier checks its built-in list - the retired mh-tele* six - and passes on avatars
      // nobody touched.
      const list = (Array.isArray(slugs) ? slugs : []).filter((s) => /^[A-Za-z0-9._-]+$/.test(s));
      const args = ['-ApiBase', target.apiBase];
      if (list.length) args.push('-Slugs', list.join(','));
      return stream(res, VERIFIER, args, { TG_ADMIN_TOKEN: session.token });
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
      // ktx2 is the only build (owner decision 2026-10-08). A page still asking for another is
      // refused rather than quietly given ktx2, so the mismatch is seen.
      if (mode && mode !== BUILD) {
        return send(res, 400, { error: `only the ${BUILD} build is made here (asked for ${mode})` });
      }
      // One per request, like publish: the page drives the loop so a failure stops the batch
      // at a character anyone can name.
      const char = characters[0];
      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      const args = ['-Char', char, '-Root', root, '-Mode', BUILD];
      if (Number(texturePx) > 0) args.push('-TexturePx', String(Number(texturePx)));
      return stream(res, COMPRESSOR, args);
    }

    if (url.pathname === '/api/publish' && req.method === 'POST') {
      const { env, characters, bakeHashSuffix, avatarIds, build: requested, reason } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      if (!Array.isArray(characters) || characters.length === 0) {
        return send(res, 400, { error: 'no characters selected' });
      }
      // ktx2 is the only build. A stale page that still sends `build: 'meshopt'` is refused.
      if (requested && requested !== BUILD) {
        return send(res, 400, { error: `only the ${BUILD} build is published (asked for ${requested})` });
      }
      if (!/^[A-Za-z0-9._-]+$/.test(bakeHashSuffix || '')) {
        return send(res, 400, { error: 'bake hash suffix must be [A-Za-z0-9._-]' });
      }
      // telegamez#4632 phase 1b: every version carries why it was published.
      const why = reasonOf(reason);
      if (!why) return send(res, 400, { error: 'a reason is required: it goes into the character history with the version' });
      // One character per request keeps the stream readable and lets a failure stop the batch
      // at a known point; the page drives the loop.
      const char = characters[0];
      const avatarId = (avatarIds || {})[char];
      if (!avatarId) return send(res, 400, { error: `no avatarId for ${char}` });

      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      const pkgDir = path.join(root, char);
      if (!fs.existsSync(path.join(pkgDir, `${char}.glb`))) {
        return send(res, 400, { error: `no package for ${char} in ${root}` });
      }

      // What goes up is the ktx2 build, always. A missing build, a stale one or one that does not
      // name this character's files is remade here first, so a publish is one action.
      let made = compressedInfo(root, char, contentTimeOf(pkgDir, char));
      if (!made || !made.current) {
        if (!fs.existsSync(COMPRESSOR)) {
          return send(res, 400, {
            error: `compress_companion.ps1 is not beside this publisher and ${char} has no current ${BUILD} build`,
          });
        }
        res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
        const why = !made ? `no ${BUILD} build yet`
          : made.stale ? `${BUILD} build is older than the package`
            : `${BUILD} build is mode="${made.mode}" or does not name ${char}'s two models`;
        const code = await runInto(res, `Compress — ${char} (${why})`, COMPRESSOR,
          ['-Char', char, '-Root', root, '-Mode', BUILD]);
        made = code === 0 ? compressedInfo(root, char, contentTimeOf(pkgDir, char)) : null;
        if (!made || !made.current) {
          res.write(`\n[console] compress failed (exit ${code}) — nothing was uploaded\n[console] exit 1\n`);
          res.end();
          return undefined;
        }
      }

      // The build is part of the bake hash, so the CDN path, the go-live SQL and the journal all
      // name it: `avatars/<id>/ktx2-drake-ktx2-20261008-6670/ktx2-drake.glb`. Which avatar it
      // lands on is the binding's business, not the name's.
      const bakeHash = `${char}-${BUILD}-${bakeHashSuffix}`;
      // telegamez#4632 phase 1b: the previews captured for this package ride inside the version,
      // named by the character.json the script generates, so they go live, roll back and get
      // removed with the model. No captured previews: the version carries none and the avatar
      // keeps whatever it has (the platform's compatibility rule), which the page reports.
      const previews = hasPreviews(pkgDir);
      return stream(res, PUBLISHER, [
        '-Char', char, '-AvatarId', avatarId, '-ApiBase', target.apiBase,
        '-Source', made.dir,
        '-BakeHash', bakeHash,
        '-Build', BUILD,
        '-Reason', why,
        ...(previews ? ['-PreviewsFrom', pkgDir] : []),
      ], { TG_ADMIN_TOKEN: session.token }, (output, code) => recordPublish({
        env, apiBase: target.apiBase, char, avatarId,
        bakeSuffix: bakeHashSuffix, bakeHash, reason: why, previews,
        previous: session.prior[`${env}/${char}`] ?? null,
        output, code,
      }));
    }

    /**
     * Rename a package: folder, model files, its ktx2 build, and the name the sidecar carries.
     * Local only - no environment is touched, because the binding inside the folder is what ties
     * it to its avatars, and it moves with the folder.
     *
     * `from` is the folder as it is now. It also repairs a folder renamed by hand (`to` = the
     * folder, its model still under the old name). File times are kept, so a current ktx2 build
     * stays current and Publish does not remake it.
     */
    if (url.pathname === '/api/rename' && req.method === 'POST') {
      const { from, to } = await readBody(req);
      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      if (!/^[A-Za-z0-9._-]+$/.test(String(from || '')) || from.startsWith('_')) return send(res, 400, { error: `bad package name ${from}` });
      if (!NAME_OK.test(String(to || ''))) {
        return send(res, 400, { error: `"${to}": use lowercase letters, digits, dot, dash or underscore, 2-63 long, starting with a letter or digit` });
      }
      if (PROFILE_NAMES.includes(to)) {
        return send(res, 400, { error: `"${to}" is a calibrated character id in the runtime (HAIR_PROFILES): a model with this name would take that character's hair, scalp and eye tuning` });
      }
      const fromDir = path.join(root, from);
      const toDir = path.join(root, to);
      const model = modelFileOf(fromDir, from);
      if (!model) return send(res, 400, { error: `no package ${from} in ${root}` });
      const stem = model.replace(/\.glb$/i, '');
      if (from === to && stem === to) return send(res, 400, { error: `${from} is already called that` });
      if (from !== to && fs.existsSync(toDir)) return send(res, 400, { error: `${to} already exists in ${root}` });

      // The build may sit under the old folder name or, after a hand rename, the model's.
      const buildFrom = [compressedPath(root, from), compressedPath(root, stem)]
        .find((d) => fs.existsSync(path.join(d, `${stem}.glb`))) ?? null;
      const buildTo = compressedPath(root, to);
      if (buildFrom && buildFrom !== buildTo && fs.existsSync(buildTo)) {
        return send(res, 400, { error: `${path.relative(root, buildTo)} already exists` });
      }

      const done = [];
      try {
        if (from !== to) { fs.renameSync(fromDir, toDir); done.push(`folder ${from} -> ${to}`); }
        if (stem !== to) { fs.renameSync(path.join(toDir, model), path.join(toDir, `${to}.glb`)); done.push(`model ${model} -> ${to}.glb`); }
        // `character` must stay a string: web-next's visitor room lists a companion only when it
        // is one. Its value drives nothing else, and neither does hair_tuning_source.character_id.
        rewriteJson(path.join(toDir, 'mh_materials.json'), (j) => {
          j.character = to;
          if (j.hair_tuning_source && typeof j.hair_tuning_source === 'object') j.hair_tuning_source.character_id = to;
        });
        if (buildFrom) {
          if (buildFrom !== buildTo) fs.renameSync(buildFrom, buildTo);
          for (const [a, b] of [[`${stem}.glb`, `${to}.glb`], [`${stem}.compositor.glb`, `${to}.compositor.glb`]]) {
            if (a !== b && fs.existsSync(path.join(buildTo, a))) fs.renameSync(path.join(buildTo, a), path.join(buildTo, b));
          }
          rewriteJson(path.join(buildTo, 'compression_manifest.json'), (j) => {
            j.companion = to;
            j.source = toDir;
            if (j.variants) j.variants = { ...j.variants, room: `${to}.glb`, compositor: `${to}.compositor.glb` };
          });
          rewriteJson(path.join(buildTo, 'mh_materials.json'), (j) => {
            j.character = to;
            if (j.hair_tuning_source && typeof j.hair_tuning_source === 'object') j.hair_tuning_source.character_id = to;
          });
          done.push(`ktx2 build -> ${path.relative(root, buildTo)}`);
        }
      } catch (e) {
        recordRename([`## ${from} -> ${to} — STOPPED`, '', `- when    ${new Date().toISOString()}`, `- root    ${root}`,
          ...done.map((d) => `- done    ${d}`), `- failed  ${e.message}`]);
        return send(res, 500, { error: `rename stopped after ${done.length} step(s): ${e.message}`, done });
      }
      const bindings = readBinding(toDir).environments;
      recordRename([`## ${from} -> ${to}`, '', `- when    ${new Date().toISOString()}`, `- root    ${root}`,
        ...done.map((d) => `- done    ${d}`),
        `- bound   ${Object.entries(bindings).map(([e, b]) => `${e}=${b.avatarId}`).join(', ') || '(nowhere yet)'}`]);
      return send(res, 200, { from, to, done, boundIn: Object.keys(bindings) });
    }

    /**
     * Make each ticked package's avatar slug read `mh-<folder>` here - so a renamed package shows
     * its new name in the admin pages too. Cosmetic: the binding, not the slug, is what the
     * publisher follows, and the runtime reads neither. The avatar's id, display name, model and
     * every companion on it are untouched. Journaled with the undo.
     */
    if (url.pathname === '/api/sync-slugs' && req.method === 'POST') {
      const { env, characters, confirm } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      if (!Array.isArray(characters) || !characters.length) return send(res, 400, { error: 'no characters selected' });
      if (target.danger && confirm !== env) return send(res, 400, { error: `${env} is production: type "${env}" to confirm` });
      const root = url.searchParams.get('root') || DEFAULT_ROOT;

      res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
      const journal = [];
      let code = 0;
      for (const char of characters) {
        const avatarId = readBinding(path.join(root, String(char))).environments[env]?.avatarId;
        if (!avatarId) { res.write(`${char}: not bound on ${env} — Connect once so it is matched, then retry\n`); code = 1; continue; }
        const want = `mh-${char}`;
        const cur = await adminCall(target, 'GET', `/api/admin/avatars/${avatarId}`);
        const was = (cur.body?.avatar ?? cur.body)?.slug;
        if (!cur.ok || !was) { res.write(`${char}: could not read avatar ${avatarId} (${cur.status}) — stopped\n`); code = 1; break; }
        if (was === want) { res.write(`${char}: already ${want}\n`); continue; }
        const r = await adminCall(target, 'PUT', `/api/admin/avatars/${avatarId}`, { slug: want });
        const back = r.ok ? await adminCall(target, 'GET', `/api/admin/avatars/${avatarId}`) : null;
        const now = (back?.body?.avatar ?? back?.body)?.slug;
        if (!r.ok || now !== want) {
          res.write(`${char}: slug change failed (${r.status}${r.error ? ` ${r.error}` : ''}${r.ok ? `, reads back ${now}` : ''}) — stopped\n`);
          journal.push(`## ${char}`, '', `- avatar \`${avatarId}\`: slug ${was} -> ${want} FAILED (HTTP ${r.status}${r.error ? ` ${r.error}` : ''})`, '');
          code = 1; break;
        }
        res.write(`${char}: ${was} -> ${want}\n`);
        journal.push(`## ${char}`, '', `- avatar \`${avatarId}\`: slug ${was} -> ${want}. Undo: PUT /api/admin/avatars/${avatarId} {"slug":"${was}"}`, '');
      }
      const recorded = journal.length ? recordCleanup(env, target.apiBase, ['Slug sync', '', ...journal]) : null;
      if (recorded) res.write(`\n[console] recorded: ${recorded}\n`);
      res.write(`\n[console] exit ${code}\n`);
      res.end();
      return undefined;
    }

    /**
     * Old versions of the ticked packages' avatars: every published bake the journals know of,
     * except the one the avatar is live on. Read-only; the page shows it as a list to tick.
     */
    if (url.pathname === '/api/versions/scan' && req.method === 'POST') {
      const { env, characters } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      const versions = journalVersions(env, target.apiBase);
      const out = [];
      let registry = null;
      for (const char of characters || []) {
        const avatarId = readBinding(path.join(root, String(char))).environments[env]?.avatarId;
        if (!avatarId) { out.push({ char, problem: `not bound on ${env} — Connect once first` }); continue; }
        const r = await adminCall(target, 'GET', `/api/admin/avatars/${avatarId}`);
        const a = r.body?.avatar ?? r.body;
        if (!r.ok || !a?.id) { out.push({ char, avatarId, problem: `could not read the avatar (${r.status})` }); continue; }
        const live = bakeOfPath(a.modelPath);
        // telegamez#4632 phase 1b: the registry is the authority where it exists, with a state per
        // version. Versions it does not know yet (published before the backfill) are still offered
        // from the journals, marked `unregistered`; `deleted` ones are not offered again. Without
        // the route (beta before its rc) the journals are all there is, as before.
        const reg = await registryOf(target, avatarId, live);
        if (registry === null) registry = reg.registry;
        const known = new Map(reg.versions.map((v) => [v.version, v]));
        const old = [];
        for (const v of reg.versions) {
          if (v.version === live || v.state === 'live' || v.state === 'deleted') continue;
          // `modelKey` rides along so a `ready` version can be taken live from the page without a
          // re-publish (a page refresh otherwise forgets what was published this session).
          old.push({ bakeHash: v.version, build: v.build && v.build !== 'unknown' ? v.build : buildOf(v.version, versions), state: v.state, modelKey: v.modelKey ?? null });
        }
        for (const [hash, v] of versions.entries()) {
          if (v.avatarId !== avatarId || hash === live || known.has(hash)) continue;
          old.push({ bakeHash: hash, build: buildOf(hash, versions), state: reg.registry ? 'unregistered' : null });
        }
        old.sort((p, q) => p.bakeHash.localeCompare(q.bakeHash));
        out.push({ char, avatarId, slug: a.slug, live, liveBuild: live ? buildOf(live, versions) : null, liveState: reg.state, old });
      }
      return send(res, 200, { environment: env, registry: registry ?? false, packages: out });
    }

    /**
     * Delete old versions: [{ avatarId, bakeHash }]. One `DELETE companion-directory?bakeHash=`
     * each - the route that removes exactly one bake and refuses the one a row points at
     * (PREFIX_IS_LIVE), so the live version cannot go even if the page were wrong. Re-checked
     * against the avatar first anyway. No undo: these are deleted from the bucket. Journaled.
     */
    if (url.pathname === '/api/versions/remove' && req.method === 'POST') {
      const { env, items, confirm, reason } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      if (!Array.isArray(items) || !items.length) return send(res, 400, { error: 'nothing selected' });
      if (target.danger && confirm !== env) return send(res, 400, { error: `${env} is production: type "${env}" to confirm` });
      // telegamez#4632 phase 1b: a deletion is a state change with a reason in the history.
      const why = reasonOf(reason);
      if (!why) return send(res, 400, { error: 'a reason is required: it goes into the character history' });

      res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
      const journal = ['Remove old versions', '', `- reason ${why}`, ''];
      const liveOf = new Map();
      let code = 0;
      for (const { avatarId, bakeHash } of items) {
        if (!/^[0-9a-f-]{36}$/i.test(String(avatarId)) || !/^[A-Za-z0-9._-]{6,64}$/.test(String(bakeHash))) {
          res.write(`skipped a malformed item\n`); continue;
        }
        if (!liveOf.has(avatarId)) {
          const r = await adminCall(target, 'GET', `/api/admin/avatars/${avatarId}`);
          if (!r.ok) { res.write(`${avatarId}: could not read the avatar (${r.status}) — stopped\n`); code = 1; break; }
          liveOf.set(avatarId, bakeOfPath((r.body?.avatar ?? r.body)?.modelPath));
        }
        if (liveOf.get(avatarId) === bakeHash) { res.write(`${bakeHash}: is live — kept\n`); continue; }
        const r = await adminCall(target, 'DELETE', `/api/admin/avatars/${avatarId}/companion-directory?bakeHash=${encodeURIComponent(bakeHash)}&reason=${encodeURIComponent(why)}`);
        if (!r.ok) {
          // Since telegamez#4645 the platform deletes only through the registry: a bake it does not
          // know (listed here from the journals as `unregistered`) is a 404, never a prefix delete.
          const hint = r.status === 404 ? ' — not in the platform\'s registry; its backfill registers it, then scan again' : '';
          res.write(`${bakeHash}: delete failed (${r.status}${r.error ? ` ${r.error}` : ''})${hint} — stopped\n`);
          journal.push(`- \`${avatarId}\` ${bakeHash}: FAILED HTTP ${r.status}${r.error ? ` ${r.error}` : ''}`);
          code = 1; break;
        }
        // The version row answers: `deleted` means the objects went one by one and the trail says
        // so. A platform without the registry (beta before its rc) answers without one.
        const state = r.body?.version?.state ?? null;
        res.write(`${bakeHash}: deleted (avatars/${avatarId}/${bakeHash}/)${state ? ` — registry: ${state}` : ' — no registry on this platform; prefix removed'}\n`);
        journal.push(`- \`${avatarId}\` deleted avatars/${avatarId}/${bakeHash}/${state ? ` (registry: ${state})` : ' (no registry)'}. No undo.`);
      }
      const recorded = journal.length > 2 ? recordCleanup(env, target.apiBase, journal) : null;
      if (recorded) res.write(`\n[console] recorded: ${recorded}\n`);
      res.write(`\n[console] exit ${code}\n`);
      res.end();
      return undefined;
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
    /**
     * Capture (or recapture) one character's previews into its folder, without uploading them.
     * The page then shows them in the Previews panel; Publish sends them inside the version once
     * they look right. Starts the local web app the capture drives if nothing answers at STACK.
     */
    if (url.pathname === '/api/capture-previews' && req.method === 'POST') {
      const { characters } = await readBody(req);
      if (!Array.isArray(characters) || characters.length === 0) return send(res, 400, { error: 'no characters selected' });
      const char = characters[0];
      const pkgDir = path.join(url.searchParams.get('root') || DEFAULT_ROOT, char);
      if (!fs.existsSync(path.join(pkgDir, `${char}.glb`))) {
        return send(res, 400, { error: `no package for ${char} in ${path.dirname(pkgDir)}` });
      }
      const monorepo = guessMonorepo();
      if (!monorepo) {
        return send(res, 400, {
          error: 'the telegamez checkout needed to capture previews was not found beside this publisher. '
            + 'Start it with --monorepo <path to telegamez>.',
        });
      }
      res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
      if (!(await ensureStack(res, monorepo))) {
        res.write('\n[console] previews not captured\n[console] exit 1\n');
        res.end();
        return undefined;
      }
      const code = await runCapture(res, monorepo, pkgDir);
      const ok = code === 0 && hasPreviews(pkgDir);
      res.write(ok ? '\n[console] captured - check them in the Previews panel before publishing\n[console] exit 0\n'
        : `\n[console] capture failed (exit ${code})\n[console] exit 1\n`);
      res.end();
      return undefined;
    }

    /** One captured preview file, for the Previews panel. Read-only, confined to the package. */
    if (url.pathname === '/api/preview-file' && req.method === 'GET') {
      const char = url.searchParams.get('char') || '';
      const file = url.searchParams.get('file') || '';
      if (!/^[A-Za-z0-9._-]+$/.test(char) || !PREVIEW_FILES.includes(file)) return send(res, 400, { error: 'bad request' });
      const full = path.join(url.searchParams.get('root') || DEFAULT_ROOT, char, file);
      if (!fs.existsSync(full)) return send(res, 404, { error: 'not captured yet' });
      const type = file.endsWith('.png') ? 'image/png' : file.endsWith('.jpg') ? 'image/jpeg' : 'video/mp4';
      // Replaced in place by a recapture, so a cached copy would show the old one.
      res.writeHead(200, { 'content-type': type, 'cache-control': 'no-store', 'content-length': fs.statSync(full).size });
      fs.createReadStream(full).pipe(res);
      return undefined;
    }

    // `/api/publish-previews` is gone: previews ride inside the version that Publish sends
    // (telegamez#4632 phase 1b), and the platform's separate preview upload answers 410 (#4645).

    /**
     * Point a published directory at its avatar row — what the SQL block used to do by hand.
     *
     * Still a separate press from Publish, because it is still a separate decision: bytes go up
     * without changing anything a user sees, and this is the moment they do. What changes is
     * that it no longer needs database access, and the rollback value is written down by the
     * tool instead of depending on someone keeping a text file.
     */
    /** Avatars with their companions and talk-show slots, for the Clean up step. */
    if (url.pathname === '/api/cleanup/scan' && req.method === 'POST') {
      const { env } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      try {
        return send(res, 200, { avatars: await cleanupScan(target, boundAvatars(url.searchParams.get('root') || DEFAULT_ROOT, env)) });
      } catch (e) {
        return send(res, 502, { error: `${e.message} — is the cookie for ${env}?` });
      }
    }

    /**
     * Apply a cleanup plan: [{ avatarId, replacementId, hard }]. Streams progress, stops at the
     * first failure, and journals every change with its undo. The plan is re-checked against a
     * fresh scan, so a companion added since the dry run is reassigned too, or the run stops.
     */
    if (url.pathname === '/api/cleanup/apply' && req.method === 'POST') {
      const { env, items, confirm, reason } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      if (!Array.isArray(items) || items.length === 0) return send(res, 400, { error: 'nothing selected' });
      if (target.danger && confirm !== env) {
        return send(res, 400, { error: `${env} is production: type "${env}" to confirm` });
      }
      // telegamez#4632 phase 1b: archiving is audited with a reason.
      const why = reasonOf(reason);
      if (!why) return send(res, 400, { error: 'a reason is required: it goes into the character history' });

      res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
      const journal = [];
      const say = (s) => res.write(s);
      const finish = (code) => {
        const recorded = journal.length ? recordCleanup(env, target.apiBase, journal) : null;
        if (recorded) say(`\n[console] recorded: ${recorded}\n`);
        say(`\n[console] exit ${code}\n`);
        res.end();
      };

      let fresh;
      try { fresh = await cleanupScan(target); } catch (e) { say(`could not read ${env}: ${e.message}\n`); return finish(1); }
      const byId = new Map(fresh.map((a) => [a.id, a]));
      const retiring = new Set(items.map((i) => i.avatarId));

      for (const item of items) {
        const a = byId.get(item.avatarId);
        if (!a) { say(`\n${item.avatarId}: not found on ${env} — skipped\n`); continue; }
        say(`\n=== ${a.slug} (${a.name})\n`);
        journal.push(`## ${a.slug}`, '', `- avatar \`${a.id}\` (${a.name}), was status \`${a.status}\`, isActive ${a.isActive}`);
        const uses = a.companions.length + a.talkShowSlots.length;
        const rep = item.replacementId ? byId.get(item.replacementId) : null;
        if (uses > 0) {
          if (!rep) { say(`  used by ${uses} companion(s)/slot(s) and no replacement chosen — stopped\n`); journal.push('- STOPPED: in use, no replacement', ''); return finish(1); }
          if (rep.id === a.id || retiring.has(rep.id) || rep.status === 'archived' || !rep.isActive) {
            say(`  replacement ${rep.slug} is not an active avatar outside this cleanup — stopped\n`);
            journal.push(`- STOPPED: replacement ${rep.slug} unusable`, '');
            return finish(1);
          }
          journal.push(`- replacement \`${rep.id}\` (${rep.slug})`);
        }

        for (const c of a.companions) {
          // `admin.reason` is required: CompanionService.update reads `data.admin.reason` for its
          // audit row inside the transaction, so a body without it rolls back as a bare 500
          // UPDATE_ERROR (panda, 2026-10-06). The admin form always sends it.
          const r = await adminCall(target, 'PUT', `/api/admin/companions/${c.id}`, {
            companion: { avatarId: rep.id },
            admin: { reason: `Publisher cleanup: ${a.slug} retired, companion moved to ${rep.slug}` },
          });
          // The companion update has no declared avatarId field; it reaches the column through
          // the update's spread. Read it back so a future API change cannot make this a no-op.
          const back = r.ok ? await adminCall(target, 'GET', `/api/admin/companions/${c.id}`) : null;
          const now = back?.body?.companion?.avatarId ?? back?.body?.avatarId ?? null;
          if (!r.ok || now !== rep.id) {
            say(`  companion ${c.name}: reassign failed (${r.status}${r.error ? ` ${r.error}` : ''}${r.ok ? `, now ${now}` : ''}) — stopped\n`);
            journal.push(`- companion \`${c.id}\` (${c.name}): reassign FAILED: HTTP ${r.status}${r.error ? ` ${r.error}` : ''}${r.ok ? `, read back avatarId ${now}` : ''}`, '');
            return finish(1);
          }
          say(`  companion ${c.name} -> ${rep.slug}\n`);
          journal.push(`- companion \`${c.id}\` (${c.name}) -> ${rep.slug}. Undo: PUT /api/admin/companions/${c.id} {"companion":{"avatarId":"${a.id}"},"admin":{"reason":"undo cleanup"}}`);
        }
        for (const s of a.talkShowSlots) {
          const r = await adminCall(target, 'PUT', `/api/admin/talk-shows/${s.showId}/companions/${s.slotId}`, { avatarId: rep.id });
          if (!r.ok) {
            say(`  talk show ${s.showName} / ${s.label}: reassign failed (${r.status}${r.error ? ` ${r.error}` : ''}) — stopped\n`);
            journal.push(`- talk-show slot \`${s.slotId}\` (${s.showName} / ${s.label}): reassign FAILED: HTTP ${r.status}${r.error ? ` ${r.error}` : ''}`, '');
            return finish(1);
          }
          say(`  talk show ${s.showName} / ${s.label} -> ${rep.slug}\n`);
          journal.push(`- talk-show slot \`${s.slotId}\` (${s.showName} / ${s.label}) -> ${rep.slug}. Undo: PUT /api/admin/talk-shows/${s.showId}/companions/${s.slotId} {"avatarId":"${a.id}"}`);
        }

        if (a.status !== 'archived') {
          const r = await adminCall(target, 'DELETE', `/api/admin/avatars/${a.id}?reason=${encodeURIComponent(why)}`);
          if (!r.ok) { say(`  archive failed (${r.status}${r.error ? ` ${r.error}` : ''}) — stopped\n`); journal.push(`- archive FAILED: HTTP ${r.status}${r.error ? ` ${r.error}` : ''}`, ''); return finish(1); }
          say('  archived\n');
          journal.push(`- archived. Undo: PUT /api/admin/avatars/${a.id} {"status":"${a.status ?? 'active'}","isActive":${a.isActive}}`);
        } else {
          say('  already archived\n');
        }

        // No delete-for-good: the platform removed it (telegamez#4648, 410 on `?hard=true`). The
        // row and its history stay; old versions go through Remove old versions.
        journal.push('');
      }
      return finish(0);
    }

    if (url.pathname === '/api/go-live' && req.method === 'POST') {
      const { env, char, avatarId, modelKey, reason } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      if (!avatarId || !modelKey) return send(res, 400, { error: 'avatarId and modelKey are required' });
      // telegamez#4632 phase 1b: going live is a state change on the version, with its reason.
      const why = reasonOf(reason);
      if (!why) return send(res, 400, { error: 'a reason is required: it goes into the character history' });

      const r = await fetch(`${target.apiBase}/api/admin/avatars/${avatarId}/model-path`, {
        method: 'PUT',
        headers: { Authorization: `Bearer ${session.token}`, 'content-type': 'application/json' },
        body: JSON.stringify({ modelKey, reason: why }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        return send(res, 502, { error: body?.error ?? body?.message ?? `admin API ${r.status}` });
      }
      // Read the registry back: the row's path alone cannot say the registry agrees. `live` is
      // the proof the page shows; a platform without the versions route is reported as such.
      const check = await registryOf(target, avatarId, String(modelKey).split('/')[2] ?? null);
      const status = body?.data?.status ?? null;

      // The bake suffix is already in the key, so the record lands in the same file as the
      // publish it completes rather than needing the page to carry it back here.
      //
      // The key's segment is `<char>-<build>-<suffix>` (older publishes: `<char>-<suffix>`),
      // because publish passes -BakeHash that way, but publish names its file from the bare
      // suffix. Taking the segment whole put every go-live in a file of its own beside the
      // publish it belonged to - which is how the original six ended up as panda-tele-*,
      // panda-tele1-* and so on, apart from their publishes.
      const segment = String(modelKey).split('/')[2] ?? 'unknown';
      const suffix = segment.startsWith(`${char}-`)
        ? segment.slice(char.length + 1).replace(/^(?:ktx2|meshopt)-/, '')
        : segment;
      const previous = body?.data?.previousModelPath ?? null;
      const recorded = recordGoLive({
        env, apiBase: target.apiBase, char, avatarId,
        bakeSuffix: suffix, modelKey, previous,
        reason: why, state: check.state, registry: check.registry, status,
      });

      return send(res, 200, { char, avatarId, modelKey, previous, recorded, status, versionState: check.state, registry: check.registry });
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
});
