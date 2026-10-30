import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { Retargeter } from './retarget.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';
import { KTX2Loader } from 'three/addons/loaders/KTX2Loader.js';

const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
};
let toastTimer;
function toast(msg, ms = 3500) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), ms);
}

// ------------------------------------------------------------------ scene
const canvas = $('viewport');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x14171d);
scene.environment = new THREE.PMREMGenerator(renderer).fromScene(new RoomEnvironment(), 0.04).texture;
const camera = new THREE.PerspectiveCamera(30, 1, 0.05, 100);
camera.position.set(0.6, 1.3, 3.6);
const controls = new OrbitControls(camera, canvas);
controls.target.set(0, 1.0, 0);
controls.enableDamping = true;
const key = new THREE.DirectionalLight(0xffffff, 1.6); key.position.set(2, 4, 3); scene.add(key);
scene.add(new THREE.HemisphereLight(0xbcc6d8, 0x2a2420, 0.6));
const grid = new THREE.GridHelper(10, 20, 0x3a404c, 0x252a33); scene.add(grid);

const ghostMat = new THREE.LineBasicMaterial({ color: 0xe8743b });
const ghost = new THREE.LineSegments(new THREE.BufferGeometry(), ghostMat);
ghost.visible = false; scene.add(ghost);

function resize() {
  const r = canvas.parentElement.getBoundingClientRect();
  renderer.setSize(r.width, r.height, false);
  camera.aspect = r.width / r.height; camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(canvas.parentElement);

// Standard glTF materials and decoders. No proprietary material overrides are applied.
const loader = new GLTFLoader();
const draco = new DRACOLoader();
draco.setDecoderPath('https://cdn.jsdelivr.net/npm/three@0.176.0/examples/jsm/libs/draco/');
loader.setDRACOLoader(draco);
loader.setMeshoptDecoder(MeshoptDecoder);
const ktx2 = new KTX2Loader();
ktx2.setTranscoderPath('https://cdn.jsdelivr.net/npm/three@0.176.0/examples/jsm/libs/basis/');
ktx2.detectSupport(renderer);
loader.setKTX2Loader(ktx2);

// ------------------------------------------------------------------ state
const S = {
  soma: null, characters: [], charId: null, char: null, rt: null, mixer: null,
  action: null, clipKind: null, clipMotion: null, playing: true,
  anims: [], selected: null, tab: 'kimodo',
  motionCache: new Map(), clipCache: new Map(), production: [], prodClips: new Map(),
  pack: null, poses: [], state: null,
};
window.studio = { S, THREE, poke: () => import('./poke.js'), prepare: (c) => prepareLikeRuntime(c), camera: () => camera, controls: () => controls, select: (...a) => select(...a), loadCharacter: (...a) => loadCharacter(...a),
  selectState: (...a) => selectState(...a) }; // console / test-driver access

// ------------------------------------------------------------------ character
async function loadCharacter(id) {
  const c = S.characters.find((x) => x.id === id); if (!c) return;
  $('loading').textContent = `Loading ${id} (${c.mb} MB)…`; $('loading').classList.add('show');
  try {
    const gltf = await loader.loadAsync(c.url);
    if (S.char) { scene.remove(S.char); S.mixer?.stopAllAction(); }
    S.char = gltf.scene; S.charId = id; $('character').value = id;
    S.char.traverse((o) => { if (o.isMesh) { o.frustumCulled = false; } });

    scene.add(S.char);
    S.rt = new Retargeter(S.char, S.soma);
    if (S.rt.missing.length) console.info('[studio] unmapped SOMA bones:', S.rt.missing.join(', '));
    S.mixer = new THREE.AnimationMixer(S.char);
    S.clipCache.clear();
    frameCharacter();
    localStorage.setItem('studio.char', id);
    if (S.selected) await playSelection(S.selected);
  } catch (e) { toast(`Character failed: ${e.message}`, 8000); console.error(e); }
  finally { $('loading').classList.remove('show'); }
}

function frameCharacter() {
  if (!S.rt) return;
  const h = S.rt.hipsRestWorld;
  controls.target.set(h.x, h.y * 1.05, h.z);
  camera.position.set(h.x + 0.7, h.y * 1.35, h.z + 3.4);
}

// ------------------------------------------------------------------ playback
async function motionFor(id) {
  if (!S.motionCache.has(id)) S.motionCache.set(id, await api(`/api/animations/${id}/motion`));
  return S.motionCache.get(id);
}

function startAction(clip, kind, motion, label) {
  S.mixer.stopAllAction();
  S.rt.resetPose();
  const a = S.mixer.clipAction(clip);
  a.setLoop($('loopPlay').checked ? THREE.LoopRepeat : THREE.LoopOnce, Infinity);
  a.clampWhenFinished = true;
  a.reset().play();
  a.paused = !S.playing;
  S.twist = null; // Preview does not reproduce Unreal post-process correctives.
  S.action = a; S.clipKind = kind; S.clipMotion = motion;
  $('clipLabel').textContent = label;
  ghost.visible = $('ghost').checked && kind === 'kimodo';
}

async function playSelection(sel) {
  if (!S.rt) return;
  if (sel.kind === 'kimodo') {
    const motion = await motionFor(sel.id);
    const key = `${S.charId}|${sel.id}`;
    if (!S.clipCache.has(key)) S.clipCache.set(key, S.rt.bakeClip(motion, sel.id));
    const meta = S.anims.find((a) => a.id === sel.id);
    if (meta && meta.audit?.character !== S.charId) {
      const au = S.rt.audit(motion);
      if (au) {
        meta.audit = { character: S.charId, ...au };
        api(`/api/animations/${sel.id}`, { method: 'PATCH', body: JSON.stringify({ audit: meta.audit }) }).catch(() => {});
        renderLibrary(); renderStateCard(); renderDetails();
      }
    }
    startAction(S.clipCache.get(key), 'kimodo', motion, `${meta?.name ?? sel.id} · Kimodo`);
  } else {
    const clip = S.prodClips.get(sel.file)?.find((c) => c.name === sel.clip);
    if (clip) startAction(clip, 'production', null, `${sel.clip} · ${sel.file}`);
  }
}

$('play').onclick = () => togglePlay();
function togglePlay() {
  S.playing = !S.playing;
  if (S.action) { if (S.action.paused === false && !S.playing) S.action.paused = true; else if (S.playing) { S.action.paused = false; if (!S.action.isRunning()) S.action.reset().play(); } }
  $('play').textContent = S.playing ? '⏸' : '▶';
}
document.addEventListener('keydown', (e) => {
  if (e.code === 'Space' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) { e.preventDefault(); togglePlay(); }
});
let scrubbing = false;
$('scrub').addEventListener('input', () => {
  if (!S.action) return; scrubbing = true;
  S.action.time = ($('scrub').value / 1000) * S.action.getClip().duration;
  S.action.paused = true; S.mixer.update(0);
});
$('scrub').addEventListener('change', () => { scrubbing = false; if (S.action) S.action.paused = !S.playing; });
$('speed').onchange = () => { if (S.mixer) S.mixer.timeScale = parseFloat($('speed').value); };
$('loopPlay').onchange = () => { if (S.action) S.action.setLoop($('loopPlay').checked ? THREE.LoopRepeat : THREE.LoopOnce, Infinity); };
$('ghost').onchange = () => { ghost.visible = $('ghost').checked && S.clipKind === 'kimodo'; };
$('frame').onclick = frameCharacter;

const clock = new THREE.Clock();
function tick() {
  const dt = clock.getDelta();
  if (S.mixer && !scrubbing) S.mixer.update(dt);
  S.twist?.update(); // after the mixer has posed the skeleton, before render (as the runtime does)
  if (S.action) {
    const d = S.action.getClip().duration || 1;
    const t = S.action.time % (d + 1e-6);
    if (!scrubbing) $('scrub').value = Math.round((t / d) * 1000);
    $('time').textContent = `${t.toFixed(2)} / ${d.toFixed(2)} s`;
    if (ghost.visible && S.clipMotion) {
      const f = Math.min(S.clipMotion.frames - 1, Math.round(t * S.clipMotion.fps));
      const off = new THREE.Vector3(1.0, 0, 0).applyQuaternion(S.rt.F).add(new THREE.Vector3(S.rt.hipsRestWorld.x, 0, S.rt.hipsRestWorld.z));
      ghost.geometry.setFromPoints(S.rt.ghostPoints(S.clipMotion, f, off));
    }
  }
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}

// ------------------------------------------------------------------ library
function fmtAgo(ts) {
  const s = (Date.now() / 1000 - ts); if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`; if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function refreshLibrary(selectId) {
  S.anims = await api('/api/animations');
  if (selectId) S.selected = { kind: 'kimodo', id: selectId };
  renderLibrary();
  renderDetails();
}

function renderLibrary() {
  const q = $('search').value.trim().toLowerCase();
  const el = $('library'); el.innerHTML = '';
  if (S.tab === 'states') {
    if (!S.pack) { el.innerHTML = '<div class="hint" style="padding:12px">Loading pack…</div>'; return; }
    const known = new Set(S.anims.map((a) => a.id));
    let done = 0;
    for (const st of S.pack.states) {
      const cands = st.candidates.filter((id) => known.has(id));
      const ok = st.approved && known.has(st.approved);
      if (ok) done++;
      const d = document.createElement('div');
      d.className = 'item' + (S.state === st.state ? ' sel' : '');
      d.innerHTML = `<div class="name">${esc(st.state)}</div><div class="prompt">${esc(st.intent)}</div>
        <div class="badges">${ok ? '<span class="badge ok">approved</span>' : '<span class="badge miss">no approved take</span>'}
        <span class="badge">${cands.length} candidate${cands.length === 1 ? '' : 's'}</span></div>`;
      d.onclick = () => selectState(st.state);
      el.appendChild(d);
    }
    const neutral = S.pack.neutral_pose ? `neutral: ${S.pack.neutral_pose}` : 'no neutral pose set';
    $('libCount').textContent = `${S.pack.name} · ${done}/${S.pack.states.length} approved · ${neutral}`;
    return;
  }
  if (S.tab === 'kimodo') {
    const list = S.anims.filter((a) => !q || [a.name, a.prompt, ...(a.tags || [])].join(' ').toLowerCase().includes(q))
      .sort((a, b) => (b.favorite - a.favorite) || (b.created - a.created));
    for (const a of list) {
      const d = document.createElement('div');
      d.className = 'item' + (S.selected?.kind === 'kimodo' && S.selected.id === a.id ? ' sel' : '');
      const loop = a.loop !== 'none';
      const seamBad = a.seam_deg > 2;
      const bake = a.bake?.status;
      d.innerHTML = `<div class="name">${a.favorite ? '<span class="star">★</span>' : ''}${esc(a.name)}</div>
        <div class="prompt">${esc(a.prompt)}</div>
        <div class="badges"><span class="badge">${a.duration.toFixed(1)}s</span>
        ${loop ? `<span class="badge loop">loop</span>` : ''}
        <span class="badge ${seamBad ? 'seam-bad' : ''}" title="first vs last frame, max joint">seam ${a.seam_deg.toFixed(2)}°</span>
        ${bake ? `<span class="badge bake">bake: ${bake}</span>` : ''}
        ${auditBadges(a)}
        ${(a.tags || []).map((t) => `<span class="badge">#${esc(t)}</span>`).join('')}
        <span class="badge">${fmtAgo(a.created)}</span></div>`;
      d.onclick = () => select({ kind: 'kimodo', id: a.id });
      el.appendChild(d);
    }
    $('libCount').textContent = `${list.length} of ${S.anims.length} takes`;
    if (!S.anims.length) el.innerHTML = '<div class="hint" style="padding:12px">No takes yet. Write a prompt and press Generate.</div>';
  } else {
    let n = 0;
    for (const p of S.production) {
      const head = document.createElement('div');
      head.className = 'section-title'; head.style.padding = '10px 8px 4px'; head.textContent = p.id;
      el.appendChild(head);
      const clips = S.prodClips.get(p.id);
      if (!clips) {
        const b = document.createElement('button'); b.textContent = `Load clips (${p.id}.glb)`; b.style.margin = '0 8px 8px';
        b.onclick = async () => { b.disabled = true; b.textContent = 'Loading…'; await loadProduction(p); renderLibrary(); };
        el.appendChild(b); continue;
      }
      for (const c of clips.filter((c) => !q || c.name.toLowerCase().includes(q))) {
        n++;
        const d = document.createElement('div');
        d.className = 'item' + (S.selected?.kind === 'production' && S.selected.file === p.id && S.selected.clip === c.name ? ' sel' : '');
        d.innerHTML = `<div class="name">${esc(c.name)}</div><div class="badges"><span class="badge prod">production</span><span class="badge">${c.duration.toFixed(1)}s</span></div>`;
        d.onclick = () => select({ kind: 'production', file: p.id, clip: c.name });
        el.appendChild(d);
      }
    }
    $('libCount').textContent = `${n} production clips loaded`;
  }
}

// Production clips play exactly as on the platform: every shipping surface runs
// prepareCompanionClips(clips, filterEndBoneTracks, stabilizeRootMotion) (Agent3DHead.tsx:828),
// which strips the bake body's non-root bone translation. Playing them raw put another body's
// proportions on the character and pushed skin through cloth that the platform renders cleanly.
export function prepareLikeRuntime(clips) {
  return clips; // Preserve tracks; retargeting between different rest poses is not automatic.
}
async function loadProduction(p) {
  const gltf = await loader.loadAsync(p.url);
  S.prodClips.set(p.id, prepareLikeRuntime(gltf.animations));
}

async function select(sel, keepState = false) {
  S.selected = sel;
  if (!keepState) S.state = null;
  renderLibrary(); renderDetails(); renderStateCard();
  try { await playSelection(sel); } catch (e) { toast(e.message, 6000); console.error(e); }
}

$('search').oninput = renderLibrary;
document.querySelectorAll('.lib-tabs button').forEach((b) => b.onclick = () => {
  S.tab = b.dataset.tab; document.querySelectorAll('.lib-tabs button').forEach((x) => x.classList.toggle('on', x === b));
  renderLibrary(); renderStateCard();
});

// 07 §3 contract badges (measured on the character the take was last played on).
function auditBadges(a) {
  const au = a.audit; if (!au) return '<span class="badge" title="play the take to measure">contract: —</span>';
  const ok = au.pass.handRise && au.pass.handHead && au.pass.duration;
  const tip = `on ${au.character}: maxHandRise ${au.maxHandRise} (≤0.50) · minHandHeadDist ${au.minHandHeadDist} (≥0.30) · ${au.duration}s (1.5–9)`;
  const why = [!au.pass.handRise && 'hand rise', !au.pass.handHead && 'hand↔head', !au.pass.duration && 'duration'].filter(Boolean).join(', ');
  return `<span class="badge ${ok ? 'ok' : 'seam-bad'}" title="${esc(tip)}">${ok ? '07§3 pass' : `07§3: ${why}`}</span>`;
}

// ------------------------------------------------------------------ states (packs)
async function refreshPacks() {
  const packs = await api('/api/packs');
  S.packs = packs;
  // ?pack=<name> opens another pack; the default stays the emotions pack.
  const wanted = S.pack?.name ?? new URLSearchParams(location.search).get('pack') ?? 'telegames-emotions';
  S.pack = packs.find((p) => p.name === wanted) || packs[0];
}
async function refreshPoses() { S.poses = await api('/api/poses'); }
async function savePack() { S.pack = await api(`/api/packs/${S.pack.name}`, { method: 'PUT', body: JSON.stringify(S.pack) }); }
const curState = () => S.pack?.states.find((s) => s.state === S.state);

async function selectState(name) {
  S.state = name;
  const st = curState();
  renderLibrary(); renderStateCard();
  const known = new Set(S.anims.map((a) => a.id));
  const id = (st.approved && known.has(st.approved)) ? st.approved : st.candidates.filter((c) => known.has(c)).at(-1);
  if (id) await select({ kind: 'kimodo', id }, true);
}

function renderStateCard() {
  const st = S.tab === 'states' ? curState() : null;
  $('stateCard').style.display = st ? '' : 'none';
  $('promptCard').style.display = S.tab === 'states' ? 'none' : '';
  $('packCard').style.display = S.tab === 'states' && S.pack ? '' : 'none';
  renderPackCard();
  if (!st) return;
  $('stName').textContent = st.state; $('stIntent').textContent = st.intent;
  if (document.activeElement !== $('stPrompt')) $('stPrompt').value = st.prompt;
  if (document.activeElement !== $('stClip')) $('stClip').value = st.clip || pascal(st.state);
  $('stNeutral').innerHTML = '<option value="">(none: seamless self-loop)</option>' +
    S.poses.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('');
  $('stNeutral').value = S.pack.neutral_pose || '';
  const known = new Map(S.anims.map((a) => [a.id, a]));
  const box = $('stCands'); box.innerHTML = '';
  const cands = st.candidates.filter((id) => known.has(id));
  if (!cands.length) box.innerHTML = '<div class="hint">No candidates yet.</div>';
  for (const id of cands.slice().reverse()) {
    const a = known.get(id);
    const row = document.createElement('div');
    row.className = 'cand' + (st.approved === id ? ' approved' : '') + (S.selected?.id === id ? ' sel' : '');
    row.innerHTML = `<span class="grow">${esc(a.name)} <span class="badge">seam ${a.seam_deg.toFixed(2)}°</span> ${auditBadges(a)}</span>`;
    const ap = document.createElement('button');
    ap.textContent = st.approved === id ? 'Approved' : 'Approve';
    ap.onclick = async (e) => { e.stopPropagation(); st.approved = st.approved === id ? null : id; await savePack(); renderLibrary(); renderStateCard(); };
    row.appendChild(ap);
    row.onclick = () => select({ kind: 'kimodo', id }, true);
    box.appendChild(row);
  }
}

$('stPrompt').onchange = async () => { curState().prompt = $('stPrompt').value.trim(); await savePack(); };
$('stClip').onchange = async () => { curState().clip = $('stClip').value.trim() || pascal(curState().state); await savePack(); renderPackCard(); };
const pascal = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function renderPackCard() {
  if (!S.pack || S.tab !== 'states') return;
  const known = new Set(S.anims.map((a) => a.id));
  const approved = S.pack.states.filter((s) => s.approved && known.has(s.approved));
  const missing = S.pack.states.filter((s) => !s.candidates.some((c) => known.has(c)));
  const ex = S.pack.export;
  $('pkName').textContent = S.pack.name;
  const pick = $('pkPick');
  pick.innerHTML = (S.packs || [S.pack]).map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('');
  pick.value = S.pack.name;
  pick.onchange = () => {
    S.pack = S.packs.find((p) => p.name === pick.value) || S.pack;
    S.state = null;
    renderLibrary(); renderPackCard();
  };
  const rows = [['states', `${approved.length}/${S.pack.states.length} approved · ${missing.length} without candidates`],
    ['neutral', S.pack.neutral_pose || '— (capture one: play production Idle, pause on frame 0, Capture neutral)'],
    ['duration', `${S.pack.duration}s per clip`],
    ['end-pin weight', `${S.pack.constraint_weight ?? 1.0} (2.0 suppressed motion)`]];
  if (ex) rows.push(['export', `${ex.status}${ex.progress ? ` · ${ex.progress}` : ''}`], ['animset', ex.animset || ex.error || '']);
  $('pkKv').innerHTML = rows.map(([k, v]) => `<div>${k}</div><div>${esc(v)}</div>`).join('');
  $('pkGenAll').disabled = !missing.length || S.genAllRunning;
  $('pkGenAll').textContent = S.genAllRunning ? `Generating… ${S.genAllRunning}` : `Generate all missing states (${missing.length})`;
  $('pkExport').disabled = !approved.length || ex?.status === 'running';
  $('pkExport').textContent = ex?.status === 'running' ? 'Baking…' : `Bake & export approved (${approved.length})`;
  $('pkHint').textContent = S.pack.neutral_pose ? '' : 'Without a neutral pose each clip loops on its own first frame; clips will not share the Idle pose.';
}

$('pkGenAll').onclick = async () => {
  const known = new Set(S.anims.map((a) => a.id));
  const todo = S.pack.states.filter((s) => !s.candidates.some((c) => known.has(c)));
  for (const [i, st] of todo.entries()) {
    S.genAllRunning = `${i + 1}/${todo.length} ${st.state}`; renderPackCard();
    try {
      const neutral = S.pack.neutral_pose;
      const created = await api('/api/generate', { method: 'POST', body: JSON.stringify({
        prompt: st.prompt, duration: S.pack.duration || 4, samples: parseInt($('stCount').value, 10) || 3,
        loop: neutral ? 'neutral' : 'self', loop_from: neutral || null, constraint_weight: S.pack.constraint_weight ?? 1.0, name: st.state, tags: [st.state, S.pack.name] }) });
      await refreshPacks();
      S.pack.states.find((s) => s.state === st.state).candidates.push(...created.map((c) => c.id));
      await savePack(); await refreshLibrary();
    } catch (e) { toast(`${st.state} failed: ${e.message.slice(0, 200)}`, 8000); }
  }
  S.genAllRunning = null; renderLibrary(); renderPackCard();
  toast('All missing states generated - review and approve one take per state');
};

$('pkExport').onclick = async () => {
  S.pack = await api(`/api/packs/${S.pack.name}/export`, { method: 'POST' });
  renderPackCard(); toast('Baking approved takes one at a time (≈1.5 min each), then writing the animset', 7000);
};
$('stNeutral').onchange = async () => { S.pack.neutral_pose = $('stNeutral').value || null; await savePack(); renderLibrary(); };
$('stGen').onclick = async () => {
  const st = curState(); if (!st) return;
  st.prompt = $('stPrompt').value.trim(); await savePack();
  const btn = $('stGen'); btn.disabled = true;
  const t0 = performance.now();
  const timer = setInterval(() => { btn.innerHTML = `<span class="spin"></span>Generating ${st.state}… ${((performance.now() - t0) / 1000).toFixed(0)}s`; }, 250);
  try {
    const neutral = S.pack.neutral_pose;
    const created = await api('/api/generate', { method: 'POST', body: JSON.stringify({
      prompt: st.prompt, duration: S.pack.duration || 4, samples: parseInt($('stCount').value, 10) || 3,
      loop: neutral ? 'neutral' : 'self', loop_from: neutral || null, constraint_weight: S.pack.constraint_weight ?? 1.0, name: st.state,
      tags: [st.state, S.pack.name], steps: 100, text_weight: 2.0 }) });
    await refreshPacks();
    const fresh = curState();
    fresh.candidates.push(...created.map((c) => c.id)); await savePack();
    await refreshLibrary();
    if (created.length) await select({ kind: 'kimodo', id: created[0].id }, true);
    toast(`${created.length} ${st.state} candidate(s) in ${((performance.now() - t0) / 1000).toFixed(1)}s`);
  } catch (e) { toast(`Generate failed: ${e.message.slice(0, 300)}`, 9000); }
  finally { clearInterval(timer); btn.disabled = false; btn.textContent = 'Generate candidates'; }
};

$('capture').onclick = async () => {
  if (!S.rt || !S.action) { toast('Play a clip and pause on the frame you want as the neutral'); return; }
  S.playing = true; togglePlay(); // pause
  const pose = S.rt.capturePose();
  const label = S.clipKind === 'production' ? `${S.selected.clip}-${S.selected.file}` : (S.anims.find((a) => a.id === S.selected?.id)?.name || 'pose');
  const name = `${label}-${S.action.time.toFixed(2)}s`;
  const saved = await api('/api/poses', { method: 'POST', body: JSON.stringify({ ...pose, name, source: `${S.charId}: ${label} @ ${S.action.time.toFixed(2)}s` }) });
  await refreshPoses();
  if (S.pack && !S.pack.neutral_pose) { S.pack.neutral_pose = saved.name; await savePack(); }
  renderLibrary(); renderStateCard();
  toast(`Neutral pose "${saved.name}" saved (hips ${saved.hips_y.toFixed(3)} m)`);
};

// ------------------------------------------------------------------ details
let deleteArmed = false;
function renderDetails() {
  const a = S.selected?.kind === 'kimodo' ? S.anims.find((x) => x.id === S.selected.id) : null;
  $('details').style.display = a ? '' : 'none';
  if (!a) return;
  $('dName').value = a.name; $('dTags').value = (a.tags || []).join(', '); $('dNotes').value = a.notes || '';
  $('dFav').textContent = a.favorite ? '★ Favorite' : '☆ Favorite';
  const bake = a.bake || {};
  const rows = [['prompt', a.prompt], ['frames', `${a.frames} @ ${a.fps} fps`], ['seed', a.seed], ['steps / cfg', `${a.steps} / ${a.cfg.join(', ')}`],
    ['loop', a.loop + (a.loop_from ? ` from ${a.loop_from}` : '')], ['seam', `${a.seam_deg}°`], ['generated in', `${a.gen_seconds}s`], ['id', a.id]];
  if (bake.status) rows.push(['bake', bake.status], ['UE asset', bake.ue_asset || bake.take || ''], ['baked GLB', bake.glb || bake.error || '']);
  $('dKv').innerHTML = rows.map(([k, v]) => `<div>${k}</div><div>${esc(v)}</div>`).join('');
  $('dBake').disabled = bake.status === 'running';
  $('dBake').textContent = bake.status === 'running' ? 'Baking…' : 'Bake for production';
  deleteArmed = false; $('dDel').textContent = 'Delete';
}
const selId = () => S.selected?.id;
$('dSave').onclick = async () => {
  await api(`/api/animations/${selId()}`, { method: 'PATCH', body: JSON.stringify({
    name: $('dName').value.trim(), notes: $('dNotes').value,
    tags: $('dTags').value.split(',').map((t) => t.trim()).filter(Boolean) }) });
  await refreshLibrary(); toast('Saved');
};
$('dFav').onclick = async () => {
  const a = S.anims.find((x) => x.id === selId());
  await api(`/api/animations/${selId()}`, { method: 'PATCH', body: JSON.stringify({ favorite: !a.favorite }) });
  await refreshLibrary();
};
$('dDup').onclick = async () => { const m = await api(`/api/animations/${selId()}/duplicate`, { method: 'POST' }); await refreshLibrary(m.id); toast('Duplicated'); };
$('dDel').onclick = async () => {
  if (!deleteArmed) { deleteArmed = true; $('dDel').textContent = 'Click again to delete'; return; }
  await api(`/api/animations/${selId()}`, { method: 'DELETE' });
  S.selected = null; S.mixer?.stopAllAction(); S.rt?.resetPose(); $('clipLabel').textContent = 'no clip';
  await refreshLibrary(); toast('Deleted');
};
$('dRegen').onclick = () => {
  const a = S.anims.find((x) => x.id === selId());
  $('prompt').value = a.prompt; $('duration').value = a.duration; $('steps').value = a.steps;
  $('textw').value = a.cfg[0]; $('loop').value = a.loop === 'pose' ? 'pose' : a.loop; $('prompt').focus();
};
$('dBake').onclick = async () => {
  await api(`/api/animations/${selId()}/bake`, { method: 'POST' });
  await refreshLibrary(); toast('Bake started: headless Unreal retarget + stage 05 evaluated bake. Takes several minutes.', 6000);
};

// ------------------------------------------------------------------ generate
const EXAMPLES = [
  'A person stands, listening attentively and nodding slowly.',
  'A person stands and talks, gesturing with both hands while explaining something.',
  'A person stands thinking, touching their chin, shifting weight.',
  'A person laughs heartily, leaning back slightly.',
  'A person shrugs with both shoulders and opens their palms.',
  'A person waves hello with their right hand.',
];
for (const ex of EXAMPLES) {
  const b = document.createElement('button'); b.textContent = ex.replace(/^A person /, '').slice(0, 34) + '…';
  b.title = ex; b.onclick = () => { $('prompt').value = ex; }; $('examples').appendChild(b);
}

$('generate').onclick = async () => {
  const prompt = $('prompt').value.trim();
  if (!prompt) { toast('Write a prompt first'); return; }
  const loop = $('loop').value;
  if (loop === 'pose' && S.selected?.kind !== 'kimodo') { toast('Select a Kimodo take to borrow its start pose'); return; }
  const body = {
    prompt, duration: parseFloat($('duration').value), samples: parseInt($('samples').value, 10),
    seed: $('seed').value ? parseInt($('seed').value, 10) : null, steps: parseInt($('steps').value, 10),
    text_weight: parseFloat($('textw').value), loop, loop_from: loop === 'pose' ? S.selected.id : null,
    name: $('name').value.trim() || null,
  };
  const btn = $('generate'); btn.disabled = true;
  const t0 = performance.now();
  const timer = setInterval(() => { btn.innerHTML = `<span class="spin"></span>Generating… ${((performance.now() - t0) / 1000).toFixed(0)}s`; }, 250);
  try {
    const created = await api('/api/generate', { method: 'POST', body: JSON.stringify(body) });
    await refreshLibrary();
    if (created.length) await select({ kind: 'kimodo', id: created[0].id });
    toast(`${created.length} take(s) in ${((performance.now() - t0) / 1000).toFixed(1)}s`);
  } catch (e) { toast(`Generate failed: ${e.message.slice(0, 300)}`, 9000); }
  finally { clearInterval(timer); btn.disabled = false; btn.textContent = 'Generate'; }
};
$('prompt').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('generate').click(); });

// ------------------------------------------------------------------ status
async function pollStatus() {
  try {
    const s = await api('/api/status');
    const el = $('status');
    el.textContent = s.status === 'ready' ? `${s.model} ready` : s.status === 'loading' ? 'loading Kimodo…' : 'model error';
    el.className = 'pill ' + (s.status === 'ready' ? 'ok' : s.status === 'loading' ? 'warn' : 'bad');
    el.title = s.error || '';
    $('vram').textContent = s.vram_total_gb ? `VRAM ${(s.vram_total_gb - s.vram_free_gb).toFixed(1)}/${s.vram_total_gb} GB` : '';
    $('generate').disabled = s.status !== 'ready' || $('generate').innerHTML.includes('spin');
    if (S.anims.some((a) => a.bake?.status === 'running')) await refreshLibrary();
    if (S.pack?.export?.status === 'running') { await refreshPacks(); await refreshLibrary(); renderPackCard(); }
  } catch { $('status').textContent = 'server offline'; $('status').className = 'pill bad'; }
  setTimeout(pollStatus, 3000);
}

// ------------------------------------------------------------------ boot
(async function boot() {
  resize(); tick();
  [S.soma, S.characters, S.production] = await Promise.all([api('/api/soma'), api('/api/characters'), api('/api/production')]);
  const sel = $('character');
  sel.innerHTML = S.characters.map((c) => `<option value="${c.id}">${c.id}</option>`).join('');
  const saved = localStorage.getItem('studio.char');
  sel.value = S.characters.some((c) => c.id === saved) ? saved : (S.characters[0]?.id ?? '');
  sel.onchange = () => loadCharacter(sel.value);
  await Promise.all([refreshLibrary(), refreshPacks(), refreshPoses()]);
  renderLibrary();
  pollStatus();
  if (sel.value) await loadCharacter(sel.value);
})();
