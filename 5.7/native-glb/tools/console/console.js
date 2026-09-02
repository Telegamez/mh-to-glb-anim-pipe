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
  checkRoot();
}

/**
 * Warn when the folder being browsed is not the folder `bake` writes into.
 *
 * These are chosen independently -- the browse root is remembered between runs, while bake
 * always writes to the pipeline's packages folder. When they differ the bake succeeds, the
 * grid rescans correctly, and the character simply is not there. Nothing fails, so nothing
 * says anything, and the obvious reading is that the bake broke.
 */
function checkRoot() {
  const pkg = CONFIG && CONFIG.packagesRoot;
  const btn = $('gotoPackages');
  if (!pkg || !btn) return;
  const same = String(ROOT).replace(/[\/]+$/, '').toLowerCase()
             === String(pkg).replace(/[\/]+$/, '').toLowerCase();
  btn.hidden = same;
  if (!same) {
    btn.textContent = 'Bakes go to ' + pkg + ' — show that folder';
    btn.title = 'You are browsing a different folder from the one bake writes into, so a new '
              + 'bake will not appear in this list.';
  }
}

$('gotoPackages').onclick = async () => {
  ROOT = CONFIG.packagesRoot;
  await scan();
  await resolveAll();
};

function toggle(id) {
  if (PICKED.has(id)) PICKED.delete(id); else PICKED.add(id);
  renderGrid();
}

/**
 * Ticked characters the environment has no row for.
 *
 * Only answerable once the environment has spoken: while disconnected ROWS is empty, and an
 * empty ROWS means "not asked yet", never "no record there". Treating the two as the same
 * would offer to create rows that already exist.
 */
const needRecord = () => (connected() ? picked().filter((c) => ROWS[c.id] && !ROWS[c.id].avatarId) : []);

function refreshButtons() {
  const n = PICKED.size;
  $('count').textContent = n ? n + ' selected' : '';
  const on = connected() && n > 0;
  const missing = needRecord();
  $('mkRecord').disabled = missing.length === 0;
  $('mkRecord').textContent = missing.length > 1 ? `Create ${missing.length} records` : 'Create record';
  $('pubModel').disabled = !on;
  $('pubPreviews').disabled = !on;
  $('goLive').disabled = !on || !Object.keys(UPLOADED).length;
  $('verify').disabled = !connected();
  $('bake').disabled = !(ASSET && BODY);
  $('capture').disabled = n === 0;
  $('del').hidden = n === 0;
  $('del').textContent = n > 1 ? `Delete ${n}` : 'Delete';
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
  const custom = ($('charName').value || '').trim();
  // The id is the hair-calibration key: the runtime derives it from the model filename and
  // looks it up in HAIR_PROFILES. A name with no entry renders with default hair -- straw
  // instead of a tuned groom, and nothing anywhere reports it. So say so before baking
  // rather than leaving it to be noticed in a render.
  const TUNED = ['telly','tele','telie','telee','tele1','tele2'];
  const derived = (ASSET.split(/[\/]/).pop() || '').replace(/\.uasset$/i,'').toLowerCase();
  if (custom && custom.toLowerCase() !== derived && TUNED.includes(derived)
      && !TUNED.includes(custom.toLowerCase())) {
    if (!confirm(
        'Naming this "' + custom + '" gives it a new character id.\n\n'
      + '"' + derived + '" has tuned hair calibration; "' + custom + '" does not, so this '
      + 'bake will render with DEFAULT hair.\n\nBake anyway?')) return;
  }
  await streamTo('/api/bake', { asset: ASSET, body: BODY, force: true, capture: true,
                                ...(custom ? { id: custom } : {}) });
  await scan();
  await resolveAll();
};

$('capture').onclick = async () => {
  setLog('');
  for (const c of picked()) await streamTo('/api/capture', { id: c.id });
  await scan();
};

/** Bytes as the operator would say them, so a delete reports a size and not a number. */
const size = (b) => (b >= 1073741824 ? (b / 1073741824).toFixed(2) + ' GB' : Math.round(b / 1048576) + ' MB');

/**
 * Delete is local and total: all three directories, so a re-bake starts from nothing rather
 * than on top of the last one.
 *
 * The prompt names what goes and what stays. A Delete button beside a row showing a green Live
 * dot reads like it takes the character down, and it does not - the bytes are on the CDN and
 * the environment's row still points at them. An operator deleting in order to undo a publish
 * has to be told that here, because nothing further along will tell them.
 */
$('del').onclick = async () => {
  const list = picked();
  if (!list.length) return;
  const names = list.map((c) => c.id).join(', ');
  const lines = [
    list.length === 1 ? `Delete ${names}?` : `Delete these ${list.length} characters?\n${names}`,
    '',
    'Removes the finished package, the pipeline working copy and the viewer copy from this machine.',
    'The MetaHuman in Unreal is untouched, so it can be baked again.',
  ];
  // Only ever said when the environment itself reported it: steps() gives 'unknown', not 'ok',
  // while disconnected, so a missing answer never becomes a reassurance either way.
  const live = list.filter((c) => steps(c).live === 'ok').map((c) => c.id);
  if (live.length) {
    lines.push('', `Still live on ${env()}: ${live.join(', ')}.`,
      'Deleting these files does not take them down or unpublish them.');
  }
  if (!confirm(lines.join('\n'))) return;

  setLog('');
  const r = await fetch('/api/delete?root=' + encodeURIComponent(ROOT), {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ ids: list.map((c) => c.id) }),
  });
  const b = await r.json().catch(() => ({}));
  // A failure part-way still reports what it removed, so the log says where it got to.
  for (const x of b.removed || []) log(`removed ${x.what.padEnd(12)} ${x.dir}  (${size(x.bytes)})\n`);
  if (!r.ok) log('\n' + (b.error || 'delete failed') + '\n');
  else log(`\n${b.removed.length} folder(s) removed, ${size(b.freed)} freed\n`);
  PICKED.clear();
  await scan();
  await resolveAll();
};

// ---- phase 2 ----------------------------------------------------------------------------

/**
 * Create the avatar row a publish needs, for a character the environment has never seen.
 *
 * Upload refuses without one - it has nowhere to put the bytes and will not guess - which is
 * the wall a seventh character hits after six were made for you by hand.
 *
 * What this makes is the AVATAR row, not the companion. The row starts at status 'processing'
 * with placeholder preview URLs; Upload fills the model and Go live's SQL sets it active. The
 * companion that the picker lists is a separate record needing a voice, and still an engineer's
 * job - so this unblocks publishing, it does not finish a character.
 */
$('mkRecord').onclick = async () => {
  const missing = needRecord();
  if (!missing.length) return;
  const e = target();
  if (e.danger && !confirm(`Create ${missing.length} avatar record(s) on PRODUCTION?`)) return;

  setLog('');
  for (const c of missing) {
    // Refused rather than defaulted, for the reason the bake refuses it: a missing body ships
    // silently as female (telegamez#3285), and this row is what the product reads.
    if (c.body !== 'female' && c.body !== 'male') {
      log(`${c.id}: no body recorded in its manifest - cannot create a record without it\n`);
      continue;
    }
    const suggested = c.id.charAt(0).toUpperCase() + c.id.slice(1);
    const name = prompt(`Display name for ${c.id} - what the picker will show:`, suggested);
    if (name === null) { log(`${c.id}: cancelled\n`); continue; }
    if (!name.trim()) { log(`${c.id}: no name given - skipped\n`); continue; }

    const r = await fetch('/api/create-avatar', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ env: env(), char: c.id, name: name.trim(), gender: c.body }),
    });
    const b = await r.json().catch(() => ({}));
    if (!r.ok) { log(`${c.id}: ${b.error || 'create failed'}\nStopped.\n`); break; }
    // Created but unidentifiable: resolve below still finds it by slug, so nothing is lost -
    // but stop rather than make more rows through a route answering in a shape we do not know.
    if (!b.avatarId) { log(`${c.id}: created, but no id came back. Stopped - check ${env()}.\n`); break; }
    log(`${c.id}: avatar ${b.avatarId}  slug mh-${c.id}  ${c.body}\n`);
  }
  // The Upload column is read from the environment, so it has to be asked again.
  await resolveAll();
  log(`\nRecords start as processing with placeholder previews. Upload model next; Go live sets them active.\n`);
};

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
