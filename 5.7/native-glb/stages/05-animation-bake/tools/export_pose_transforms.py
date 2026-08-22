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
