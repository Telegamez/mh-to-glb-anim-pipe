"""Export Unreal's ACTUAL posed geometry — the only unambiguous ground truth.

## Why not reconstruct it

Every attempt to rebuild Unreal's deformation from exported pieces has encoded a different guess
about which rest pose is authoritative, and each guess produced a different answer:

  * The mesh's node rest and the animation's node rest disagree by up to 70mm on 295 of 342
    bones — MetaHuman shares one Skeleton asset and overrides its reference pose per character.
  * Worse, WITHIN one exported animation the translation TRACK VALUES disagree with that same
    file's node rest by up to 120mm (`pelvis` 120.5mm, `calf_r` 51.4mm).

So there is no self-consistent rest pose in the exported data to measure a delta against, and
any tool that picks one is measuring its own assumption. This side-steps the question entirely:
pose the mesh in Unreal, with every post-process ABP running, and read the vertices back.

## How

`EditorActorSubsystem.convert_actors` is the scripted form of "Convert Actor to Static Mesh". It
bakes the component's CURRENT evaluated pose into geometry, so posing the actor first and
converting gives exactly what Unreal draws. Each frame becomes one static mesh, exported to GLB
for the diff tool to read.

Vertex ORDER is not guaranteed to survive the conversion, so the diff must match by position
against the bind mesh rather than by index — see verify_deformation.py.
"""
from __future__ import annotations

import json
import os
import sys

import unreal  # type: ignore

BAKE_PACKAGE = "/Game/_posebake"


DIAG = []


def _log(msg):
    # Also collected into the output JSON: UE's commandlet does not forward Python stdout to the
    # log file, so a stage whose diagnostics only print is a stage you debug blind.
    line = f"[posed_mesh] {msg}"
    DIAG.append(str(msg))
    print(line, flush=True)
    unreal.log(line)


def _arg(name, default=None):
    for a in sys.argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default


def main():
    mesh_path = _arg("mesh")
    anim_path = _arg("anim")
    out_dir = _arg("out-dir")
    frames = int(_arg("frames", "4"))
    if not (mesh_path and anim_path and out_dir):
        raise RuntimeError("need --mesh= --anim= --out-dir=")
    os.makedirs(out_dir, exist_ok=True)

    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    anim = unreal.EditorAssetLibrary.load_asset(anim_path)
    if mesh is None or anim is None:
        raise RuntimeError(f"missing asset: mesh={mesh_path} anim={anim_path}")
    abp = mesh.get_editor_property("post_process_anim_blueprint")
    _log(f"mesh={mesh_path}  post-process ABP={'YES' if abp else 'NO'}")
    if not abp:
        raise RuntimeError("that mesh has no post-process ABP; rig logic would not run")

    bake_map = f"{BAKE_PACKAGE}/PoseMap"
    loaded = False
    if unreal.EditorAssetLibrary.does_asset_exist(bake_map):
        try:
            loaded = unreal.EditorLevelLibrary.load_level(bake_map)
        except Exception as e:  # noqa: BLE001
            _log(f"load_level failed: {e}")
    if not loaded:
        unreal.EditorLevelLibrary.new_level(bake_map)

    length = float(anim.get_editor_property("sequence_length"))
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    results = []

    for k in range(frames):
        t = length * (k + 0.5) / frames
        actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
            unreal.SkeletalMeshActor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
        comp = actor.skeletal_mesh_component
        try:
            comp.set_editor_property("skeletal_mesh_asset", mesh)
        except Exception:
            comp.set_editor_property("skeletal_mesh", mesh)
        # Single-node animation playback so the pose is deterministic, then force an evaluation
        # so the post-process ABP has actually run before the geometry is read.
        comp.set_editor_property("animation_mode", unreal.AnimationMode.ANIMATION_SINGLE_NODE)
        comp.set_animation(anim)
        comp.set_position(t, False)
        comp.set_play_rate(0.0)
        comp.stop()
        comp.set_position(t, False)
        try:
            comp.tick_component(0.0, unreal.TickingGroup.TG_PRE_PHYSICS, None)
        except Exception:
            pass
        # Not all of these are exposed to Python in every 5.x; the pose is already set by
        # set_position, these just force the evaluation to land before the geometry is read.
        for nudge in ("refresh_bone_transforms", "update_bounds", "recreate_render_state_context"):
            fn = getattr(comp, nudge, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

        name = f"SM_pose_{k:02d}"
        try:
            actors.convert_actors([actor], unreal.StaticMeshActor, f"{BAKE_PACKAGE}/{name}")
        except Exception as e:  # noqa: BLE001
            _log(f"convert_actors raised at frame {k}: {type(e).__name__}: {e}")
            unreal.EditorLevelLibrary.destroy_actor(actor)
            continue

        asset_path = f"{BAKE_PACKAGE}/{name}/{name}"
        static = unreal.EditorAssetLibrary.load_asset(asset_path)
        if static is None:
            # convert_actors names the asset from the package path in some versions
            candidates = unreal.EditorAssetLibrary.list_assets(f"{BAKE_PACKAGE}/{name}", True, False)
            static = unreal.EditorAssetLibrary.load_asset(candidates[0]) if candidates else None
        if static is None:
            listed = []
            try:
                listed = list(unreal.EditorAssetLibrary.list_assets(BAKE_PACKAGE, True, False))[:8]
            except Exception:
                pass
            _log(f"frame {k}: no static mesh at {asset_path}; package contains {listed}")
            continue

        out_glb = os.path.join(out_dir, f"pose_{k:02d}.glb")
        opts = unreal.GLTFExportOptions()
        for key, value in (("export_uniform_scale", 0.01), ("export_vertex_skin_weights", False),
                           ("export_morph_targets", False), ("default_level_of_detail", 0)):
            try:
                opts.set_editor_property(key, value)
            except Exception as e:  # noqa: BLE001
                _log(f"  option {key}: {e}")
        task = unreal.AssetExportTask()
        task.object = static
        task.filename = out_glb
        task.automated = True
        task.replace_identical = True
        task.prompt = False
        task.options = opts
        unreal.Exporter.run_asset_export_task(task)
        ok = os.path.isfile(out_glb) and os.path.getsize(out_glb) > 1000
        _log(f"frame {k} t={t:.2f}s -> {out_glb} {'OK' if ok else 'FAILED'}")
        results.append({"frame": k, "time": t, "glb": out_glb, "ok": ok})
        unreal.EditorLevelLibrary.destroy_actor(actor)

    with open(os.path.join(out_dir, "poses.json"), "w", encoding="utf-8") as f:
        json.dump({"mesh": mesh_path, "anim": anim_path, "length": length,
                   "frames": results, "diagnostics": DIAG}, f, indent=2)
    good = sum(1 for r in results if r["ok"])
    _log(f"{good}/{frames} posed meshes exported")
    if good == 0:
        sys.exit(1)


main()
