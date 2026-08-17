"""Stage 05, UE half — export each configured AnimSequence to FBX.

Run inside UnrealEditor-Cmd via run_animbake.ps1. Reads animset.json, exports
one FBX per clip into _work/fbx/, and verifies each file exists and is
non-trivial afterwards — stages report intent, not outcome (handoff §6.1), so
the artifact check lives here rather than in the caller's exit code.
"""

import json
import os
import sys

import unreal  # type: ignore

STAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FBX_DIR = os.path.join(STAGE_DIR, "_work", "fbx")


def _log(msg):
    unreal.log(f"[stage05-anim] {msg}")


def _load_animset():
    path = os.path.join(STAGE_DIR, "animset.json")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    clips = {}
    clips.update(cfg.get("required") or {})
    clips.update(cfg.get("gestures") or {})
    return cfg, clips


def _export_one(clip_name, asset_path):
    if not unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        _log(f"  {clip_name}: ASSET NOT FOUND {asset_path}")
        return False
    anim = unreal.EditorAssetLibrary.load_asset(asset_path)
    if anim is None or not isinstance(anim, unreal.AnimSequence):
        _log(f"  {clip_name}: not an AnimSequence: {asset_path} "
             f"({type(anim).__name__ if anim else 'None'})")
        return False

    out_fbx = os.path.join(FBX_DIR, f"{clip_name}.fbx")

    options = unreal.FbxExportOption()
    options.ascii = False
    options.export_morph_targets = False
    options.export_preview_mesh = False   # skeleton + animation only
    options.map_skeletal_motion_to_root = False

    task = unreal.AssetExportTask()
    task.object = anim
    task.filename = out_fbx
    task.automated = True
    task.replace_identical = True
    task.prompt = False
    task.options = options

    ok = unreal.Exporter.run_asset_export_task(task)

    # Assert on the artifact, not the return value: an exporter that "succeeds"
    # into a 0-byte file has happened elsewhere in this pipeline family.
    if not ok or not os.path.isfile(out_fbx) or os.path.getsize(out_fbx) < 10_000:
        size = os.path.getsize(out_fbx) if os.path.isfile(out_fbx) else "absent"
        _log(f"  {clip_name}: EXPORT FAILED (ok={ok}, file={size})")
        return False

    _log(f"  {clip_name}: exported {os.path.getsize(out_fbx)/1024:.0f} KB "
         f"({anim.get_editor_property('sequence_length'):.2f}s) <- {asset_path}")
    return True


def main():
    os.makedirs(FBX_DIR, exist_ok=True)
    _cfg, clips = _load_animset()

    empty = sorted(n for n, p in clips.items() if not p)
    if empty:
        _log(f"animset.json has unsourced clips: {empty} — fill in the UE asset "
             f"paths (decision D7 pack) and re-run")
        sys.exit(2)

    failed = []
    for name, path in sorted(clips.items()):
        if not _export_one(name, path):
            failed.append(name)

    if failed:
        _log(f"FAILED clips: {failed}")
        sys.exit(1)
    _log(f"all {len(clips)} clips exported to {FBX_DIR}")


if __name__ == "__main__":
    main()
