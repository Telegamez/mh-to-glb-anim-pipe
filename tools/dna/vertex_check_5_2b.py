"""Slice 5.2b [A]: does driving the joints reproduce the baked shape, in vertex space?

    blender --background --python vertex_check_5_2b.py -- --glb <telly-research.glb>
            --dump <telly_eval_selfdesc.npz> --poses <tg_validation_poses.json> --viseme viseme_aa

## The question

Rung 1 poses the face with a baked morph target. Rung 2 poses it by driving 843 facial joints.
Both are supposed to produce the same face. Nothing has ever checked that in the space a person
actually sees, and every check so far has been in attribute space — joint rows and blendshape
weights — where a systematic error can hide behind matching magnitudes.

So: one held viseme, two ways, compared per vertex.

    path A   morph `viseme_aa` at 1.0, joints at rest        the shape F2 ships
    path B   morphs at 0, joints posed from the UE dump      what rung 2 would do

**Never both.** A face driven by morph AND joint at once is the failure 03 §2's channel
ownership exists to prevent, and it would also make this comparison meaningless.

## Why the UE dump rather than the oracle

The oracle is already bitwise against Epic's library on every single-control pose, so running
it here would re-answer a settled question. What is NOT settled is whether the joint transforms
those evaluators produce, applied to THIS GLB's skin, land where the baked morph does. That
needs the transforms and the mesh together, and the dump is the transforms.

## The coordinate map is solved, not assumed

UE's dump is centimetres in its own component space; the GLB is metres, Y-up, right-handed. The
map between them is fitted from the REST pose over the shared bone names — Kabsch with
reflection allowed, because Unreal is left-handed — and gated on its residual. A map that does
not fit the rest pose cannot be trusted to place a posed one.

`FACIAL_L_Eye` and `FACIAL_R_Eye` are renamed to `LeftEye`/`RightEye` by stage 02's
CONTRACT_BONE_RENAMES; the other 14 renames are body bones the face never touches.
"""
import argparse
import json
import sys

import numpy as np

try:
    import bpy
    from mathutils import Matrix, Vector
except ImportError:  # pragma: no cover - lets the file be linted outside Blender
    bpy = None

CONTRACT_BONE_RENAMES = {"FACIAL_L_Eye": "LeftEye", "FACIAL_R_Eye": "RightEye"}
FACE_MESH = "Head_Mesh"


def _args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--glb", required=True)
    p.add_argument("--dump", required=True)
    p.add_argument("--poses", required=True)
    p.add_argument("--viseme", default="viseme_aa")
    p.add_argument("--json")
    return p.parse_args(argv)


def similarity_fit(src, dst):
    """Kabsch with scale and reflection. Mirrors 5.7/native-glb/tools/diff_against_unreal.py."""
    sc, dc = src.mean(0), dst.mean(0)
    s0, d0 = src - sc, dst - dc
    U, S, Vt = np.linalg.svd(s0.T @ d0 / len(src))
    var = (s0 ** 2).sum() / len(src)
    best = None
    for det in (1.0, -1.0):
        R = Vt.T @ np.diag([1.0, 1.0, det]) @ U.T
        scale = float((S * np.array([1.0, 1.0, det])).sum() / var)
        t = dc - scale * R @ sc
        residual = float(np.sqrt((((src @ (scale * R).T + t) - dst) ** 2).sum(1)).mean())
        if best is None or residual < best[0]:
            M = np.eye(4)
            M[:3, :3] = scale * R
            M[:3, 3] = t
            best = (residual, M)
    return best[1], best[0]


def log(msg):
    print(f"[5.2b] {msg}", flush=True)


def main():
    ns = _args()
    # NOT read_factory_settings(use_empty=True): in --background it unregisters addon classes
    # and Blender 4.4 dies with an EXCEPTION_ACCESS_VIOLATION before the import runs. Clearing
    # the default scene by hand does the same job without touching the addon registry.
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.import_scene.gltf(filepath=ns.glb)

    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    face = next((o for o in bpy.data.objects if o.type == "MESH"
                 and o.name.split(".")[0] == FACE_MESH), None)
    if arm is None or face is None:
        raise SystemExit(f"need an armature and {FACE_MESH}; got arm={arm} face={face}")

    keys = face.data.shape_keys
    names = [k.name for k in keys.key_blocks] if keys else []
    facial_bones = [b.name for b in arm.data.bones if b.name.upper().startswith("FACIAL")]
    log(f"{arm.name}: {len(arm.data.bones)} bones, {len(facial_bones)} FACIAL_*")
    log(f"{face.name}: {len(face.data.vertices)} verts, {len(names)} shape keys")
    if ns.viseme not in names:
        raise SystemExit(f"{ns.viseme} is not a shape key on {face.name}")

    deps = bpy.context.evaluated_depsgraph_get()

    def evaluated_verts():
        ev = face.evaluated_get(deps)
        me = ev.to_mesh()
        out = np.array([[v.co.x, v.co.y, v.co.z] for v in me.vertices], dtype=np.float64)
        ev.to_mesh_clear()
        return out

    # ---- rest -----------------------------------------------------------------------
    for kb in keys.key_blocks:
        kb.value = 0.0
    for pb in arm.pose.bones:
        pb.matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()
    rest = evaluated_verts()

    # ---- path A: the baked morph -----------------------------------------------------
    keys.key_blocks[ns.viseme].value = 1.0
    bpy.context.view_layer.update()
    morph = evaluated_verts()
    keys.key_blocks[ns.viseme].value = 0.0
    bpy.context.view_layer.update()
    dA = morph - rest
    log(f"path A (morph {ns.viseme}): max {np.abs(dA).max()*1000:.2f} mm, "
        f"{int((np.linalg.norm(dA,axis=1)*1000 > 0.1).sum())} verts move >0.1mm")

    # ---- the coordinate map, from the rest pose ---------------------------------------
    d = np.load(ns.dump, allow_pickle=True)
    ue_names = [str(b) for b in d["bone_names"]]
    ue_pos = d["raw"][:, :, :3].astype(np.float64)          # frames x bones x xyz, cm

    bone_world = {b.name: np.array(arm.matrix_world @ b.head_local, dtype=np.float64)
                  for b in arm.data.bones}
    shared = []
    for i, n in enumerate(ue_names):
        gl = CONTRACT_BONE_RENAMES.get(n, n)
        if gl in bone_world:
            shared.append((i, gl))
    src = np.array([ue_pos[0][i] for i, _ in shared])
    dst = np.array([bone_world[g] for _, g in shared])
    M, residual = similarity_fit(src, dst)
    det = np.linalg.det(M[:3, :3])
    log(f"UE -> GLB fit over {len(shared)} shared bones: residual {residual*1000:.4f} mm, "
        f"det {det:+.5f} ({'reflection' if det < 0 else 'proper'}), "
        f"scale {abs(det)**(1/3):.6f}")
    if residual * 1000 > 1.0:
        raise SystemExit(
            f"REFUSED: rest-pose map residual {residual*1000:.3f} mm. A map that cannot place "
            f"the rest pose cannot be trusted to place a posed one, and every number after "
            f"this point would be measuring the map instead of the rig.")

    # ---- path B: joints posed from the dump -------------------------------------------
    spec = json.load(open(ns.poses, encoding="utf-8"))
    frame = next((i + 1 for i, p in enumerate(spec["poses"])
                  if p["name"] in (ns.viseme, f"viseme__{ns.viseme}")), None)
    if frame is None:
        raise SystemExit(f"{ns.viseme} not found in {ns.poses}")
    log(f"dump frame for {ns.viseme}: {frame}")

    L = M[:3, :3]
    t = M[:3, 3]
    posed = 0
    for i, gl in shared:
        pb = arm.pose.bones.get(gl)
        if pb is None:
            continue
        target = L @ ue_pos[frame][i] + t
        rest_head = bone_world[gl]
        pb.matrix_basis = Matrix.Translation(Vector(target - rest_head))
        posed += 1
    bpy.context.view_layer.update()
    joint = evaluated_verts()
    dB = joint - rest
    log(f"path B (joints, {posed} posed): max {np.abs(dB).max()*1000:.2f} mm, "
        f"{int((np.linalg.norm(dB,axis=1)*1000 > 0.1).sum())} verts move >0.1mm")

    # ---- compare -----------------------------------------------------------------------
    err = np.linalg.norm(dA - dB, axis=1) * 1000.0
    magA = np.linalg.norm(dA, axis=1) * 1000.0
    moving = magA > 0.1
    log("")
    log(f"VERTEX-SPACE COMPARISON on {ns.viseme}")
    log(f"  verts the morph moves      {int(moving.sum())}/{len(magA)}")
    log(f"  morph displacement         max {magA.max():.2f} mm  median(moving) "
        f"{np.median(magA[moving]):.2f} mm")
    log(f"  |A - B| over moving verts  max {err[moving].max():.2f} mm  median "
        f"{np.median(err[moving]):.2f} mm  p90 {np.percentile(err[moving], 90):.2f} mm")
    log(f"  |A - B| as % of morph      median "
        f"{np.median(err[moving]/np.maximum(magA[moving],1e-6))*100:.1f}%")

    if ns.json:
        json.dump({"viseme": ns.viseme, "glb": ns.glb, "frame": frame,
                   "map_residual_mm": residual * 1000, "map_det": det,
                   "verts": int(len(magA)), "verts_moving": int(moving.sum()),
                   "morph_max_mm": float(magA.max()),
                   "err_max_mm": float(err[moving].max()),
                   "err_median_mm": float(np.median(err[moving])),
                   "err_p90_mm": float(np.percentile(err[moving], 90))},
                  open(ns.json, "w"), indent=2)
        log(f"  wrote {ns.json}")


if bpy is not None:
    main()
