"""Ground truth, straight from the component: Unreal's evaluated bone transforms + its ref pose.

## Why this and not the exported animation

Reconstructing Unreal's deformation from exported files kept failing because the files disagree
about the rest pose, and every tool that picked one was measuring its own assumption:

  * the character MESH's node rest and the ANIMATION's node rest differ by up to 70mm on 295 of
    342 bones (MetaHuman shares one Skeleton asset and overrides its reference pose per mesh)
  * within a single exported animation, translation TRACK VALUES disagree with that same file's
    node rest by up to 120mm

Alembic would have side-stepped it by carrying deformed vertices, but Unreal's Python exposes
Alembic as IMPORT only - there is no exporter. `convert_actors` does not produce a mesh for a
skeletal actor either.

What the component itself exposes is better than both, because it removes the ambiguity at the
source rather than working around it:

    get_socket_transform(bone, RTS_COMPONENT)   component-space transform of any bone

Read it twice from the SAME component: once with the post-process ABP disabled and no animation,
which is the reference pose, and once per frame with the ABP running, which is what Unreal draws.
Deformation is then exactly

    v' = sum_i  w_i . CS_i(t) . inverse(refCS_i) . v_bind

No second file to disagree with, and no hierarchy composition to get wrong. That is Unreal's own
skinning maths with Unreal's own numbers, and it is what `verify_deformation.py` diffs against.

## What it does not capture

Cloth simulation and morph targets. The body and garment deform by linear blend skinning over
these joints, which is what this reproduces; the face is morph-driven and the browser already
receives those directly.
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


def _xform_to_list(t: "unreal.Transform"):
    loc, rot, scale = t.translation, t.rotation, t.scale3d
    return {
        "t": [loc.x, loc.y, loc.z],
        "q": [rot.x, rot.y, rot.z, rot.w],
        "s": [scale.x, scale.y, scale.z],
    }


def main():
    mesh_path = _arg("mesh")
    anim_path = _arg("anim")
    out_path = _arg("out")
    frames = int(_arg("frames", "6"))
    if not (mesh_path and anim_path and out_path):
        raise RuntimeError("need --mesh= --anim= --out=")

    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    anim = unreal.EditorAssetLibrary.load_asset(anim_path)
    if mesh is None or anim is None:
        raise RuntimeError(f"missing asset: mesh={mesh_path} anim={anim_path}")
    abp = mesh.get_editor_property("post_process_anim_blueprint")
    _log(f"mesh={mesh_path} post-process ABP={'YES' if abp else 'NO'}")
    if not abp:
        raise RuntimeError("mesh has no post-process ABP; rig logic would not run")

    bake_map = "/Game/_posebake/PoseMap"
    loaded = False
    if unreal.EditorAssetLibrary.does_asset_exist(bake_map):
        try:
            loaded = unreal.EditorLevelLibrary.load_level(bake_map)
        except Exception as e:  # noqa: BLE001
            _log(f"load_level: {e}")
    if not loaded:
        unreal.EditorLevelLibrary.new_level(bake_map)

    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.SkeletalMeshActor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
    comp = actor.skeletal_mesh_component
    try:
        comp.set_editor_property("skeletal_mesh_asset", mesh)
    except Exception:
        comp.set_editor_property("skeletal_mesh", mesh)

    # get_bone_names is not exposed in 5.8. For a skeletal mesh component get_all_socket_names
    # returns every BONE plus any attachment sockets; the diff matches by name against the GLB's
    # joint list, so extra entries are harmless.
    bone_names = [str(b) for b in comp.get_all_socket_names()]
    _log(f"{len(bone_names)} socket/bone names")

    # REFERENCE POSE, in component space, read the same way as the animated pose so the two
    # cannot disagree. The post-process ABP is disabled for this read: the mesh's vertices are
    # authored against the raw reference skeleton, so letting rig logic run here would fold a
    # correction into the bind and quietly bias every later measurement.
    for prop in ("b_disable_post_process_blueprint", "disable_post_process_blueprint"):
        try:
            comp.set_editor_property(prop, True)
            _log(f"post-process ABP disabled for the ref-pose read via {prop}")
            break
        except Exception:
            continue
    comp.set_editor_property("animation_mode", unreal.AnimationMode.ANIMATION_CUSTOM_MODE)
    try:
        comp.tick_component(0.0, unreal.TickingGroup.TG_PRE_PHYSICS, None)
    except Exception:
        pass
    ref_cs = {}
    for name in bone_names:
        try:
            ref_cs[name] = _xform_to_list(
                comp.get_socket_transform(name, unreal.RelativeTransformSpace.RTS_COMPONENT))
        except Exception as e:  # noqa: BLE001
            _log(f"ref transform failed for {name}: {e}")
            break
    _log(f"reference pose: {len(ref_cs)} component-space transforms")

    for prop in ("b_disable_post_process_blueprint", "disable_post_process_blueprint"):
        try:
            comp.set_editor_property(prop, False)
            break
        except Exception:
            continue
    comp.set_editor_property("animation_mode", unreal.AnimationMode.ANIMATION_SINGLE_NODE)
    comp.set_animation(anim)
    comp.set_play_rate(0.0)
    comp.stop()

    length = float(anim.get_editor_property("sequence_length"))
    out_frames = []
    for k in range(frames):
        t = length * (k + 0.5) / frames
        comp.set_position(t, False)
        try:
            comp.tick_component(0.0, unreal.TickingGroup.TG_PRE_PHYSICS, None)
        except Exception:
            pass
        pose = {}
        for name in bone_names:
            try:
                pose[name] = _xform_to_list(
                    comp.get_socket_transform(name, unreal.RelativeTransformSpace.RTS_COMPONENT))
            except Exception as e:  # noqa: BLE001
                _log(f"socket transform failed for {name}: {e}")
                break
        out_frames.append({"time": t, "bones": pose})
        _log(f"frame {k} t={t:.2f}s: {len(pose)} bone transforms")

    unreal.EditorLevelLibrary.destroy_actor(actor)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "mesh": mesh_path, "anim": anim_path, "length": length,
            "units": "centimetres, Unreal component space",
            "bone_names": bone_names,
            "ref_pose_component": ref_cs, "frames": out_frames,
            "diagnostics": DIAG,
        }, f)
    _log(f"wrote {out_path} ({os.path.getsize(out_path)/1e6:.1f} MB)")
    if not out_frames or not out_frames[0]["bones"]:
        sys.exit(1)


main()
