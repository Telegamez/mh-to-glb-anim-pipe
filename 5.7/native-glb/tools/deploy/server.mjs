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
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};

const PORT = Number(arg('port', 7311));
const DEFAULT_ROOT = arg('root', 'S:\\Avatars\\metahuman\\telegamez\\packages\\web-next\\public\\models');
const PUBLISHER = path.resolve(HERE, '..', 'publish_to_cdn.ps1');
const VERIFIER = path.resolve(HERE, 'Test-CompanionsLive.ps1');

/** Environments this console will talk to. Adding one is a line here, not a code change. */
const ENVIRONMENTS = {
  panda: { apiBase: 'https://api.panda.telegames.ai', label: 'panda' },
  beta: { apiBase: 'https://api.telegamez.com', label: 'beta — PRODUCTION', danger: true },
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
const session = { token: null };

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

    const problems = [];
    if (!sidecar) problems.push('no mh_materials.json — would render untextured');
    if (textures === 0) problems.push('no textures/');
    if (strayManifest) problems.push('stray manifest.json — publish would be refused');

    characters.push({
      id: name, dir, mb, sidecar, textures, strayManifest,
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
function stream(res, file, args, extraEnv = {}) {
  res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
  const child = spawn('pwsh', ['-NoProfile', '-File', file, ...args], {
    env: { ...process.env, ...extraEnv },
    windowsHide: true,
  });
  child.stdout.on('data', (d) => res.write(d));
  child.stderr.on('data', (d) => res.write(d));
  child.on('error', (e) => { res.write(`\n[console] failed to start: ${e.message}\n`); res.end(); });
  child.on('close', (code) => { res.write(`\n[console] exit ${code}\n`); res.end(); });
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

    if (url.pathname === '/api/scan') {
      return send(res, 200, scanRoot(url.searchParams.get('root') || DEFAULT_ROOT));
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
          published: Boolean(row && /^avatars\/[0-9a-f-]{36}\/[A-Za-z0-9._-]+\//i.test(row.modelPath || '')),
        };
      });
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
      ], { TG_ADMIN_TOKEN: session.token });
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
