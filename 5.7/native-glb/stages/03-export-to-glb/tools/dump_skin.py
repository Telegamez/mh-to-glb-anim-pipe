"""Stage 03, step 1 of the weight solve — dump the FULL skin from the stage 02 blend.

## Why a dump-solve-apply round trip

The remaining deformation error is the 4-influence limit, and nothing else. Everything else has
been chased down: the animation now carries Unreal's evaluated pose so the correctives actually
move (§6.18), the skeletons are rest-identical to 0.05 deg, and the bones do not change length.
What is left is that glTF and three.js allow four bone influences per vertex while MetaHuman's
weight map wants twelve, and the exporter's "keep the largest four" discards a median 21.9% of
each garment vertex's weight.

The fix is to stop TRUNCATING and start SOLVING: find the four influences that best reproduce
the twelve-influence deformation across a range of motion. That is skinning decomposition, and
it is the standard way a film rig is brought to a four-influence engine target.

It needs scipy, which Blender does not ship, so the work is split:

    dump_skin.py   (Blender)  this file — full weights + bind skeleton -> .npz
    solve_skin.py  (Python)   the least-squares solve            .npz -> .npz
    export_glb.py  (Blender)  applies the solved weights instead of folding and smoothing

## What ground truth is here, precisely

NOT "what Unreal renders". The runtime cannot reproduce that: it drops non-root translation
because those values carry the bake body's proportions (§6.18), and no re-weighting recovers it.

Ground truth is **the twelve-influence deformation as the runtime would produce it** — clip
rotations and scale, the character's own bind translations, all 342 bones. That is achievable by
construction, it isolates exactly the truncation loss, and it is the largest remaining error.
Aiming at anything the runtime cannot reach would make the solve unfalsifiable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import bpy  # type: ignore
import numpy as np  # type: ignore


def _log(msg):
    print(f"[dump_skin] {msg}", flush=True)


def _parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    return parser.parse_args(argv)


def main():
    args = _parse_args()

    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if len(armatures) != 1:
        # Stage 02 joins everything into one keeper armature (§6.12). More than one here means
        # that join regressed, and a solve against the wrong skeleton would be silently wrong.
        raise RuntimeError(f"expected exactly 1 armature, found {len(armatures)}")
    arm = armatures[0]

    bone_names = [b.name for b in arm.data.bones]
    bone_index = {n: i for i, n in enumerate(bone_names)}
    parents = np.array(
        [bone_index.get(b.parent.name, -1) if b.parent else -1 for b in arm.data.bones],
        dtype=np.int32,
    )
    # matrix_local is the bone's BIND transform in armature space — the inverse bind matrix is
    # its inverse, and the runtime's joint matrix is world(t) @ inverse(bind).
    bind = np.stack([np.array(b.matrix_local, dtype=np.float64) for b in arm.data.bones])

    payload = {
        "bone_names": np.array(bone_names, dtype=object),
        "bone_parents": parents,
        "bone_bind": bind,
        "arm_matrix_world": np.array(arm.matrix_world, dtype=np.float64),
    }
    meshes = []

    for obj in bpy.data.objects:
        if obj.type != "MESH" or not obj.vertex_groups:
            continue
        if not any(m.type == "ARMATURE" for m in obj.modifiers):
            continue
        group_name = {vg.index: vg.name for vg in obj.vertex_groups}
        verts = obj.data.vertices
        positions = np.empty((len(verts), 3), dtype=np.float64)

        # Ragged weights, flattened: offsets[i]..offsets[i+1] indexes into idx/wgt.
        offsets = np.zeros(len(verts) + 1, dtype=np.int64)
        idx_list: list[int] = []
        wgt_list: list[float] = []
        for v in verts:
            positions[v.index] = (v.co.x, v.co.y, v.co.z)
            for g in v.groups:
                name = group_name.get(g.group)
                bi = bone_index.get(name) if name else None
                if bi is None or g.weight <= 0.0:
                    continue
                idx_list.append(bi)
                wgt_list.append(g.weight)
            offsets[v.index + 1] = len(idx_list)
        # offsets was filled per-vertex in index order; make it a true prefix sum in case the
        # iteration order ever stops matching v.index.
        offsets = np.maximum.accumulate(offsets)

        # Bind vertex normals: the tuck solve needs a direction to push skin inward along, and
        # the mesh is the only thing that knows which way "in" is.
        normals = np.empty((len(verts), 3), dtype=np.float64)
        obj.data.vertices.foreach_get("normal", normals.ravel())

        # Triangles: containment has to be measured against the SURFACE. A nearest-vertex test
        # is unreliable inside a narrow sleeve, where the nearest vertices can sit on the far
        # wall and their normals face back at you.
        obj.data.calc_loop_triangles()
        tris = np.empty((len(obj.data.loop_triangles), 3), dtype=np.int32)
        obj.data.loop_triangles.foreach_get("vertices", tris.ravel())

        key = obj.name
        payload[f"mesh::{key}::triangles"] = tris
        payload[f"mesh::{key}::normals"] = normals
        payload[f"mesh::{key}::positions"] = positions
        payload[f"mesh::{key}::offsets"] = offsets
        payload[f"mesh::{key}::indices"] = np.array(idx_list, dtype=np.int32)
        payload[f"mesh::{key}::weights"] = np.array(wgt_list, dtype=np.float64)
        payload[f"mesh::{key}::matrix_world"] = np.array(obj.matrix_world, dtype=np.float64)
        meshes.append(key)

        counts = np.diff(offsets)
        _log(f"{key}: {len(verts)} verts, influences med={int(np.median(counts))} "
             f"max={int(counts.max())}, {int((counts > 4).sum())} verts over 4")

    payload["meshes"] = np.array(meshes, dtype=object)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    np.savez_compressed(args.out, **payload)
    _log(f"wrote {args.out} ({os.path.getsize(args.out)/1e6:.1f} MB, {len(meshes)} meshes, "
         f"{len(bone_names)} bones)")


main()
