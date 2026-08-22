/**
 * Slice 5.2b — what does three.js charge, per frame, to drive an 870-joint face skin?
 *
 *     node skinning_bench.mjs [frames]
 *
 * 5.1 established that RigLogic itself is cheap (0.29 ms in wasm). The cost most likely to
 * actually bite is downstream of it: rung 2 hands over 870 joint transforms every frame, and
 * three.js has to turn those into the `boneMatrices` array the skinning shader samples.
 *
 * Three CPU stages, all measurable without a GPU:
 *
 *   1. apply     write 870 transforms onto Bone.position/quaternion/scale
 *   2. world     updateMatrixWorld over the 870-node hierarchy (compose + parent multiply)
 *   3. skeleton  Skeleton.update() — boneMatrix = matrixWorld x boneInverse, 16 floats each
 *
 * What is NOT here: the GPU upload of the bone texture and the vertex shader's own skinning
 * work. Those need a real context and belong in the harness. This is the CPU floor, and the
 * point of measuring it separately is that it is the part a worker cannot take off the main
 * thread — stages 2 and 3 touch three.js objects that live on the render thread.
 *
 * That last sentence is the whole reason this number matters. The evaluator's 0.29 ms is
 * parallel; this is serial with rendering.
 */
import { performance } from 'node:perf_hooks';

const three = await import(
  'file:///s:/Avatars/metahuman/telegamez/node_modules/.pnpm/three@0.176.0/node_modules/three/build/three.module.js'
);
const { Bone, Skeleton, Matrix4, Quaternion, Vector3 } = three;

const JOINTS = Number(process.env.TG_JOINTS ?? 870);
const FRAMES = Number(process.argv[2] ?? 2000);

// A hierarchy shaped like a face rig rather than a flat list: MetaHuman's facial joints hang
// off a head root in shallow chains, and updateMatrixWorld's cost depends on depth because
// each node multiplies by its parent. A flat array would understate stage 2.
const root = new Bone();
const bones = [root];
for (let i = 1; i < JOINTS; i++) {
  const b = new Bone();
  // Fan out with a shallow depth, ~4-6 levels, which is what the face rig looks like.
  const parent = bones[Math.max(0, i - 1 - (i % 7))];
  parent.add(b);
  b.position.set(0.01, 0.02, 0.003);
  bones.push(b);
}

const inverses = bones.map((b) => {
  root.updateMatrixWorld(true);
  return new Matrix4().copy(b.matrixWorld).invert();
});
const skeleton = new Skeleton(bones, inverses);

// Stand-in for RigLogic's output: 870 x 10 floats, [tx ty tz, qx qy qz qw, sx sy sz] — the
// exact shape export_pose_transforms.py dumps and getJointOutputs() produces.
const OUT = new Float32Array(JOINTS * 10);
for (let i = 0; i < JOINTS; i++) {
  const o = i * 10;
  OUT[o] = 0.01; OUT[o + 1] = 0.02; OUT[o + 2] = 0.003;
  OUT[o + 3] = 0; OUT[o + 4] = 0; OUT[o + 5] = 0; OUT[o + 6] = 1;
  OUT[o + 7] = 1; OUT[o + 8] = 1; OUT[o + 9] = 1;
}

const q = new Quaternion();
const v = new Vector3();

function applyTransforms(frame) {
  // A small per-frame perturbation so nothing is constant-folded and the matrices genuinely
  // change, which is what forces the work.
  const j = 1 + (frame % 3) * 1e-4;
  for (let i = 0; i < JOINTS; i++) {
    const o = i * 10;
    const b = bones[i];
    b.position.set(OUT[o] * j, OUT[o + 1] * j, OUT[o + 2] * j);
    b.quaternion.set(OUT[o + 3], OUT[o + 4], OUT[o + 5], OUT[o + 6]);
    b.scale.set(OUT[o + 7], OUT[o + 8], OUT[o + 9]);
  }
}

function time(label, fn, frames) {
  fn(0);
  const t0 = performance.now();
  for (let f = 0; f < frames; f++) fn(f);
  const dt = performance.now() - t0;
  return { label, perFrameMs: dt / frames };
}

const stages = [
  time('1 apply transforms', (f) => applyTransforms(f), FRAMES),
  time('2 updateMatrixWorld', (f) => { root.matrixWorldNeedsUpdate = true; root.updateMatrixWorld(true); }, FRAMES),
  time('3 Skeleton.update', () => skeleton.update(), FRAMES),
  time('all three, in order', (f) => {
    applyTransforms(f);
    root.updateMatrixWorld(true);
    skeleton.update();
  }, FRAMES),
];

console.log(`three.js ${three.REVISION}  ${JOINTS} joints  ${FRAMES} frames\n`);
console.log('stage                  per frame    % of 16.67ms');
console.log('---------------------------------------------------');
for (const s of stages) {
  console.log(`${s.label.padEnd(22)} ${s.perFrameMs.toFixed(4)} ms   ` +
              `${(s.perFrameMs / (1000 / 60) * 100).toFixed(2)}%`);
}

const total = stages[stages.length - 1].perFrameMs;
console.log(`\nCPU only — no GPU upload, no vertex shader. This is the part a worker CANNOT`);
console.log(`take off the main thread: stages 2 and 3 touch three.js objects on the render`);
console.log(`thread, so unlike the evaluator's 0.29 ms this is SERIAL with rendering.`);
console.log(`\nbudget so far: 0.29 (wasm eval, parallel) + 0.04 (transfer) + ` +
            `${total.toFixed(2)} (skinning, serial) ms`);
