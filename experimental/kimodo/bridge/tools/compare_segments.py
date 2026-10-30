"""Retarget fidelity: angle between matching body-segment directions, SOMA source vs MetaHuman bake.

blender --factory-startup --background --python compare_segments.py -- --bvh take.bvh --glb baked.glb

Both are brought into Blender world space (Z-up). Frames are matched by normalised time. A good
FK retarget keeps segment directions close (a few degrees); proportions differ, directions should
not. Prints per-segment median / max angle and the forward-lean of the spine for both.
"""
import argparse
import statistics
import sys

import bpy
from mathutils import Vector

PAIRS = {
    # name: (source_from, source_to, target_from, target_to)
    "spine": ("Hips", "Neck1", "pelvis", "neck_01"),
    "neck_head": ("Neck1", "Head", "neck_01", "head"),
    "upperarm_l": ("LeftArm", "LeftForeArm", "upperarm_l", "lowerarm_l"),
    "forearm_l": ("LeftForeArm", "LeftHand", "lowerarm_l", "hand_l"),
    "upperarm_r": ("RightArm", "RightForeArm", "upperarm_r", "lowerarm_r"),
    "forearm_r": ("RightForeArm", "RightHand", "lowerarm_r", "hand_r"),
    "thigh_l": ("LeftLeg", "LeftShin", "thigh_l", "calf_l"),
    "shin_l": ("LeftShin", "LeftFoot", "calf_l", "foot_l"),
    "thigh_r": ("RightLeg", "RightShin", "thigh_r", "calf_r"),
    "shin_r": ("RightShin", "RightFoot", "calf_r", "foot_r"),
    "shoulders": ("LeftArm", "RightArm", "upperarm_l", "upperarm_r"),
    "hips_lr": ("LeftLeg", "RightLeg", "thigh_l", "thigh_r"),
}


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--bvh", required=True)
    p.add_argument("--glb", required=True)
    p.add_argument("--samples", type=int, default=24)
    return p.parse_args(argv)


def _range(arm):
    act = arm.animation_data.action
    return act.frame_range[0], act.frame_range[1]


def _pos(arm, bone):
    return arm.matrix_world @ arm.pose.bones[bone].head


def main():
    a = _args()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 24
    bpy.ops.import_anim.bvh(filepath=a.bvh, global_scale=0.01, frame_start=0, use_fps_scale=True,
                            rotate_mode="NATIVE", axis_forward="-Z", axis_up="Y")
    src = next(o for o in scene.objects if o.type == "ARMATURE")
    bpy.ops.import_scene.gltf(filepath=a.glb)
    tgt = next(o for o in scene.objects if o.type == "ARMATURE" and o is not src)
    s0, s1 = _range(src)
    t0, t1 = _range(tgt)

    angles = {k: [] for k in PAIRS}
    lean = {"source": [], "target": []}
    for i in range(a.samples):
        u = i / (a.samples - 1)
        # Each armature is evaluated at its own frame for the same normalised time; the scene
        # frame drives both, so evaluate them one after the other.
        dirs = {}
        for side, arm, f in (("s", src, s0 + u * (s1 - s0)), ("t", tgt, t0 + u * (t1 - t0))):
            scene.frame_set(int(f), subframe=f - int(f))
            bpy.context.view_layer.update()
            for k, (sa, sb, ta, tb) in PAIRS.items():
                b0, b1 = (sa, sb) if side == "s" else (ta, tb)
                dirs[(side, k)] = (_pos(arm, b1) - _pos(arm, b0)).normalized()
        for k in PAIRS:
            ds, dt = dirs[("s", k)], dirs[("t", k)]
            angles[k].append(ds.angle(dt) * 57.2958)
        for side, key in (("s", "source"), ("t", "target")):
            sp = dirs[(side, "spine")]
            lean[key].append(Vector((sp.x, sp.y, 0)).length and (sp.angle(Vector((0, 0, 1))) * 57.2958))

    print("[compare] per-sample (u, forearm_l, forearm_r, upperarm_r, neck_head):")
    for i in range(a.samples):
        print(f"[compare]   {i / (a.samples - 1):.2f}  {angles['forearm_l'][i]:5.1f} {angles['forearm_r'][i]:5.1f}"
              f" {angles['upperarm_r'][i]:5.1f} {angles['neck_head'][i]:5.1f}")
    print("[compare] segment            median   max   (deg between source and target direction)")
    for k, v in angles.items():
        print(f"[compare] {k:18s} {statistics.median(v):7.1f} {max(v):6.1f}")
    print(f"[compare] spine tilt from vertical: source median {statistics.median(lean['source']):.1f}"
          f"  target median {statistics.median(lean['target']):.1f}")


main()
