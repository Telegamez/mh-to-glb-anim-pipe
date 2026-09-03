"""Turn stage 01's weight FBX files into one npz for reweight_from_fbx.py.

Runs in a clean Blender. Imports every *_weights.fbx in the character's source/ dir and dumps
each mesh's positions + CSR weights. Keys are normalised to the character-agnostic roles the
reweighter expects:

    <AssetName>::...   e.g. SKM_Telly_BodyMesh::positions, Tele_Outfits_2::weights

one block per *_weights.fbx — characters can carry several outfit meshes (Tele has two), so
keys are the asset names, not roles.

    blender --factory-startup --background --python dump_fbx_weights.py -- \
        --source-dir characters/<id>/source --out characters/<id>/source/fbx_weights.npz
"""
from __future__ import annotations

import glob
import os
import sys

import bpy  # type: ignore
import numpy as np


def _log(msg):
    print(f"[dump_fbx] {msg}", flush=True)


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    src = out = None
    for i, a in enumerate(argv):
        if a == "--source-dir":
            src = argv[i + 1]
        elif a == "--out":
            out = argv[i + 1]
    if not (src and out):
        raise RuntimeError("usage: -- --source-dir <dir> --out <npz>")
    return src, out


def main():
    src, out_path = _args()
    fbxs = sorted(glob.glob(os.path.join(src, "*_weights.fbx")))
    if len(fbxs) < 2:
        raise RuntimeError(f"expected body+outfit *_weights.fbx in {src}, found {fbxs}")

    result = {}
    for fbx in fbxs:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        # TELEGAMEZ (telegamez#3725): NEVER ignore leaf bones here. Unreal's FBX
        # carries no marker "end" bones, so Blender's `ignore_leaf_bones` drops
        # real bones that simply have no children - `head` in the body skeleton,
        # every fingertip, `ball_l/r` - and folds their weights into the parent.
        # Tommy's helmet crest came out bound to neck_02 instead of head; on the
        # roster the same option has been quietly stiffening fingertips.
        bpy.ops.import_scene.fbx(filepath=fbx, ignore_leaf_bones=False)
        meshes = [o for o in bpy.data.objects if o.type == "MESH"]
        if len(meshes) != 1:
            raise RuntimeError(f"{fbx}: expected exactly 1 mesh, found {[m.name for m in meshes]}")
        obj = meshes[0]
        role = os.path.basename(fbx)[:-len("_weights.fbx")]

        gn = {g.index: g.name for g in obj.vertex_groups}
        names = sorted({g.name for g in obj.vertex_groups})
        ni = {m: i for i, m in enumerate(names)}
        mesh = obj.data
        pos = np.empty((len(mesh.vertices), 3))
        mesh.vertices.foreach_get("co", pos.ravel())
        mw = np.array(obj.matrix_world)
        pos = pos @ mw[:3, :3].T + mw[:3, 3]
        idx, wt, off = [], [], [0]
        for v in mesh.vertices:
            for g in v.groups:
                if g.weight > 0.0:
                    idx.append(ni[gn[g.group]])
                    wt.append(g.weight)
            off.append(len(idx))
        result[f"{role}::positions"] = pos
        result[f"{role}::offsets"] = np.array(off)
        result[f"{role}::indices"] = np.array(idx)
        result[f"{role}::weights"] = np.array(wt)
        result[f"{role}::bones"] = np.array(names, dtype=object)
        _log(f"{role}: {obj.name} {len(mesh.vertices)} verts, {len(names)} bones")

    np.savez_compressed(out_path, **result)
    _log(f"wrote {out_path}")


main()
