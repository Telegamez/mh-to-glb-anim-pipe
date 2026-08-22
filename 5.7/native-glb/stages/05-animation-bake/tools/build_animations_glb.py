"""Stage 05, Blender half — per-clip GLBs → one animation-only GLB with contract bone names.

Imports every _work/glb/<Clip>.glb, rewrites the animation f-curve paths through the
platform's 16-pair bone-rename map, and exports a single GLB whose glTF animations are
named exactly by clip. The runtime binds these onto companions by node name.

## Why f-curve paths get rewritten rather than bones renamed per armature

Each GLB import brings its own armature; only one survives into the export. Actions,
however, bind by *path string* ('pose.bones["pelvis"].rotation_quaternion'), so rewriting
the paths makes every action valid against the one surviving renamed armature — and the
rewrite count is logged per clip, because a clip whose rewrite count is zero was authored
against a non-MetaHuman skeleton and will bind nothing at runtime. That number failing
loudly here is cheaper than the harness discovering it visually later.
"""

import argparse
import json
import os
import re
import sys

import bpy  # type: ignore

# Duplicated from stage 02 `import_glb.py` (CONTRACT_BONE_RENAMES) on purpose — this
# stage must track that map exactly. If stage 02's map changes, change this with it.
CONTRACT_BONE_RENAMES = (
    ("pelvis", "Hips"),
    ("spine_01", "Spine"),
    ("spine_03", "Spine1"),
    ("spine_05", "Spine2"),
    ("neck_01", "Neck"),
    ("head", "Head"),
    ("clavicle_l", "LeftShoulder"),
    ("upperarm_l", "LeftArm"),
    ("lowerarm_l", "LeftForeArm"),
    ("hand_l", "LeftHand"),
    ("clavicle_r", "RightShoulder"),
    ("upperarm_r", "RightArm"),
    ("lowerarm_r", "RightForeArm"),
    ("hand_r", "RightHand"),
    ("FACIAL_L_Eye", "LeftEye"),
    ("FACIAL_R_Eye", "RightEye"),
)


def _log(msg):
    print(f"[stage05-glb] {msg}", flush=True)


def _rename_bones(arm):
    renamed = 0
    for src, dst in CONTRACT_BONE_RENAMES:
        b = arm.data.bones.get(src)
        if b is not None and arm.data.bones.get(dst) is None:
            b.name = dst
            renamed += 1
    return renamed


def _rewrite_action_paths(action):
    """Rewrite pose.bones["<src>"] → ["<dst>"] on every f-curve. Returns rewrites made."""
    n = 0
    for fc in action.fcurves:
        path = fc.data_path
        for src, dst in CONTRACT_BONE_RENAMES:
            token = f'pose.bones["{src}"]'
            if token in path:
                path = path.replace(token, f'pose.bones["{dst}"]')
        if path != fc.data_path:
            fc.data_path = path
            n += 1
    return n


def _trim_loop_tail(action):
    """Drop the final keyframe of every f-curve.

    UE's glTF exporter samples an AnimSequence's range INCLUSIVELY, so a clip of N
    frames ships N+1 samples. Measured across both published libraries, every clip
    has exactly `round(duration * fps) + 1` keyframes.

    For a LOOPING clip that extra sample is a defect, and a loud one. In the male
    `Idle` the first keyframe and the SECOND-TO-LAST are identical to 0.000 deg -
    the loop itself is clean - while the last sits 36.5 deg away. That is the
    largest frame-to-frame step in the clip, worth 181 frames of normal motion, and
    THREE.LoopRepeat replays it every cycle. It reads as a glitch rather than as
    motion because it repeats on a fixed period (telegamez#3321).

    Gestures are NOT trimmed. They play LoopOnce and their final frame is the pose
    they hold: `Dismiss` measures 5.2 deg against its last frame and 13.4 deg
    against its second-to-last, so dropping its tail would damage it.
    """
    trimmed = 0
    for fc in action.fcurves:
        kps = fc.keyframe_points
        # Two keys is a constant channel; there is no tail to drop.
        if len(kps) < 3:
            continue
        kps.remove(kps[len(kps) - 1])
        trimmed += 1
    for fc in action.fcurves:
        fc.update()
    return trimmed



def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--glb-dir", required=True)
    ap.add_argument("--animset", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    with open(args.animset, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # `required` clips are the conversational STATES; the runtime plays them with
    # THREE.LoopRepeat. `gestures` play LoopOnce. Only the former are trimmed, so
    # the split has to survive here rather than being merged away.
    looping = set((cfg.get("required") or {}).keys())
    clip_names = list((cfg.get("required") or {}).keys()) + \
                 list((cfg.get("gestures") or {}).keys())

    bpy.ops.wm.read_factory_settings(use_empty=True)

    keeper = None          # the one armature that survives into the export
    actions_out = []

    for clip in clip_names:
        glb = os.path.join(args.glb_dir, f"{clip}.glb")
        if not os.path.isfile(glb):
            _log(f"  {clip}: GLB MISSING at {glb}")
            sys.exit(1)

        before = set(bpy.data.actions)
        # glTF in, glTF out — the same toolchain the characters ride (stage 02/03),
        # so bone-local frames and units match them by construction. The FBX
        # detour (v1) produced centimetre tracks in alien bone frames; see the
        # module docstring of export_anims.py.
        bpy.ops.import_scene.gltf(filepath=glb)
        new_actions = [a for a in bpy.data.actions if a not in before]
        arms = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]

        if not new_actions:
            # Log the empty case — an FBX with no takes and a failed import look
            # identical in silence (§6.1).
            _log(f"  {clip}: imported but produced NO action — wrong export?")
            sys.exit(1)

        action = max(new_actions, key=lambda a: len(a.fcurves))
        action.name = clip

        # Purge the spares this import created (the glTF importer makes one action
        # per animated object). v2 leaked one into the export as 'TG-Anim_Idle_2_0'
        # and the clip-set gate caught it.
        for spare in new_actions:
            if spare is not action:
                bpy.data.actions.remove(spare)

        # MetaHuman-rig guard. The UE5 Mannequin shares the core bone names, so the
        # rename count CANNOT catch a Manny-rigged clip — it half-binds with wrong
        # proportions, which is worse than failing. MetaHuman bodies carry marker
        # bones Manny lacks (FACIAL_*, *_bulge_*); require them in the curves.
        if not any(('FACIAL_' in fc.data_path) or ('_bulge_' in fc.data_path)
                   for fc in action.fcurves):
            _log(f"  {clip}: rigged for a non-MetaHuman skeleton (Manny?) — "
                 f"no FACIAL_/_bulge_ curves. Refusing.")
            sys.exit(1)

        rewrites = _rewrite_action_paths(action)
        total = len(action.fcurves)
        _log(f"  {clip}: {total} f-curves, {rewrites} contract-renamed "
             f"({action.frame_range[1] - action.frame_range[0]:.0f} frames)")
        if rewrites == 0:
            _log(f"  {clip}: ZERO contract renames — this clip was not authored "
                 f"against the MetaHuman skeleton and will bind nothing. Refusing.")
            sys.exit(1)
        if clip in looping:
            before_n = int(action.frame_range[1] - action.frame_range[0]) + 1
            trimmed = _trim_loop_tail(action)
            after_n = int(action.frame_range[1] - action.frame_range[0]) + 1
            _log(f"  {clip}: loop tail trimmed on {trimmed} f-curves "
                 f"({before_n} -> {after_n} frames)")
            if after_n >= before_n:
                _log(f"  {clip}: frame count did not shrink - the exporter may have "
                     f"stopped emitting the inclusive extra sample. Re-measure before "
                     f"assuming this is still needed.")

        actions_out.append(action)

        if keeper is None and arms:
            keeper = arms[0]
            n = _rename_bones(keeper)
            _log(f"  keeper armature '{keeper.name}': {n} contract bones renamed")

    if keeper is None:
        _log("no armature imported — nothing to export")
        sys.exit(1)

    # Delete everything except the keeper armature: meshes, extra armatures, empties.
    # The GLB carries the node tree + animations; the runtime binds by name onto the
    # character's own skeleton, so geometry here is dead weight.
    for o in list(bpy.data.objects):
        if o is not keeper:
            bpy.data.objects.remove(o, do_unlink=True)

    # Stash every action as an NLA strip so the exporter emits each as a named
    # glTF animation targeting the keeper.
    if keeper.animation_data is None:
        keeper.animation_data_create()
    # The glTF importer stashes its own NLA track for the source animation on
    # the armature it creates ('<AssetName>_0'), and the NLA_TRACKS exporter
    # ships every track it finds — v2 and v3 both leaked exactly one extra
    # clip this way, caught both times by the clip-set gate. Start from zero.
    for tr in list(keeper.animation_data.nla_tracks):
        keeper.animation_data.nla_tracks.remove(tr)
    for act in actions_out:
        track = keeper.animation_data.nla_tracks.new()
        track.name = act.name
        track.strips.new(act.name, int(act.frame_range[0]), act)
        track.mute = True

    # No active action at export — NLA tracks are the animation set; an active
    # action would ride along as a twelfth.
    keeper.animation_data.action = None

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    bpy.ops.export_scene.gltf(
        filepath=args.out,
        export_format="GLB",
        export_animations=True,
        export_animation_mode="NLA_TRACKS",
        export_skins=False,
        export_morph=False,
        export_cameras=False,
        export_lights=False,
        export_yup=True,
    )

    if not os.path.isfile(args.out) or os.path.getsize(args.out) < 10_000:
        _log(f"EXPORT FAILED — {args.out} absent or trivial")
        sys.exit(1)

    # Verify the artifact, not the exit code: reopen the GLB and list its animations.
    import struct
    with open(args.out, "rb") as fh:
        struct.unpack("<III", fh.read(12))
        clen, _ = struct.unpack("<II", fh.read(8))
        j = json.loads(fh.read(clen).decode("utf-8"))
    got = sorted(a.get("name", "?") for a in j.get("animations", []))
    want = sorted(clip_names)
    _log(f"exported {args.out} ({os.path.getsize(args.out)/1048576:.1f} MB)")
    _log(f"animations in GLB: {got}")
    if got != want:
        _log(f"ANIMATION SET MISMATCH — wanted {want}")
        sys.exit(1)
    _log("clip set verified against animset.json")

    # Units gate — the exact failure v1 shipped. A standing humanoid's Hips
    # rest height is ~0.9 METRES; the FBX detour produced 90 (centimetres
    # passed through as-is), which sheared the mesh apart on screen. Refuse
    # any output where Hips is outside a plausible metre range.
    hips = next((n for n in j.get("nodes", []) if n.get("name") == "Hips"), None)
    if hips is None:
        _log("UNITS GATE: no 'Hips' node in output — rename did not reach the export")
        sys.exit(1)
    hy = abs((hips.get("translation") or [0, 0, 0])[1])
    if not (0.3 <= hy <= 2.0):
        _log(f"UNITS GATE: Hips rest y = {hy:.3f} — not metres. Refusing to ship.")
        sys.exit(1)
    _log(f"units gate: Hips rest y = {hy:.3f} m — OK")


if __name__ == "__main__":
    main()
