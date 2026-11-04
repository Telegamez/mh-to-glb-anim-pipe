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


# Largest rotation away from the bind pose, on any bone, that the final key may have and
# still count as the evaluated bake's spurious bind sample. Measured 2026-11-03 on raw UE
# exports of nine clips: 0.19 deg at most, against up to 38 deg for the key before it.
BIND_TAIL_MAX_DEG = 1.0


def _tail_bind_distance(action):
    """Largest rotation of the final key away from the bind pose, over every bone.

    In pose space the bind pose is the identity quaternion, so this is the angle of each
    bone's last rotation_quaternion key from (1, 0, 0, 0). Returns (deg, bone).
    """
    import math
    last = {}
    for fc in action.fcurves:
        m = re.match(r'pose\.bones\["(.+)"\]\.rotation_quaternion$', fc.data_path)
        if not m or len(fc.keyframe_points) < 3:
            continue
        last.setdefault(m.group(1), [1.0, 0.0, 0.0, 0.0])[fc.array_index] = \
            fc.keyframe_points[len(fc.keyframe_points) - 1].co[1]
    worst = (0.0, "")
    for bone, q in last.items():
        n = math.sqrt(sum(c * c for c in q)) or 1.0
        deg = math.degrees(2 * math.acos(min(1.0, abs(q[0]) / n)))
        if deg > worst[0]:
            worst = (deg, bone)
    return worst


def _trim_bind_tail(action):
    """Drop the final keyframe of every f-curve: the evaluated bake's bind-pose sample.

    `bake_evaluated_anims.py` records frames 0..N of a Level Sequence whose skeletal
    animation section spans [0, N), so every clip ships N+1 samples and the last one
    is the BIND pose - measured on raw UE exports at <= 0.19 deg from bind on every
    bone, against up to 38 deg for the key before it. (Inferred cause: frame N lies
    outside the section's exclusive end, so the mesh falls back to its reference
    pose. The measurement, not the cause, is what the guard below checks.) It is not
    authored motion on any clip.

    LOOPING clips (telegamez#3321): THREE.LoopRepeat replays the jump into that
    sample every cycle. In the male `Idle` the first keyframe and the SECOND-TO-LAST
    are identical to 0.000 deg while the last sat 36.5 deg away.

    GESTURES play LoopOnce and hold their final frame, so the same sample is the
    pose they END on: Shrug's last key sat 39.2 deg from its real last frame,
    HeadShake/Angry 24.7 deg. These were left untrimmed on the belief that the last
    frame was authored; the evidence for that was `Dismiss`, whose shipped tail
    looked genuine. It was - but only because Blender's 24 fps export resample of a
    2.533 s clip stops at 2.500 s, a real 30 fps key, one sample short of the bind
    key at 2.533 s. Clips whose length is a whole number of 24 fps frames (Shrug,
    HeadShake) ship the bind key itself; the rest (Nod) blend a fraction of it into
    their last sample. Trimming the imported bind key before the resample fixes all
    three cases and leaves Dismiss's samples as they were.

    The trim is guarded: the removed key must be bind-like (BIND_TAIL_MAX_DEG). If
    the bake ever stops emitting that sample, the guard refuses rather than cutting
    a real frame. Returns the number of f-curves trimmed.
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



FINGER_BONE = re.compile(r'pose\.bones\["((?:thumb|index|middle|ring|pinky)_[^"]*)"\]\.(\w+)$')
WRIST_CORRECTIVE = re.compile(r'pose\.bones\["(wrist_(?:inner|outer)_[lr])"\]\.(\w+)$')
# An animset entry's hold options: key -> the bones it holds at Idle's first frame.
HOLDS = {"fingers": FINGER_BONE, "wrist": WRIST_CORRECTIVE}


def _first_values(action):
    """{(data_path, index): value at the first key} for every f-curve of `action`."""
    return {(fc.data_path, fc.array_index): fc.keyframe_points[0].co[1]
            for fc in action.fcurves if len(fc.keyframe_points)}


def _hold_fingers(action, held, pattern=FINGER_BONE):
    """Replace every f-curve of `action` whose bone matches `pattern` with a constant from `held` (Idle's first frame).

    `fingers`: retargeted clips (the Avaturn dances) carry their source rig's finger animation, which reads as
    half-curled claws on a MetaHuman hand. The runtime's hand-pose layer owns intentional finger
    shapes, so these clips keep the hand relaxed instead: Idle's pose, which every clip crossfades
    from and back to.

    `wrist`: the wrist correctives. Wave (TG-Anim_Wave_3) drives wrist_inner_r 61 deg and slides wrist_outer_r
    3.3 cm, against 7 deg and under 1 cm in every other gesture, and the skin dents at the wrist
    (panda UAT, 2026-11-04). Held at Idle, the wrist deforms as plain skinning, as it does in Dance.

    A channel Idle does not have is dropped. Returns (held, dropped).
    """
    f0, f1 = action.frame_range
    kept = dropped = 0
    for fc in list(action.fcurves):
        if not pattern.match(fc.data_path):
            continue
        v = held.get((fc.data_path, fc.array_index))
        if v is None:
            action.fcurves.remove(fc)
            dropped += 1
            continue
        kps = fc.keyframe_points
        while len(kps):
            kps.remove(kps[0])
        kps.insert(f0, v)
        kps.insert(f1, v)
        fc.update()
        kept += 1
    return kept, dropped


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
    # THREE.LoopRepeat. `gestures` play LoopOnce. Both lose the bake's bind tail;
    # the split survives for the log and the seam gate.
    looping = set((cfg.get("required") or {}).keys())
    clip_names = list((cfg.get("required") or {}).keys()) + \
                 list((cfg.get("gestures") or {}).keys())
    specs = {**(cfg.get("required") or {}), **(cfg.get("gestures") or {})}
    idle_fingers = None

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
        # Every clip, looping or not, ends on the bake's bind-pose sample; see
        # _trim_bind_tail. Prove the key is that sample before removing it.
        tail_deg, tail_bone = _tail_bind_distance(action)
        if tail_deg > BIND_TAIL_MAX_DEG:
            _log(f"  {clip}: final key is {tail_deg:.2f} deg from bind on {tail_bone} - "
                 f"not the bake's spurious bind sample. Refusing to trim authored motion; "
                 f"re-measure the bake's tail before changing BIND_TAIL_MAX_DEG.")
            sys.exit(1)
        kind = "loop" if clip in looping else "gesture"
        before_end = action.frame_range[1]
        trimmed = _trim_bind_tail(action)
        after_end = action.frame_range[1]
        # The 30 fps keys land on 24 fps export frames as floats: Dismiss's real last key
        # (2.500 s) sits at frame 59.99999, and the exporter's sampler floors it to 59 and
        # drops that authored key. Pin a near-integer end to the integer it means.
        snapped = round(after_end)
        if abs(after_end - snapped) < 1e-3 and after_end != snapped:
            action.use_frame_range = True
            action.frame_start = round(action.frame_range[0])
            action.frame_end = snapped
            _log(f"  {clip}: end frame {after_end:.6f} pinned to {snapped} so its last key is sampled")
            after_end = action.frame_range[1]
        _log(f"  {clip}: {kind} bind tail trimmed on {trimmed} f-curves (removed key "
             f"<= {tail_deg:.3f} deg from bind; end {before_end:.2f} -> {after_end:.2f})")
        if after_end >= before_end:
            _log(f"  {clip}: the clip did not get shorter - the bake may have stopped "
                 f"emitting the bind sample. Re-measure before assuming this is needed.")
            sys.exit(1)

        if clip == "Idle":
            idle_fingers = _first_values(action)
        for key, pattern in HOLDS.items():
            if not (isinstance(specs.get(clip), dict) and specs[clip].get(key) == "idle"):
                continue
            if not idle_fingers:
                _log(f"  {clip}: {key} 'idle' asked for, but Idle has not been read. "
                     f"Idle must be in 'required'. Refusing.")
                sys.exit(1)
            held, dropped = _hold_fingers(action, idle_fingers, pattern)
            if not held:
                _log(f"  {clip}: {key} 'idle' matched no f-curves. Refusing rather than ship a no-op.")
                sys.exit(1)
            _log(f"  {clip}: {key} held at Idle's first frame on {held} f-curves, {dropped} dropped")

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
