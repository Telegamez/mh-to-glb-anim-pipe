import importlib.util, json, os, sys
import unreal
STAGE01 = ("s:/Avatars/metahuman/metahuman-to-glb/5.7/native-glb/"
           "stages/01-unreal-glb-export/tools/export_glb.py")
OUT = os.environ["TG_VERIFY_OUT"]
spec = importlib.util.spec_from_file_location("export_glb", STAGE01)
mod = importlib.util.module_from_spec(spec); sys.modules["export_glb"] = mod
spec.loader.exec_module(mod)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
ar.scan_paths_synchronous(["/Game/Unpacked/Telly"], force_rescan=True)
flt = unreal.ARFilter(package_paths=["/Game/Unpacked/Telly"], recursive_paths=True,
                      class_names=["SkeletalMesh"])
face = next(unreal.EditorAssetLibrary.load_asset(str(a.package_name))
            for a in ar.get_assets(flt) if "face" in str(a.asset_name).lower())
ar.scan_paths_synchronous(["/MetaHumanCharacter/Face/ARKit"], force_rescan=True)
pa = unreal.EditorAssetLibrary.load_asset("/MetaHumanCharacter/Face/ARKit/PA_MetaHuman_ARKit_Mapping")
aseq = unreal.EditorAssetLibrary.load_asset("/MetaHumanCharacter/Face/ARKit/AS_MetaHuman_ARKit_Mapping")
names = [str(n) for n in (pa.get_pose_names() or [])]
with open(os.path.join(OUT, "arkit_pose_names.json"), "w", encoding="utf-8") as f:
    json.dump(names, f, indent=2)
ok = mod._export_sequencer_bake(face, aseq, os.path.join(OUT, "LS_arkit_full.fbx"),
                                seq_name="LS_arkit_ref_export")
unreal.log(f"[tg-verify] arkit bake ok={ok} poses={len(names)}")
