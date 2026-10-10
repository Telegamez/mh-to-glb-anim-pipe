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
 * The character's identity, stored IN the package folder as `character.json` (telegamez#4632
 * phase 2): `{ "id": "drake", "name": "Drake", "gender": "male", "tags": [] }`. The id is the
 * character's id in every environment's registry, chosen once and never renamed; the folder and
 * model names play no part. A re-bake keeps the file (package_character.ps1). Missing or invalid,
 * the package is listed and cannot be published. `name`, `gender` and `tags` are optional for a
 * character the registry already knows (its values are kept); a new id needs at least a name.
 */
const IDENTITY_FILE = 'character.json';
const ID_OK = /^[a-z0-9][a-z0-9-]{0,63}$/;
function readIdentity(dir) {
  try {
    const j = JSON.parse(fs.readFileSync(path.join(dir, IDENTITY_FILE), 'utf8'));
    if (!j || typeof j !== 'object' || Array.isArray(j)) return { error: `${IDENTITY_FILE} must hold an object` };
    if (typeof j.id !== 'string' || !ID_OK.test(j.id)) return { error: `${IDENTITY_FILE}: "id" must be lowercase letters, digits and dashes, e.g. "drake"` };
    if (j.name !== undefined && (typeof j.name !== 'string' || !j.name.trim())) return { error: `${IDENTITY_FILE}: "name" must be a non-empty string when given` };
    if (j.gender !== undefined && j.gender !== null && typeof j.gender !== 'string') return { error: `${IDENTITY_FILE}: "gender" must be a string or null` };
    if (j.tags !== undefined && (!Array.isArray(j.tags) || j.tags.some((t) => typeof t !== 'string'))) return { error: `${IDENTITY_FILE}: "tags" must be a list of strings` };
    return { id: j.id, name: j.name?.trim() || null, gender: j.gender ?? null, tags: j.tags ?? null };
  } catch (e) {
    return { error: e.code === 'ENOENT' ? `no ${IDENTITY_FILE}: add one with the character's id (PUBLISHER.md, "The character's id")` : `${IDENTITY_FILE} is not valid JSON` };
  }
}

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
    // telegamez#4632 phase 2: the character's id comes from the package's character.json.
    const identity = readIdentity(dir);

    const problems = [];
    if (modelMismatch) problems.push(`model is ${modelMismatch}, folder is ${name} — Rename to bring them in line`);
    if (!sidecar) problems.push('no mh_materials.json — would render untextured');
    if (textures === 0) problems.push('no textures/');
    if (strayManifest) problems.push('stray manifest.json — publish would be refused');
    if (identity.error) problems.push(identity.error);

    characters.push({
      id: name, dir, mb, sidecar, textures, strayManifest, previews, compressed, modelMismatch,
      identity: identity.error ? null : identity,
      // When the previews were captured, for the Previews panel (and to tell a recapture apart).
      previewsAt: previews
        ? Math.max(...PREVIEW_FILES.map((f) => fs.statSync(path.join(dir, f)).mtimeMs)) : null,
      // Pre-ticked when it carries an identity: a folder found beside the roster with no
      // character.json (a research variant, a stray copy) is listed and left unticked.
      ready: problems.length === 0, problems,
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

function recordPublish({ env, apiBase, char, characterId, bakeSuffix, bakeHash, previous, output, code, reason = null, previews = false }) {
  const grab = (re) => { const m = output.match(re); return m ? m[1] : null; };
  const modelKey = grab(/modelKey:\s*(\S+)/);
  const avatarId = grab(/avatarId:\s*(\S+)/);
  const files = grab(/OK - (\d+ files published)/);
  const size = grab(/\d+ files, ([\d.]+ MB)/);

  const out = [`## ${char}`, ''];
  out.push(`- when    ${new Date().toISOString()}`);
  out.push(`- character \`${characterId}\``);
  if (avatarId) out.push(`- avatar  \`${avatarId}\``);
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
    // Through the API, never SQL: going live is a state change on the version (telegamez#4632),
    // and a hand-run UPDATE leaves the registry behind (db:characters:verify reports it as
    // pointer drift). Since phase 2 the calls name the character and the version, not a key.
    out.push('```');
    out.push('-- go live');
    out.push(`POST ${apiBase}/api/admin/characters/${characterId}/versions/${bakeHash}/live  { "reason": "..." }`);
    out.push('');
    out.push('-- roll back');
    const wasVersion = wasPath ? bakeOfPath(wasPath) : null;
    if (wasVersion) {
      out.push(`POST ${apiBase}/api/admin/characters/${characterId}/versions/${wasVersion}/live  { "reason": "..." }`);
    } else if (wasPath) {
      out.push(`-- the previous model is not a registered version (${wasPath}); restoring it needs a re-publish of that bake`);
    } else {
      out.push('-- the character had no live version before this publish, so there is nothing to restore');
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
function recordGoLive({ env, apiBase, char, characterId, version, bakeSuffix, modelKey, previous, reason = null, state = null, status = null }) {
  const out = [`## ${char} — live`, ''];
  out.push(`- when    ${new Date().toISOString()}`);
  out.push(`- character \`${characterId}\``);
  out.push(`- version \`${version}\`${state ? ` (registry: ${state})` : ''}`);
  out.push(`- now     \`${modelKey}\``);
  out.push(`- was     ${previous ? `\`${previous}\`` : '(no live version before)'}`);
  if (status) out.push(`- status  \`${status}\`${status !== 'active' ? ' (going live does not change it; set it in admin with a reason)' : ''}`);
  if (reason) out.push(`- reason  ${reason}`);
  out.push('');
  // Rolling back is taking the previous version live again. The first time a character goes
  // live there is nothing to go back to, and saying so here is better than printing a call
  // that would be refused at the moment somebody needed it to work.
  const previousVersion = previous ? bakeOfPath(previous) : null;
  out.push('To put it back:');
  out.push('```');
  if (previousVersion) {
    out.push(`POST ${apiBase}/api/admin/characters/${characterId}/versions/${previousVersion}/live  { "reason": "..." }`);
  } else if (previous) {
    out.push(`the previous model is not a registered version, so the API will not accept it:`);
    out.push(`  ${previous}`);
    out.push(`restoring it needs a re-publish of that bake`);
  } else {
    out.push('the character had no live version before this, so there is nothing to restore');
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

/** What a version holds, when its name says so (`-ktx2-`); the registry records it as well. */
const buildOf = (hash, recorded = null) => (recorded && recorded !== 'unknown' ? recorded : (String(hash).match(/-(ktx2|meshopt)-\d{8}-/) || [])[1] ?? 'not recorded');

/** The version a model path points into, whether stored relative or returned as a full URL. */
const bakeOfPath = (p) => (String(p || '').match(/avatars\/[0-9a-f-]{36}\/([A-Za-z0-9._-]+)\/[^/]+\.glb/i) || [])[1] ?? null;

/** A reason for the character history (telegamez#4632 phase 1b): required, trimmed, bounded. */
const reasonOf = (r) => { const s = String(r ?? '').trim(); return s && s.length <= 500 ? s : null; };

/**
 * The platform's character registry (telegamez#4632 phase 2), by character id. `registry: false`
 * where the platform has no characters route (an environment before the rc that carries phase
 * 2): nothing can be published there from here, and the page says so.
 */
async function listCharacters(target) {
  const r = await adminCall(target, 'GET', '/api/admin/characters');
  if (r.status === 404) return { registry: false, characters: new Map() };
  if (!r.ok) return { registry: true, characters: new Map(), problem: `characters ${r.status}${r.error ? ` ${r.error}` : ''}` };
  const rows = Array.isArray(r.body?.characters) ? r.body.characters : [];
  return { registry: true, environment: r.body?.environment ?? null,
    characters: new Map(rows.map((row) => [row.character?.id, { character: row.character, liveVersion: row.liveVersion ?? null }])) };
}

/** One character's versions and trail from the registry, and the state of one version in particular. */
async function registryOf(target, characterId, version = null) {
  const r = await adminCall(target, 'GET', `/api/admin/characters/${encodeURIComponent(characterId)}/versions`);
  if (r.status === 404) return { registry: true, exists: false, versions: [], state: null };
  if (!r.ok) return { registry: true, exists: null, versions: [], state: null, problem: `versions ${r.status}${r.error ? ` ${r.error}` : ''}` };
  const versions = Array.isArray(r.body?.versions) ? r.body.versions : [];
  return { registry: true, exists: true, character: r.body?.character ?? null, versions,
    state: version ? versions.find((v) => v.version === version)?.state ?? null : null };
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
// Talking to the platform's admin API. Retiring, withdrawing and re-pointing characters are the
// platform's own (its admin Characters page, telegamez#4632 phase 2); this console publishes
// versions, takes them live and removes old ones, nothing else.

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
     * Resolve each package to its character in the TARGET environment's registry, by the id in
     * its character.json (telegamez#4632 phase 2). The id is the same everywhere, so nothing is
     * remembered per environment and nothing is matched by name. A character the registry does
     * not have yet comes back `exists: false`: a real, expected state for one never published
     * here, and the first publish creates it.
     *
     * An environment without the characters route (`registry: false`) cannot be published to
     * from here at all: it receives characters by promotion from panda, on its admin page.
     */
    if (url.pathname === '/api/resolve' && req.method === 'POST') {
      const { env, characters } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      const root = url.searchParams.get('root') || DEFAULT_ROOT;

      const registry = await listCharacters(target);
      if (registry.problem) return send(res, 502, { error: `${registry.problem} — is the cookie for ${env}?` });

      const resolved = (characters || []).map((char) => {
        const identity = readIdentity(path.join(root, char));
        if (identity.error) return { char, characterId: null, problem: identity.error };
        const known = registry.registry ? registry.characters.get(identity.id) ?? null : null;
        const live = known?.liveVersion ?? null;
        const previews = live?.metadata?.previews ?? {};
        return {
          char, characterId: identity.id,
          exists: Boolean(known),
          name: known?.character?.displayName ?? identity.name ?? null,
          state: known?.character?.state ?? null,
          // What the registry serves now: the live version, its build and its model key.
          liveBake: live?.version ?? null,
          liveBuild: live ? buildOf(live.version, live.build) : null,
          avatarId: live?.avatarId ?? null,
          modelPath: live?.modelKey ?? null,
          published: Boolean(live),
          // Go live waits for previews (telegamez#3662): either inside the version it just
          // published, or already carried by the live version.
          previewsPublished: Boolean(previews.video || previews.image || previews.thumbnail),
        };
      });
      // What each character serves *now*, kept for the journal: a go-live retires it, and the
      // rollback call needs its version.
      for (const row of resolved) {
        session.prior[`${env}/${row.char}`] = { modelPath: row.modelPath || null, status: row.state || null };
      }
      return send(res, 200, { environment: env, registry: registry.registry, promotionOnly: Boolean(target.danger), resolved });
    }

    // `/api/bind` and `/api/create-avatar` are gone (telegamez#4632 phase 2): the character's id
    // in its package's character.json is the binding, and a first publish of a new id creates
    // the character on the platform.

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
      const { env, characters, bakeHashSuffix, build: requested, reason } = await readBody(req);
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

      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      const pkgDir = path.join(root, char);
      if (!fs.existsSync(path.join(pkgDir, `${char}.glb`))) {
        return send(res, 400, { error: `no package for ${char} in ${root}` });
      }

      // telegamez#4632 phase 2: the version is published under the character's id from the
      // package's character.json. Its name, gender and tags come from the registry when the
      // character exists there (so a package that names none of them changes none), with the
      // package's values on top; a new id needs at least a name.
      const local = readIdentity(pkgDir);
      if (local.error) return send(res, 400, { error: `${char}: ${local.error}` });
      const registry = await listCharacters(target);
      if (!registry.registry) return send(res, 400, { error: `${env} has no character registry: characters reach it by promotion from panda, not by publishing` });
      if (registry.problem) return send(res, 502, { error: `${registry.problem} — is the cookie for ${env}?` });
      const known = registry.characters.get(local.id)?.character ?? null;
      const identity = {
        id: local.id,
        name: local.name ?? known?.displayName ?? null,
        gender: local.gender ?? known?.gender ?? null,
        tags: local.tags ?? known?.tags ?? [],
      };
      if (!identity.name) return send(res, 400, { error: `${char}: ${local.id} is new to ${env}, so its character.json needs a "name"` });

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
        '-Char', char, '-CharacterId', identity.id, '-Identity', JSON.stringify(identity),
        '-ApiBase', target.apiBase,
        '-Source', made.dir,
        '-BakeHash', bakeHash,
        '-Build', BUILD,
        '-Reason', why,
        ...(previews ? ['-PreviewsFrom', pkgDir] : []),
      ], { TG_ADMIN_TOKEN: session.token }, (output, code) => recordPublish({
        env, apiBase: target.apiBase, char, characterId: identity.id,
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
      // The character's id lives in character.json and is untouched by a rename: the package
      // keeps publishing to the same character (telegamez#4632 phase 2).
      const identity = readIdentity(toDir);
      recordRename([`## ${from} -> ${to}`, '', `- when    ${new Date().toISOString()}`, `- root    ${root}`,
        ...done.map((d) => `- done    ${d}`),
        `- character ${identity.error ? `(none: ${identity.error})` : `\`${identity.id}\``}`]);
      return send(res, 200, { from, to, done, characterId: identity.error ? null : identity.id });
    }

    // `/api/sync-slugs` is gone (telegamez#4632 phase 2): the character's id is the identity in
    // every environment, and the legacy `mh-<folder>` slug convention is retired with it.

    /**
     * Old versions of the ticked packages' characters, from the platform's registry: every
     * version except the live one, with its state. Read-only; the page shows it as a list to tick.
     */
    if (url.pathname === '/api/versions/scan' && req.method === 'POST') {
      const { env, characters } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      const registry = await listCharacters(target);
      if (!registry.registry) return send(res, 400, { error: `${env} has no character registry yet` });
      if (registry.problem) return send(res, 502, { error: `${registry.problem} — is the cookie for ${env}?` });
      const out = [];
      for (const char of characters || []) {
        const identity = readIdentity(path.join(root, String(char)));
        if (identity.error) { out.push({ char, problem: identity.error }); continue; }
        const reg = await registryOf(target, identity.id);
        if (reg.exists === false) { out.push({ char, characterId: identity.id, problem: `${identity.id} is not on ${env} yet` }); continue; }
        if (reg.problem) { out.push({ char, characterId: identity.id, problem: reg.problem }); continue; }
        const liveRow = reg.versions.find((v) => v.state === 'live') ?? null;
        const old = reg.versions
          .filter((v) => v.state !== 'live' && v.state !== 'deleted')
          // `uploadedAt` and the state ride along so the page can tell a publish still uploading
          // from a stale partial, and a `ready` version can be taken live from the list.
          .map((v) => ({ bakeHash: v.version, build: buildOf(v.version, v.build), state: v.state, uploadedAt: v.uploadedAt ?? null }))
          .sort((p, q) => p.bakeHash.localeCompare(q.bakeHash));
        out.push({ char, characterId: identity.id, name: reg.character?.displayName ?? null, state: reg.character?.state ?? null,
          live: liveRow?.version ?? null, liveBuild: liveRow ? buildOf(liveRow.version, liveRow.build) : null, old });
      }
      return send(res, 200, { environment: env, registry: true, packages: out });
    }

    /**
     * Delete old versions: [{ characterId, version }]. One `DELETE /api/admin/characters/<id>/
     * versions/<version>` each, with the reason: the platform walks the version through
     * `deleting` to `deleted`, removes its objects one by one, and refuses the live version and
     * any version a preview still points into. Re-checked against the registry first anyway.
     * No undo: these are deleted from the bucket. Journaled.
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
      for (const { characterId, version } of items) {
        if (!ID_OK.test(String(characterId)) || !/^[A-Za-z0-9._-]{6,64}$/.test(String(version))) {
          res.write(`skipped a malformed item\n`); continue;
        }
        if (!liveOf.has(characterId)) {
          const reg = await registryOf(target, characterId);
          if (reg.exists === false || reg.problem) { res.write(`${characterId}: could not read the character (${reg.problem ?? 'not found'}) — stopped\n`); code = 1; break; }
          liveOf.set(characterId, reg.versions.find((v) => v.state === 'live')?.version ?? null);
        }
        if (liveOf.get(characterId) === version) { res.write(`${version}: is live — kept\n`); continue; }
        const r = await adminCall(target, 'DELETE', `/api/admin/characters/${encodeURIComponent(characterId)}/versions/${encodeURIComponent(version)}`, { reason: why });
        if (!r.ok) {
          res.write(`${version}: delete failed (${r.status}${r.error ? ` ${r.error}` : ''}) — stopped\n`);
          journal.push(`- \`${characterId}\` ${version}: FAILED HTTP ${r.status}${r.error ? ` ${r.error}` : ''}`);
          code = 1; break;
        }
        // The version row answers: `deleted` means the objects went one by one and the trail says so.
        const state = r.body?.version?.state ?? null;
        res.write(`${version}: deleted (${characterId})${state ? ` — registry: ${state}` : ''}\n`);
        journal.push(`- \`${characterId}\` deleted version ${version}${state ? ` (registry: ${state})` : ''}. No undo.`);
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

    // `/api/cleanup/scan` and `/api/cleanup/apply` are gone (telegamez#4632 phase 2): retiring,
    // withdrawing and re-pointing characters are the platform's, on its admin Characters page.

    /**
     * Take a published version live: `POST /api/admin/characters/<id>/versions/<version>/live`
     * with the reason (telegamez#4632 phase 2). Still its own press, because it is still its
     * own decision: publishing changed nothing anybody sees, and this is the moment that does.
     * The previous version becomes `retired`; the response carries the version row and the
     * rooms whose cached config was dropped, and the journal records how to put it back.
     */
    if (url.pathname === '/api/go-live' && req.method === 'POST') {
      const { env, char, characterId, version, reason } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      if (!ID_OK.test(String(characterId || '')) || !/^[A-Za-z0-9._-]{6,64}$/.test(String(version || ''))) {
        return send(res, 400, { error: 'characterId and version are required' });
      }
      // telegamez#4632 phase 1b: going live is a state change on the version, with its reason.
      const why = reasonOf(reason);
      if (!why) return send(res, 400, { error: 'a reason is required: it goes into the character history' });

      const r = await adminCall(target, 'POST', `/api/admin/characters/${encodeURIComponent(characterId)}/versions/${encodeURIComponent(version)}/live`, { reason: why });
      if (!r.ok) return send(res, 502, { error: r.error ?? `admin API ${r.status}` });
      const activated = r.body?.version ?? {};
      const modelKey = activated.modelKey ?? null;
      const previous = activated.previousModelPath ?? null;
      const status = activated.avatarStatus ?? null;
      // The registry read back, independently of the response: `live` is the proof the page shows.
      const check = await registryOf(target, characterId, version);

      // The version is `<char>-<build>-<suffix>`; the record lands in the same journal file as the
      // publish it completes, which publish named from the bare suffix.
      const suffix = String(version).startsWith(`${char}-`)
        ? String(version).slice(String(char).length + 1).replace(/^(?:ktx2|meshopt)-/, '')
        : String(version);
      const recorded = recordGoLive({
        env, apiBase: target.apiBase, char, characterId, version,
        bakeSuffix: suffix, modelKey, previous,
        reason: why, state: check.state, status,
      });

      return send(res, 200, { char, characterId, version, modelKey, previous, recorded, status,
        versionState: check.state, roomsRefreshed: r.body?.roomsRefreshed ?? [] });
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
