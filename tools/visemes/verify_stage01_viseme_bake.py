"""Slice 2.2 validation harness — exercise the real stage-01 code path.

Imports `export_glb.py` as a module and calls `_export_tg_viseme_sources`
against Telly's face SkeletalMesh, into a scratch directory. No character
workspace required, and no stage-00 rebuild: this validates the new export
path itself, which is what the slice claims.
"""
import importlib.util
import json
import os
import sys
import traceback

import unreal

STAGE01 = ("s:/Avatars/metahuman/metahuman-to-glb/5.7/native-glb/"
           "stages/01-unreal-glb-export/tools/export_glb.py")
OUT = os.environ.get("TG_VERIFY_OUT", "C:/temp/tg_viseme_verify")
REPORT = os.environ.get("TG_VERIFY_REPORT", os.path.join(OUT, "verify.json"))

os.makedirs(OUT, exist_ok=True)
res = {"out_dir": OUT}

spec = importlib.util.spec_from_file_location("export_glb", STAGE01)
mod = importlib.util.module_from_spec(spec)
sys.modules["export_glb"] = mod
spec.loader.exec_module(mod)
res["module_loaded"] = True

ar = unreal.AssetRegistryHelpers.get_asset_registry()
ar.scan_paths_synchronous(["/Game/Unpacked/Telly"], force_rescan=True)
flt = unreal.ARFilter(package_paths=["/Game/Unpacked/Telly"],
                      recursive_paths=True, class_names=["SkeletalMesh"])
face = None
for a in ar.get_assets(flt):
    if "face" in str(a.asset_name).lower():
        face = unreal.EditorAssetLibrary.load_asset(str(a.package_name))
        break
res["face_skm"] = str(face.get_path_name()) if face else None

# --- the measured frame rate of both sequences ------------------------------
try:
    ar.scan_paths_synchronous(["/MetaHumanCharacter/Face/ARKit"], force_rescan=True)
    arkit = unreal.EditorAssetLibrary.load_asset(
        "/MetaHumanCharacter/Face/ARKit/AS_MetaHuman_ARKit_Mapping")
    res["arkit_rate_measured"] = mod._source_frame_rate(arkit)
    tg = unreal.EditorAssetLibrary.load_asset(mod.TG_VISEME_ASSET)
    res["tg_rate_measured"] = mod._source_frame_rate(tg) if tg else None
except Exception:
    res["rate_error"] = traceback.format_exc()[-800:]

# --- the export itself ------------------------------------------------------
try:
    block = mod._export_tg_viseme_sources(face, OUT)
    res["manifest_block"] = block
except Exception:
    res["export_error"] = traceback.format_exc()[-2500:]
    block = None

res["files"] = sorted(
    {"name": f, "size": os.path.getsize(os.path.join(OUT, f))}["name"]
    for f in os.listdir(OUT)) if os.path.isdir(OUT) else []
res["file_sizes"] = {f: os.path.getsize(os.path.join(OUT, f))
                     for f in os.listdir(OUT)} if os.path.isdir(OUT) else {}

# --- absence must be loud, not silent --------------------------------------
# Re-run with the asset renamed out of the way and confirm the function
# returns None and says so, rather than pretending it ran.
try:
    saved = mod.TG_VISEME_ASSET
    mod.TG_VISEME_ASSET = "/Game/TG_Face/AS_TG_DoesNotExist"
    absent_dir = os.path.join(OUT, "_absent")
    os.makedirs(absent_dir, exist_ok=True)
    res["absent_returns"] = mod._export_tg_viseme_sources(face, absent_dir)
    res["absent_wrote_files"] = os.listdir(absent_dir)
    mod.TG_VISEME_ASSET = saved
except Exception:
    res["absent_error"] = traceback.format_exc()[-800:]

with open(REPORT, "w", encoding="utf-8") as f:
    json.dump(res, f, indent=2)
unreal.log(f"[tg-verify] wrote {REPORT}")
