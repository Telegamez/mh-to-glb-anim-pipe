/**
 * Slice 5.2b — how expensive is it to hand 870 joint matrices from a worker to the renderer?
 *
 *     node transfer_bench.mjs [frames]
 *
 * RigLogic evaluates in a worker (5.1: 0.29 ms/frame in wasm). The result has to reach
 * three.js on the main thread every frame. Three ways to do that, and the choice is an
 * architecture decision rather than a micro-optimisation, because one of them is gated on a
 * deployment change:
 *
 *   1. postMessage(copy)       structured clone — the buffer is duplicated
 *   2. postMessage(transfer)   ownership moves, zero copy, sender loses the buffer
 *   3. SharedArrayBuffer       both threads address the same memory, no message payload
 *
 * ## The finding that decides it, before any number below
 *
 * **SharedArrayBuffer is not available to this app.** It requires cross-origin isolation, and
 * as of this measurement `packages/web-next` serves no COOP/COEP headers: the only definitions
 * in the repo sit in `src/lib/security-headers.ts`, whose `applySecurityHeaders` has **zero
 * callers**, and `next.config.ts` sets Referrer-Policy, Permissions-Policy and Cache-Control
 * but neither cross-origin header.
 *
 * Enabling it is not a config toggle. `Cross-Origin-Embedder-Policy: require-corp` blocks every
 * cross-origin subresource that does not opt in with CORP/CORS — the CDN assets, embedded
 * players, anything third-party. That is a product-wide change with its own blast radius, and
 * it should be justified by a measured need rather than assumed into the design.
 *
 * So this benchmark exists to answer: **is option 2 good enough that we never have to ask?**
 *
 * ## What is being moved
 *
 * three.js uploads skinning as `Skeleton.boneMatrices`, a Float32Array of 16 floats per bone.
 * At 870 joints that is 13,920 floats = 55,680 bytes per frame, 3.3 MB/s at 60 Hz.
 *
 * ## What this measurement is and is not
 *
 * Node's `worker_threads` over the same V8 structured-clone implementation the browser uses,
 * so the relative costs carry. It is NOT a browser measurement: it excludes the renderer's own
 * work, and the absolute numbers will differ. The comparison between the three modes is the
 * durable part; treat the absolutes as indicative until the harness runs it.
 */
import { Worker, isMainThread, parentPort, workerData } from 'node:worker_threads';
import { performance } from 'node:perf_hooks';

const JOINTS = 870;
const FLOATS = JOINTS * 16;
const BYTES = FLOATS * 4;

if (!isMainThread) {
  const { mode, frames, sab } = workerData;
  const shared = sab ? new Float32Array(sab) : null;

  // A reusable scratch buffer for the copy mode: allocating per frame would measure the
  // allocator rather than the transfer, and a real evaluator writes into RigLogic's own
  // output buffer anyway.
  let scratch = new Float32Array(FLOATS);

  parentPort.on('message', (msg) => {
    if (msg?.type === 'return' && msg.buffer) {
      // Transfer mode hands the buffer back so the worker can reuse it. Without this the
      // worker allocates 3.3 MB/s of garbage, which is a real cost and belongs in the number.
      scratch = new Float32Array(msg.buffer);
      return;
    }
    if (msg?.type !== 'go') return;

    for (let f = 0; f < frames; f++) {
      // Stand in for the evaluator writing its output. Cheap, but not free, and identical
      // across modes so it cancels in the comparison.
      scratch[0] = f;
      scratch[FLOATS - 1] = f;

      if (mode === 'sab') {
        shared.set(scratch);
        parentPort.postMessage({ frame: f });
      } else if (mode === 'transfer') {
        const out = scratch;
        scratch = new Float32Array(FLOATS);   // sender loses ownership; needs a fresh one
        parentPort.postMessage({ frame: f, buffer: out.buffer }, [out.buffer]);
      } else {
        parentPort.postMessage({ frame: f, data: scratch });
      }
    }
    parentPort.postMessage({ type: 'done' });
  });
} else {
  const frames = Number(process.argv[2] ?? 3000);

  const run = (mode) => new Promise((resolve) => {
    const sab = mode === 'sab'
      ? (typeof SharedArrayBuffer === 'undefined' ? null : new SharedArrayBuffer(BYTES))
      : null;
    if (mode === 'sab' && !sab) return resolve(null);

    const view = sab ? new Float32Array(sab) : null;
    const worker = new Worker(new URL(import.meta.url), { workerData: { mode, frames, sab } });

    let received = 0;
    let sink = 0;
    let t0 = 0;

    worker.on('message', (msg) => {
      if (msg?.type === 'done') {
        const dt = performance.now() - t0;
        worker.terminate();
        return resolve({
          mode,
          frames: received,
          totalMs: dt,
          perFrameMs: dt / received,
          mbPerSec: (received * BYTES) / 1e6 / (dt / 1000),
          sink,
        });
      }
      // Touch the payload the way the renderer would, so a mode is not flattered by the
      // main thread never reading what it was sent.
      if (msg.data) sink += msg.data[0] + msg.data[FLOATS - 1];
      else if (msg.buffer) {
        const f32 = new Float32Array(msg.buffer);
        sink += f32[0] + f32[FLOATS - 1];
        worker.postMessage({ type: 'return', buffer: msg.buffer }, [msg.buffer]);
      } else if (view) sink += view[0] + view[FLOATS - 1];
      received++;
    });

    t0 = performance.now();
    worker.postMessage({ type: 'go' });
  });

  console.log(`${JOINTS} joints x 16 floats = ${BYTES.toLocaleString()} bytes/frame`);
  console.log(`${frames} frames per mode\n`);
  console.log('mode              per frame     at 60Hz      throughput   % of 16.67ms');
  console.log('----------------------------------------------------------------------');
  for (const mode of ['copy', 'transfer', 'sab']) {
    const r = await run(mode);
    if (!r) { console.log(`${mode.padEnd(16)} SharedArrayBuffer unavailable in this runtime`); continue; }
    console.log(
      `${r.mode.padEnd(16)} ${r.perFrameMs.toFixed(4)} ms   ` +
      `${(r.perFrameMs * 60).toFixed(2)} ms/s   ` +
      `${r.mbPerSec.toFixed(0).padStart(5)} MB/s   ` +
      `${(r.perFrameMs / (1000 / 60) * 100).toFixed(2)}%`);
  }
  console.log('\nNode worker_threads, same V8 structured clone as the browser.');
  console.log('Relative costs carry; absolutes are indicative until measured in the harness.');
}
