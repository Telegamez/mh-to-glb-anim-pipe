/**
 * Custom gltf-transform commands for compress_companion.mjs (telegamez#3662).
 *
 * Loaded with `gltf-transform --config <this file>`. The CLI merges this over its defaults, so the
 * Draco and meshopt codecs it registers stay available, and the transform below needs nothing but
 * the document API it is handed. That is deliberate: this repo has no package.json, and a config
 * that imported `@gltf-transform/*` itself would resolve against this directory and fail.
 *
 * ## prune-morphs
 *
 * Removes, per mesh, every morph target that moves NONE of that mesh's vertices in the build we
 * publish. "Moves none" means every delta is under half a step of the meshopt quantization that
 * runs afterwards (14-bit positions over the mesh's bounds, 10-bit normals and tangents,
 * gltf-transform's MESHOPT_DEFAULTS), so the published file already stores those targets as
 * zeros. Pruning them is lossless against what ships today.
 *
 * Not exact zero, because that finds nothing: the Draco source carries ~1.2e-7 of noise on the
 * normals of targets that move nothing, and the mouth targets spill micron-scale deltas onto the
 * brows, EyeAO and Hide meshes, which meshopt rounds away anyway.
 *
 * Why it matters: MetaHuman gives every face mesh the full 72-channel set, and three.js uploads
 * every target of a mesh as float32 RGBA texels whether it moves anything or not. On tg-drake the
 * teeth use 18 of 72, each eye 8, lashes/EyeAO/Lacrimal 19, brows 48 of 67: about 84 MB of morph
 * texture down to about 65 MB with no visible change.
 *
 * Safe for the runtime because the face writer looks targets up by NAME per mesh and skips a mesh
 * that lacks one (`writeFaceMorph` in telegamez `motion/writeFaceMorphs.ts`). The names travel in
 * `mesh.extras.targetNames`, which is rewritten here to match, along with `mesh.weights`.
 *
 * A target is kept for a mesh if it is live in ANY of that mesh's primitives: glTF requires every
 * primitive of a mesh to carry the same target list.
 *
 * Prints one `PRUNE <mesh>\t<kept>\t<total>` line per mesh with targets, for the caller's manifest.
 */

// Mirrors gltf-transform's MESHOPT_DEFAULTS (quantizePosition 14, quantizeNormal 10). Signed
// normalized storage: a b-bit value has 2^(b-1) - 1 steps per unit.
const POSITION_BITS = 14;
const NORMAL_BITS = 10;

/** Half the meshopt position step for this mesh: half its largest extent over the int range. */
function positionThreshold(mesh) {
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  const el = [];
  for (const prim of mesh.listPrimitives()) {
    const pos = prim.getAttribute('POSITION');
    for (let i = 0; i < pos.getCount(); i++) {
      pos.getElement(i, el);
      for (let k = 0; k < 3; k++) { min[k] = Math.min(min[k], el[k]); max[k] = Math.max(max[k], el[k]); }
    }
  }
  const halfExtent = Math.max(max[0] - min[0], max[1] - min[1], max[2] - min[2]) / 2;
  return 0.5 * halfExtent / (2 ** (POSITION_BITS - 1) - 1);
}

const NORMAL_THRESHOLD = 0.5 / (2 ** (NORMAL_BITS - 1) - 1);

function isLive(target, posThreshold) {
  for (const semantic of target.listSemantics()) {
    const accessor = target.getAttribute(semantic);
    if (!accessor) continue;
    const limit = semantic === 'POSITION' ? posThreshold : NORMAL_THRESHOLD;
    // getElement denormalises quantized accessors, so the threshold means the same thing in a
    // float file and a quantized one.
    const el = [];
    for (let i = 0; i < accessor.getCount(); i++) {
      accessor.getElement(i, el);
      for (const v of el) if (Math.abs(v) > limit) return true;
    }
  }
  return false;
}

function pruneMorphs() {
  return (document) => {
    // Pass 1: decide, per mesh, which target indices are live.
    const plans = [];
    for (const mesh of document.getRoot().listMeshes()) {
      const prims = mesh.listPrimitives();
      const count = Math.max(0, ...prims.map((p) => p.listTargets().length));
      if (count === 0) continue;
      // A primitive with a different target count is malformed; leave the whole mesh alone.
      if (prims.some((p) => p.listTargets().length !== count)) {
        console.log(`PRUNE ${mesh.getName()}\t${count}\t${count}\tskipped: uneven target lists`);
        continue;
      }
      const posThreshold = positionThreshold(mesh);
      const keep = [];
      for (let t = 0; t < count; t++) {
        if (prims.some((p) => isLive(p.listTargets()[t], posThreshold))) keep.push(t);
      }
      const names = mesh.getExtras()?.targetNames;
      const verts = prims.reduce((acc, p) => acc + p.getAttribute('POSITION').getCount(), 0);
      plans.push({ mesh, prims, count, keep, names: Array.isArray(names) ? names : null, verts });
    }

    // Pass 2: never let a NAME vanish from the model. A channel that moves nothing anywhere
    // (`viseme_sil`, the rest-pose viseme, is all zeros on every mesh) would otherwise be pruned
    // from every mesh, and code that asks the model for it by name (the lipsync probe, the
    // "not found in any mesh" check in writeFaceMorph) would see it disappear. Keep it on the
    // smallest mesh that carried it: on tg-drake that is Lacrimal, 276 vertices, ~9 KB.
    const liveNames = new Set();
    for (const p of plans) if (p.names) for (const t of p.keep) liveNames.add(p.names[t]);
    for (const p of [...plans].sort((x, y) => x.verts - y.verts)) {
      if (!p.names) continue;
      p.names.forEach((n, t) => {
        if (liveNames.has(n)) return;
        p.keep.push(t);
        liveNames.add(n);
      });
      p.keep.sort((x, y) => x - y);
    }

    // Pass 3: apply.
    for (const { mesh, prims, count, keep } of plans) {
      if (keep.length === count) {
        console.log(`PRUNE ${mesh.getName()}\t${count}\t${count}`);
        continue;
      }
      const extras = mesh.getExtras() || {};
      const names = Array.isArray(extras.targetNames) ? extras.targetNames : null;
      const weights = mesh.getWeights() || [];
      for (const prim of prims) {
        const targets = prim.listTargets();
        targets.forEach((target, t) => {
          if (keep.includes(t)) return;
          prim.removeTarget(target);
          // Drop the target and any accessor nothing else uses, or they are still written out.
          const attributes = target.listSemantics().map((s) => target.getAttribute(s));
          target.dispose();
          for (const a of attributes) {
            if (a && a.listParents().every((parent) => parent === document.getRoot())) a.dispose();
          }
        });
      }
      if (names) mesh.setExtras({ ...extras, targetNames: keep.map((t) => names[t]) });
      if (weights.length === count) mesh.setWeights(keep.map((t) => weights[t]));
      console.log(`PRUNE ${mesh.getName()}\t${keep.length}\t${count}`);
    }
  };
}

/**
 * ## strip-morph-normals
 *
 * Removes NORMAL and TANGENT deltas from the morph targets of every mesh whose name matches
 * `pattern`, keeping POSITION. three.js stores one RGBA float32 texel per vertex per morphed
 * attribute, so this halves a mesh's morph texture. NOT lossless: with no normal deltas the
 * shading normal stays at its rest value while the surface moves, so expression creases (lip
 * corners, nasolabial fold, brow furrows) shade flatter. On small meshes (teeth, eyes, lashes)
 * the difference is not expected to be visible; on the head it has to be judged on a real face.
 *
 * Prints one `STRIP <mesh>\t<targets>` line per mesh changed.
 */
function stripMorphNormals(pattern) {
  return (document) => {
    for (const mesh of document.getRoot().listMeshes()) {
      if (!pattern.test(mesh.getName())) continue;
      let changed = 0;
      for (const prim of mesh.listPrimitives()) {
        for (const target of prim.listTargets()) {
          for (const semantic of ['NORMAL', 'TANGENT']) {
            const accessor = target.getAttribute(semantic);
            if (!accessor) continue;
            target.setAttribute(semantic, null);
            if (accessor.listParents().every((parent) => parent === document.getRoot())) accessor.dispose();
            changed++;
          }
        }
      }
      if (changed) console.log(`STRIP ${mesh.getName()}\t${mesh.listPrimitives()[0]?.listTargets().length ?? 0}`);
    }
  };
}

export default {
  onProgramReady: ({ program, io, Session }) => {
    program
      .command('strip-morph-normals', 'Remove NORMAL/TANGENT deltas from morph targets')
      .help('Keeps POSITION. Halves the three.js morph texture of each matched mesh. Lossy on shading.')
      .argument('<input>', 'Path to read glTF 2.0 (.glb, .gltf) model')
      .argument('<output>', 'Path to write output')
      .option('--meshes <regex>', 'Mesh names to strip (regular expression)', { default: '.*' })
      .action(({ args, options, logger }) =>
        Session.create(io, logger, args.input, args.output)
          .transform(stripMorphNormals(new RegExp(String(options.meshes)))));
    program
      .command('prune-morphs', 'Remove morph targets that move none of a mesh\'s vertices')
      .help('Lossless, per mesh. Rewrites extras.targetNames and weights to match.')
      .argument('<input>', 'Path to read glTF 2.0 (.glb, .gltf) model')
      .argument('<output>', 'Path to write output')
      .action(({ args, logger }) =>
        Session.create(io, logger, args.input, args.output).transform(pruneMorphs()));
  },
};
