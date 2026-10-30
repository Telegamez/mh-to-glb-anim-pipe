"""Kimodo take (FBX on the SOMA skeleton) -> AnimSequence on metahuman_base_skel.

Runs inside UnrealEditor-Cmd (-run=pythonscript). Inputs travel by env var because the
commandlet gives the script no argv:

    KIMODO_FBX        path to the FBX written by bvh_to_fbx.py
    KIMODO_TAKE       asset name for the take (e.g. KM_Listen_00)
    KIMODO_TARGET     target SkeletalMesh (default: the male body stage 05 bakes against)

Creates / refreshes, all under /Game/Kimodo:
    Source/<take>              imported SOMA skeletal mesh, skeleton, AnimSequence
    Rigs/IK_SOMA               IK Rig for SOMA, chain names identical to IK_metahuman
    Rigs/RTG_SOMA_to_MH        IK Retargeter SOMA -> IK_metahuman
    Retargeted/<take>          the AnimSequence stage 05 consumes

Fingers and twist chains are deliberately unmapped: Kimodo does not animate fingers
(somaskel30), and the MetaHuman post-process ABP drives the twists during the stage 05 bake.
"""

import json
import os

import unreal  # type: ignore

ROOT = "/Game/Kimodo"
RIG_DIR = f"{ROOT}/Rigs"
SOURCE_RIG = f"{RIG_DIR}/IK_SOMA"
RETARGETER = f"{RIG_DIR}/RTG_SOMA_to_MH"
SOMA_DIR = f"{ROOT}/SOMA"
SOMA_MESH = f"{SOMA_DIR}/SK_SOMA"
TARGET_RIG = "/Game/MetaHumans/Common/Common/IK_metahuman"
DEFAULT_TARGET_MESH = "/Game/MetaHumans/Tele/Body/SKM_Tele_BodyMesh"

# SOMA chain -> (start, end, goal bone or None). Names match IK_metahuman's chains so
# auto_map_chains pairs them exactly.
SOMA_CHAINS = {
    "Spine": ("Spine1", "Chest", None),
    "head": ("Neck1", "Head", None),
    "LeftClavicle": ("LeftShoulder", "LeftShoulder", None),
    "RightClavicle": ("RightShoulder", "RightShoulder", None),
    "LeftArm": ("LeftArm", "LeftHand", "LeftHand"),
    "RightArm": ("RightArm", "RightHand", "RightHand"),
    "LeftLeg": ("LeftLeg", "LeftToeBase", "LeftFoot"),
    "RightLeg": ("RightLeg", "RightToeBase", "RightFoot"),
}


LOG_FILE = os.environ.get("KIMODO_LOG")


LIMB_BONES = [f"{b}_{s}" for s in ("l", "r") for b in
              ("clavicle", "upperarm", "lowerarm", "hand", "thigh", "calf", "foot", "ball")]


def _log(msg):
    line = f"[kimodo-retarget] {msg}"
    print(line, flush=True)
    unreal.log(line)
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _delete(path):
    if unreal.EditorAssetLibrary.does_directory_exist(path):
        unreal.EditorAssetLibrary.delete_directory(path)
    elif unreal.EditorAssetLibrary.does_asset_exist(path):
        unreal.EditorAssetLibrary.delete_asset(path)


def _fbx_task(fbx, dest, skeleton=None):
    task = unreal.AssetImportTask()
    task.filename = fbx
    task.destination_path = dest
    task.automated = True
    task.replace_existing = True
    task.save = True
    ui = unreal.FbxImportUI()
    ui.import_as_skeletal = True
    ui.import_animations = True
    ui.import_materials = False
    ui.import_textures = False
    ui.mesh_type_to_import = unreal.FBXImportType.FBXIT_SKELETAL_MESH
    if skeleton is not None:
        # Animation only, onto the one shared SOMA skeleton.
        ui.import_mesh = False
        ui.skeleton = skeleton
        ui.mesh_type_to_import = unreal.FBXImportType.FBXIT_ANIMATION
    else:
        ui.import_mesh = True
    ui.anim_sequence_import_data.set_editor_property("import_bone_tracks", True)
    ui.anim_sequence_import_data.set_editor_property(
        "animation_length", unreal.FBXAnimationLengthImportType.FBXALIT_EXPORTED_TIME)
    task.options = ui
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])


def _find(dest, cls):
    for p in unreal.EditorAssetLibrary.list_assets(dest, recursive=True):
        a = unreal.EditorAssetLibrary.load_asset(p)
        if isinstance(a, cls):
            return a
    return None


def ensure_soma_mesh(fbx):
    """One SOMA skeletal mesh + skeleton shared by every take (all Kimodo BVHs share the
    standard T-pose rest). Created from the first take imported; never per take, because the
    IK Rig is bound to it and a per-take skeleton left the rig undeletable on the next run."""
    if unreal.EditorAssetLibrary.does_asset_exist(SOMA_MESH):
        return unreal.EditorAssetLibrary.load_asset(SOMA_MESH)
    _fbx_task(fbx, SOMA_DIR)
    mesh = _find(SOMA_DIR, unreal.SkeletalMesh)
    if mesh is None:
        raise RuntimeError(f"SOMA mesh import produced nothing under {SOMA_DIR}")
    unreal.EditorAssetLibrary.rename_asset(mesh.get_path_name().split(".")[0], SOMA_MESH)
    mesh = unreal.EditorAssetLibrary.load_asset(SOMA_MESH)
    unreal.EditorAssetLibrary.save_loaded_asset(mesh)
    _log(f"created shared SOMA mesh {SOMA_MESH} (skeleton {mesh.get_editor_property('skeleton').get_path_name()})")
    return mesh


def import_take(fbx, take, mesh):
    dest = f"{ROOT}/Source/{take}"
    _delete(dest)
    skeleton = mesh.get_editor_property("skeleton")
    _fbx_task(fbx, dest, skeleton)
    anim = _find(dest, unreal.AnimSequence)
    if anim is None:
        raise RuntimeError(f"animation import produced nothing under {dest}")
    if anim.get_editor_property("skeleton") != skeleton:
        raise RuntimeError(f"{anim.get_path_name()} landed on {anim.get_editor_property('skeleton').get_path_name()},"
                           f" not the shared SOMA skeleton {skeleton.get_path_name()}")
    _log(f"imported {anim.get_path_name()} ({anim.get_editor_property('sequence_length'):.2f}s) onto {skeleton.get_path_name()}")
    return anim


def ensure_rigs(mesh, target_mesh):
    """Reuse IK_SOMA + RTG_SOMA_to_MH unless KIMODO_REBUILD_RIGS=1. Rebuild deletes in
    dependency order (retargeter references rig) - the other order silently fails."""
    rebuild = os.environ.get("KIMODO_REBUILD_RIGS") == "1"
    have = all(unreal.EditorAssetLibrary.does_asset_exist(p) for p in (SOURCE_RIG, RETARGETER))
    if have and not rebuild:
        _log("reusing IK_SOMA and RTG_SOMA_to_MH")
        return unreal.EditorAssetLibrary.load_asset(RETARGETER)
    for p in (RETARGETER, SOURCE_RIG):
        _delete(p)
        if unreal.EditorAssetLibrary.does_asset_exist(p):
            raise RuntimeError(f"could not delete {p}; something still references it")
    rig = build_source_rig(mesh)
    return build_retargeter(rig, target_mesh)


def build_source_rig(mesh):
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    rig = tools.create_asset("IK_SOMA", RIG_DIR, unreal.IKRigDefinition, unreal.IKRigDefinitionFactory())
    c = unreal.IKRigController.get_controller(rig)
    c.set_skeletal_mesh(mesh)
    c.set_retarget_root("Hips")
    for name, (start, end, goal) in SOMA_CHAINS.items():
        goal_name = ""
        if goal:
            goal_name = c.add_new_goal(f"{goal}_Goal", goal)
        c.add_retarget_chain(name, start, end, goal_name or "")
    unreal.EditorAssetLibrary.save_loaded_asset(rig)
    _log(f"IK_SOMA: root={c.get_retarget_root()} chains={[str(x.chain_name) for x in c.get_retarget_chains()]}")
    return rig


def build_retargeter(source_rig, target_mesh):
    _delete(RETARGETER)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    rtg = tools.create_asset("RTG_SOMA_to_MH", RIG_DIR, unreal.IKRetargeter, unreal.IKRetargetFactory())
    c = unreal.IKRetargeterController.get_controller(rtg)
    target_rig = unreal.EditorAssetLibrary.load_asset(TARGET_RIG)
    c.set_ik_rig(unreal.RetargetSourceOrTarget.SOURCE, source_rig)
    c.set_ik_rig(unreal.RetargetSourceOrTarget.TARGET, target_rig)
    c.set_preview_mesh(unreal.RetargetSourceOrTarget.TARGET, target_mesh)
    c.add_default_ops()
    c.assign_ik_rig_to_all_ops(unreal.RetargetSourceOrTarget.SOURCE, source_rig)
    c.assign_ik_rig_to_all_ops(unreal.RetargetSourceOrTarget.TARGET, target_rig)
    # Ops are created before they know the rigs; initial setup fills in the pelvis bones,
    # root-motion bones and chain lists from the assigned IK Rigs. Without it the Pelvis
    # Motion op had no bones and the pelvis height landed on spine_01 / thigh_* instead.
    for i in range(c.get_num_retarget_ops()):
        c.run_op_initial_setup(i)
    # ...except it does not, headless: both pelvis bones read back as empty BoneReferences and
    # the pelvis stayed frozen at a constant pose. Set every op bone explicitly.
    for i in range(c.get_num_retarget_ops()):
        oc = c.get_op_controller(i)
        if isinstance(oc, unreal.IKRetargetPelvisMotionController):
            oc.set_source_pelvis_bone("Hips")
            oc.set_target_pelvis_bone("pelvis")
            _log(f"  pelvis op: {oc.get_source_pelvis_bone()} -> {oc.get_target_pelvis_bone()}")
        elif isinstance(oc, unreal.IKRetargetRootMotionController):
            oc.set_source_root_bone("Root")
            oc.set_target_root_bone("root")
            oc.set_target_pelvis_bone("pelvis")
            _log(f"  root op: {oc.get_source_root_bone()} -> {oc.get_target_root_bone()} (pelvis {oc.get_target_pelvis_bone()})")
    c.auto_map_chains(unreal.AutoMapChainType.EXACT, True)
    # SOMA rest is a T-pose, MetaHuman's ref pose is an A-pose: align the target's retarget
    # pose to the source so rotation deltas start from matching limb directions.
    # Align ONLY the limbs. auto_align_all_bones also straightens MetaHuman's pelvis (~10 deg)
    # and spine/neck (3-10 deg each) to SOMA's straight T-pose column, which ships as a constant
    # ~8 deg backward lean on every frame (source tilt 2.9 deg, target 10.9 deg; production clips
    # sit at 1.4-5 deg). Resetting the torso AFTER a full align is not enough either: the limb
    # offsets were solved under the rotated torso and drift 7-13 deg once it is put back. So:
    # torso stays at MetaHuman's ref pose, then the limbs are aligned under it.
    pose = c.get_current_retarget_pose_name(unreal.RetargetSourceOrTarget.TARGET)
    c.reset_retarget_pose(pose, [], unreal.RetargetSourceOrTarget.TARGET)
    c.auto_align_bones(LIMB_BONES, unreal.RetargetAutoAlignMethod.CHAIN_TO_CHAIN,
                       unreal.RetargetSourceOrTarget.TARGET)
    for b in ("pelvis", "spine_03", "upperarm_l", "lowerarm_l", "thigh_l"):
        r = c.get_rotation_offset_for_retarget_pose_bone(b, unreal.RetargetSourceOrTarget.TARGET).rotator()
        _log(f"  retarget pose {b}: roll={r.roll:.1f} pitch={r.pitch:.1f} yaw={r.yaw:.1f}")
    unreal.EditorAssetLibrary.save_loaded_asset(rtg)
    ops = [str(c.get_op_name(i)) for i in range(c.get_num_retarget_ops())]
    _log(f"RTG_SOMA_to_MH: ops={ops}")
    for i in range(c.get_num_retarget_ops()):
        try:
            oc = c.get_op_controller(i)
            settings = oc.get_settings() if hasattr(oc, "get_settings") else None
            _log(f"  op {ops[i]}: {type(oc).__name__} {settings.to_dict() if hasattr(settings, 'to_dict') else settings}")
        except Exception as e:  # noqa: BLE001
            _log(f"  op {ops[i]}: settings unreadable ({e})")
    return rtg


def retarget(anim, source_mesh, target_mesh, rtg, take):
    out_dir = f"{ROOT}/Retargeted"
    dest = f"{out_dir}/{take}"
    _delete(dest)
    inputs = unreal.IKRetargetBatchOperationInputs()
    inputs.set_editor_property("assets_to_retarget", [unreal.EditorAssetLibrary.find_asset_data(anim.get_path_name())])
    inputs.set_editor_property("source_mesh", source_mesh)
    inputs.set_editor_property("target_mesh", target_mesh)
    inputs.set_editor_property("ik_retarget_asset", rtg)
    inputs.set_editor_property("include_referenced_assets", False)
    inputs.set_editor_property("overwrite_existing_files", True)
    inputs.set_editor_property("use_source_path", False)
    inputs.set_editor_property("target_path", out_dir)
    inputs.set_editor_property("suffix", "__rtg")
    new = unreal.IKRetargetBatchOperation.run_batch_retarget(inputs)
    seqs = [a for a in (x.get_asset() for x in new) if isinstance(a, unreal.AnimSequence)]
    if not seqs:
        raise RuntimeError(f"run_batch_retarget returned no AnimSequence: {[str(x.package_name) for x in new]}")
    seq = seqs[0]
    unreal.EditorAssetLibrary.rename_asset(seq.get_path_name().split(".")[0], dest)
    seq = unreal.EditorAssetLibrary.load_asset(dest)
    unreal.EditorAssetLibrary.save_loaded_asset(seq)
    _log(f"retargeted -> {seq.get_path_name()} skeleton={seq.get_editor_property('skeleton').get_path_name()}")
    return seq


# --- collar settle (telegamez#4908) -------------------------------------------------------------
#
# Kimodo's shoulders and neck move more than the production MetaHuman clips do, and the retarget
# carried that onto two places the polo is skinned to:
#   - clavicle_l/r swung ~19 deg from the Idle pose in every Kimodo clip; production clips stay
#     under 12-15 deg. Measured as skin popping through the polo collar on panda (2026-10-23).
#   - neck_02, which no production clip rotates at all, turned up to 17.5 deg.
# So, after the retarget and before the stage 05 bake:
#   - each clavicle keeps COLLAR_KEEP of its motion away from the take's first frame. Every take is
#     bookended on the production Idle pose, so frame 0 IS that pose and the loop ends stay put.
#   - neck_02 is held at its frame-0 rotation and its motion is folded into neck_01
#     (R01' = R01 * R02 * inv(R02_0)), so the head keeps its orientation.
CLAVICLES = ("clavicle_l", "clavicle_r")
COLLAR_KEEP = float(os.environ.get("KIMODO_COLLAR_KEEP", "0.5"))


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def _qinv(q):
    x, y, z, w = q
    n = x * x + y * y + z * z + w * w
    return (-x / n, -y / n, -z / n, w / n)


def _qslerp(a, b, t):
    d = sum(p * q for p, q in zip(a, b))
    if d < 0:
        b, d = tuple(-v for v in b), -d
    if d > 0.9995:
        out = tuple(p + (q - p) * t for p, q in zip(a, b))
    else:
        import math
        th = math.acos(min(1.0, d))
        s = math.sin(th)
        wa, wb = math.sin((1 - t) * th) / s, math.sin(t * th) / s
        out = tuple(wa * p + wb * q for p, q in zip(a, b))
    n = sum(v * v for v in out) ** 0.5
    return tuple(v / n for v in out)


def _track(seq, bone, n):
    # The raw-track getters return nothing for a retargeted sequence in 5.8, so evaluate the
    # bone-space pose at every key instead.
    lib = unreal.AnimationLibrary
    pos, rot, scl = [], [], []
    for f in range(n):
        t = lib.get_bone_pose_for_frame(seq, bone, f, False)
        r = t.rotation
        pos.append(t.translation)
        rot.append((r.x, r.y, r.z, r.w))
        scl.append(t.scale3d)
    return pos, rot, scl


def _expand(keys, n):
    return keys if len(keys) == n else [keys[0]] * n


def settle_collar(seq):
    names = {str(t) for t in unreal.AnimationLibrary.get_animation_track_names(seq)}
    ctrl = seq.get_editor_property("controller")
    n = unreal.AnimationLibrary.get_num_keys(seq)
    tracks = {b: _track(seq, b, n) for b in (*CLAVICLES, "neck_01", "neck_02") if b in names}
    ctrl.open_bracket(unreal.Text("kimodo collar settle"), True)
    try:
        for bone in CLAVICLES:
            if bone not in tracks:
                continue
            pos, rot, scl = (_expand(k, n) for k in tracks[bone])
            new = [_qslerp(rot[0], q, COLLAR_KEEP) for q in rot]
            ctrl.set_bone_track_keys(bone, pos, [unreal.Quat(*q) for q in new], scl, True)
        if "neck_01" in tracks and "neck_02" in tracks:
            p1, r1, s1 = (_expand(k, n) for k in tracks["neck_01"])
            p2, r2, s2 = (_expand(k, n) for k in tracks["neck_02"])
            r2_0_inv = _qinv(r2[0])
            r1_new = [_qmul(_qmul(a, b), r2_0_inv) for a, b in zip(r1, r2)]
            ctrl.set_bone_track_keys("neck_01", p1, [unreal.Quat(*q) for q in r1_new], s1, True)
            ctrl.set_bone_track_keys("neck_02", p2, [unreal.Quat(*r2[0])] * n, s2, True)
    finally:
        ctrl.close_bracket(True)
    unreal.EditorAssetLibrary.save_loaded_asset(seq)
    _log(f"collar settle: clavicles keep {COLLAR_KEEP:.2f} of their motion, neck_02 folded into neck_01 "
         f"({n} keys, tracks {sorted(tracks)})")


def main():
    fbx = os.environ["KIMODO_FBX"]
    take = os.environ["KIMODO_TAKE"]
    target_mesh = unreal.EditorAssetLibrary.load_asset(os.environ.get("KIMODO_TARGET") or DEFAULT_TARGET_MESH)
    _log(f"fbx={fbx} take={take} target={target_mesh.get_path_name()}")
    if os.environ.get("KIMODO_REBUILD_RIGS") == "1":
        # Full reset: rigs first (they reference the SOMA mesh), then the shared mesh and any
        # per-take sources left over from before the skeleton was shared.
        for p in (RETARGETER, SOURCE_RIG, f"{ROOT}/Source", SOMA_DIR):
            _delete(p)
    mesh = ensure_soma_mesh(fbx)
    anim = import_take(fbx, take, mesh)
    rtg = ensure_rigs(mesh, target_mesh)
    seq = retarget(anim, mesh, target_mesh, rtg, take)
    if os.environ.get("KIMODO_COLLAR_SETTLE", "1") != "0":
        settle_collar(seq)
    result = {"take": take, "retargeted": seq.get_path_name().split(".")[0],
              "source_anim": anim.get_path_name().split(".")[0]}
    out = os.environ.get("KIMODO_RESULT")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    _log(f"done {json.dumps(result)}")


main()
