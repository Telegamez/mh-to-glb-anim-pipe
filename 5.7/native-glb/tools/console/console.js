/**
 * One console, two phases, one grid.
 *
 * The grid is the design. Everything else is a button that fills one of its columns, and it is
 * on screen the whole time — so "where am I" is answered by looking rather than by remembering
 * which tool was run last. Local steps (Bake, Previews) are read from the packages folder;
 * environment steps (Upload, Assets, Live) come from the target's own avatar rows, because the
 * environment is the only authority on what it has.
 *
 * Every row carries its own thumbnail. A tick tells you a step ran; the picture tells you it
 * ran on the character you meant.
 */
const $ = (id) => document.getElementById(id);
const log = (s) => { $('log').textContent += s; $('log').scrollTop = 1e9; };
const setLog = (s) => { $('log').textContent = s; };

let CONFIG = null;
let CHARS = [];          // packages on disk
let ROWS = {};           // avatar rows in the target environment, by character id
let UPLOADED = {};       // what this session published, by character id
let PICKED = new Set();  // ticked characters
let ASSET = null;        // .uasset chosen for a bake
let BODY = null;         // female | male
let ROOT = '';

const env = () => $('env').value;
const target = () => CONFIG.environments.find((x) => x.key === env());
const connected = () => Boolean(CONFIG && CONFIG.hasToken);

// ---------------------------------------------------------------------------------------
// State: one row per character, one cell per step.

/**
 * Upload is the one step with no direct evidence.
 *
 * Bytes on the CDN leave no database trace — publishing deliberately touches no row — and this
 * console cannot list the bucket. So it is knowable in exactly two ways: this session did it,
 * or the row points at a published key, which cannot be true unless the bytes are there.
 *
 * Any other case is genuinely UNKNOWN, and says so. Drawing it as "not done" would assert
 * something false: the six were uploaded to panda hours before this console existed, and a grid
 * claiming otherwise would send an operator to re-upload work that is already there — or worse,
 * be believed when it says a step is missing that is not.
 */
function steps(c) {
  const row = ROWS[c.id];
  const publishedKey = Boolean(row && row.published);
  const live = Boolean(publishedKey && row.status === 'active');
  return {
    bake: c.ready ? 'ok' : 'warn',
    previews: c.previews ? 'ok' : 'no',
    upload: !connected() ? 'unknown'
      : (UPLOADED[c.id] || publishedKey) ? 'ok'
      : 'unknown',
    assets: !connected() ? 'unknown' : row && row.previewsPublished ? 'ok' : 'no',
    live: !connected() ? 'unknown' : live ? 'ok' : 'no',
  };
}

const cell = (state) => state === 'unknown'
  ? '<span class="unknown">&mdash;</span>'
  : `<span class="dot ${state}"></span>`;

function renderGrid() {
  const body = $('grid');
  body.innerHTML = '';
  if (!CHARS.length) {
    body.innerHTML = '<tr><td colspan="7" class="sub">no characters in this folder yet — bake one below</td></tr>';
    return;
  }
  for (const c of CHARS) {
    const s = steps(c);
    const tr = document.createElement('tr');
    if (!PICKED.has(c.id)) tr.className = 'off';

    const tick = document.createElement('td');
    const box = document.createElement('input');
    box.type = 'checkbox'; box.checked = PICKED.has(c.id);
    box.onclick = (ev) => { ev.stopPropagation(); toggle(c.id); };
    tick.append(box);

    const who = document.createElement('td');
    who.innerHTML = `<div class="row" style="gap:9px">
        ${c.previews ? `<img src="/api/asset?char=${encodeURIComponent(c.id)}&file=thumbnail.png&root=${encodeURIComponent(ROOT)}"
             width="46" height="46" style="border-radius:5px;background:#0d1b2e" alt="">`
          : '<div style="width:46px;height:46px;border-radius:5px;background:#0d1b2e"></div>'}
        <div><div class="who">${c.id}</div><div class="sub">${c.mb} MB${c.body ? ' · ' + c.body : ''}</div></div>
      </div>`;

    tr.append(tick, who);
    for (const k of ['bake', 'previews', 'upload', 'assets', 'live']) {
      const td = document.createElement('td');
      td.className = 'step';
      td.innerHTML = cell(s[k]);
      tr.append(td);
    }
    tr.style.cursor = 'pointer';
    tr.onclick = () => showDetail(c);
    body.append(tr);
  }
  refreshButtons();
}

function toggle(id) {
  if (PICKED.has(id)) PICKED.delete(id); else PICKED.add(id);
  renderGrid();
}

function refreshButtons() {
  const n = PICKED.size;
  $('count').textContent = n ? n + ' selected' : '';
  const on = connected() && n > 0;
  $('pubModel').disabled = !on;
  $('pubPreviews').disabled = !on;
  $('goLive').disabled = !on || !Object.keys(UPLOADED).length;
  $('verify').disabled = !connected();
  $('bake').disabled = !(ASSET && BODY);
  $('capture').disabled = n === 0;
}

// ---------------------------------------------------------------------------------------
// Detail: is this the character you meant?

function showDetail(c) {
  const q = `char=${encodeURIComponent(c.id)}&root=${encodeURIComponent(ROOT)}`;
  const row = ROWS[c.id];
  const dlg = $('dlg');
  dlg.querySelector('.dhead').innerHTML = `<b>${c.id}</b><span class="sub">${c.mb} MB${c.body ? ' · ' + c.body : ''}</span>`;
  $('dList').innerHTML = c.previews
    ? `<div style="padding:12px;display:flex;gap:14px;align-items:flex-start">
         <img src="/api/asset?${q}&file=imagePreview.jpg" style="width:250px;border-radius:6px" alt="">
         <video src="/api/asset?${q}&file=videoPreview.mp4" style="width:250px;border-radius:6px"
                autoplay loop muted playsinline></video>
       </div>
       <div class="sub" style="padding:0 12px 12px">
         ${row && row.avatarId ? 'avatar ' + row.avatarId : 'no avatar record in ' + env()}<br>
         ${row && row.modelPath ? row.modelPath : ''}
       </div>`
    : `<div style="padding:16px" class="sub">No previews captured yet. Tick this character and press
         <b>Previews only</b> to photograph the model that is actually in the package.</div>`;
  $('dUse').hidden = true;
  $('dCancel').textContent = 'Close';
  dlg.showModal();
}

// ---------------------------------------------------------------------------------------
// Loading

async function boot() {
  CONFIG = await (await fetch('/api/config')).json();
  ROOT = CONFIG.defaultRoot;
  $('rootPath').textContent = ROOT;
  for (const e of CONFIG.environments) {
    const o = document.createElement('option');
    o.value = e.key; o.textContent = e.label;
    $('env').append(o);
  }
  $('stackNote').hidden = CONFIG.stackUp !== false;
  if (CONFIG.stackUp === false) {
    $('stackNote').textContent =
      'The local stack is not running, so previews cannot be captured. The model still bakes correctly.';
  }
  envChanged();
  await scan();
}

/** Environment-specific state must never survive an environment change. Ids differ per env. */
function envChanged() {
  ROWS = {}; UPLOADED = {};
  CONFIG.hasToken = false;
  $('conn').textContent = 'not connected';
  $('token').value = '';
  const err = $('connErr');
  if (err) err.hidden = true;
  const e = target();
  $('prodWarn').innerHTML = e && e.danger
    ? '<div class="alert"><b>Production.</b> Real customers. Beta also needs its CORS fix first.</div>'
    : '';
  renderGrid();
}
$('env').onchange = envChanged;

async function scan() {
  const r = await fetch('/api/scan?root=' + encodeURIComponent(ROOT));
  const body = await r.json();
  CHARS = body.characters || [];
  ROOT = body.root || ROOT;
  $('rootPath').textContent = ROOT;
  renderGrid();
}

async function resolveAll() {
  if (!connected() || !CHARS.length) return { ok: true };
  const r = await fetch('/api/resolve', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ env: env(), characters: CHARS.map((c) => c.id) }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) return { ok: false, error: body.error || ('resolve failed (' + r.status + ')') };
  ROWS = {};
  for (const row of body.resolved) ROWS[row.char] = row;
  renderGrid();
  return { ok: true };
}

// ---------------------------------------------------------------------------------------
// Connect

$('getCookie').onclick = (ev) => { ev.preventDefault(); window.open(target().apiBase + '/api/auth/signin', '_blank'); };

/**
 * Connecting means the environment answered, not that a value was typed.
 *
 * Storing a token and reporting success are not the same thing: an expired cookie, or one from
 * another environment, is accepted by the field and refused by the API. Saying "connected" at
 * that point leaves the operator looking at enabled buttons with the refusal buried in the log.
 * So the cookie is proved by asking the environment to resolve, and only a real answer counts.
 */
$('connect').onclick = async () => {
  const v = $('token').value.trim();
  if (!v) return;
  $('connErr').hidden = true;
  $('conn').textContent = 'checking...';

  await fetch('/api/token', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ token: v }),
  });
  CONFIG.hasToken = true;

  const r = await resolveAll();
  if (!r.ok) {
    // Clear it again, so nothing downstream believes this console is connected.
    await fetch('/api/token', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ token: '' }),
    });
    CONFIG.hasToken = false;
    $('conn').textContent = 'not connected';
    $('connErr').textContent = r.error;
    $('connErr').hidden = false;
    refreshButtons();
    return;
  }

  $('token').value = '';
  $('conn').textContent = 'connected to ' + env();
  refreshButtons();
};

// ---------------------------------------------------------------------------------------
// Streaming actions. One character per request so a failure names the character it stopped on.

async function streamTo(endpoint, payload) {
  const res = await fetch(endpoint, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload),
  });
  if (!res.headers.get('content-type')?.startsWith('text/plain')) {
    const b = await res.json().catch(() => ({}));
    log((b.error || 'failed') + '\n');
    return null;
  }
  let out = '';
  const rd = res.body.getReader(); const dec = new TextDecoder();
  for (;;) {
    const t = await rd.read();
    if (t.done) break;
    const s = dec.decode(t.value, { stream: true });
    out += s; log(s);
  }
  return out;
}

const picked = () => CHARS.filter((c) => PICKED.has(c.id));

// ---- phase 1 ----------------------------------------------------------------------------

for (const b of document.querySelectorAll('.body')) {
  b.onclick = () => {
    BODY = b.dataset.body;
    for (const o of document.querySelectorAll('.body')) o.classList.toggle('sel', o === b);
    refreshButtons();
  };
}

$('bake').onclick = async () => {
  setLog('');
  $('bake').disabled = true;
  await streamTo('/api/bake', { asset: ASSET, body: BODY, force: true, capture: true });
  await scan();
  await resolveAll();
};

$('capture').onclick = async () => {
  setLog('');
  for (const c of picked()) await streamTo('/api/capture', { id: c.id });
  await scan();
};

// ---- phase 2 ----------------------------------------------------------------------------

$('pubModel').onclick = async () => {
  const list = picked();
  const e = target();
  if (e.danger && !confirm('Upload ' + list.length + ' to PRODUCTION?')) return;
  const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '') + '-' + String(Date.now()).slice(-4);
  setLog('');
  for (const c of list) {
    log('\n' + c.id + '\n');
    if (!ROWS[c.id] || !ROWS[c.id].avatarId) { log('  no avatar record here yet\n'); break; }
    const out = await streamTo('/api/publish?root=' + encodeURIComponent(ROOT), {
      env: env(), characters: [c.id], bakeHashSuffix: stamp, avatarIds: { [c.id]: ROWS[c.id].avatarId },
    });
    const m = out && out.match(/avatars\/[0-9a-f-]{36}\/[A-Za-z0-9._-]+\/\S+\.glb/);
    if (!m) { log('\nNo published path came back. Stopped.\n'); break; }
    UPLOADED[c.id] = m[0];
    renderGrid();
  }
};

$('pubPreviews').onclick = async () => {
  const list = picked();
  const missing = list.filter((c) => !c.previews);
  if (missing.length) { setLog('No previews captured for: ' + missing.map((c) => c.id).join(', ') + '\n'); return; }
  setLog('');
  for (const c of list) {
    log('\n' + c.id + '\n');
    if (!ROWS[c.id] || !ROWS[c.id].avatarId) { log('  no avatar record here yet\n'); break; }
    const out = await streamTo('/api/publish-previews?root=' + encodeURIComponent(ROOT), {
      env: env(), characters: [c.id], avatarIds: { [c.id]: ROWS[c.id].avatarId },
    });
    if (!out || !/\[publish_previews\] OK/.test(out)) { log('\nStopped.\n'); break; }
  }
  await resolveAll();
};

$('goLive').onclick = async () => {
  const e = target();
  const entries = Object.entries(UPLOADED);
  if (!entries.length) return;
  if (e.danger && !confirm('Take ' + entries.length + ' live on PRODUCTION?')) return;
  setLog('');
  for (const [id, key] of entries) {
    const r = await fetch('/api/go-live', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ env: env(), char: id, avatarId: ROWS[id].avatarId, modelKey: key }),
    });
    const b = await r.json().catch(() => ({}));
    if (!r.ok) { log('\n' + id + ': ' + (b.error || 'failed') + '\nStopped.\n'); break; }
    log('\n' + id + ' is live\n   now ' + b.modelKey + '\n   was ' + (b.previous || '(none)') + '\n');
    if (b.recorded) log('   recorded in ' + b.recorded + '\n');
  }
  await resolveAll();
};

$('verify').onclick = async () => { setLog(''); await streamTo('/api/verify', { env: env() }); };

// ---------------------------------------------------------------------------------------
// Pickers. Two jobs, one dialog: choose the packages folder, or choose a MetaHuman to bake.

let pickMode = 'folder';

async function openPicker(p) {
  const url = pickMode === 'folder'
    ? '/api/browse' + (p ? '?path=' + encodeURIComponent(p) : '')
    : '/api/assets' + (p ? '?path=' + encodeURIComponent(p) : '');
  const d = await (await fetch(url)).json();
  const here = d.path ?? d.here ?? '';
  $('dPath').value = here || 'This PC';
  $('dUp').onclick = () => openPicker(d.parent ?? d.up ?? '');
  $('dUse').hidden = pickMode !== 'folder';
  $('dCancel').textContent = 'Cancel';
  $('dUse').onclick = async () => { ROOT = here; $('dlg').close(); await scan(); await resolveAll(); };

  const list = $('dList');
  list.innerHTML = '';
  const entries = pickMode === 'folder'
    ? (d.dirs || []).map((x) => ({ name: x.name, path: x.path, dir: true,
        tag: x.isCharacter ? 'character' : x.characterCount ? x.characterCount + ' characters' : '' }))
    : (d.entries || []).map((x) => ({ name: x.name, path: (here.replace(/[\\/]$/, '') + '\\' + x.name),
        dir: x.dir, tag: x.dir ? '' : (x.kind || 'unknown') }));

  for (const e of entries) {
    const row = document.createElement('div');
    row.className = 'item';
    const mh = e.tag === 'MetaHumanCharacter';
    row.innerHTML = `<span>${e.dir ? '📁' : mh ? '🎭' : '·'}</span><span>${e.name}</span>` +
                    (e.tag ? `<span class="tag">${e.tag}</span>` : '');
    if (!e.dir && !mh) row.style.opacity = '.55';
    row.onclick = () => {
      if (e.dir) return openPicker(e.path);
      ASSET = e.path;
      $('picked').textContent = 'to bake: ' + e.name.replace(/\.uasset$/i, '');
      $('dlg').close();
      refreshButtons();
    };
    list.append(row);
  }
  $('dlg').showModal();
}

$('browseRoot').onclick = () => { pickMode = 'folder'; openPicker(ROOT); };
$('pickAsset').onclick = () => { pickMode = 'asset'; openPicker(null); };
$('dCancel').onclick = () => $('dlg').close();

boot();
