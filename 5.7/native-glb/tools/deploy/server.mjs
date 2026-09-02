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
const COMPRESSOR = findBeside('compress_companion.ps1');

/**
 * Where a compressed build of a character lives: `<root>/_compressed/<char>/`.
 *
 * Beside the character folders rather than inside one, because `publish_to_cdn.ps1` uploads
 * its `-Source` recursively — a compressed copy nested in the character directory would be
 * published alongside the original and double the payload instead of cutting it.
 */
const COMPRESSED_DIR = '_compressed';
const compressedPath = (root, char) => path.join(root, COMPRESSED_DIR, char);

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
    if (name === COMPRESSED_DIR) continue;   // holds character folders, is not one
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

    const compDir = compressedPath(root, name);
    const compGlb = path.join(compDir, `${name}.glb`);
    let compressed = null;
    if (fs.existsSync(compGlb)) {
      let compMode = 'unknown';
      try {
        compMode = JSON.parse(
          fs.readFileSync(path.join(compDir, 'compression_manifest.json'), 'utf8'),
        ).mode || 'unknown';
      } catch { /* a build without a readable manifest still has a size worth showing */ }
      const compTex = path.join(compDir, 'textures');
      const bytes = fs.statSync(compGlb).size + (fs.existsSync(compTex)
        ? fs.readdirSync(compTex).reduce((a, f) => a + fs.statSync(path.join(compTex, f)).size, 0)
        : 0);
      compressed = {
        mode: compMode,
        mb: Math.round(bytes / 1048576),
        stale: fs.statSync(compGlb).mtimeMs < contentTime,
        // `none` and `meshopt` are both publishable. meshopt is lossy on geometry but was
        // compared against the original through the real runtime — material profile, hair
        // calibration and clothing textures live — and is indistinguishable. `full` carries
        // KTX2, which no surface can load: it would upload cleanly, report success and then
        // fail to render.
        publishable: compMode === 'none' || compMode === 'meshopt',
      };
    }

    const problems = [];
    if (!sidecar) problems.push('no mh_materials.json — would render untextured');
    if (textures === 0) problems.push('no textures/');
    if (strayManifest) problems.push('stray manifest.json — publish would be refused');

    characters.push({
      id: name, dir, mb, sidecar, textures, strayManifest, previews, compressed,
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

function recordPublish({ env, apiBase, char, avatarId, bakeSuffix, bakeHash, previous, output, code, compressed }) {
  const grab = (re) => { const m = output.match(re); return m ? m[1] : null; };
  const modelKey = grab(/modelKey:\s*(\S+)/);
  const files = grab(/OK - (\d+ files published)/);
  const size = grab(/\d+ files, ([\d.]+ MB)/);

  const out = [`## ${char}`, ''];
  out.push(`- when    ${new Date().toISOString()}`);
  out.push(`- avatar  \`${avatarId}\``);
  out.push(`- bake    \`${bakeHash}\``);
  if (files || size) out.push(`- upload  ${[files, size].filter(Boolean).join(', ')}`);
  // Which bytes went up. A compressed publish and an uncompressed one produce the same
  // model key and the same SQL, so nothing else in this record distinguishes them — and
  // "why is this companion 87 MB again" is answered here or not at all.
  out.push(`- source  ${compressed ? 'COMPRESSED build (_compressed/)' : 'uncompressed bake'}`);
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
  const restorable = previous && /^avatars\/[0-9a-f-]{36}\/[A-Za-z0-9._-]+\/.+\.glb$/i.test(previous);
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
      return send(res, 200, { avatarId: out.id || out.avatar?.id || null, raw: out });
    }

    if (url.pathname === '/api/verify' && req.method === 'POST') {
      const { env } = await readBody(req);
      const target = ENVIRONMENTS[env];
      if (!target) return send(res, 400, { error: `unknown environment: ${env}` });
      if (!session.token) return send(res, 400, { error: 'no token set' });
      return stream(res, VERIFIER, ['-ApiBase', target.apiBase], { TG_ADMIN_TOKEN: session.token });
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
      if (mode && !['none', 'meshopt', 'full'].includes(mode)) {
        return send(res, 400, { error: `unknown mode: ${mode}` });
      }
      // One per request, like publish: the page drives the loop so a failure stops the batch
      // at a character anyone can name.
      const char = characters[0];
      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      const args = ['-Char', char, '-Root', root, '-Mode', mode || 'meshopt'];
      if (Number(texturePx) > 0) args.push('-TexturePx', String(Number(texturePx)));
      return stream(res, COMPRESSOR, args);
    }

    if (url.pathname === '/api/publish' && req.method === 'POST') {
      const { env, characters, bakeHashSuffix, avatarIds, useCompressed } = await readBody(req);
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

      const root = url.searchParams.get('root') || DEFAULT_ROOT;
      let source = path.join(root, char);

      if (useCompressed) {
        const compDir = compressedPath(root, char);
        if (!fs.existsSync(path.join(compDir, `${char}.glb`))) {
          return send(res, 400, { error: `no compressed build for ${char} — compress it first` });
        }
        // Refuse `full` outright rather than warn. It carries KTX2, which no surface can load:
        // it would upload cleanly, report success everywhere, and render nothing — the same
        // silent failure as beta's G13, and nothing downstream would catch it either.
        let compMode = 'unknown';
        try {
          compMode = JSON.parse(
            fs.readFileSync(path.join(compDir, 'compression_manifest.json'), 'utf8'),
          ).mode;
        } catch { /* falls through to the refusal below */ }
        if (compMode !== 'none' && compMode !== 'meshopt') {
          return send(res, 400, {
            error: `compressed build for ${char} is mode="${compMode}", and the runtime `
              + 'cannot load KTX2 (no KTX2Loader in web-next) - it would publish and then '
              + 'fail to render. Re-compress with mode=meshopt.',
          });
        }
        source = compDir;
      }

      return stream(res, PUBLISHER, [
        '-Char', char, '-AvatarId', avatarId, '-ApiBase', target.apiBase,
        '-Source', source,
        '-BakeHash', `${char}-${bakeHashSuffix}`,
      ], { TG_ADMIN_TOKEN: session.token }, (output, code) => recordPublish({
        env, apiBase: target.apiBase, char, avatarId,
        bakeSuffix: bakeHashSuffix, bakeHash: `${char}-${bakeHashSuffix}`,
        previous: session.prior[`${env}/${char}`] ?? null,
        output, code, compressed: !!useCompressed,
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
      //
      // The key's segment is `<char>-<suffix>`, because publish passes -BakeHash that way, but
      // publish names its file from the bare suffix. Taking the segment whole put every go-live
      // in a file of its own beside the publish it belonged to - which is how the original six
      // ended up as panda-tele-*, panda-tele1-* and so on, apart from their publishes.
      const segment = String(modelKey).split('/')[2] ?? 'unknown';
      const suffix = segment.startsWith(`${char}-`) ? segment.slice(char.length + 1) : segment;
      const previous = body?.data?.previousModelPath ?? null;
      const recorded = recordGoLive({
        env, apiBase: target.apiBase, char, avatarId,
        bakeSuffix: suffix, modelKey, previous,
      });

      return send(res, 200, { char, avatarId, modelKey, previous, recorded });
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
