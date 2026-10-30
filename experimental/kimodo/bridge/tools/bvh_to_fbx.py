"""Kimodo SOMA BVH -> FBX that Unreal can import as SkeletalMesh + Skeleton + AnimSequence.

Run with Blender (same binary as the pipeline):
    blender --factory-startup --background --python-exit-code 1 --python bvh_to_fbx.py -- \
        --bvh take.bvh --out take.fbx

Unreal will not import a bare armature as a new skeleton, so every bone gets a small box
rigidly skinned to it. The boxes exist only to carry the skeleton; nothing downstream renders
them. Export the BVH with --bvh_standard_tpose so the bind pose is SOMA's T-pose, which is what
the IK Rig's retarget pose is authored against.

Units: Kimodo BVH is centimetres, Y-up. It is imported at scale 1 (one Blender unit = 1 cm,
scene scale_length 0.01) and exported with FBX_SCALE_UNITS, so the FBX declares centimetres and
the bone translations are already centimetres. Do NOT import at 0.01 and export FBX_SCALE_ALL:
that parks a x100 on the armature object, which Unreal's importer drops for animation tracks -
the first test take arrived with Hips 1 cm off the floor and the retarget's IK stretched the
spine 87 m.
"""

import argparse
import sys

import bpy
import bmesh
from mathutils import Matrix

BOX = 2.0  # centimetres (scene units), edge length of each placeholder box


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--bvh", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--fps", type=int, default=30)
    return p.parse_args(argv)


def _log(msg):
    print(f"[bvh_to_fbx] {msg}", flush=True)


def main():
    a = _args()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = a.fps
    scene.render.fps_base = 1.0
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 0.01  # one Blender unit = 1 cm

    bpy.ops.import_anim.bvh(
        filepath=a.bvh, global_scale=1.0, frame_start=0, use_fps_scale=False,
        update_scene_fps=False, update_scene_duration=True, rotate_mode="NATIVE",
        axis_forward="-Z", axis_up="Y",
    )
    arm = next(o for o in scene.objects if o.type == "ARMATURE")
    # Unreal turns the armature OBJECT into an extra root bone unless it is named "Armature".
    arm.name = "Armature"
    arm.data.name = "Armature"
    action = arm.animation_data.action
    f0, f1 = (int(round(v)) for v in action.frame_range)
    scene.frame_start, scene.frame_end = f0, f1
    _log(f"armature: {len(arm.data.bones)} bones, frames {f0}..{f1} @ {a.fps} fps")

    # Placeholder mesh: one box per bone at the bone head in rest pose, fully weighted to it.
    mesh = bpy.data.meshes.new("SOMA_proxy")
    obj = bpy.data.objects.new("SOMA_proxy", mesh)
    scene.collection.objects.link(obj)
    bm = bmesh.new()
    groups = []
    for bone in arm.data.bones:
        head = arm.matrix_world @ bone.head_local
        before = len(bm.verts)
        bmesh.ops.create_cube(bm, size=BOX, matrix=Matrix.Translation(head))
        groups.append((bone.name, list(range(before, len(bm.verts)))))
    bm.to_mesh(mesh)
    bm.free()
    for name, idx in groups:
        vg = obj.vertex_groups.new(name=name)
        vg.add(idx, 1.0, "REPLACE")
    obj.parent = arm
    mod = obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm

    bpy.ops.object.select_all(action="DESELECT")
    arm.select_set(True)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = arm

    bpy.ops.export_scene.fbx(
        filepath=a.out, use_selection=True, object_types={"ARMATURE", "MESH"},
        apply_scale_options="FBX_SCALE_UNITS", global_scale=1.0, add_leaf_bones=False,
        primary_bone_axis="Y", secondary_bone_axis="X",
        bake_anim=True, bake_anim_use_all_actions=False, bake_anim_use_nla_strips=False,
        bake_anim_force_startend_keying=True, bake_anim_simplify_factor=0.0,
        axis_forward="-Z", axis_up="Y",
    )
    _log(f"wrote {a.out}")


main()
