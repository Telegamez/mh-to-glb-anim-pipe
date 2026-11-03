"""ARKit regression evidence for face-fidelity slice 2.3.

Bakes the ARKit mapping TWICE from one UE session — once through a pre-change
copy of `export_glb.py`, once through the current one — so the 51 captured
ARKit shape keys can be compared directly.

    UnrealEditor-Cmd.exe <uproject> -run=pythonscript \
        -script="<repo>/tools/visemes/export_arkit_before_after.py" -unattended \
        -nosplash -AllowCommandletRendering

Environment:
    TG_ARKIT_OUT      output directory
    TG_EXPORT_BEFORE  path to the pre-change export_glb.py, e.g.
                      `git show <sha>:5.7/.../export_glb.py > before.py`
    TG_CHAR           character folder under /Game/Unpacked (default Telly)

Why this exists
---------------
Three changes on the F2 branch sit on the path that EVERY character's ARKit
bake runs through: the measured frame rate, the playback-end fix, and the
groom re-propagation. The ARKit shapes are not incidental — every FaceProfile
value Phase 1 sweeps is calibrated against them, and Phase 2's own
baked-vs-composed comparison uses them as the composed baseline. If they moved,
every number downstream moved with them and nothing would have said so.

The comparison must be against a real prior build, not against a reimplementation
of the old behaviour, so this imports the actual pre-change module.
"""
import importlib.util
import json
import os
import sys
import traceback

import unreal

OUT = os.environ.get("TG_ARKIT_OUT", "C:/temp/arkit_regression")
BEFORE = os.environ["TG_EXPORT_BEFORE"]
CHAR = os.environ.get("TG_CHAR", "Telly")
AFTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                     "5.7", "native-glb", "stages", "01-unreal-glb-export",
                     "tools", "export_glb.py")

os.makedirs(OUT, exist_ok=True)
res = {"out_dir": OUT, "character": CHAR, "before_module": BEFORE}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _face_skm(char):
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    ar.scan_paths_synchronous([f"/Game/Unpacked/{char}"], force_rescan=True)
    flt = unreal.ARFilter(package_paths=[f"/Game/Unpacked/{char}"],
                          recursive_paths=True, class_names=["SkeletalMesh"])
    for a in ar.get_assets(flt):
        if "face" in str(a.asset_name).lower():
            return unreal.EditorAssetLibrary.load_asset(str(a.package_name))
    return None


try:
    face = _face_skm(CHAR)
    res["face_skm"] = str(face.get_path_name()) if face else None

    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    ar.scan_paths_synchronous(["/MetaHumanCharacter/Face/ARKit"], force_rescan=True)
    pa = unreal.EditorAssetLibrary.load_asset(
        "/MetaHumanCharacter/Face/ARKit/PA_MetaHuman_ARKit_Mapping")
    aseq = unreal.EditorAssetLibrary.load_asset(
        "/MetaHumanCharacter/Face/ARKit/AS_MetaHuman_ARKit_Mapping")
    names = [str(n) for n in (pa.get_pose_names() or [])]
    with open(os.path.join(OUT, "arkit_pose_names.json"), "w", encoding="utf-8") as f:
        json.dump(names, f, indent=2)
    res["pose_count"] = len(names)

    mod_before = _load(BEFORE, "export_glb_before")
    mod_after = _load(AFTER, "export_glb_after")

    before_fbx = os.path.join(OUT, "LS_arkit_BEFORE.fbx")
    res["before_ok"] = mod_before._export_sequencer_bake(face, aseq, before_fbx)

    after_fbx = os.path.join(OUT, "LS_arkit_AFTER.fbx")
    res["after_ok"] = mod_after._export_sequencer_bake(
        face, aseq, after_fbx, seq_name="LS_arkit_after_export")

    res["after_measured_rate"] = mod_after._source_frame_rate(aseq)
    for label, path in (("before", before_fbx), ("after", after_fbx)):
        res[f"{label}_size"] = os.path.getsize(path) if os.path.exists(path) else None
except Exception:
    res["error"] = traceback.format_exc()[-2500:]

with open(os.path.join(OUT, "bake_report.json"), "w", encoding="utf-8") as f:
    json.dump(res, f, indent=2)
unreal.log(f"[tg-arkit] {json.dumps(res)[:400]}")
