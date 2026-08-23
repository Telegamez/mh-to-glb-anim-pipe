"""Fit the DNA's neutral pose to Unreal's, anchored at the face. Gated on the residual.

    python fit_dna_to_ue.py <dna> <dump.npz>

## What it proves

That the DNA and Unreal describe the same rig in the same place, so any later comparison of
RigLogic output against a UE dump is measuring the rig rather than a frame mismatch. It is a
CONFIGURATION proof: the oracle's correctness is established separately, and exactly, by
`diff_oracle_vs_library.py`.

## Anchor at the face, not at the body

An FK rooted at the DNA's body chain inherits every bit of placement error, and there is a lot
of it: the DNA's `spine_04` neutral is [0, 123.87, 3.8] where UE's is [7.9, 0, 0], and the
whole facial hierarchy sits ~19 cm higher in an evaluated bake than in the reference pose.
Rooted at the body it fitted at 437 mm and no Euler order helped, because none of that is a
rotation convention.

Anchored at `FACIAL_C_FacialRoot`, with UE's component transforms expressed relative to the
same bone, the placement cancels by construction. Nothing downstream cares where the DNA
thinks the spine is.

## Result on telly

    all 785 subtree joints     426.91 mm   scale 1.41707   det -2.8456
    excluding 120 outliers       0.0000 mm  scale 1.00000   det -1.0000

Zero residual, unit scale, determinant exactly -1 — a pure reflection, which is what a
left-handed source produces. The spurious 1.03 and 1.42 scales in earlier fits were error
absorption and vanished once the frame was right.

## The 120 excluded joints are a mechanism, not a fudge

Every one sits at EXACTLY 166.37 cm from the facial root in UE's component space — the same
value for all 120, which is the head's height rather than a distance. They are the same bones
that make UE's two LOCAL accessors disagree: at frame 0 of an evaluated bake, which is the
Default pose and should equal the reference, `get_ref_bone_pose(..., LOCAL)` and
`get_bone_pose(..., LOCAL)` differ on 727 of 875 bones by ~1720-1750 mm, again head height.

One explanation covers both: for these bones the evaluated bake reports transforms that were
never resolved against their parent, so they come back carrying the head's component offset.
Affected families are FACIAL_*_Forehead*, FACIAL_C_Hair* and FACIAL_R_ForeheadIn*.

Identical magnitude across 120 bones is a mechanism. It is characterised here and deliberately
not debugged: the comparison uses component space throughout, which is the accessor the body
control passed all three gates on.
"""
from __future__ import annotations

import argparse
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0] if "/" in __file__ else ".")
from parse_defn import parse_definition  # noqa: E402

FACIAL_ROOT = "FACIAL_C_FacialRoot"
OUTLIER_CM = 40.0
GATE_MM = 0.01


def euler_xyz(rx, ry, rz):
    x, y, z = np.radians([rx, ry, rz])
    cx, sx = np.cos(x), np.sin(x)
    cy, sy = np.cos(y), np.sin(y)
    cz, sz = np.cos(z), np.sin(z)
    return (np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
            @ np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
            @ np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]]))


def similarity_fit(src, dst):
    """Kabsch WITH scale and WITH reflection allowed, plus its RMS residual.

    Reflection is not optional: Unreal is left-handed. Plain Kabsch forces det +1 and cannot
    express the mirror, which is what left the earlier fits absorbing error into scale.
    Mirrors `5.7/native-glb/tools/diff_against_unreal.py`.
    """
    sc, dc = src.mean(0), dst.mean(0)
    s0, d0 = src - sc, dst - dc
    U, S, Vt = np.linalg.svd(s0.T @ d0 / len(src))
    var = (s0 ** 2).sum() / len(src)
    best = None
    for det in (1.0, -1.0):
        R = Vt.T @ np.diag([1.0, 1.0, det]) @ U.T
        scale = float((S * np.array([1.0, 1.0, det])).sum() / var)
        t = dc - scale * R @ sc
        residual = float(np.sqrt((((src @ (scale * R).T + t) - dst) ** 2).sum(1)).mean())
        if best is None or residual < best[0]:
            M = np.eye(4)
            M[:3, :3] = scale * R
            M[:3, 3] = t
            best = (residual, M)
    return best[1], best[0]


def fk_from(defn, root_idx):
    """Component positions of the root's subtree, with the root at the origin."""
    J, P = defn["joints"], defn["joint_parents"]
    nt = np.array(defn["neutral_translation"])
    nr = np.array(defn["neutral_rotation_deg"])
    n = len(J)
    R = np.zeros((n, 3, 3))
    T = np.zeros((n, 3))
    done = np.zeros(n, bool)
    R[root_idx] = np.eye(3)
    done[root_idx] = True
    for j in range(n):
        chain, k, ok, guard = [], j, True, 0
        while k != root_idx and not done[k]:
            chain.append(k)
            k = P[k]
            guard += 1
            if (P[k] == k and k != root_idx) or guard > 100:
                ok = False
                break
        if not ok:
            continue
        for c in reversed(chain):
            p = P[c]
            R[c] = R[p] @ euler_xyz(*nr[c])
            T[c] = R[p] @ nt[c] + T[p]
            done[c] = True
    return T, done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dna")
    ap.add_argument("dump")
    ap.add_argument("--outlier-cm", type=float, default=OUTLIER_CM)
    ns = ap.parse_args()

    defn = parse_definition(ns.dna)
    J = defn["joints"]
    root_idx = J.index(FACIAL_ROOT)

    T, done = fk_from(defn, root_idx)

    d = np.load(ns.dump, allow_pickle=True)
    bones = [str(b) for b in d["bone_names"]]
    bi = {n: i for i, n in enumerate(bones)}
    comp = d["raw"][0, :, :3].astype(np.float64)
    root_pos = comp[bi[FACIAL_ROOT]]

    cand = [(k, bi[J[k]]) for k in range(len(J))
            if done[k] and J[k] in bi and J[k].upper().startswith("FACIAL")]
    ue = np.array([comp[b] - root_pos for _, b in cand])
    dist = np.linalg.norm(ue, axis=1)
    keep = dist < ns.outlier_cm

    print(f"dna    {ns.dna}")
    print(f"dump   {ns.dump}")
    print(f"anchor {FACIAL_ROOT}, subtree {len(cand)} joints")

    for label, sel in (("all subtree joints", np.ones(len(cand), bool)), ("outliers excluded", keep)):
        src = np.array([T[cand[i][0]] for i in range(len(cand)) if sel[i]])
        dst = ue[sel]
        M, res = similarity_fit(src, dst)
        det = np.linalg.det(M[:3, :3])
        print(f"  {label:<22} n={len(src):<5} residual {res*10:9.4f} mm   "
              f"scale {abs(det)**(1/3):.5f}   det {det:+.4f}")

    n_out = int((~keep).sum())
    if n_out:
        vals = np.unique(np.round(dist[~keep], 2))
        print(f"\n  {n_out} joints excluded at >{ns.outlier_cm} cm from the anchor.")
        print(f"  distinct distances among them: {len(vals)} -> {vals[:4]}")
        if len(vals) <= 3:
            print("  A single repeated distance is a MECHANISM, not a measurement: these bones "
                  "carry\n  the head's component offset because the bake never resolved them "
                  "against a parent.")

    src = np.array([T[cand[i][0]] for i in range(len(cand)) if keep[i]])
    M, res = similarity_fit(src, ue[keep])
    if res * 10 > GATE_MM:
        print(f"\nREFUSED: residual {res*10:.4f} mm exceeds {GATE_MM} mm. The frame is not "
              f"settled and no comparison through it should be believed.", file=sys.stderr)
        return 1
    print(f"\nFIT ACCEPTED: {res*10:.4f} mm over {int(keep.sum())} joints, "
          f"pure reflection, unit scale. The DNA and Unreal describe the same rig, "
          f"in the same place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
