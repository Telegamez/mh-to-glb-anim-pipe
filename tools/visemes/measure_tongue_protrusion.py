"""Does the tongue protrude under jawOpen, and is it F2's doing?

    blender --background --factory-startup --python tongue_protrusion.py -- <dir> <out.json>

Self-calibrating on two axes so nothing depends on guessing the FBX's orientation:

  tongue vertices  = what `tongueOut` displaces (>1mm) -- purely lingual, no jaw rotation
  the "out" axis   = the mean direction `tongueOut` moves those vertices

Then every shape's tongue motion is projected onto that axis. Positive = protruding the way
`tongueOut` protrudes. That makes "is the tongue sticking out" a number instead of a look.
"""
import json
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
SRC, OUT = argv[0], argv[1]


def log(m):
    print(f"[tongue] {m}", flush=True)


def camel(n):
    if not n or n == "Default" or n.startswith("Pose_"):
        return n
    return n[0].lower() + n[1:]


def capture(fbx, names_json, label):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=fbx, use_anim=True)
    mesh_obj = next((o for o in bpy.data.objects if o.type == "MESH"), None)
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    raw = json.load(open(names_json, encoding="utf-8"))
    poses = [(camel(n), i) for i, n in enumerate(raw)
             if n != "Default" and not n.startswith("Pose_")]
    f_start = (int(arm.animation_data.action.frame_range[0])
               if arm.animation_data and arm.animation_data.action else 1)
    bpy.context.view_layer.objects.active = mesh_obj
    bpy.context.scene.frame_set(f_start)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    basis = [v.co.copy() for v in mesh_obj.evaluated_get(dg).data.vertices]
    out = {}
    for name, idx in poses:
        bpy.context.scene.frame_set(f_start + idx)
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        out[name] = [v.co.copy() for v in mesh_obj.evaluated_get(dg).data.vertices]
    log(f"{label}: {len(out)} poses")
    return basis, out


basis, arkit = capture(os.path.join(SRC, "LS_arkit_full.fbx"),
                       os.path.join(SRC, "arkit_pose_names.json"), "arkit")

# --- tongue vertices, and the direction "out" means on this mesh --------------
probe = arkit["tongueOut"]
tongue = [i for i, (b, p) in enumerate(zip(basis, probe)) if (p - b).length > 0.1]
out_axis = Vector((0, 0, 0))
for i in tongue:
    out_axis += (probe[i] - basis[i])
out_axis.normalize()
log(f"tongue verts: {len(tongue)}; out-axis (from tongueOut): "
    f"({out_axis.x:.3f}, {out_axis.y:.3f}, {out_axis.z:.3f})")


def protrusion(positions):
    """Mean and max forward travel of the tongue, in mm, along the out-axis."""
    vals = [(positions[i] - basis[i]).dot(out_axis) * 10.0 for i in tongue]
    return sum(vals) / len(vals), max(vals)


report = {"tongue_verts": len(tongue),
          "out_axis": [out_axis.x, out_axis.y, out_axis.z], "shapes": {}}

log("")
log(f"{'shape':22s} {'mean mm':>9s} {'max mm':>9s}")
for name in ("tongueOut", "jawOpen", "mouthOpen", "jawForward", "mouthFunnel",
             "mouthPucker", "tongueUp" if "tongueUp" in arkit else "tongueOut"):
    if name not in arkit:
        continue
    mean, mx = protrusion(arkit[name])
    report["shapes"][name] = {"mean_mm": round(mean, 2), "max_mm": round(mx, 2)}
    log(f"{name:22s} {mean:9.2f} {mx:9.2f}")

# --- the composed aa is PURE jawOpen 0.70 ------------------------------------
composed_aa = [basis[i] + (arkit["jawOpen"][i] - basis[i]) * 0.70
               for i in range(len(basis))]
mean, mx = protrusion(composed_aa)
report["composed_aa"] = {"mean_mm": round(mean, 2), "max_mm": round(mx, 2)}
log("")
log(f"{'composed viseme_aa':22s} {mean:9.2f} {mx:9.2f}   (= jawOpen x 0.70, nothing else)")

# --- and the baked one, for the comparison the operator is making -------------
b2, baked = capture(os.path.join(SRC, "LS_tg_visemes.fbx"),
                    os.path.join(SRC, "tg_viseme_pose_names.json"), "baked")
if "viseme_aa" in baked:
    vals = [(baked["viseme_aa"][i] - b2[i]).dot(out_axis) * 10.0 for i in tongue]
    mean, mx = sum(vals) / len(vals), max(vals)
    report["baked_aa"] = {"mean_mm": round(mean, 2), "max_mm": round(mx, 2)}
    log(f"{'baked viseme_aa':22s} {mean:9.2f} {mx:9.2f}")

json.dump(report, open(OUT, "w"), indent=2)
log(f"wrote {OUT}")
