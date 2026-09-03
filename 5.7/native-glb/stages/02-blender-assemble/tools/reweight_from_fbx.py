"""Replace GLB-imported skin weights with the FBX exporter's — the glTF ones are corrupt.

## The defect this fixes (found 2026-08-18, the shirt-tearing root cause)

Unreal's glTF exporter corrupts skin weights. Measured on Telly's outfit, same asset, same
2489 coincident seam-vertex pairs:

    FBX export    twin weight disagreement: 0.000 on every pair
    glTF export   twin weight disagreement: median 0.152, max 0.937, 1369 pairs over 10%

Coincident twins are the SAME point on the garment duplicated for UVs/materials; in Unreal's
own mesh they share one import vertex, so any disagreement is manufactured by the exporter.
Driven by Unreal's own evaluated transforms, the corrupted weights open those seams up to
58.7mm during Shrug — the tearing, the collar shredding and the placket shards that four
rounds of downstream weight surgery could only reshuffle. The corruption is in the influence
data itself, so NOTHING downstream of stage 01 could ever have fixed it.

Geometry, UVs, materials and textures from the GLB import are fine and are kept. Only the
weight map is replaced.

## How

Stage 01 exports each SkeletalMesh a second time as FBX (weights only, no textures) and a
Blender dump turns that into an npz per mesh: positions, CSR-style offsets/indices/weights,
bone names. This script runs inside the character's stage-02 blend and, for each mesh named
in the manifest:

  1. GATES on per-index vertex positions matching the npz (<0.1mm) — same mesh, same order.
     A mismatch means the blend was edited after assembly; refusing beats guessing a mapping.
  2. Clears only BONE vertex groups (shape masks and other non-bone groups are untouched).
  3. Rebuilds groups from the npz, translating UE bone names through the same rename table
     the assembly applied (pelvis -> Hips, ...). A bone absent from the armature is an ERROR,
     not a skip — silent weight loss is how this pipeline got here in the first place.

Run AFTER stage 02 assembly and BEFORE dump_skin/solve/stage 03:

    blender --background characters/<id>/02-blend/<id>.blend \
        --python stages/02-blender-assemble/tools/reweight_from_fbx.py -- \
        --npz characters/<id>/source/fbx_weights.npz \
        --map "Body_Mesh=SKM_<Char>_BodyMesh.001" "Telly_Outfits.001=Telly_Outfits.001"
"""
from __future__ import annotations

import sys

import bpy  # type: ignore
import numpy as np

# Must mirror CONTRACT_BONE_RENAMES in stages/02-blender-assemble/tools/import_glb.py.
UE_TO_CONTRACT = {
    "pelvis": "Hips",
    "spine_01": "Spine",
    "spine_03": "Spine1",
    "spine_05": "Spine2",
    "neck_01": "Neck",
    "head": "Head",
    "clavicle_l": "LeftShoulder",
    "upperarm_l": "LeftArm",
    "lowerarm_l": "LeftForeArm",
    "hand_l": "LeftHand",
    "clavicle_r": "RightShoulder",
    "upperarm_r": "RightArm",
    "lowerarm_r": "RightForeArm",
    "hand_r": "RightHand",
    "FACIAL_L_Eye": "LeftEye",
    "FACIAL_R_Eye": "RightEye",
}

POSITION_GATE = 1e-4  # 0.1mm — identical meshes, so anything above this is the wrong mesh


def _log(msg):
    print(f"[reweight] {msg}", flush=True)


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    npz, mapping = None, {}
    it = iter(range(len(argv)))
    i = 0
    while i < len(argv):
        if argv[i] == "--npz":
            npz = argv[i + 1]; i += 2
        elif argv[i] == "--map":
            i += 1
            while i < len(argv) and "=" in argv[i] and not argv[i].startswith("--"):
                blend_name, fbx_key = argv[i].split("=", 1)
                mapping[blend_name] = fbx_key
                i += 1
        else:
            i += 1
    if not npz or not mapping:
        raise RuntimeError("usage: -- --npz <fbx_weights.npz> --map Blend=FbxKey ...")
    return npz, mapping


def _auto_map(data):
    """One blend mesh per npz asset key. BodyMesh assets land on the contract's `Body_Mesh`;
    anything else matches the GLB import's datablock (`<AssetName>` or `<AssetName>.NNN`).
    Characters carry a variable number of outfit meshes (Tele has two), so this enumerates
    the npz rather than assuming one body + one garment."""
    import re
    mapping = {}
    for key in sorted({k.split("::")[0] for k in data.files if "::" in k}):
        if "BodyMesh" in key:
            mapping["Body_Mesh"] = key
            continue
        pat = re.compile(re.escape(key) + r"(\.\d+)?$")
        hits = [o.name for o in bpy.data.objects if o.type == "MESH" and pat.match(o.name)]
        if len(hits) != 1:
            raise RuntimeError(f"{key}: expected exactly 1 matching blend mesh, found {hits}")
        mapping[hits[0]] = key
    return mapping


def _repair_unweighted(obj, fpos, offsets, indices, weights, target, groups, blend_name):
    """Copy the nearest weighted vertex's influences onto every vertex that has none."""
    from mathutils.kdtree import KDTree
    n = len(fpos)
    has_weight = np.zeros(n, bool)
    for v in range(n):
        for k in range(offsets[v], offsets[v + 1]):
            if float(weights[k]) > 0.0:
                has_weight[v] = True
                break
    bad = np.where(~has_weight)[0]
    if len(bad) == 0:
        _log(f"{blend_name}: no unweighted vertices")
        return 0
    good = np.where(has_weight)[0]
    if len(good) == 0:
        raise RuntimeError(f"{blend_name}: every vertex is unweighted - the FBX carries no skin")
    kd = KDTree(len(good))
    for i in good:
        kd.insert(fpos[i], int(i))
    kd.balance()
    worst = 0.0
    for v in bad:
        _, src, dist = kd.find(fpos[v])
        worst = max(worst, float(dist))
        for k in range(offsets[src], offsets[src + 1]):
            w = float(weights[k])
            if w <= 0.0:
                continue
            name = target[int(indices[k])]
            vg = groups.get(name)
            if vg is None:
                vg = obj.vertex_groups.new(name=name)
                groups[name] = vg
            vg.add([int(v)], w, "REPLACE")
    _log(f"{blend_name}: REPAIRED {len(bad)} unweighted vertex/vertices from their nearest "
         f"weighted neighbour (farthest {worst * 1000:.1f}mm) - the source asset ships them "
         f"without influences")
    return len(bad)


def main():
    npz_path, mapping = _args()
    data = np.load(npz_path, allow_pickle=True)
    if mapping == {"auto": "auto"}:
        mapping = _auto_map(data)
        _log(f"auto map: {mapping}")

    armature_bones = set()
    for arm in (o for o in bpy.data.objects if o.type == "ARMATURE"):
        armature_bones.update(b.name for b in arm.data.bones)

    for blend_name, fbx_key in mapping.items():
        if blend_name == "auto-garment":
            # The garment object's name is per-character (<Char>_Outfits.001); resolve by the
            # same pattern stage 03 uses rather than requiring the caller to know it.
            import re
            garments = [o for o in bpy.data.objects if o.type == "MESH"
                        and re.search(r"Outfit|Garment|Cloth", o.name, re.IGNORECASE)]
            if len(garments) != 1:
                raise RuntimeError(
                    f"auto-garment: expected exactly 1 garment mesh, found "
                    f"{[o.name for o in garments]}")
            blend_name = garments[0].name
        obj = bpy.data.objects.get(blend_name)
        if obj is None or obj.type != "MESH":
            raise RuntimeError(f"{blend_name}: not in this blend")
        pos_key = f"{fbx_key}::positions"
        if pos_key not in data:
            raise RuntimeError(f"{fbx_key}: not in {npz_path} (has {sorted({k.split('::')[0] for k in data})})")

        fpos = data[pos_key]
        mesh = obj.data
        if len(fpos) != len(mesh.vertices):
            raise RuntimeError(
                f"{blend_name}: blend has {len(mesh.vertices)} verts, FBX dump has {len(fpos)} — "
                f"not the same mesh")
        ours = np.empty((len(mesh.vertices), 3))
        mesh.vertices.foreach_get("co", ours.ravel())
        mw = np.array(obj.matrix_world)
        ours = ours @ mw[:3, :3].T + mw[:3, 3]
        delta = np.abs(ours - fpos).max()
        if delta > POSITION_GATE:
            raise RuntimeError(
                f"GATE FAILED: {blend_name} positions differ from the FBX dump by up to "
                f"{delta * 1000:.2f}mm — vertex order does not correspond, refusing to reweight")

        offsets = data[f"{fbx_key}::offsets"]
        indices = data[f"{fbx_key}::indices"]
        weights = data[f"{fbx_key}::weights"]
        fbx_bones = [str(b) for b in data[f"{fbx_key}::bones"]]
        target = [UE_TO_CONTRACT.get(b, b) for b in fbx_bones]
        absent = sorted({t for t in target if t not in armature_bones})
        if absent:
            raise RuntimeError(
                f"{blend_name}: FBX weights reference bones missing from the armature: "
                f"{absent[:8]} — silent weight loss is not acceptable, fix the rename table")

        # Only bone groups are rebuilt; anything else (shape masks etc.) survives untouched.
        for vg in [vg for vg in obj.vertex_groups if vg.name in armature_bones]:
            obj.vertex_groups.remove(vg)
        groups = {}
        replaced = 0
        for v in range(len(mesh.vertices)):
            for k in range(offsets[v], offsets[v + 1]):
                w = float(weights[k])
                if w <= 0.0:
                    continue
                name = target[int(indices[k])]
                vg = groups.get(name)
                if vg is None:
                    vg = obj.vertex_groups.new(name=name)
                    groups[name] = vg
                vg.add([v], w, "REPLACE")
            replaced += 1
        _log(f"{blend_name}: rebuilt {len(groups)} bone groups over {replaced} verts "
             f"(position gate {delta * 1000:.4f}mm)")

        # TELEGAMEZ (telegamez#3725): Fab outfits can ship vertices with NO
        # influences (Tommy's Roman Soldier: 24 on the breastplate, 56 on the
        # greaves; Unreal raises the ClothAsset.cpp:552 ensure on every build).
        # Unweighted vertices bind to root and spike the moment the body
        # animates. Give each one the weights of its nearest weighted vertex
        # on the same mesh - the seam next to it is the best available guess
        # and a few millimetres of drift beats a spike to the origin. Logged
        # loudly, because a large count means the source asset is broken.
        _repair_unweighted(obj, fpos, offsets, indices, weights, target, groups, blend_name)

    bpy.ops.wm.save_mainfile()
    _log(f"saved {bpy.data.filepath}")


main()
