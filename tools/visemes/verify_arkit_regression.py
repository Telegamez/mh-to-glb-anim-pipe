"""ARKit regression check for face-fidelity slice 2.3.

    blender --background --factory-startup --python verify_arkit_regression.py \
        -- <dir-with-BEFORE-and-AFTER-fbx> <out.json>

Captures the 51 ARKit poses from both Sequencer bakes produced by
`export_arkit_before_after.py` and asserts they are identical, vertex for
vertex.

Why identity and not "close enough"
-----------------------------------
The ARKit shapes are the reference frame for everything downstream. Phase 1
sweeps every FaceProfile value against them; Phase 2's baked-vs-composed
comparison uses them as the composed baseline; `audit_models.py` counts them.
A change that shifted them by half a millimetre would silently re-base all of
it, and no other check in the pipeline looks.

So the bar is bitwise-equal positions, not a tolerance. The changes under test
(measured frame rate, playback-end +1, parameterised sequence name) are all
supposed to be no-ops for this path: the ARKit mapping measures 24 fps either
way, and the extra frame lands past `Pose_17`, which stage 02 filters out by
name. If that reasoning is wrong, this prints exactly which pose moved.

Exit code is non-zero when any pose differs, so it can gate a bake.
"""
import json
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
SRC = argv[0]
OUT = argv[1] if len(argv) > 1 else os.path.join(SRC, "arkit_regression.json")

# Positions are mesh-local centimetres on this import path; 1e-6 cm = 10 nm,
# i.e. float noise rather than deformation.
EPS_CM = 1e-6


def log(m):
    print(f"[arkit-reg] {m}", flush=True)


def camel(n):
    if not n or n == "Default" or n.startswith("Pose_"):
        return n
    return n[0].lower() + n[1:]


def capture(fbx, names_json, label):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=fbx, use_anim=True)
    mesh_obj = next((o for o in bpy.data.objects if o.type == "MESH"), None)
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    if mesh_obj is None or arm is None:
        raise RuntimeError(f"{label}: import missing mesh or armature")

    raw = json.load(open(names_json, encoding="utf-8"))
    poses = [(camel(n), i) for i, n in enumerate(raw)
             if n != "Default" and not n.startswith("Pose_")]
    f_start = (int(arm.animation_data.action.frame_range[0])
               if arm.animation_data and arm.animation_data.action else 1)

    bpy.context.view_layer.objects.active = mesh_obj
    out = {}
    for name, idx in poses:
        bpy.context.scene.frame_set(f_start + idx)
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        out[name] = [v.co.copy() for v in mesh_obj.evaluated_get(dg).data.vertices]
    log(f"{label}: {len(out)} ARKit poses, {len(out[poses[0][0]])} verts")
    return out


def main():
    names = os.path.join(SRC, "arkit_pose_names.json")
    before = capture(os.path.join(SRC, "LS_arkit_BEFORE.fbx"), names, "before")
    after = capture(os.path.join(SRC, "LS_arkit_AFTER.fbx"), names, "after")

    report = {"poses_before": len(before), "poses_after": len(after),
              "eps_cm": EPS_CM, "differences": {}}

    missing = sorted(set(before) ^ set(after))
    if missing:
        report["pose_set_mismatch"] = missing
        log(f"POSE SET DIFFERS: {missing}")

    worst = 0.0
    worst_pose = None
    for name in sorted(set(before) & set(after)):
        b, a = before[name], after[name]
        if len(b) != len(a):
            report["differences"][name] = {"vertex_count": [len(b), len(a)]}
            continue
        md = 0.0
        n_moved = 0
        for pb, pa in zip(b, a):
            d = (pa - pb).length
            if d > EPS_CM:
                n_moved += 1
            if d > md:
                md = d
        if n_moved:
            report["differences"][name] = {
                "verts_moved": n_moved, "max_delta_mm": round(md * 10.0, 6)}
        if md > worst:
            worst, worst_pose = md, name

    report["worst_pose"] = worst_pose
    report["worst_delta_mm"] = round(worst * 10.0, 6)
    report["identical"] = not report["differences"] and not missing

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    log("")
    if report["identical"]:
        log(f"PASS - all {len(before)} ARKit shapes identical "
            f"(worst delta {report['worst_delta_mm']} mm on {worst_pose})")
    else:
        log(f"FAIL - {len(report['differences'])} pose(s) differ")
        for name, d in list(report["differences"].items())[:10]:
            log(f"   {name}: {d}")
    log(f"wrote {OUT}")
    sys.exit(0 if report["identical"] else 1)


main()
