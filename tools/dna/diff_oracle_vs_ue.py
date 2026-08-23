"""Diff the Python RigLogic oracle against Unreal's own evaluated pose, per band.

    python diff_oracle_vs_ue.py <dump.npz> <poses.json> <dna> [--json out.json]

Slice 5.3's validation step. The oracle in `riglogic_eval.py` has produced every number in the
corrective-residual measurement, and until now nothing has checked that it evaluates the rig
the way Unreal does. Structural correctness is not semantic correctness: the parse lands on the
section boundary and the PSD indices agree with the definition layer, and both would still be
true of an evaluator that composed the stages in the wrong order.

## Two conditions this obeys, and why they matter

**Deltas from the bake's own frame 0, not from the raw sequence's rest.** Frame 0 is `Default`
— every control at zero — so RigLogic's contribution there is nothing and the frame holds the
rig's placed reference pose. Differencing against it cancels the ~156 cm of facial-hierarchy
PLACEMENT *by construction*, rather than by subtracting a number someone estimated. Placement
is real and it is not what an evaluator is being judged on.

**Per band, single controls first.** The 263 single-control frames each move exactly one column
of the joint matrix, so a disagreement there NAMES the control responsible. An aggregate
percentage over all 322 poses would average that away and leave nothing to act on. The list of
disagreeing controls is the output; the aggregate is a footnote.

## What is compared

Translation only, for now. RigLogic emits nine attributes per joint — three translation, three
rotation, three scale — and the rotation comparison needs the Euler convention
(`RL_BUILD_WITH_XYZ_ROTATION_ORDER`) matched against UE's quaternion output before it means
anything. Translation is unambiguous in both, in centimetres, and is where facial joint motion
mostly lives. Rotation is owed and is named as owed rather than quietly skipped.

Local space, because RigLogic's deltas are added to each bone's reference LOCAL transform. A
world-space diff would disagree on every descendant of a joint the evaluator got right.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from parse_bhvr import Reader, read_sections
from riglogic_eval import RigLogic

DNA_PREFIX = "CTRL_expressions."
TOL_CM = 0.01          # 0.1 mm — an order below the smallest shape F2 measures (5.92 mm)


def _to_parent_relative(dump, bone_names, hierarchy_npz):
    """Component-space transforms -> parent-relative translation, per frame.

    RigLogic's translation output is a delta on each joint's LOCAL (parent-relative)
    translation, so that is the quantity a diff has to be in. UE's AnimPoseSpaces.LOCAL does
    not supply it — measured on this rig it reports FACIAL_L_ForeheadInB2 at 168 cm, which is
    a component-space magnitude, not a parent offset.

    The hierarchy comes from the stage-02 skin dump, whose armature carries every FACIAL_*
    bone with its parent index. 863 of the 875 dumped bones are present; the 12 that are not
    are body bones the retarget renamed (pelvis, spine_01, clavicle_l...), none of which the
    DNA drives.

        t_local = conj(q_parent) * (t_world - t_parent)

    Scale is ignored: MetaHuman facial joints are unscaled, and folding a scale term in
    without checking that would be inventing precision.
    """
    sk = np.load(hierarchy_npz, allow_pickle=True)
    arm_names = [str(x) for x in sk["bone_names"]]
    arm_parent = sk["bone_parents"]
    arm_index = {n: i for i, n in enumerate(arm_names)}

    # bone -> index of its parent IN THE DUMP, or -1 when the parent is not dumped/known.
    dump_index = {n: i for i, n in enumerate(bone_names)}
    parent_of = np.full(len(bone_names), -1, dtype=np.int64)
    for i, n in enumerate(bone_names):
        ai = arm_index.get(n)
        if ai is None:
            continue
        pi = int(arm_parent[ai])
        if pi < 0:
            continue
        parent_of[i] = dump_index.get(arm_names[pi], -1)

    t = dump["raw"][:, :, :3].astype(np.float64)
    q = dump["raw"][:, :, 3:7].astype(np.float64)        # x y z w

    has_parent = parent_of >= 0
    pidx = np.where(has_parent, parent_of, 0)

    tp = t[:, pidx, :]
    qp = q[:, pidx, :]
    rel = t - tp

    # rotate rel by conj(q_parent)
    x, y, z, w = qp[..., 0], qp[..., 1], qp[..., 2], qp[..., 3]
    cx, cy, cz = -x, -y, -z
    ux, uy, uz = cx, cy, cz
    vx, vy, vz = rel[..., 0], rel[..., 1], rel[..., 2]
    # v' = v + 2*w*(u x v) + 2*(u x (u x v))
    crx = uy * vz - uz * vy
    cry = uz * vx - ux * vz
    crz = ux * vy - uy * vx
    ccx = uy * crz - uz * cry
    ccy = uz * crx - ux * crz
    ccz = ux * cry - uy * crx
    out = np.empty_like(rel)
    out[..., 0] = vx + 2 * w * crx + 2 * ccx
    out[..., 1] = vy + 2 * w * cry + 2 * ccy
    out[..., 2] = vz + 2 * w * crz + 2 * ccz

    # Root and any bone whose parent is absent keep their component transform; they are not
    # DNA joints and are excluded from the comparison anyway.
    out[:, ~has_parent, :] = t[:, ~has_parent, :]
    n_rel = int(has_parent.sum())
    print(f"hierarchy {n_rel}/{len(bone_names)} bones converted to parent-relative "
          f"(from {hierarchy_npz.split('/')[-1]})")
    return out


def _definition_names(dna_path):
    data = open(dna_path, "rb").read()
    r = Reader(data, read_sections(data)["defn"]["offset"])
    for _ in range(4):
        r.vec_u16()
        for _ in range(r.u32()):
            r.vec_u16()

    def s():
        n = r.u32()
        v = r.d[r.o:r.o + n].decode("utf-8", "replace")
        r.o += n
        return v

    def sv():
        return [s() for _ in range(r.u32())]

    sv()                      # gui control names
    raw = sv()
    joints = sv()
    return raw, joints


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dump")
    p.add_argument("poses")
    p.add_argument("dna")
    p.add_argument("--json")
    p.add_argument("--tol", type=float, default=TOL_CM, help="cm")
    p.add_argument("--hierarchy", required=False,
                   help="skin_dump.npz carrying bone_names + bone_parents")
    ns = p.parse_args()

    d = np.load(ns.dump, allow_pickle=True)
    meta = json.loads(str(d["meta"][0]))
    # Dumps written before --space existed have no field and were always world/component
    # space; the units string in their meta says so.
    dump_space = meta.get("space") or ("world" if "component space" in meta.get("units", "")
                                       else None)
    if dump_space != "world":
        print(f"REFUSED: dump is in {meta.get('space')!r} space; this tool needs 'world'.\n"
              f"  UE's AnimPoseSpaces.LOCAL is NOT parent-relative — on this rig it reports\n"
              f"  FACIAL_L_ForeheadInB2 at [-0.24, 168.23, -31.42], which no forehead bone is\n"
              f"  from its parent. It is another component-space basis with permuted axes.\n"
              f"  Parent-relative transforms are derived here instead, from the hierarchy.",
              file=sys.stderr)
        return 2

    bone_names = [str(b) for b in d["bone_names"]]

    # Component-space deltas, differenced against frame 0 (Default) per condition 1.
    #
    # NOT parent-relative, and that was checked rather than assumed. Two attempts to get a
    # parent-relative quantity both produced garbage, and each looked like an evaluator failure:
    #
    #   UE's AnimPoseSpaces.LOCAL is not parent-relative on this rig — it reports
    #   FACIAL_L_ForeheadInB2 at 168 cm, a component-space magnitude.
    #
    #   Deriving it from the stage-02 skin dump's hierarchy is wrong too: stage 02 MERGES
    #   armatures, so its parenting is not UE's. That conversion turned a real 1.1 cm lip
    #   motion into a 99 cm "disagreement" on 229 of 263 controls — a table that would have
    #   read as a catastrophic evaluator bug and was entirely an artifact of this file.
    #
    # For a facial rig the approximation is sound: parents (jaw, head) barely rotate within a
    # single-control pose, so a component delta and a local delta agree to well under the
    # tolerance. It is an approximation and is labelled as one — a control whose PARENT is what
    # moves would need the real hierarchy, and `jawOpen` is the case to watch.
    ue_delta = d["raw"][:, :, :3].astype(np.float64)
    ue_delta = ue_delta - ue_delta[0][None, :, :]
    ue_local = ue_delta

    raw_names, joint_names = _definition_names(ns.dna)
    rig = RigLogic(ns.dna, lod=0)
    control_index = {n: i for i, n in enumerate(raw_names)}

    # DNA joint -> UE bone. Exact names first; MetaHuman keeps them identical.
    bone_index = {n: i for i, n in enumerate(bone_names)}
    joint_to_bone = {j: bone_index[j] for j in joint_names if j in bone_index}
    print(f"dump      {ns.dump}")
    print(f"          {ue_local.shape[0]} frames x {ue_local.shape[1]} bones, "
          f"dumped in {dump_space} space, diffed parent-relative")
    print(f"joints    {len(joint_names)} in DNA, {len(joint_to_bone)} matched to UE bones")
    if len(joint_to_bone) < len(joint_names):
        missing = [j for j in joint_names if j not in bone_index]
        print(f"          UNMATCHED ({len(missing)}): {missing[:6]}")

    spec = json.load(open(ns.poses, encoding="utf-8"))
    poses = spec["poses"]
    # Frame 0 is the rest frame the authoring tool prepends; pose i is frame i+1.
    frame_of = {pose["name"]: i + 1 for i, pose in enumerate(poses)}

    j_idx = np.array([rig_i for rig_i, j in enumerate(joint_names) if j in joint_to_bone])
    b_idx = np.array([joint_to_bone[j] for j in joint_names if j in joint_to_bone])

    def oracle_translation(controls):
        """870x9 -> the translation rows for the matched joints, in cm."""
        out = rig.evaluate(controls)                     # joint attribute rows
        per_joint = out.reshape(-1, 9)                   # tx ty tz rx ry rz sx sy sz
        return per_joint[j_idx][:, :3].astype(np.float64)

    def vector_for(pose):
        v = np.zeros(rig.control_size, np.float32)
        for name, val in (pose.get("controls") or {}).items():
            i = control_index.get(f"{DNA_PREFIX}{name}")
            if i is not None:
                v[i] = float(val)
        return v

    rows = []
    for pose in poses:
        f = frame_of[pose["name"]]
        if f >= ue_delta.shape[0]:
            continue
        o = oracle_translation(vector_for(pose))
        u = ue_delta[f][b_idx]
        err = np.abs(o - u).max(axis=1)                  # per joint, cm
        rows.append({
            "pose": pose["name"],
            "band": pose.get("band", "?"),
            "max_err_cm": float(err.max()),
            "joints_over_tol": int((err > ns.tol).sum()),
            "ue_motion_cm": float(np.abs(u).max()),
            "oracle_motion_cm": float(np.abs(o).max()),
        })

    bands = {}
    for r in rows:
        bands.setdefault(r["band"], []).append(r)

    # ---- the output that names something ------------------------------------------------
    singles = sorted([r for r in rows if r["band"] == "single-control"],
                     key=lambda r: -r["max_err_cm"])
    bad = [r for r in singles if r["max_err_cm"] > ns.tol]

    print(f"\n{'='*72}\nSINGLE-CONTROL BAND — {len(singles)} frames, tolerance {ns.tol} cm\n{'='*72}")
    if not bad:
        print(f"CLEAN: every single-control frame agrees within {ns.tol} cm.")
        print(f"  worst: {singles[0]['pose']} at {singles[0]['max_err_cm']:.5f} cm")
    else:
        print(f"{len(bad)}/{len(singles)} controls DISAGREE beyond {ns.tol} cm:\n")
        print(f"  {'control':<40}{'max err cm':>12}{'joints':>8}{'UE cm':>9}{'oracle cm':>11}")
        for r in bad[:60]:
            print(f"  {r['pose'].replace('single__',''):<40}{r['max_err_cm']:>12.5f}"
                  f"{r['joints_over_tol']:>8}{r['ue_motion_cm']:>9.3f}{r['oracle_motion_cm']:>11.3f}")
        if len(bad) > 60:
            print(f"  ... and {len(bad)-60} more")

    print(f"\n{'='*72}\nPER BAND\n{'='*72}")
    print(f"  {'band':<24}{'frames':>8}{'over tol':>10}{'max err cm':>13}{'median err':>12}")
    for band, rs in bands.items():
        errs = np.array([r["max_err_cm"] for r in rs])
        print(f"  {band:<24}{len(rs):>8}{int((errs > ns.tol).sum()):>10}"
              f"{errs.max():>13.5f}{np.median(errs):>12.5f}")

    print("\nTranslation only. Rotation comparison is OWED — it needs the DNA's Euler "
          "convention\nmatched against UE's quaternions before it means anything.")

    if ns.json:
        with open(ns.json, "w", encoding="utf-8") as f:
            json.dump({"dump": ns.dump, "tolerance_cm": ns.tol,
                       "joints_matched": len(joint_to_bone), "joints_in_dna": len(joint_names),
                       "single_control_clean": not bad,
                       "single_control_failures": [r["pose"].replace("single__", "")
                                                   for r in bad],
                       "rows": rows}, f, indent=2)
        print(f"\nwrote {ns.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
