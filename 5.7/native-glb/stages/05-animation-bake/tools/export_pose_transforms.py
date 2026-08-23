"""Ground truth: Unreal's own evaluated pose, per bone, per frame, per character.

## What this is for

Every deformation question on this project — does the browser deform like Unreal, which bones
disagree, is a compensation earning its keep — needs one thing: Unreal's answer. Without it,
tools measure our renderer against our own assumptions, which is how five real defects were
"fixed" without any of them changing what a person sees.

It also has to scale. A heterogeneous roster means every character has its own mesh, its own
reference pose, and potentially its own post-process Anim Blueprint, so ground truth is
per-character by construction: `evaluate_animation_blueprint_with_input_pose` takes the target
mesh, and the reference pose is read from the same pose object.

## The API, and why the earlier attempts failed

    get_anim_pose_at_time(anim, t, options)                       the raw AnimSequence pose
    evaluate_animation_blueprint_with_input_pose(pose, mesh, abp) runs the post-process ABP
    get_bone_pose(pose, bone, WORLD)                              component-space transform
    get_ref_bone_pose(pose, bone, WORLD)                          the mesh's reference pose

No component, nothing to tick, no Sequencer. That matters, because every other route is a dead
end and each cost a cycle to discover:

  * `SkeletalMeshComponent.set_position` does not evaluate anything in an editor world. Every
    frame it exported was byte-identical, and a chain of conclusions was drawn from what was
    really the reference pose repeated six times.
  * `LevelSequenceEditorBlueprintLibrary.open_level_sequence` CRASHES a commandlet - it needs the
    editor UI.
  * `convert_actors` returns without producing a mesh for a skeletal actor.
  * Alembic is import-only in Unreal's Python; there is no exporter.

## STATUS 2026-08-22: the ABP-evaluation call crashes on the FACE mesh

`evaluate_animation_blueprint_with_input_pose` aborts inside the engine's own C++ when the
mesh is `SKM_Telly_FaceMesh` and the ABP is `ABP_Face_PostProcess`:

    Script Stack (1 frames):
    /Script/AnimationBlueprintLibrary.AnimPoseExtensions.EvaluateAnimationBlueprintWithInputPose
    Assertion failed: (Index >= 0) & (Index < ArrayNum)
    Array index out of bounds: 737 into an array of size 737

Gate 1 passes first — the ABP is found and logged — and the clip loads. The crash is one frame
deep in engine code, in compact-pose bookkeeping we do not control.

**Scope, established by control experiment rather than by argument.** The same command was run
against `AS_TG_Visemes` (17 frames, 45 curves) instead of `AS_TG_Validation` (323 frames, 263
curves). Identical crash, identical numbers. So it is structural, not data, and not a function
of sequence size.

**What is NOT established: whether this is a UE 5.8 regression or has always been true of the
face path.** The intended control — the same call against a BODY mesh and a body clip, which
is reported to have worked on 2026-08-18 — could not be run: the clip paths in
`stages/05-animation-bake/animset.json` (e.g. `/Game/Animations/Shrug_Anim_Don_t_Know`) do not
resolve through `EditorAssetLibrary.load_asset` on this machine, and a registry-scanning lister
run to find valid paths produced no output. Four commandlet runs were spent; the question was
left open rather than answered by guessing. **737 matches no bone set on this character** —
1185 merged, 841 `FACIAL_*`, 344 body — so it is internal to the face SKM's own skeleton or to
the ABP.

**Do not debug this to use this tool.** Phase 5 needs the transforms, not this API, and a
proven mechanism produces the same bytes: `bake_evaluated_anims.py` records an ABP-evaluated
pose through the Sequencer, which is the mechanism behind every trusted artifact on this
project — all 51 ARKit shapes on six characters, the stage-05 body bakes, and the viseme bake.
Bake the sequence with `--anim=`, then read the BAKED sequence here with `--raw-only`, which
uses `get_anim_pose_at_time` alone and never touches the crashing call. The raw pose of an
evaluated bake IS the evaluated pose of the original.

Gate 3 then becomes a comparison between two dumps rather than a check inside one process, so
both carry `run_id` and `source_anim` in `meta` and the comparator refuses a mismatch — a gate
split across two files is a gate that a re-run of one side can silently defeat.

## The gates

Three, because the failures above were all silent and each produced a plausible-looking file:

  1. the mesh must have a post-process ABP, or rig logic never runs
  2. consecutive frames must DIFFER, or the pose was never driven
  3. the evaluated pose must differ from the RAW pose, or the ABP did not contribute

Gate 3 is the one that matters. It separates "an animation played" from "MetaHuman's rig logic
ran", and nothing downstream can tell those apart by looking at the file.
"""
from __future__ import annotations

import json
import os
import sys

import unreal  # type: ignore

DIAG: list[str] = []


def _log(msg):
    line = f"[pose_transforms] {msg}"
    DIAG.append(str(msg))
    print(line, flush=True)
    unreal.log(line)


def _arg(name, default=None):
    for a in sys.argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default


def _pack(transform):
    t, r, s = transform.translation, transform.rotation, transform.scale3d
    return [t.x, t.y, t.z, r.x, r.y, r.z, r.w, s.x, s.y, s.z]


def main():
    mesh_path = _arg("mesh")
    anim_path = _arg("anim")
    out_path = _arg("out")
    fps = float(_arg("fps", "30"))
    if not (mesh_path and anim_path and out_path):
        raise RuntimeError("need --mesh= --anim= --out=")

    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    anim = unreal.EditorAssetLibrary.load_asset(anim_path)
    if mesh is None or anim is None:
        raise RuntimeError(f"missing asset: mesh={mesh_path} anim={anim_path}")

    abp = mesh.get_editor_property("post_process_anim_blueprint")
    if not abp:
        raise RuntimeError(
            f"GATE 1 FAILED: {mesh_path} has no post-process Anim Blueprint. Rig logic would not "
            f"run and the result would be the raw AnimSequence wearing a ground-truth label.")
    _log(f"mesh={mesh_path}")
    _log(f"post-process ABP class={abp.get_path_name()}")

    # The mesh stores the GENERATED CLASS; evaluate_animation_blueprint_with_input_pose wants the
    # AnimationBlueprint asset. The class path is the asset path with a `_C` suffix.
    class_path = abp.get_path_name()
    asset_path = class_path[:-2] if class_path.endswith("_C") else class_path
    abp_asset = unreal.EditorAssetLibrary.load_asset(asset_path)
    if abp_asset is None:
        raise RuntimeError(
            f"GATE 1 FAILED: could not load the AnimationBlueprint behind {class_path}. The mesh "
            f"holds a generated class and this stage needs the asset it was generated from.")
    _log(f"post-process ABP asset={asset_path}")

    options = unreal.AnimPoseEvaluationOptions()
    for prop, value in (("evaluate_curves", True), ("retrieve_additive_as_full_pose", True)):
        try:
            options.set_editor_property(prop, value)
        except Exception as e:  # noqa: BLE001
            _log(f"  option {prop}: {e}")

    ape = unreal.AnimPoseExtensions
    length = float(anim.get_editor_property("sequence_length"))
    frames = max(2, int(round(length * fps)) + 1)
    _log(f"clip={anim_path} length={length:.3f}s frames={frames} @ {fps}fps")

    bone_names = None
    ref = None
    raw_rows, eval_rows, times = [], [], []
    abp_delta_max = 0.0

    for k in range(frames):
        t = length * k / (frames - 1)
        raw_pose = ape.get_anim_pose_at_time(anim, t, options)
        if not ape.is_valid(raw_pose):
            raise RuntimeError(f"invalid pose at t={t:.3f}")
        evaluated = ape.evaluate_animation_blueprint_with_input_pose(
            raw_pose, mesh, abp_asset)
        if not ape.is_valid(evaluated):
            raise RuntimeError(f"ABP evaluation produced an invalid pose at t={t:.3f}")

        if bone_names is None:
            bone_names = [str(b) for b in ape.get_bone_names(evaluated)]
            ref = [_pack(ape.get_ref_bone_pose(evaluated, b, unreal.AnimPoseSpaces.WORLD))
                   for b in bone_names]
            _log(f"{len(bone_names)} bones")

        raw = [_pack(ape.get_bone_pose(raw_pose, b, unreal.AnimPoseSpaces.WORLD))
               for b in bone_names]
        ev = [_pack(ape.get_bone_pose(evaluated, b, unreal.AnimPoseSpaces.WORLD))
              for b in bone_names]
        for a, b in zip(raw, ev):
            for x, y in zip(a[:3], b[:3]):
                abp_delta_max = max(abp_delta_max, abs(x - y))
        raw_rows.append(raw)
        eval_rows.append(ev)
        times.append(t)
        if k % 200 == 0:
            _log(f"  frame {k}/{frames} t={t:.2f}s")

    # GATE 2 — the pose must actually change between frames.
    varied = any(eval_rows[i] != eval_rows[0] for i in range(1, len(eval_rows)))
    if not varied:
        raise RuntimeError(
            "GATE 2 FAILED: every sampled frame is identical. The animation was not driven; "
            "refusing to write a static file that would be mistaken for ground truth.")

    # GATE 3 — the ABP must have contributed, or rig logic did not run.
    if abp_delta_max < 1e-4:
        raise RuntimeError(
            f"GATE 3 FAILED: the evaluated pose is identical to the raw AnimSequence pose "
            f"(max delta {abp_delta_max:.6f}cm). The post-process ABP did not contribute, so "
            f"this is not MetaHuman's deformation.")
    _log(f"GATES PASSED: frames vary, and the ABP moves bones by up to {abp_delta_max:.2f}cm "
         f"against the raw sequence")

    import numpy as np
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    np.savez_compressed(
        out_path,
        bone_names=np.array(bone_names, dtype=object),
        ref=np.array(ref, dtype=np.float32),
        times=np.array(times, dtype=np.float32),
        raw=np.array(raw_rows, dtype=np.float32),
        evaluated=np.array(eval_rows, dtype=np.float32),
        meta=np.array([json.dumps({
            "mesh": mesh_path, "anim": anim_path, "abp": asset_path, "length": length,
            "fps": fps, "units": "centimetres, Unreal component space",
            "abp_delta_max_cm": abp_delta_max, "diagnostics": DIAG,
        })], dtype=object),
    )
    _log(f"wrote {out_path} ({os.path.getsize(out_path)/1e6:.2f} MB)")


main()
