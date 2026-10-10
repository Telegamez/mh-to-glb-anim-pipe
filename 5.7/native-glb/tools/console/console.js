/**
 * The bake console: one grid, one phase.
 *
 * The grid is the design. Everything else is a button that fills one of its columns, and it is
 * on screen the whole time — so "where am I" is answered by looking rather than by remembering
 * which tool was run last. Every column is read from the packages folder on this machine.
 *
 * Publishing is not here. A finished package goes to an environment through the Publisher
 * (Start-Publisher.ps1, tools/deploy), by the character id in its character.json, and go-live
 * and version removal live there too (telegamez#4632 phase 2). This page used to carry a copy
 * of that flow against the platform's avatar routes; those routes are retired and the copy
 * went with them, so nothing here needs a token or reaches an environment.
 *
 * Every row carries its own thumbnail. A tick tells you a step ran; the picture tells you it
 * ran on the character you meant.
 */
const $ = (id) => document.getElementById(id);
const log = (s) => { $('log').textContent += s; $('log').scrollTop = 1e9; };
const setLog = (s) => { $('log').textContent = s; };

let CONFIG = null;
let CHARS = [];          // packages on disk
let PICKED = new Set();  // ticked characters
let ASSET = null;        // .uasset chosen for a bake
let BODY = null;         // female | male
let ROOT = '';

/** The one build (owner decision 2026-10-08); the server reports it and refuses any other. */
const BUILD = 'ktx2';

// ---------------------------------------------------------------------------------------
// State: one row per character, one cell per step. All three are read from the folder.

function steps(c) {
  return {
    bake: c.ready ? 'ok' : 'warn',
    previews: c.previews ? 'ok' : 'no',
    // telegamez#3728/#3662: the bake makes the ktx2 build and the Publisher remakes a missing
    // or stale one, so neither blocks anything. 'warn' means "the Publisher will compress
    // first"; 'no' is a package the compressor cannot help because it is not finished.
    size: !c.compressed ? (c.ready ? 'warn' : 'no')
      : (c.compressed.stale || !c.compressed.publishable || c.compressed.mode !== 'ktx2') ? 'warn'
        : 'ok',
  };
}

const cell = (state) => `<span class="dot ${state}"></span>`;

/**
 * The download, before and after — both measured the same way.
 *
 * Both numbers are the whole payload: model, sidecar and clothing textures. The textures are
 * roughly half of it and are fetched at runtime beside the model, so a model-only figure would
 * understate the companion and, compared against a compressed build, would flatter the saving.
 *
 * A stale build says so instead of showing its size. The number would be true and the
 * conclusion false: it is the size of a build made from content that has since changed.
 *
 * Only the ktx2 build is shown: it is the only build (owner decision 2026-10-08). Its figure is
 * per browser visit (room model, sidecar, .ktx2 textures); the compositor's PNG copy beside it
 * is not something a browser downloads.
 */
function sizeLine(c) {
  const k = c.compressed;
  if (!k) return `${c.payloadMb} MB · compresses on publish`;
  if (k.stale) return `${c.payloadMb} MB · ktx2 build is STALE — remade on publish`;
  if (k.mode !== 'ktx2') return `${c.payloadMb} MB · ${k.mode} build — remade as ktx2 on publish`;
  return `${c.payloadMb} → ${k.mb} MB · ktx2${k.publishable ? '' : ' (no compositor variant — remade on publish)'}`;
}

function renderGrid() {
  const body = $('grid');
  body.innerHTML = '';
  if (!CHARS.length) {
    body.innerHTML = '<tr><td colspan="5" class="sub">no characters in this folder yet — bake one below</td></tr>';
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
        <div><div class="who">${c.id}</div><div class="sub">${sizeLine(c)}${c.body ? ' · ' + c.body : ''}</div></div>
      </div>`;

    tr.append(tick, who);
    for (const k of ['bake', 'previews', 'size']) {
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
};

function toggle(id) {
  if (PICKED.has(id)) PICKED.delete(id); else PICKED.add(id);
  renderGrid();
}

function refreshButtons() {
  const n = PICKED.size;
  $('count').textContent = n ? n + ' selected' : '';
  $('bake').disabled = !(ASSET && BODY);
  $('capture').disabled = n === 0;
  $('del').hidden = n === 0;
  $('del').textContent = n > 1 ? `Delete ${n}` : 'Delete';

  // Compress reads the finished package, so it needs a finished one. A character still
  // missing its sidecar has nothing worth compressing and would produce a build that looks
  // publishable and renders untextured.
  const sel = picked();
  $('compress').disabled = n === 0 || !sel.every((c) => c.ready) || !(CONFIG && CONFIG.compressorFound);
  $('compress').textContent = n > 1 ? `Compress ${n}` : 'Compress';

  // telegamez#3728: there is no choice of source any more, and since 2026-10-08 no choice of
  // build either. The Publisher uploads the ktx2 build and makes it first when it is missing,
  // stale or without its compositor variant; said out loud rather than left in a tooltip.
  const held = sel.map((c) => c.compressed).filter((k) => k && !k.publishable);
  const note = $('compNote');
  if (!held.length) {
    note.hidden = true;
  } else {
    note.hidden = false;
    note.textContent = 'A ktx2 build without its compositor variant would leave the compositor '
      + 'on the KTX2 room model, which it cannot decode. The Publisher remakes it.';
  }
}

// ---------------------------------------------------------------------------------------
// Detail: is this the character you meant?

function showDetail(c) {
  const q = `char=${encodeURIComponent(c.id)}&root=${encodeURIComponent(ROOT)}`;
  const dlg = $('dlg');
  dlg.querySelector('.dhead').innerHTML = `<b>${c.id}</b><span class="sub">${sizeLine(c)}${c.body ? ' · ' + c.body : ''}</span>`;
  $('dList').innerHTML = c.previews
    ? `<div style="padding:12px;display:flex;gap:14px;align-items:flex-start">
         <img src="/api/asset?${q}&file=imagePreview.jpg" style="width:250px;border-radius:6px" alt="">
         <video src="/api/asset?${q}&file=videoPreview.mp4" style="width:250px;border-radius:6px"
                autoplay loop muted playsinline></video>
       </div>
       <div class="sub" style="padding:0 12px 12px">${c.dir}</div>`
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
  $('stackNote').hidden = CONFIG.stackUp !== false;
  if (CONFIG.stackUp === false) {
    $('stackNote').textContent =
      'The local stack is not running, so previews cannot be captured. The model still bakes correctly.';
  }
  await scan();
}

async function scan() {
  const r = await fetch('/api/scan?root=' + encodeURIComponent(ROOT));
  const body = await r.json();
  CHARS = body.characters || [];
  ROOT = body.root || ROOT;
  $('rootPath').textContent = ROOT;
  renderGrid();
}

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

// ---- make a package ----------------------------------------------------------------------

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
};

$('capture').onclick = async () => {
  setLog('');
  for (const c of picked()) await streamTo('/api/capture', { id: c.id });
  await scan();
};

/**
 * Compress the ticked characters — #3662.
 *
 * Writes the ktx2 build into `_compressed_ktx2/` beside the packages and leaves the original
 * alone, so the bake stays the thing that can always be rebuilt from and this is never a
 * change to the character.
 *
 * Always ktx2: the only build since the owner decision of 2026-10-08. meshopt, `none` and
 * `full` were removed so there is one build end to end.
 */
$('compress').onclick = async () => {
  setLog('');
  $('compress').disabled = true;
  for (const c of picked()) {
    log('\n' + c.id + '\n');
    const out = await streamTo('/api/compress?root=' + encodeURIComponent(ROOT),
      { characters: [c.id], mode: BUILD });
    if (!out || !/\[console\] exit 0\b/.test(out)) { log('\nStopped.\n'); break; }
  }
  await scan();
};

/** Bytes as the operator would say them, so a delete reports a size and not a number. */
const size = (b) => (b >= 1073741824 ? (b / 1073741824).toFixed(2) + ' GB' : Math.round(b / 1048576) + ' MB');

/**
 * Delete is local and total: all three directories, so a re-bake starts from nothing rather
 * than on top of the last one.
 *
 * The prompt names what goes and what stays. This page cannot see an environment, so it says
 * every time that a version already published stays published: an operator deleting in order
 * to undo a publish has to be told that here, because nothing further along will tell them.
 * Taking a version down is the Publisher's Remove old versions, or the platform's own admin.
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
    '',
    'A version already published to panda or beta stays there: deleting these files does not',
    'take it down or unpublish it. That is the Publisher\'s job.',
  ];
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
};

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
  $('dUse').onclick = async () => { ROOT = here; $('dlg').close(); await scan(); };

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
