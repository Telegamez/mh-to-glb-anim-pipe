"""Export each companion's MetaHuman face DNA to a loose `.dna` file.

Runs inside UnrealEditor-Cmd.exe:

    UnrealEditor-Cmd.exe <uproject> -run=pythonscript \
        -script="<abs>/export_dna.py -- --out=<dir> [--chars=Telly,Tele,...]"

Why this exists
---------------
`decisions.md` §5 item 3 recorded that the factory had **never been confirmed**
to emit or extract a MetaHuman DNA file, and the face-fidelity plan made that
the gate on Phase 5 (the RigLogic → WASM spike). It does. Every companion's
face SkeletalMesh carries a `DNAAssetUserData` pointing at an in-package `DNA`
uasset (`SKM_<Char>_FaceMesh_DNA`), and
`MetaHumanCharacterExportBlueprintLibrary.export_dna` writes it out as a
standalone binary DNA when `external_path` is set.

The DNA is the *whole* rig — 870 joints, 263 raw controls, 782 blendshape
channels, 82 animated maps — i.e. everything the ARKit bake collapses. Phase 5
consumes this file directly; nothing else in the pipeline reads it, so this
tool is deliberately standalone rather than a stage.

Pair with `parse_dna.py`, which validates the output without an engine.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

import unreal

DEFAULT_CHARS = ("Telly", "Tele", "Telee", "Telie", "Tele1", "Tele2")


def _log(msg):
    unreal.log(f"[tg-dna] {msg}")


def _parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True, help="output folder for the .dna files")
    p.add_argument("--chars", default=",".join(DEFAULT_CHARS),
                   help="comma-separated MetaHumanCharacter asset names under /Game/")
    p.add_argument("--body", action="store_true", help="also export body DNA")
    return p.parse_args(argv)


def _face_skm(char):
    """The character's face SkeletalMesh, or None.

    Force-scans first: UE's AssetRegistry lazy-loads sub-paths and a fresh
    commandlet returns zero hits for assets that are demonstrably on disk —
    the same hazard `_list_under` and the ARKit lookup in stage 01 both carry
    a guard for.
    """
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        ar.scan_paths_synchronous([f"/Game/Unpacked/{char}"], force_rescan=True)
    except Exception as e:
        _log(f"  scan_paths_synchronous({char}) raised: {e}")
    flt = unreal.ARFilter(package_paths=[f"/Game/Unpacked/{char}"],
                          recursive_paths=True, class_names=["SkeletalMesh"])
    for a in ar.get_assets(flt):
        if "face" in str(a.asset_name).lower():
            return unreal.EditorAssetLibrary.load_asset(str(a.package_name))
    return None


def _dna_provenance(char):
    """Where the DNA lives for this character, read off the face mesh.

    Recorded alongside the export so a `.dna` on disk can always be traced back
    to the asset it came from — the export API takes the MetaHumanCharacter,
    not the mesh, so without this there is nothing tying the two together.
    """
    skm = _face_skm(char)
    if skm is None:
        return {"face_skm": None, "dna_asset": None,
                "note": "no face SkeletalMesh under /Game/Unpacked/"}
    rec = {"face_skm": str(skm.get_path_name())}
    try:
        rec["morph_targets"] = len(list(skm.morph_targets))
    except Exception:
        rec["morph_targets"] = None
    auds = skm.get_editor_property("asset_user_data") or []
    rec["asset_user_data"] = [u.__class__.__name__ for u in auds]
    ud = next((u for u in auds if u.__class__.__name__ == "DNAAssetUserData"), None)
    if ud is None:
        rec["dna_asset"] = None
        return rec
    dna = ud.get_editor_property("dna_asset")
    rec["dna_asset"] = str(dna.get_path_name()) if dna is not None else None
    if dna is not None:
        try:
            rec["rig_logic_configuration"] = str(
                dna.get_editor_property("rig_logic_configuration"))
        except Exception:
            pass
    return rec


def main():
    ns = _parse_args()
    out_dir = os.path.abspath(ns.out)
    os.makedirs(out_dir, exist_ok=True)
    chars = [c.strip() for c in ns.chars.split(",") if c.strip()]

    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        ar.scan_paths_synchronous(["/Game"], force_rescan=False)
    except Exception:
        pass

    report = {"out_dir": out_dir, "characters": {}, "errors": []}
    before = set(os.listdir(out_dir))

    for char in chars:
        entry = {"provenance": _dna_provenance(char)}
        asset = unreal.EditorAssetLibrary.load_asset(f"/Game/{char}")
        if asset is None:
            entry["error"] = f"/Game/{char} did not load"
            report["errors"].append(entry["error"])
            report["characters"][char] = entry
            _log(f"  {char}: MISSING /Game/{char}")
            continue
        entry["character_asset"] = str(asset.get_path_name())
        entry["character_class"] = asset.__class__.__name__
        try:
            params = unreal.MetaHumanDNAExportParams()
            params.set_editor_property("dna_head", True)
            params.set_editor_property("dna_body", bool(ns.body))
            params.set_editor_property("external_path", out_dir)
            params.set_editor_property("overwrite_existing_assets", True)
            unreal.MetaHumanCharacterExportBlueprintLibrary.export_dna(asset, params)
        except Exception:
            entry["error"] = traceback.format_exc()[-2000:]
            report["errors"].append(f"{char}: export_dna raised")
            _log(f"  {char}: export_dna FAILED")
            report["characters"][char] = entry
            continue

        # Assert on the artifact, not the call: `export_dna` returns None and
        # raises nothing when it declines to write, so the only honest evidence
        # that this character exported is a new file on disk (00-START-HERE §6.1).
        now = set(os.listdir(out_dir))
        produced = sorted(now - before)
        before = now
        entry["files"] = [{"name": f, "size": os.path.getsize(os.path.join(out_dir, f))}
                          for f in produced]
        if not produced:
            entry["error"] = "export_dna returned without writing a file"
            report["errors"].append(f"{char}: no .dna written")
            _log(f"  {char}: NO FILE WRITTEN")
        else:
            _log(f"  {char}: + {produced} "
                 f"({entry['files'][0]['size']/1_000_000:.1f} MB)")
        report["characters"][char] = entry

    report_path = os.path.join(out_dir, "dna_export_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    _log(f"wrote {report_path} "
         f"({len(chars)} character(s), {len(report['errors'])} error(s))")


if __name__ == "__main__":
    main()
