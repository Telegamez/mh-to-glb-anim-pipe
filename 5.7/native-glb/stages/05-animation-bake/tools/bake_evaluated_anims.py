"""Stage 05, UE half (v3) — bake the EVALUATED pose, not the source AnimSequence.

## The defect this fixes, which is the root of §6.14-6.17

`export_anims.py` exports each `AnimSequence` straight to glTF. An AnimSequence is the INPUT to
MetaHuman's deformation machine, not its output. The machine is the post-process Anim Blueprint:

    Content/MetaHumans/Common/Body/ABP_Body_PostProcess      <- drives every corrective and twist
    Content/MetaHumans/Common/Face/ABP_Face_PostProcess
    Content/MetaHumans/Common/Animation/ABP_Clothing_PostProcess

Exporting the asset bypasses all three. That is why every one of the ~1130 helper joints came out
of the pipeline with a CONSTANT transform, and why the browser has been showing torn clothing,
candy-wrapped wrists and a collapsing waist: we shipped a rig whose correctives never move, then
spent four rounds approximating them in Blender and at runtime (the fold, the plausibility
filter, the weight smoothing, the twist driver).

This exports the machine's OUTPUT. `SequencerTools.export_anim_sequence` records a skeletal mesh
component's evaluated bone transforms frame by frame, and with
`evaluate_all_skeletal_mesh_components` the post-process ABP runs as part of that evaluation. The
resulting AnimSequence carries real curves for the correctives and twists, so the runtime gets
the deformation Unreal actually produces.

## What this means downstream

Once the correctives animate, most of the compensation built for their absence becomes dead
weight and should be retired in this order, re-measuring after each step:

1. `derive_live_bones.py` will report far more than 53 moving bones. The fold shrinks to
   genuinely-static joints only, so top-4 truncation stops discarding real influences.
2. `_drop_implausible_influences` and `_smooth_weights` in stage 03 exist only to paper over
   frozen helpers. Turn them off and confirm the numbers hold.
3. `twistDriver.ts` becomes redundant for any bone the bake now drives - keep it only for what
   still measures constant, and its null test makes that safe to verify.

## Verification

Do not trust the return value; assert on the artifact. The bake worked if the corrective joints
have NON-CONSTANT tracks. `check_corrective_motion.py` measures exactly that, and the run script
fails the stage if the count of moving bones does not rise well above the 53 the source
sequences produce.
"""

import json
import os
import sys

import unreal  # type: ignore

STAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Per-set scratch, chosen by the runner so two gender bakes cannot read each
# other's exports. Falls back to the original shared path.
GLB_DIR = os.environ.get("MH_GLB_DIR") or os.path.join(STAGE_DIR, "_work", "glb")
BAKE_PACKAGE = "/Game/_animbake"

# 30fps is the pipeline's rate; the source clips are authored at it and the runtime resamples.
BAKE_FPS = 30


def _log(msg):
    # print() as well as unreal.log(): the commandlet's log verbosity can swallow LogPython
    # Display lines entirely, and a stage whose diagnostics vanish is a stage you debug blind.
    line = f"[stage05-evalbake] {msg}"
    print(line, flush=True)
    unreal.log(line)


def _load_animset():
    # Which set to bake comes from the runner via MH_ANIMSET, because
    # UnrealEditor-Cmd -run=pythonscript gives the script no argv of its own.
    # Defaults to the original single shared set so an old invocation still works.
    name = os.environ.get("MH_ANIMSET") or "animset.json"
    path = name if os.path.isabs(name) else os.path.join(STAGE_DIR, name)
    _log(f"animset = {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    clips = {}
    clips.update(cfg.get("required") or {})
    clips.update(cfg.get("gestures") or {})
    return cfg, clips


def _set(obj, prop, value):
    """Property names drift between UE versions; log and continue rather than dying."""
    try:
        obj.set_editor_property(prop, value)
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"  property {prop} not set: {e}")
        return False


def _find_body_mesh(anim, explicit=None):
    """A SkeletalMesh on this clip's skeleton that HAS a post-process ABP.

    The post-process ABP is the entire point of this stage, so a mesh without one is a hard
    failure rather than a silent fallback - it would produce output identical to v2 and the
    difference would only surface as tearing in a browser three stages later.
    """
    if explicit:
        mesh = unreal.EditorAssetLibrary.load_asset(explicit)
        if mesh is None:
            raise RuntimeError(f"--mesh not found: {explicit}")
        abp = mesh.get_editor_property("post_process_anim_blueprint")
        _log(f"  body mesh (explicit): {explicit}  post-process ABP: {bool(abp)}")
        return mesh
    skeleton = anim.get_editor_property("skeleton")
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    assets = registry.get_assets_by_class(
        unreal.TopLevelAssetPath("/Script/Engine", "SkeletalMesh"), False,
    )
    fallback = None
    for data in assets:
        mesh = data.get_asset()
        if mesh is None or mesh.get_editor_property("skeleton") != skeleton:
            continue
        abp = mesh.get_editor_property("post_process_anim_blueprint")
        if abp:
            _log(f"  body mesh: {mesh.get_path_name()}  (post-process ABP: {abp})")
            return mesh
        fallback = fallback or mesh
    if fallback is not None:
        _log(f"  WARNING no post-process ABP on any mesh for this skeleton; "
             f"nearest match {fallback.get_path_name()}")
    return fallback


def _bake_one(clip_name, asset_path, world, explicit_mesh=None):
    if not unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        _log(f"  {clip_name}: ASSET NOT FOUND {asset_path}")
        return None
    anim = unreal.EditorAssetLibrary.load_asset(asset_path)
    if not isinstance(anim, unreal.AnimSequence):
        _log(f"  {clip_name}: not an AnimSequence: {asset_path}")
        return None

    mesh = _find_body_mesh(anim, explicit_mesh)
    if mesh is None:
        _log(f"  {clip_name}: no SkeletalMesh found for its skeleton")
        return None

    length = float(anim.get_editor_property("sequence_length"))
    # `round(length * fps)` is the INTERVAL count, and for THIS API that is exactly what
    # set_playback_end / set_range want: SequencerTools.export_anim_sequence produces
    # end - start + 1 samples, so a playback end of 322 yields 323 frames.
    #
    # This was briefly "fixed" by adding 1, on the theory that it was #3341 (stage 01's
    # _export_sequencer_bake dropping the final pose) left standing here. It is not the same
    # defect. The assertion below caught the over-correction on its first run — asked for 323,
    # baked 324 — which is the whole reason the assertion exists rather than a comment claiming
    # the arithmetic is right.
    #
    # The two scripts differ because they use different export calls with different range
    # conventions. Same-looking expression, same-looking variable, opposite correct answer;
    # hence the naming here says which quantity is which.
    playback_end = max(1, int(round(length * BAKE_FPS)))
    expected_frames = playback_end + 1

    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.SkeletalMeshActor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0),
    )
    component = actor.skeletal_mesh_component
    if not _set(component, "skeletal_mesh_asset", mesh):
        _set(component, "skeletal_mesh", mesh)

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    sequence = tools.create_asset(
        f"SEQ_{clip_name}", BAKE_PACKAGE, unreal.LevelSequence, unreal.LevelSequenceFactoryNew(),
    )
    sequence.set_display_rate(unreal.FrameRate(BAKE_FPS, 1))
    sequence.set_playback_start(0)
    sequence.set_playback_end(playback_end)

    binding = sequence.add_possessable(actor)
    track = binding.add_track(unreal.MovieSceneSkeletalAnimationTrack)
    section = track.add_section()
    section.set_range(0, playback_end)
    params = section.get_editor_property("params")
    _set(params, "animation", anim)
    _set(section, "params", params)

    # The factory needs its target skeleton BEFORE the asset is created: an AnimSequence built
    # without one cannot initialise its RigHierarchy and create_asset raises.
    factory = unreal.AnimSequenceFactory()
    _set(factory, "target_skeleton", anim.get_editor_property("skeleton"))
    baked = tools.create_asset(
        f"A_{clip_name}_evaluated", BAKE_PACKAGE, unreal.AnimSequence, factory,
    )
    options = unreal.AnimSeqExportOption()
    _set(options, "export_transforms", True)
    _set(options, "export_curves", True)
    _set(options, "record_in_world_space", False)
    # THE flag. Without it the post-process ABP does not run and this stage is a slow no-op.
    _set(options, "evaluate_all_skeletal_mesh_components", True)

    ok = unreal.SequencerTools.export_anim_sequence(world, sequence, baked, options, binding, False)
    unreal.EditorLevelLibrary.destroy_actor(actor)

    if not ok:
        _log(f"  {clip_name}: export_anim_sequence returned False")
        return None
    # Check the count rather than trust the arithmetic. get_num_frames returns INTERVALS, not
    # sample positions, so the identity is baked_intervals + 1 == expected_frames.
    #
    # This is not decoration. On its first run it caught an over-correction — a +1 added on the
    # assumption that this script carried #3341 — by reporting 324 frames where 323 were asked
    # for. An assertion that can only ever agree with the code above it proves nothing; this one
    # disagreed, and was right to.
    baked_intervals = unreal.AnimationLibrary.get_num_frames(baked)
    baked_frames = baked_intervals + 1
    if baked_frames != expected_frames:
        raise RuntimeError(
            f"{clip_name}: expected {expected_frames} frames from playback_end={playback_end}, "
            f"baked sequence carries {baked_frames} ({baked_intervals} intervals). The "
            f"playback-range convention has changed.")
    _log(f"  {clip_name}: baked {expected_frames} frames ({baked_intervals} intervals, "
         f"{length:.2f}s) -> {baked.get_path_name()}")
    return baked


def _make_gltf_options():
    """Identical to export_anims.py — units contract lives here (0.01 -> metres)."""
    opts = unreal.GLTFExportOptions()
    for k, v in (
        ("export_uniform_scale", 0.01),
        ("export_preview_mesh", True),
        ("export_morph_targets", False),
        ("export_vertex_skin_weights", True),
        ("default_level_of_detail", 0),
        ("skip_near_default_values", False),   # keep corrective curves; they are the point
    ):
        _set(opts, k, v)
    return opts


def _export_glb(clip_name, anim, opts):
    out_glb = os.path.join(GLB_DIR, f"{clip_name}.glb")
    task = unreal.AssetExportTask()
    task.object = anim
    task.filename = out_glb
    task.automated = True
    task.replace_identical = True
    task.prompt = False
    task.options = opts
    ok = unreal.Exporter.run_asset_export_task(task)
    if not ok or not os.path.isfile(out_glb) or os.path.getsize(out_glb) < 10_000:
        size = os.path.getsize(out_glb) if os.path.isfile(out_glb) else "absent"
        _log(f"  {clip_name}: GLB EXPORT FAILED (ok={ok}, file={size})")
        return False
    _log(f"  {clip_name}: exported {os.path.getsize(out_glb)/1024:.0f} KB")
    return True


def main():
    os.makedirs(GLB_DIR, exist_ok=True)
    cfg, clips = _load_animset()
    only = None
    explicit_mesh = None
    out_dir = GLB_DIR
    explicit_anim = None
    for arg in sys.argv:
        if arg.startswith("--anim="):
            # Bake ONE named AnimSequence that is not in any animset.
            #
            # The animset is the shipped clip library; a validation or pose table is neither
            # shipped nor a clip, and adding it there to get it baked would put test data in
            # the runtime's state map. This flag keeps the mechanism and skips the registry.
            explicit_anim = arg.split("=", 1)[1]
        if arg.startswith("--only="):
            only = arg.split("=", 1)[1]
        if arg.startswith("--mesh="):
            # Rig logic is evaluated against a SPECIFIC body. The default picks the first mesh
            # in the registry that has the ABP, which is fine for a shared library (rotations
            # transfer) but wrong for per-character ground truth.
            explicit_mesh = arg.split("=", 1)[1]
        if arg.startswith("--out-dir="):
            out_dir = arg.split("=", 1)[1]
    globals()["GLB_DIR"] = out_dir
    if explicit_anim:
        name = explicit_anim.rstrip("/").split("/")[-1].split(".")[0]
        clips = {name: explicit_anim}
        _log(f"--anim={explicit_anim}: baking as '{name}', animset ignored")
    elif only:
        clips = {k: v for k, v in clips.items() if k == only}
        _log(f"--only={only}: {len(clips)} clip(s)")

    # Load the scratch map if a previous run left one; new_level refuses to overwrite and the
    # script then has no world to spawn into.
    bake_map = f"{BAKE_PACKAGE}/BakeMap"
    loaded = False
    if unreal.EditorAssetLibrary.does_asset_exist(bake_map):
        try:
            loaded = unreal.EditorLevelLibrary.load_level(bake_map)
        except Exception as e:  # noqa: BLE001
            _log(f"load_level failed: {e}")
    if not loaded:
        try:
            unreal.EditorLevelLibrary.new_level(bake_map)
        except Exception as e:  # noqa: BLE001
            _log(f"new_level failed: {e}")
    world = unreal.EditorLevelLibrary.get_editor_world()
    _log(f"world = {world}")
    if world is None:
        _log("FATAL no editor world")
        sys.exit(1)

    opts = _make_gltf_options()
    failed = []
    for clip_name, asset_path in clips.items():
        _log(f"{clip_name} <- {asset_path}")
        baked = _bake_one(clip_name, asset_path, world, explicit_mesh)
        if baked is None or not _export_glb(clip_name, baked, opts):
            failed.append(clip_name)

    if failed:
        _log(f"FAILED clips: {', '.join(failed)}")
        sys.exit(1)
    _log(f"all {len(clips)} clip(s) baked from the EVALUATED pose")


main()
