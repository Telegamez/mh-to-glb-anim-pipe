"""Measure skin punching through cloth under animation. THE TUCK IT COMPUTES DOES NOT WORK.

## Status: the measurement is sound, the fix is disproven — do not re-wire it into the pipeline

Two versions were built, measured and rejected (2026-08-19). Both made the defect WORSE on a
like-for-like test, and the reason is structural rather than a tuning miss:

    a bind-space offset cannot fix a pose-dependent penetration

The body and the garment are skinned by DIFFERENT weight sets, so the penetration is driven by
their RELATIVE motion (up to 28mm on Telly). A bind-time offset rotates with the BODY's bones,
so at some poses the vector that pointed into the garment at rest points out of it. Verified
directly: at bind the tuck moves skin from -10.4mm to -15.3mm (correctly inward), yet over the
shipped clips the poking count rises 36 -> 50 and total penetration 414mm -> 1081mm.

The first version compounded this by pushing along the BODY normal, which in the armpit is
nearly tangent to the sleeve: a 7mm push reduced penetration by -1.2mm. Pushing along the
GARMENT normal instead fixed the direction and still failed, which is what isolated the cause
to the bind-space premise rather than the geometry.

Anything that actually fixes this has to act per-pose (Unreal uses a clothing ABP we cannot
carry into glTF) or hide the skin outright — and deletion near a sleeve hem is vetoed because
it opens a visible gap (00-START-HERE 7.0).

## What this file is still good for

The measurement. `_tri_signed` (nearest-triangle signed distance, barycentric-clamped) and
`_boundary_verts` (garment openings, so skin leaving through a cuff is not counted as a
failure) quantify the defect honestly: Telly, 36 of 15444 body verts, median 10.7mm. Use it to
size the problem or to score a real fix — not to ship offsets.

Original design notes follow.

## Pose-aware body tuck — stop skin punching through cloth during animation.

## The defect

`_hide_body_under_clothing()` in stage 03 decides what is covered ONCE, in the bind pose. Skin
that sits inside a sleeve at rest does not stay inside it while the arm moves, so a handful of
vertices swing out through the garment and render as skin-coloured shards on the shoulder and
sleeve. Measured on Telly over 10 clips:

    121 of 15444 body verts poke at some pose, up to 24mm, and they are almost all
    upperarm_twist_02 / upperarm_tricep / upperarm_bicep — the arm inside the short sleeve

Deleting them is not available: they sit within the sleeve-hem opening veto, and deleting near
an opening leaves a visible gap (00-START-HERE §7.0). So they get pushed inward instead.

## The solve

For each body vertex, measure how far it pokes outside the garment at every sampled pose and
push it in along its bind normal by the worst of those, plus a margin. Two properties make this
safe rather than a fudge:

  * the offset is the MEASURED penetration, not a tuned constant, so it is zero everywhere the
    skin already behaves and grows only exactly where it must;
  * it is pushed along the GARMENT's inward normal, not the body's. Those are not the same
    direction and assuming they were is what broke the first two attempts: in the armpit the
    body normal is nearly tangent to the sleeve, so a 7mm push along it moved the vertex 7mm
    and reduced penetration by -1.2mm (i.e. made it worse). Along the garment normal the
    reduction is 1:1 by construction.
  * because it is zero for non-poking neighbours, the field is continuous at the sleeve hem —
    the arm keeps its silhouette where it emerges and thins only where it is hidden.

A few smoothing passes remove the pixel-level roughness that a per-vertex max leaves behind.

Containment is measured against the garment SURFACE: the closest point on each of the nearest
candidate triangles, signed by that triangle's own normal. An earlier version used the average
over the K nearest garment VERTICES and it does not survive a sleeve — inside a narrow tube the
nearest vertices sit on the far wall with their normals facing back, so contained skin reads as
a 24mm poke. Acting on that inflated number pushed the arm in by 26mm (over half its radius),
which distorted the arm AND made the measurement worse; it was measured, rejected and reverted.

    python tools/solve_body_tuck.py --dump <skin_dump.npz> --solved <skin_solved.npz> \
        --animations docs/animations/mh-animations.glb --out <body_tuck.npz>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

K_NEIGHBOURS = 8
OPENING_REACH = 0.030     # skin this close to a garment opening is free to leave through it
BIND_INSIDE = -0.004      # must be this far inside the garment at bind to be considered covered
BIND_NEAR = 0.03          # ...and this close to it, so free skin (neck, hands) is never touched
POKE_EPS = 0.001          # penetration past this counts
TUCK_MARGIN = 0.002       # extra clearance so the fix survives frames we did not sample
TUCK_CAP = 0.010          # never push skin further than this. A deeper "penetration" is more
                          # likely a measurement artifact than real cloth failure, and pushing
                          # a comparable fraction of a limb's radius distorts the limb - an
                          # earlier uncapped version moved the arm 26mm and had to be reverted.
SMOOTH_PASSES = 3


def _log(msg):
    print(f"[body_tuck] {msg}", flush=True)


def _boundary_verts(tris):
    """Vertices on a garment opening — collar, cuffs, hem.

    An edge used by exactly one triangle is a boundary edge. Skin near one of these is entitled
    to be outside the garment (the neck leaves through the collar, the forearm through the cuff),
    so it must never be counted as punching through fabric or tucked for doing so.
    """
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]], 0)
    e = np.sort(e, axis=1)
    uniq, counts = np.unique(e, axis=0, return_counts=True)
    return np.unique(uniq[counts == 1])


def _tri_signed(points, verts, tris, centroids=None, tree=None, k=K_NEIGHBOURS):
    """Signed distance from each point to the garment surface. Negative = inside.

    For the k nearest triangles by centroid, clamp the point onto each triangle's plane inside
    its barycentric bounds, keep whichever closest point is nearest, and take the sign from that
    triangle's normal. Barycentric clamping matters: without it a point beside a triangle gets
    the plane distance, which near a sleeve seam is nearly zero and hides real penetration.
    """
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    n = np.cross(b - a, c - a)
    area = np.linalg.norm(n, axis=1)
    n = n / np.maximum(area, 1e-12)[:, None]
    centroids = (a + b + c) / 3.0 if centroids is None else centroids
    tree = tree or cKDTree(centroids)
    _, idx = tree.query(points, k=k)

    p = points[:, None, :]
    A, B, C, N = a[idx], b[idx], c[idx], n[idx]
    # project, then clamp into the triangle by barycentric coordinates
    d = np.einsum("nkj,nkj->nk", p - A, N)
    q = p - d[:, :, None] * N
    v0, v1, v2 = B - A, C - A, q - A
    d00 = np.einsum("nkj,nkj->nk", v0, v0)
    d01 = np.einsum("nkj,nkj->nk", v0, v1)
    d11 = np.einsum("nkj,nkj->nk", v1, v1)
    d20 = np.einsum("nkj,nkj->nk", v2, v0)
    d21 = np.einsum("nkj,nkj->nk", v2, v1)
    den = np.maximum(d00 * d11 - d01 * d01, 1e-20)
    u = np.clip((d11 * d20 - d01 * d21) / den, 0.0, 1.0)
    v = np.clip((d00 * d21 - d01 * d20) / den, 0.0, 1.0)
    over = np.maximum(u + v, 1.0)
    u, v = u / over, v / over
    closest = A + u[:, :, None] * v0 + v[:, :, None] * v1
    dist = np.linalg.norm(p - closest, axis=2)
    pick = np.argmin(dist, axis=1)
    rows = np.arange(len(points))
    sgn = np.einsum("nj,nj->n", points - closest[rows, pick], N[rows, pick])
    return np.sign(sgn) * dist[rows, pick], dist[rows, pick], N[rows, pick]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--solved", required=True)
    ap.add_argument("--animations", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--frames-per-clip", type=int, default=6)
    args = ap.parse_args()

    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "solve_skin", str(Path(__file__).with_name("solve_skin.py")))
    sk = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["solve_skin"]
    try:
        spec.loader.exec_module(sk)
    except SystemExit:
        pass
    finally:
        sys.argv = argv

    dump = np.load(args.dump, allow_pickle=True)
    solved = np.load(args.solved, allow_pickle=True)
    joints, _, _ = sk.build_joint_matrices(dump, Path(args.animations), args.frames_per_clip)

    def mesh_named(token):
        hits = [k.split("::")[1] for k in dump.files
                if k.endswith("::positions") and token in k]
        return hits[0] if hits else None

    body_key = mesh_named("Body")
    garment_keys = [k.split("::")[1] for k in dump.files
                    if k.endswith("::positions") and "Outfit" in k]
    if body_key is None or not garment_keys:
        raise RuntimeError(f"need a Body and at least one Outfit mesh; dump has "
                           f"{sorted({k.split('::')[1] for k in dump.files if '::' in k})}")
    for key in (body_key, *garment_keys):
        for need in ("normals", "triangles"):
            if f"mesh::{key}::{need}" not in dump.files:
                raise RuntimeError(
                    f"{key}: the dump carries no {need} — re-run dump_skin.py (stage 03 tools); "
                    f"the tuck needs the surface, not just a point cloud")

    def pose(key):
        pos = dump[f"mesh::{key}::positions"]
        si = solved[f"mesh::{key}::indices"]
        sw = solved[f"mesh::{key}::weights"]
        homo = np.concatenate([pos, np.ones((len(pos), 1))], 1)
        P = np.zeros((joints.shape[0], len(pos), 3))
        R = np.zeros((joints.shape[0], len(pos), 3, 3))
        for k in range(sw.shape[1]):
            P += sw[:, k][None, :, None] * np.einsum(
                "fnij,nj->fni", joints[:, si[:, k], :, :], homo)[:, :, :3]
            R += sw[:, k][None, :, None, None] * joints[:, si[:, k], :3, :3]
        return pos, P, R

    bpos, BP, _ = pose(body_key)
    bnorm = dump[f"mesh::{body_key}::normals"]
    bnorm = bnorm / np.maximum(np.linalg.norm(bnorm, axis=1, keepdims=True), 1e-9)

    tuck = np.zeros(len(bpos))
    push = np.zeros((len(bpos), 3))
    stats = {}
    for gkey in garment_keys:
        gpos, GP, GR = pose(gkey)
        gtris = dump[f"mesh::{gkey}::triangles"]
        s0, d0, gdir = _tri_signed(bpos, gpos, gtris)
        bidx = _boundary_verts(gtris)
        near_opening = cKDTree(gpos[bidx]).query(bpos)[0] < OPENING_REACH if len(bidx) else             np.zeros(len(bpos), bool)
        covered = (s0 < BIND_INSIDE) & (d0 < BIND_NEAR) & ~near_opening
        _log(f"{gkey}: {len(bidx)} opening verts; {int(near_opening.sum())} body verts sit near "
             f"an opening and are exempt")
        worst = np.zeros(len(bpos))
        for f in range(joints.shape[0]):
            s, _, _ = _tri_signed(BP[f], GP[f], gtris)
            worst = np.maximum(worst, np.where(covered & (s > POKE_EPS), s, 0.0))
        hit = worst > 0
        if hit.any():
            w = np.sort(worst[hit])[::-1] * 1000
            _log(f"{gkey}: penetration mm - max {w[0]:.1f}, p90 {np.percentile(w, 90):.1f}, "
                 f"median {np.median(w):.1f}, over {TUCK_CAP*1000:.0f}mm: {(w > TUCK_CAP*1000).sum()}")
        stats[gkey] = {
            "covered_at_bind": int(covered.sum()),
            "ever_poking": int(hit.sum()),
            "worst_mm": round(float(worst.max()) * 1000, 2),
        }
        _log(f"{gkey}: covered {covered.sum()}, poking {hit.sum()}, "
             f"worst {worst.max() * 1000:.2f} mm")
        want = np.where(hit, np.minimum(worst + TUCK_MARGIN, TUCK_CAP), 0.0)
        take = want > tuck
        tuck = np.where(take, want, tuck)
        push[take] = -gdir[take]          # into the garment, along its own normal

    # Smooth over the k-ring so the per-vertex max does not leave a stippled surface. Neighbours
    # come from proximity rather than topology: the dump has no faces, and at these distances
    # (sub-centimetre) proximity and adjacency agree.
    # Vertices that only inherited a tuck through smoothing have no direction of their own;
    # borrow the nearest pushed neighbour's so the dent stays coherent.
    if tuck.any():
        have = np.linalg.norm(push, axis=1) > 1e-9
        if have.any() and (~have).any():
            _, nn = cKDTree(bpos[have]).query(bpos[~have])
            push[~have] = push[have][nn]
        required = tuck.copy()
        tree = cKDTree(bpos)
        _, ring = tree.query(bpos, k=7)
        for _ in range(SMOOTH_PASSES):
            tuck = tuck[ring].max(1)          # dilate: carry the requirement onto neighbours
        tuck = tuck[ring].mean(1)             # one average to take the stipple off
        # Averaging pulls peaks down, and a peak here is a vertex that was MEASURED punching
        # through. Never finish shallower than the measurement.
        tuck = np.maximum(tuck, required)

    moved = int((tuck > 1e-5).sum())
    _log(f"tucking {moved} verts, max {tuck.max() * 1000:.2f} mm")
    np.savez_compressed(
        args.out,
        mesh=np.array([body_key], dtype=object),
        offsets=(push * tuck[:, None]),
        stats=np.array([json.dumps({
            "animations": str(args.animations),
            "frames_per_clip": int(args.frames_per_clip),
            "verts_moved": moved,
            "max_tuck_mm": round(float(tuck.max()) * 1000, 3),
            "per_garment": stats,
        })], dtype=object),
    )
    _log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
