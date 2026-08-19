"""Export body + outfit SkeletalMeshes as FBX — the trustworthy source of skin weights.

Unreal's glTF exporter corrupts skin weights (00-START-HERE §7.0 addendum, 2026-08-18): the
same asset's coincident seam twins disagree up to 94% in the GLB but agree exactly in the FBX.
Geometry, UVs, materials and textures still come from the GLB export; these FBX files exist
ONLY so stage 02 can replace the weight map via reweight_from_fbx.py.

Reads the character's mh_manifest.json for the body and outfit asset paths, writes
<char>/source/SKM_..._weights.fbx next to it. Face is deliberately excluded: the stage-02
face split breaks 1:1 vertex correspondence, and the face showed no twin corruption.

    UnrealEditor-Cmd <project> -run=pythonscript -script="export_fbx_weights.py
        --manifest=<abs path to mh_manifest.json> --out-dir=<abs char/source dir>"
        -AllowCommandletRendering -unattended -nosplash

-AllowCommandletRendering is REQUIRED: the FBX exporter touches MeshObject render resources
and asserts without it.
"""
from __future__ import annotations

import json
import os
import sys

import unreal  # type: ignore


def _log(msg):
    print(f"[fbx_weights] {msg}", flush=True)
    unreal.log(f"[fbx_weights] {msg}")


def _arg(name):
    for a in sys.argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None


def main():
    manifest_path = _arg("manifest")
    out_dir = _arg("out-dir")
    if not (manifest_path and out_dir):
        raise RuntimeError("need --manifest= --out-dir=")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    os.makedirs(out_dir, exist_ok=True)

    targets = [a for a in manifest["assets"] if a.get("role") in ("body", "outfit")]
    if len(targets) < 2:
        raise RuntimeError(f"manifest lists {len(targets)} of body/outfit — need both")

    exported = []
    for entry in targets:
        asset_path = entry["asset_path"]
        mesh = unreal.EditorAssetLibrary.load_asset(asset_path)
        if mesh is None:
            raise RuntimeError(f"missing asset {asset_path}")
        name = asset_path.rsplit("/", 1)[-1].split(".")[0]
        out_fbx = os.path.join(out_dir, f"{name}_weights.fbx")

        opts = unreal.FbxExportOption()
        for key, value in (("vertex_color", False), ("level_of_detail", False),
                           ("collision", False)):
            opts.set_editor_property(key, value)
        task = unreal.AssetExportTask()
        task.object = mesh
        task.filename = out_fbx
        task.automated = True
        task.replace_identical = True
        task.prompt = False
        task.options = opts
        ok = unreal.Exporter.run_asset_export_task(task)
        size = os.path.getsize(out_fbx) if os.path.isfile(out_fbx) else 0
        _log(f"{entry['role']}: {asset_path} -> {out_fbx} ok={ok} ({size/1e6:.2f} MB)")
        if not ok or size < 10000:
            raise RuntimeError(f"FBX export failed for {asset_path}")
        exported.append({"role": entry["role"], "asset_path": asset_path, "fbx": out_fbx})

    with open(os.path.join(out_dir, "fbx_weights_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"exported": exported}, f, indent=2)
    _log(f"{len(exported)} meshes exported")


main()
