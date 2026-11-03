"""Slice 2.1 — build `AS_TG_Visemes` from the committed pose table.

Runs inside UnrealEditor-Cmd.exe:

    UnrealEditor-Cmd.exe <uproject> -run=pythonscript \
        -script="<abs>/author_viseme_anim.py -- [--poses=<json>] [--report=<json>]"

What it makes
-------------
One AnimSequence on the shared MetaHuman face skeleton, 30 fps, with:

    frame 0        Default   — all controls zero, the rest pose
    frames 1..16   the 15 contract visemes + a true `mouthClose`

Each frame keys the raw RigLogic controls named in
`config/tg_viseme_poses.json`. Stage 01 then bakes it through the *same*
transient-Level-Sequence path the ARKit mapping already uses, so RigLogic
evaluates these controls through its correctives, co-articulation and the 16
tongue joints — which is the entire point of F2. The composed
`VISEME_RECIPES` sum ARKit deltas *after* RigLogic has run and can reach none
of that.

Why the sequence is authored and not hand-posed
-----------------------------------------------
`CTRL_expressions.*` are the rig's 263 raw controls — the same channels the
face board writes, verified against the DNA's definition layer. Driving them
from a committed table gives the identical evaluation with a reviewable diff,
re-derivable for the next identity. See the pose file's `_authoring_brief`,
and note the caveat it carries: the starting weights are authored from
articulatory phonetics, and slice 2.1's [V] screenshot review is what makes
them judged rather than merely plausible.

Why 30 fps and not the ARKit sequence's 24
------------------------------------------
`AnimDataController.set_frame_rate` only accepts a rate that is a multiple or
factor of the asset's current one, and a freshly created AnimSequence is 30 fps
— so 24 is rejected outright. Rather than fight it, this sequence is 30 fps and
**stage 01 reads the rate off each sequence** instead of assuming 24. That is
the more correct shape regardless: the display-rate trap at `export_glb.py:379`
is about the Level Sequence's rate MATCHING ITS SOURCE, not about the number 24.
A hardcoded 24 was only ever right because the ARKit mapping happens to be 24.

Frame 0 is `Default`
--------------------
Deliberately mirrors `AS_MetaHuman_ARKit_Mapping`, whose pose 0 is also
`Default`. Stage 02's capture skips a pose named `Default` and maps pose N to
frame `f_start + N`; keeping the same convention means the viseme sequence
rides the existing capture logic with no new frame arithmetic — and frame
arithmetic is where the display-rate trap lived (`export_glb.py:379`).

Every control is keyed on every frame
-------------------------------------
A control keyed only where it is non-zero holds that value across the whole
sequence, so `viseme_U`'s pucker would still be on the face during
`viseme_aa`. Keying all of them at every frame, zeros included, is what makes
each frame an independent pose. This is the same class of bug as the
interpolation trap and is cheap to avoid: 45 controls x 17 frames.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

import unreal

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_POSES = os.path.join(HERE, "..", "config", "tg_viseme_poses.json")

# The 16 shapes stage 02 must end up with. Asserted here so a pose file edited
# down to 14 entries fails at authoring time rather than producing a GLB that
# silently misses two visemes — a missing morph resolves BY NAME at runtime and
# does nothing at all, which is the failure this pipeline keeps re-learning.
CONTRACT_SHAPES = tuple(
    f"viseme_{v}" for v in ("sil", "PP", "FF", "TH", "DD", "kk", "CH", "SS",
                            "nn", "RR", "aa", "E", "I", "O", "U")
) + ("mouthClose",)

REST_POSE_NAME = "Default"


def _log(msg):
    unreal.log(f"[tg-visemes] {msg}")


def _parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--poses", default=DEFAULT_POSES)
    p.add_argument("--report", default=None,
                   help="where to write the authoring report (default: beside the pose file)")
    return p.parse_args(argv)


def _load_poses(path):
    with open(path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    poses = spec.get("poses") or []
    names = [p["name"] for p in poses]

    # The contract check belongs to the VISEME table, where the 16 names are the deliverable
    # and a missing one is a shipped defect. It is the wrong gate for a table whose purpose is
    # something else -- slice 5.0's 322-pose ground-truth table sweeps single controls and
    # co-activation pairs, and has no reason to carry `viseme_sil`.
    #
    # Declared by the table rather than passed as a flag, and defaulting to "visemes", so the
    # existing pose file keeps its gate untouched and a new table has to say what it is. A
    # command-line opt-out would let the viseme table lose its contract check silently.
    kind = spec.get("table_kind", "visemes")
    if kind == "visemes":
        missing = [n for n in CONTRACT_SHAPES if n not in names]
        if missing:
            raise RuntimeError(f"pose table is missing contract shapes: {missing}")
        extra = [n for n in names if n not in CONTRACT_SHAPES]
        if extra:
            raise RuntimeError(f"pose table has shapes outside the contract: {extra}")
    elif kind != "validation":
        raise RuntimeError(f"unknown table_kind {kind!r}; expected 'visemes' or 'validation'")

    if len(set(names)) != len(names):
        raise RuntimeError("pose table has duplicate names")
    if not poses:
        raise RuntimeError("pose table is empty")

    for p in poses:
        for c, w in (p.get("controls") or {}).items():
            if not isinstance(w, (int, float)) or not (0.0 <= float(w) <= 1.0):
                raise RuntimeError(f"{p['name']}.{c} = {w!r} is outside 0..1")
    return spec


def _curve_name(prefix, control):
    """`mouthLipsPurseUL` -> `ctrl_expressions_mouthlipspurseul`.

    The AnimSequence carries RigLogic's control curves fully lowercased; the
    DNA and the face board spell them in camel case. Doing the fold here, once,
    keeps the pose table readable in the rig's own spelling.
    """
    return f"{prefix}{control.lower()}"


def _delete_if_exists(path):
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        unreal.EditorAssetLibrary.delete_asset(path)


def main():
    ns = _parse_args()
    poses_path = os.path.abspath(ns.poses)
    spec = _load_poses(poses_path)

    asset_path = spec["asset_path"]
    package_path, asset_name = asset_path.rsplit("/", 1)
    skeleton_path = spec["skeleton"]
    fps = int(spec.get("frame_rate", 24))
    prefix = spec.get("control_prefix", "ctrl_expressions_")
    poses = spec["poses"]

    report = {
        "pose_file": poses_path,
        "asset_path": asset_path,
        "frame_rate": fps,
        "errors": [],
        "warnings": [],
    }

    # --- the skeleton ------------------------------------------------------
    # Force-scan first: in a fresh commandlet the plugin content root is not
    # scanned and `load_asset` returns None for assets that are on disk. Stage
    # 01 carries this same guard for the ARKit lookup, and the bug it prevents
    # (a stage reporting success having skipped its entire job) is the worst
    # output this pipeline can produce.
    try:
        unreal.AssetRegistryHelpers.get_asset_registry().scan_paths_synchronous(
            [skeleton_path.rsplit("/", 1)[0]], force_rescan=True)
    except Exception as e:
        _log(f"  scan_paths_synchronous raised: {e}")

    skeleton = unreal.EditorAssetLibrary.load_asset(skeleton_path)
    if skeleton is None:
        raise RuntimeError(f"face skeleton not found: {skeleton_path}")
    _log(f"skeleton: {skeleton_path}")

    # --- the asset ---------------------------------------------------------
    _delete_if_exists(asset_path)
    factory = unreal.AnimSequenceFactory()
    factory.set_editor_property("target_skeleton", skeleton)
    anim = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        asset_name=asset_name, package_path=package_path,
        asset_class=unreal.AnimSequence, factory=factory)
    if anim is None:
        raise RuntimeError(f"could not create {asset_path}")

    # --- frame rate and length --------------------------------------------
    # Adding curve keys does NOT extend an AnimSequence; a sequence left at its
    # default length holds one frame and every pose past the first is dropped
    # on export. The controller is the only thing that sets this.
    frame_names = [REST_POSE_NAME] + [p["name"] for p in poses]
    n_frames = len(frame_names)

    controller = anim.get_editor_property("controller")
    controller.open_bracket("author AS_TG_Visemes")
    try:
        controller.set_frame_rate(unreal.FrameRate(fps, 1))
        # `SetNumberOfFrames` takes an FFrameNumber, not an int — a bare int
        # fails to nativize rather than being coerced.
        controller.set_number_of_frames(unreal.FrameNumber(n_frames - 1))
    finally:
        controller.close_bracket()

    # The controller silently accepts only rates compatible with the asset's
    # current one and raises otherwise, so reaching here means `fps` took. Read
    # it back anyway and put it in the report: stage 01 sets the Level
    # Sequence's display rate from this number, and a mismatch there is exactly
    # the interpolation trap — every baked frame a 50/50 blend of two poses,
    # with nothing else failing.
    # Measure the rate off the saved sequence rather than asking the model for
    # it: `data_model` is None on a sequencer-backed AnimSequence, and the
    # quantity that actually matters downstream is frames-per-second as the
    # exporter will see it. (n_frames - 1) intervals over the play length is
    # that number, from the artifact.
    # `get_num_frames` reports INTERVALS, not sample positions: the 66-pose ARKit
    # mapping reports 65 over 2.7083 s at 24 fps. So the sequence carries
    # `intervals + 1` samplable frames, and the rate is intervals / length.
    measured_frames = int(unreal.AnimationLibrary.get_num_frames(anim))
    measured_length = float(anim.get_play_length())
    measured_rate = (measured_frames / measured_length
                     if measured_length > 0 and measured_frames > 0 else 0.0)
    report["frame_rate_measured"] = round(measured_rate, 4)
    if abs(measured_rate - fps) > 0.01:
        report["errors"].append(
            f"measured frame rate {measured_rate:.4f} != configured {fps} "
            f"({measured_frames} frames over {measured_length:.4f}s) — stage 01 "
            f"sets the Level Sequence display rate from this, and a mismatch is "
            f"the interpolation trap")

    # --- curves ------------------------------------------------------------
    used = sorted({c for p in poses for c in (p.get("controls") or {})})
    _log(f"{len(poses)} poses, {len(used)} distinct controls, {n_frames} frames")

    for control in used:
        cname = _curve_name(prefix, control)
        try:
            unreal.AnimationLibrary.add_curve(
                anim, cname, unreal.RawCurveTrackTypes.RCT_FLOAT, False)
        except Exception:
            report["errors"].append(f"add_curve({cname}) failed")
            continue

    # Key EVERY control on EVERY frame — see the module docstring. Frame 0 is
    # the rest pose, so every control is zero there by construction.
    for frame, name in enumerate(frame_names):
        t = float(frame) / float(fps)
        weights = {} if name == REST_POSE_NAME else (
            next(p for p in poses if p["name"] == name).get("controls") or {})
        for control in used:
            cname = _curve_name(prefix, control)
            try:
                unreal.AnimationLibrary.add_float_curve_key(
                    anim, cname, t, float(weights.get(control, 0.0)))
            except Exception:
                report["errors"].append(f"add_float_curve_key({cname}@{frame}) failed")

    unreal.EditorAssetLibrary.save_loaded_asset(anim, only_if_is_dirty=False)

    # --- assert on the artifact, not on the calls --------------------------
    # Every step above can fail quietly; what matters is what the saved asset
    # contains. Re-read it and measure.
    reloaded = unreal.EditorAssetLibrary.load_asset(asset_path)
    # Intervals, per the note above; the samplable frames are 0..intervals.
    actual_intervals = int(unreal.AnimationLibrary.get_num_frames(reloaded))
    actual_frames = actual_intervals + 1
    actual_curves = [str(c) for c in unreal.AnimationLibrary.get_animation_curve_names(
        reloaded, unreal.RawCurveTrackTypes.RCT_FLOAT)]
    report["frames"] = actual_frames
    report["intervals"] = actual_intervals
    report["curve_count"] = len(actual_curves)
    report["play_length"] = float(reloaded.get_play_length())

    if actual_frames != n_frames:
        report["errors"].append(
            f"frame count {actual_frames} != expected {n_frames}")
    expected_curves = {_curve_name(prefix, c) for c in used}
    absent = sorted(expected_curves - set(actual_curves))
    if absent:
        report["errors"].append(f"curves missing from the saved asset: {absent}")

    # Consecutive frames must DIFFER. A pose export that never posed passes
    # every other check — the sequence exists, the curves exist, the frame count
    # is right, and every frame is identical. This is the one assertion that
    # catches it.
    opts = unreal.AnimPoseEvaluationOptions()
    signatures = []
    for frame in range(actual_frames):
        try:
            pose = reloaded.get_anim_pose_at_frame(frame, opts)
            sig = {}
            for cn in actual_curves:
                try:
                    w = float(pose.get_curve_weight(cn))
                except Exception:
                    w = 0.0
                if abs(w) > 1e-4:
                    sig[cn] = round(w, 4)
            signatures.append(sig)
        except Exception:
            report["errors"].append(f"could not evaluate frame {frame}")
            signatures.append(None)

    identical = [i for i in range(1, len(signatures))
                 if signatures[i] is not None and signatures[i] == signatures[i - 1]]
    # `viseme_sil` is legitimately zero-delta, so frame 1 matching frame 0
    # (Default) is expected and is not evidence of a failed pose.
    sil_frame = frame_names.index("viseme_sil") if "viseme_sil" in frame_names else -1
    unexpected = [i for i in identical if i != sil_frame]
    if unexpected:
        report["errors"].append(
            "consecutive frames are identical (the pose export did not pose): "
            + ", ".join(f"{frame_names[i]}=={frame_names[i-1]}" for i in unexpected))

    report["per_frame_nonzero_controls"] = {
        frame_names[i]: len(s) for i, s in enumerate(signatures) if s is not None}

    tongue_frames = {
        frame_names[i]: sorted(k for k in s if "tongue" in k)
        for i, s in enumerate(signatures)
        if s and any("tongue" in k for k in s)}
    report["tongue_controls_per_frame"] = tongue_frames

    # --- the names JSON stage 02 maps frames through ----------------------
    out_dir = os.path.dirname(poses_path)
    names_json = os.path.join(out_dir, "tg_viseme_pose_names.json")
    with open(names_json, "w", encoding="utf-8") as f:
        json.dump(frame_names, f, indent=2)
    report["pose_names_json"] = names_json

    report_path = ns.report or os.path.join(out_dir, "tg_viseme_authoring_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    for w in report["warnings"]:
        _log(f"  WARN {w}")
    for e in report["errors"]:
        _log(f"  ERROR {e}")
    _log(f"{asset_path}: {actual_frames} frames, {len(actual_curves)} curves, "
         f"{len(tongue_frames)} frame(s) with tongue articulation")
    _log(f"wrote {report_path}")

    if report["errors"]:
        raise RuntimeError(f"authoring produced {len(report['errors'])} error(s); "
                           f"see {report_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        unreal.log_error("[tg-visemes] FAILED\n" + traceback.format_exc())
        raise
