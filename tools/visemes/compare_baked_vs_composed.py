"""Slice 2.3 evidence — baked viseme poses vs the composed recipes.

Runs headless in Blender:

    blender --background --factory-startup --python blender_compare_visemes.py -- <dir> <out.json>

Captures both Sequencer bakes from the same face mesh, then for every contract
mouth shape reports max displacement over the whole face and over the
TONGUE/TEETH material slot specifically. The tongue column is the one that
decides F2: a composed `viseme_DD` has no tongue term at all in
`VISEME_RECIPES`, so whatever it shows there is incidental jaw motion.
"""
import json
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
SRC = argv[0]
OUT = argv[1]

VISEME_RECIPES = {
    "viseme_sil": (),
    "viseme_PP": (("mouthPressLeft", 0.70), ("mouthPressRight", 0.70),
                  ("mouthRollLower", 0.25), ("mouthRollUpper", 0.25)),
    "viseme_FF": (("mouthRollLower", 0.55), ("mouthUpperUpLeft", 0.25),
                  ("mouthUpperUpRight", 0.25), ("jawOpen", 0.05)),
    "viseme_TH": (("jawOpen", 0.16), ("tongueOut", 0.30)),
    "viseme_DD": (("jawOpen", 0.20), ("mouthShrugUpper", 0.18)),
    "viseme_kk": (("jawOpen", 0.18), ("mouthShrugLower", 0.12)),
    "viseme_CH": (("mouthPucker", 0.45), ("mouthFunnel", 0.25), ("jawOpen", 0.10)),
    "viseme_SS": (("mouthStretchLeft", 0.35), ("mouthStretchRight", 0.35),
                  ("jawOpen", 0.05)),
    "viseme_nn": (("jawOpen", 0.13), ("tongueOut", 0.12)),
    "viseme_RR": (("mouthPucker", 0.30), ("mouthFunnel", 0.18), ("jawOpen", 0.10)),
    "viseme_aa": (("jawOpen", 0.70),),
    "viseme_E": (("jawOpen", 0.32), ("mouthStretchLeft", 0.22),
                 ("mouthStretchRight", 0.22)),
    "viseme_I": (("jawOpen", 0.10), ("mouthStretchLeft", 0.40),
                 ("mouthStretchRight", 0.40)),
    "viseme_O": (("mouthPucker", 0.40), ("mouthFunnel", 0.45), ("jawOpen", 0.28)),
    "viseme_U": (("mouthPucker", 0.85), ("mouthFunnel", 0.12)),
    "mouthClose": (("mouthPressLeft", 0.45), ("mouthPressRight", 0.45),
                   ("mouthRollLower", 0.20), ("mouthRollUpper", 0.20)),
}

TONGUE_KEYWORDS = ("teeth", "tongue", "saliva")


def log(m):
    print(f"[cmp] {m}", flush=True)


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def camel(n):
    if not n or n == "Default" or n.startswith("Pose_"):
        return n
    return n[0].lower() + n[1:]


def capture(fbx, names_json, label):
    """Import a Sequencer bake and return {pose_name: [Vector per vertex]}."""
    pre_obj = set(o.name for o in bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=fbx, use_anim=True)
    new = set(o.name for o in bpy.data.objects) - pre_obj
    mesh_obj = next((bpy.data.objects[n] for n in new
                     if bpy.data.objects[n].type == "MESH"), None)
    arm = next((bpy.data.objects[n] for n in new
                if bpy.data.objects[n].type == "ARMATURE"), None)
    if mesh_obj is None or arm is None:
        raise RuntimeError(f"{label}: import missing mesh or armature")

    raw_names = json.load(open(names_json, encoding="utf-8"))
    poses = [(camel(n), i) for i, n in enumerate(raw_names)
             if n != "Default" and not n.startswith("Pose_")]

    if arm.animation_data and arm.animation_data.action:
        f_start = int(arm.animation_data.action.frame_range[0])
    else:
        f_start = 1

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
    log(f"{label}: captured {len(out)} poses, {len(basis)} verts, f_start={f_start}")
    return mesh_obj, basis, out


def tongue_vert_ids_from_probe(basis, probe_positions, threshold_cm=0.1):
    """Tongue verts = the ones `tongueOut` displaces.

    The teeth material slot holds the teeth too, and the teeth ride the jaw, so
    a slot-based set reports jaw motion as tongue motion. `tongueOut` is purely
    lingual and does not rotate the jaw, so what it moves IS the tongue.
    Positions are mesh-local cm here; 0.1 cm = 1 mm.
    """
    ids = {i for i, (b, p) in enumerate(zip(basis, probe_positions))
           if (p - b).length > threshold_cm}
    log(f"tongue verts (probed via tongueOut, >1mm): {len(ids)}")
    return ids


def measure(basis, positions, tongue_ids):
    """(max mm anywhere, max mm within the tongue/teeth slot).

    The Sequencer FBX imports at world_scale 0.01 but `evaluated_get` returns
    mesh-local centimetres, so x10 converts to millimetres.
    """
    mx = 0.0
    tmx = 0.0
    for i, (b, p) in enumerate(zip(basis, positions)):
        d = (p - b).length
        if d > mx:
            mx = d
        if i in tongue_ids and d > tmx:
            tmx = d
    return mx * 10.0, tmx * 10.0


def compose(basis, arkit, recipe, n):
    """Linear sum of ARKit deltas — exactly what stage 02 does today."""
    out = [b.copy() for b in basis]
    for src, w in recipe:
        pos = arkit.get(src)
        if pos is None:
            continue
        for i in range(n):
            out[i] += (pos[i] - basis[i]) * w
    return out


def main():
    reset()
    report = {"source": SRC}

    obj_a, basis_a, arkit = capture(
        os.path.join(SRC, "LS_arkit_full.fbx"),
        os.path.join(SRC, "arkit_pose_names.json"), "arkit")
    tongue_a = tongue_vert_ids_from_probe(basis_a, arkit["tongueOut"])
    n = len(basis_a)
    report["arkit_poses"] = len(arkit)
    report["verts"] = n
    report["tongue_verts"] = len(tongue_a)
    report["tongue_probe"] = "tongueOut >1mm"

    # Reference magnitude already on record: jawOpen 32.7 mm on Telly.
    jaw_mx, jaw_t = measure(basis_a, arkit["jawOpen"], tongue_a)
    report["jawOpen_max_mm"] = round(jaw_mx, 2)
    log(f"jawOpen max = {jaw_mx:.2f} mm (recorded reference: 32.7 mm)")

    reset()
    obj_b, basis_b, baked = capture(
        os.path.join(SRC, "LS_tg_visemes.fbx"),
        os.path.join(SRC, "tg_viseme_pose_names.json"), "visemes")
    # Same vertex ORDER on both imports (same source mesh), so the probe set
    # derived from the ARKit capture indexes the viseme capture correctly.
    tongue_b = tongue_a
    report["baked_poses"] = len(baked)
    if len(basis_b) != len(basis_a):
        raise RuntimeError("vertex counts differ between the two bakes; the "
                           "tongue probe set cannot be shared")

    rows = {}
    for name, recipe in VISEME_RECIPES.items():
        row = {}
        if name in baked:
            mx, tmx = measure(basis_b, baked[name], tongue_b)
            row["baked_max_mm"] = round(mx, 2)
            row["baked_tongue_mm"] = round(tmx, 2)
        comp = compose(basis_a, arkit, recipe, n)
        mx, tmx = measure(basis_a, comp, tongue_a)
        row["composed_max_mm"] = round(mx, 2)
        row["composed_tongue_mm"] = round(tmx, 2)
        rows[name] = row
    report["shapes"] = rows

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    log("")
    log(f"{'shape':14s} {'baked mm':>9s} {'comp mm':>9s} "
        f"{'baked tongue':>13s} {'comp tongue':>12s}")
    for name, r in rows.items():
        log(f"{name:14s} {r.get('baked_max_mm', float('nan')):9.2f} "
            f"{r['composed_max_mm']:9.2f} "
            f"{r.get('baked_tongue_mm', float('nan')):13.2f} "
            f"{r['composed_tongue_mm']:12.2f}")
    log(f"wrote {OUT}")


main()
