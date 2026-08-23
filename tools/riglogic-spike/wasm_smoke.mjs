// Verify the library the WORKER will load actually evaluates, before building a worker on it.
//
// The module targets ENVIRONMENT=worker,web — correct for the deliverable — so it has no node
// fetch path and `Module.wasmBinary` is not wired in this emscripten build. `instantiateWasm`
// IS the supported hook, so the real artifact is exercised unmodified; only the loading is
// supplied. The browser will fetch normally.
import fs from 'node:fs';
import createModule from 'file:///s:/Avatars/metahuman/metahuman-to-glb/tools/riglogic-spike/build-wasm/riglogic.mjs';

const BASE = 'S:/Avatars/metahuman/metahuman-to-glb/tools/riglogic-spike/build-wasm';
const bytes = fs.readFileSync(`${BASE}/riglogic.wasm`);

const M = await createModule({
  instantiateWasm(imports, done) {
    WebAssembly.instantiate(bytes, imports).then((r) => done(r.instance));
  },
});

const dna = fs.readFileSync('S:/Avatars/metahuman/_dna/Telly_Head.dna');
const p = M._malloc(dna.length);
M.HEAPU8.set(dna, p);
const rc = M._rl_init(p, dna.length);
M._free(p);
if (rc !== 0) { console.error('rl_init failed:', rc); process.exit(1); }

const nRaw = M._rl_raw_control_count();
const nJoint = M._rl_joint_output_count();
console.log(`raw ${nRaw}  jointRows ${nJoint}  shapes ${M._rl_blend_shape_count()}  LODs ${M._rl_lod_count()}`);

M._rl_set_lod(0);
const names = JSON.parse(fs.readFileSync('C:/tmp/mh/raw_control_names.json', 'utf8'));

// jawOpen alone. The oracle — which is bitwise against this same library natively — reports a
// max joint magnitude of 4.386 for this control, so that number is the expected answer.
const cvPtr = M._malloc(nRaw * 4);
const cv = new Float32Array(M.HEAPF32.buffer, cvPtr, nRaw);
cv.fill(0);
cv[names.indexOf('CTRL_expressions.jawOpen')] = 1.0;
M._rl_set_raw_controls(cvPtr, nRaw);
M._rl_calculate();

const joints = new Float32Array(M.HEAPF32.buffer, M._rl_joint_outputs_ptr(), nJoint);
// Nine attributes per joint: tx ty tz rx ry rz sx sy sz. Translations are cm; rotations are
// DEGREES. Taking a max across all nine compares a distance against an angle and reports the
// angle — which is why a first pass read 22.08 against the oracle's 4.386 and looked like a
// disagreement. Same trap as every other units mismatch on this project, one row wide.
let mxT = 0, mxR = 0, nz = 0;
for (let i = 0; i < nJoint; i += 1) {
  const a = Math.abs(joints[i]);
  if (a > 0) nz += 1;
  const attr = i % 9;
  if (attr < 3) { if (a > mxT) mxT = a; }
  else if (attr < 6) { if (a > mxR) mxR = a; }
}
console.log(`jawOpen -> ${nz} non-zero rows`);
console.log(`  max translation ${mxT.toFixed(4)} cm   (oracle: 4.386)`);
console.log(`  max rotation    ${mxR.toFixed(4)} deg`);
M._free(cvPtr);
