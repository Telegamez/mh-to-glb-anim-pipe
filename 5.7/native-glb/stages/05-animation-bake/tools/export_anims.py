"""Stage 05, UE half — export each configured AnimSequence to GLB.

Run inside UnrealEditor-Cmd via run_animbake.ps1.

## Why glTF and not FBX (v2 — the v1 lesson, learned on screen)

v1 exported FBX. The first evaluation showed a detached head and shredded geometry,
and the artifact numbers said why: Blender's FBX importer builds different bone-local
frames than its glTF importer, and FBX carried centimetres where the characters are
metres — anim Hips rest y 90.1 against the character's 0.871. Rotation and position
tracks live in per-bone local space, so tracks authored in one convention applied to a
skeleton built in the other shear the mesh apart.

The characters go UE-glTF → Blender-glTF → runtime. Animations must ride the SAME
toolchain, with stage 01's exact export options (`export_uniform_scale 0.01` is what
lands the file in metres). Same exporter, same units, same frames — by construction,
not by hope. The Blender half also carries a units gate now, so this class of failure
cannot ship silently again.
"""

import json
import os
import sys

import unreal  # type: ignore

STAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GLB_DIR = os.path.join(STAGE_DIR, "_work", "glb")


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


def _make_gltf_options():
    """Stage 01's option pattern, adapted for animation export.

    `export_uniform_scale 0.01` is the units contract — it is what makes the
    character GLBs metres, so it is what makes these tracks metres.
    `export_preview_mesh True` is the one deliberate inversion from stage 01:
    a standalone AnimSequence needs the skeleton's preview mesh exported to
    carry the node tree; the Blender half strips the geometry.
    """
    opts = unreal.GLTFExportOptions()
    for k, v in (
        ("export_uniform_scale", 0.01),
        ("export_preview_mesh", True),
        ("export_morph_targets", False),
        ("export_vertex_skin_weights", True),
        ("default_level_of_detail", 0),
        ("skip_near_default_values", True),
    ):
        try:
            opts.set_editor_property(k, v)
        except Exception as e:  # noqa: BLE001 — property sets vary by UE version
            _log(f"  GLTFExportOption.{k} not set: {e}")
    return opts


def _export_one(clip_name, asset_path, opts):
    if not unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        _log(f"  {clip_name}: ASSET NOT FOUND {asset_path}")
        return False
    anim = unreal.EditorAssetLibrary.load_asset(asset_path)
    if anim is None or not isinstance(anim, unreal.AnimSequence):
        _log(f"  {clip_name}: not an AnimSequence: {asset_path} "
             f"({type(anim).__name__ if anim else 'None'})")
        return False

    out_glb = os.path.join(GLB_DIR, f"{clip_name}.glb")

    task = unreal.AssetExportTask()
    task.object = anim
    task.filename = out_glb
    task.automated = True
    task.replace_identical = True
    task.prompt = False
    task.options = opts

    ok = unreal.Exporter.run_asset_export_task(task)

    # Assert on the artifact, not the return value.
    if not ok or not os.path.isfile(out_glb) or os.path.getsize(out_glb) < 10_000:
        size = os.path.getsize(out_glb) if os.path.isfile(out_glb) else "absent"
        _log(f"  {clip_name}: EXPORT FAILED (ok={ok}, file={size})")
        return False

    _log(f"  {clip_name}: exported {os.path.getsize(out_glb)/1024:.0f} KB "
         f"({anim.get_editor_property('sequence_length'):.2f}s) <- {asset_path}")
    return True


def main():
    os.makedirs(GLB_DIR, exist_ok=True)
    _cfg, clips = _load_animset()

    empty = sorted(n for n, p in clips.items() if not p)
    if empty:
        _log(f"animset.json has unsourced clips: {empty} — fill in the UE asset "
             f"paths (decision D7 pack) and re-run")
        sys.exit(2)

    opts = _make_gltf_options()
    failed = []
    for name, path in sorted(clips.items()):
        if not _export_one(name, path, opts):
            failed.append(name)

    if failed:
        _log(f"FAILED clips: {failed}")
        sys.exit(1)
    _log(f"all {len(clips)} clips exported to {GLB_DIR}")


if __name__ == "__main__":
    main()
