"""Build the two review GLBs slice 2.1's [V] session compares.

    blender --background --factory-startup --python build_review_glbs.py \
        -- <dir-with-LS_arkit_full.fbx-and-LS_tg_visemes.fbx> <out-dir>

Emits, from the SAME source mesh so geometry is identical between them:

    <char>-visemes-baked.glb      the 16 poses captured from AS_TG_Visemes
    <char>-visemes-composed.glb   the same 16 names, built the old way as
                                  weighted sums of captured ARKit deltas

Why two files and not one
-------------------------
The harness drives `viseme_<name>` by name from a fixed list. Putting both
variants in one GLB would need new names, which the harness would not offer
and the operator could not hold. Two files with IDENTICAL shape names means
the existing harness compares them with no code change: load one, hold a
viseme, load the other with the same hold. Same instrument, same control,
one variable.

What these are NOT
------------------
Review assets, not deliverables. They carry the face mesh and its shape keys
and nothing else — **no materials, no textures, no skeleton, no body**. The
shapes are what is under review; a grey mesh here is correct and is not a
regression. They also skip the reweight and the mesh/bone contracts, so
`audit_models.py` will fail them by design. Do not publish them.
"""
import json
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
SRC = argv[0]
OUT = argv[1]
CHAR = argv[2] if len(argv) > 2 else "telly"
os.makedirs(OUT, exist_ok=True)

# Kept in step with stage 02's VISEME_RECIPES / MOUTHCLOSE_RECIPE. Duplicated rather than
# imported because that module needs the whole pipeline's imports; the comparison this feeds
# is only meaningful if they match, so a divergence here is a real defect.
COMPOSED = {
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
    "viseme_E": (("jawOpen", 0.32), ("mouthStretchLeft", 0.22), ("mouthStretchRight", 0.22)),
    "viseme_I": (("jawOpen", 0.10), ("mouthStretchLeft", 0.40), ("mouthStretchRight", 0.40)),
    "viseme_O": (("mouthPucker", 0.40), ("mouthFunnel", 0.45), ("jawOpen", 0.28)),
    "viseme_U": (("mouthPucker", 0.85), ("mouthFunnel", 0.12)),
    "mouthClose": (("mouthPressLeft", 0.45), ("mouthPressRight", 0.45),
                   ("mouthRollLower", 0.20), ("mouthRollUpper", 0.20)),
}


def log(m):
    print(f"[review-glb] {m}", flush=True)


def camel(n):
    if not n or n == "Default" or n.startswith("Pose_"):
        return n
    return n[0].lower() + n[1:]


def capture(fbx, names_json, label):
    """Import a Sequencer bake; return (mesh_obj, basis, {pose: positions})."""
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
    log(f"{label}: {len(out)} poses, {len(basis)} verts")
    return mesh_obj, basis, out


def bake_shapes(obj, basis, shapes, order):
    """Write `shapes` onto `obj` as shape keys, in `order`, then strip the armature.

    The armature goes because these are shape-review assets: a skinned mesh in the harness
    would also be posed by whatever clip library resolves, and the question here is what the
    morph does, not what the body does on top of it.
    """
    for m in list(obj.modifiers):
        obj.modifiers.remove(m)
    obj.parent = None

    if obj.data.shape_keys:
        obj.shape_key_clear()
    obj.shape_key_add(name="Basis", from_mix=False)
    kb_basis = obj.data.shape_keys.key_blocks[0]
    for i, co in enumerate(basis):
        kb_basis.data[i].co = co

    made = 0
    for name in order:
        positions = shapes.get(name)
        if positions is None:
            log(f"  MISSING {name}")
            continue
        kb = obj.shape_key_add(name=name, from_mix=False)
        for i, co in enumerate(positions):
            kb.data[i].co = co
        kb.value = 0.0
        made += 1
    log(f"  wrote {made} shape keys")
    return made


def isolate_tongue(obj):
    """Delete every polygon that is not teeth/tongue.

    Why this variant exists: the tongue-dependent visemes are the ones F2 is built for, and
    NO exterior view can show them. A front view is blocked by the teeth and lips; a profile
    is blocked by the cheek. `viseme_kk` is the extreme case -- velar constriction, zero lip
    signature -- so baked and composed render identically from every outside angle while
    measuring 1.3x apart internally.

    Stripping the head away makes the articulation directly visible from any angle. It looks
    alarming and it is not a deliverable; it is an inspection instrument.
    """
    import bmesh
    teeth = {i for i, ms in enumerate(obj.material_slots)
             if ms.material and any(k in ms.material.name.lower()
                                    for k in ("teeth", "tongue", "saliva"))}
    if not teeth:
        log("  no teeth/tongue slot found; leaving mesh intact")
        return 0
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    doomed = [f for f in bm.faces if f.material_index not in teeth]
    bmesh.ops.delete(bm, geom=doomed, context="FACES")
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    log(f"  isolated teeth/tongue: kept {len(obj.data.polygons)} faces")
    return len(obj.data.polygons)


# The Sequencer FBX imports at world_scale 0.01, but `evaluated_get` returns mesh-local
# positions in CENTIMETRES -- the same cm/m trap stage 02's ARKit transfer documents and
# converts for. Exporting without converting produced review heads 31 x 39 x 22 units against
# a published companion's whole body at 1.0 x 1.3 x 0.4, i.e. ~100x oversized: the harness
# camera sat INSIDE the head and the model read as "not loading".
CM_TO_M = 0.01


def to_metres(obj):
    """Scale the mesh AND its shape keys from centimetres to metres.

    `transform_apply` writes the scale into the mesh data and every key block, so the morph
    deltas convert with the basis rather than staying 100x. A node-level scale would also
    render correctly but would leave the shape-key data in cm, which anything reading the
    accessors directly (a measurement script, a downstream tool) would get wrong.
    """
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    obj.scale = (CM_TO_M, CM_TO_M, CM_TO_M)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    d = obj.dimensions
    log(f"  scaled to metres: {d.x:.3f} x {d.y:.3f} x {d.z:.3f} m")


def export(path, obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.export_scene.gltf(
        filepath=path, export_format="GLB", use_selection=True,
        export_morph=True, export_morph_normal=True,
        export_skins=False, export_animations=False,
        export_materials="NONE",
    )
    log(f"  -> {path} ({os.path.getsize(path)/1_000_000:.1f} MB)")


ORDER = list(COMPOSED.keys())

# ---------------------------------------------------------------- baked ----
obj, basis, baked = capture(
    os.path.join(SRC, "LS_tg_visemes.fbx"),
    os.path.join(SRC, "tg_viseme_pose_names.json"), "baked")
bake_shapes(obj, basis, baked, ORDER)
to_metres(obj)
export(os.path.join(OUT, f"{CHAR}-visemes-baked.glb"), obj)
isolate_tongue(obj)
export(os.path.join(OUT, f"{CHAR}-tongue-baked.glb"), obj)

# ------------------------------------------------------------- composed ----
obj, basis, arkit = capture(
    os.path.join(SRC, "LS_arkit_full.fbx"),
    os.path.join(SRC, "arkit_pose_names.json"), "arkit")

n = len(basis)
composed = {}
for name, recipe in COMPOSED.items():
    positions = [b.copy() for b in basis]
    for src, weight in recipe:
        src_positions = arkit.get(src)
        if src_positions is None:
            log(f"  {name}: missing source {src}")
            continue
        for i in range(n):
            positions[i] += (src_positions[i] - basis[i]) * weight
    composed[name] = positions
bake_shapes(obj, basis, composed, ORDER)
to_metres(obj)
export(os.path.join(OUT, f"{CHAR}-visemes-composed.glb"), obj)
isolate_tongue(obj)
export(os.path.join(OUT, f"{CHAR}-tongue-composed.glb"), obj)

log("done - load either with /dev/companion-harness?model=<url>")
