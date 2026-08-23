"""Diff the Python oracle against Epic's compiled RigLogic — no frame of reference involved.

    python diff_oracle_vs_library.py <dna> <poses.json> <riglogic_probe.exe> [--json out]

## Why this comparison, and why first

Three attempts to validate the oracle against Unreal's posed skeleton produced three wrong
tables, and every one failed in the comparator rather than in the thing being compared: a
`LOCAL` space that is not parent-relative, a hierarchy borrowed from an armature stage 02 had
merged, and an unhandled axis convention. All three are questions about a FRAME.

This comparison has no frame. Both sides emit in RigLogic's own output space — joint attribute
rows and blendshape weights, straight out of `calculate()`. There is no axis to permute, no
hierarchy to get wrong, no placement to cancel. It answers "is the evaluator right" on its own,
decoupled from "is my frame right", which is exactly what the three tables had tangled.

If they agree, the oracle is semantically validated against Epic's own code, and the UE dump's
job narrows to what it is actually for: proving the compiled library is configured the way the
ABP runs it, and validating the comparator.

If they disagree, the disagreement is real, named and bounded — a control and a joint row, not
a coordinate system.

## Interchange format

Deliberately dumb binary, so neither side can disagree about parsing:

    controls  u32 poseCount, u32 controlCount, then poseCount*controlCount float32
    outputs   u32 poseCount, u32 jointRows, u32 shapeCount,
              then per pose jointRows float32 followed by shapeCount float32
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile

import numpy as np

from parse_bhvr import Reader, read_sections
from riglogic_eval import RigLogic

DNA_PREFIX = "CTRL_expressions."


def _raw_control_names(dna_path):
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

    sv()
    return sv()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dna")
    p.add_argument("poses")
    p.add_argument("probe")
    p.add_argument("--json")
    p.add_argument("--tol", type=float, default=1e-4,
                   help="absolute tolerance on a joint row / shape weight")
    ns = p.parse_args()

    raw_names = _raw_control_names(ns.dna)
    rig = RigLogic(ns.dna, lod=0)
    index = {n: i for i, n in enumerate(raw_names)}
    n_raw = len(raw_names)

    spec = json.load(open(ns.poses, encoding="utf-8"))
    poses = spec["poses"]

    # The probe sets RAW controls only, so the table is the raw slice of the control vector.
    # The oracle's PSD stage derives the rest from exactly these, which is the point: if the
    # two disagree, the PSD stage is a candidate and the single-control band will say so.
    table = np.zeros((len(poses), n_raw), np.float32)
    for pi, pose in enumerate(poses):
        for name, val in (pose.get("controls") or {}).items():
            i = index.get(f"{DNA_PREFIX}{name}")
            if i is not None and i < n_raw:
                table[pi, i] = float(val)

    tmp = tempfile.mkdtemp(prefix="rl_diff_")
    cpath = os.path.join(tmp, "controls.bin")
    opath = os.path.join(tmp, "outputs.bin")
    with open(cpath, "wb") as f:
        f.write(struct.pack("<II", len(poses), n_raw))
        f.write(table.tobytes())

    r = subprocess.run([ns.probe, ns.dna, "--controls", cpath, "--outputs", opath],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(opath):
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        print(f"REFUSED: probe exited {r.returncode} and wrote no outputs.", file=sys.stderr)
        return 2

    with open(opath, "rb") as f:
        pose_count, joint_rows, shape_count = struct.unpack("<III", f.read(12))
        buf = np.frombuffer(f.read(), dtype=np.float32)
    per_pose = joint_rows + shape_count
    assert buf.size == pose_count * per_pose, (buf.size, pose_count, per_pose)
    buf = buf.reshape(pose_count, per_pose)
    lib_joints = buf[:, :joint_rows].astype(np.float64)
    lib_shapes = buf[:, joint_rows:].astype(np.float64)

    print(f"dna     {os.path.basename(ns.dna)}")
    print(f"poses   {pose_count}   joint rows {joint_rows}   shapes {shape_count}")
    print(f"probe   {os.path.basename(ns.probe)}")

    ora_joints = np.zeros_like(lib_joints)
    for pi in range(pose_count):
        v = np.zeros(rig.control_size, np.float32)
        v[:n_raw] = table[pi]
        ora_joints[pi] = rig.evaluate(v)

    err = np.abs(ora_joints - lib_joints)
    per_pose_err = err.max(axis=1)

    rows = []
    for pi, pose in enumerate(poses[:pose_count]):
        rows.append({"pose": pose["name"], "band": pose.get("band", "?"),
                     "max_err": float(per_pose_err[pi]),
                     "rows_over_tol": int((err[pi] > ns.tol).sum()),
                     "lib_max": float(np.abs(lib_joints[pi]).max()),
                     "oracle_max": float(np.abs(ora_joints[pi]).max())})

    singles = sorted([r for r in rows if r["band"] == "single-control"],
                     key=lambda r: -r["max_err"])
    bad = [r for r in singles if r["max_err"] > ns.tol]

    print(f"\n{'='*74}\nSINGLE-CONTROL BAND — {len(singles)} frames, tolerance {ns.tol}\n{'='*74}")
    if not bad:
        print(f"CLEAN: every single-control frame agrees within {ns.tol}.")
        print(f"  worst: {singles[0]['pose'].replace('single__','')} "
              f"at {singles[0]['max_err']:.3e}")
    else:
        print(f"{len(bad)}/{len(singles)} controls DISAGREE beyond {ns.tol}:\n")
        print(f"  {'control':<40}{'max err':>12}{'rows':>7}{'library':>11}{'oracle':>11}")
        for r in bad[:60]:
            print(f"  {r['pose'].replace('single__',''):<40}{r['max_err']:>12.3e}"
                  f"{r['rows_over_tol']:>7}{r['lib_max']:>11.4f}{r['oracle_max']:>11.4f}")
        if len(bad) > 60:
            print(f"  ... and {len(bad)-60} more")

    print(f"\n{'='*74}\nPER BAND\n{'='*74}")
    print(f"  {'band':<24}{'poses':>7}{'over tol':>10}{'max err':>13}{'median err':>13}")
    bands = {}
    for r in rows:
        bands.setdefault(r["band"], []).append(r)
    for band, rs in bands.items():
        e = np.array([r["max_err"] for r in rs])
        print(f"  {band:<24}{len(rs):>7}{int((e > ns.tol).sum()):>10}"
              f"{e.max():>13.3e}{np.median(e):>13.3e}")

    print(f"\nBLENDSHAPE STAGE (library only — the oracle's is a separate gather)")
    print(f"  poses with any non-zero shape weight: "
          f"{int((np.abs(lib_shapes) > 1e-6).any(axis=1).sum())}/{pose_count}")

    if ns.json:
        with open(ns.json, "w", encoding="utf-8") as f:
            json.dump({"dna": ns.dna, "tolerance": ns.tol, "poses": pose_count,
                       "joint_rows": joint_rows,
                       "single_control_clean": not bad,
                       "single_control_failures": [r["pose"].replace("single__", "")
                                                   for r in bad],
                       "rows": rows}, f, indent=2)
        print(f"\nwrote {ns.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
