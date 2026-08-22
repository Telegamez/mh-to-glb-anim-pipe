"""Diff our GLB's deformation against Unreal's, per vertex, and attribute the error.

## What makes this trustworthy where the earlier attempts were not

Ground truth comes from `export_pose_transforms.py`, which reads the SAME component twice:
the reference pose with the post-process ABP disabled, and each animated frame with it running.
Both are component-space transforms straight out of Unreal, so there is no second file with a
different opinion about the rest pose — which is what defeated every previous version
(mesh-vs-animation rest differs by up to 70mm; a single animation file disagrees with its own
node rest by up to 120mm).

The only remaining assumption is the coordinate mapping between Unreal (centimetres, Z-up) and
glTF (metres, Y-up), and that is SOLVED FROM THE DATA rather than hardcoded: the same 342 bones
are described in both spaces, so a similarity fit (Kabsch with scale) recovers the mapping and
reports its own residual. If that residual is not tiny, the mapping is wrong and every number
below is void — so it is printed first and gated.

## What it attributes

    V0   Unreal              12 influences, Unreal's own joint matrices
    V1   our transforms      12 influences, the browser's joint matrices
    V2   what we ship         4 influences, the browser's joint matrices

    |V1-V0|   error from how we build joint matrices (dropped translation, rest handling)
    |V2-V1|   error from the 4-influence limit
    |V2-V0|   the real total against Unreal
"""
from __future__ import annotations

import argparse
import json
import math
import struct
from pathlib import Path

import numpy as np

_COMPONENT = {5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2", 5125: "u4", 5126: "f4"}
_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_NORM = {5121: 255.0, 5123: 65535.0, 5126: 1.0}
ROOT_BONES = {"root", "Root", "pelvis", "Hips"}
# The shipped library carries platform contract names for 16 bones; the mesh GLB carries UE
# names. Match through the map or those 16 silently fail to bind and the diff flatters us.
CONTRACT = {"pelvis": "Hips", "spine_01": "Spine", "spine_03": "Spine1", "spine_05": "Spine2",
            "neck_01": "Neck", "head": "Head", "clavicle_l": "LeftShoulder",
            "upperarm_l": "LeftArm", "lowerarm_l": "LeftForeArm", "hand_l": "LeftHand",
            "clavicle_r": "RightShoulder", "upperarm_r": "RightArm",
            "lowerarm_r": "RightForeArm", "hand_r": "RightHand",
            "eye_l": "LeftEye", "eye_r": "RightEye"}


def _log(m):
    print(f"[diff] {m}", flush=True)


def _load_glb(path: Path):
    data = path.read_bytes()
    off, gltf, binary = 12, None, b""
    while off < len(data):
        length, kind = struct.unpack_from("<II", data, off)
        chunk = data[off + 8: off + 8 + length]
        if kind == 0x4E4F534A:
            gltf = json.loads(chunk.decode("utf-8"))
        elif kind == 0x004E4942:
            binary = chunk
        off += 8 + length + ((4 - length % 4) % 4)
    return gltf, binary


def _acc(gltf, binary, i):
    a = gltf["accessors"][i]
    n = _COUNT[a["type"]]
    dt = np.dtype("<" + _COMPONENT[a["componentType"]])
    v = gltf["bufferViews"][a["bufferView"]]
    base = v.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = v.get("byteStride") or dt.itemsize * n
    if stride == dt.itemsize * n:
        out = np.frombuffer(binary, dtype=dt, count=a["count"] * n, offset=base).reshape(a["count"], n)
    else:
        out = np.stack([np.frombuffer(binary, dtype=dt, count=n, offset=base + k * stride)
                        for k in range(a["count"])])
    return out.astype(np.float64)


def _compose(t, q, s):
    x, y, z, w = q
    nrm = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / nrm, y / nrm, z / nrm, w / nrm
    m = np.eye(4)
    m[0, 0] = 1 - 2 * (y * y + z * z); m[0, 1] = 2 * (x * y - z * w); m[0, 2] = 2 * (x * z + y * w)
    m[1, 0] = 2 * (x * y + z * w); m[1, 1] = 1 - 2 * (x * x + z * z); m[1, 2] = 2 * (y * z - x * w)
    m[2, 0] = 2 * (x * z - y * w); m[2, 1] = 2 * (y * z + x * w); m[2, 2] = 1 - 2 * (x * x + y * y)
    m[:3, 0] *= s[0]; m[:3, 1] *= s[1]; m[:3, 2] *= s[2]
    m[:3, 3] = t
    return m


def _xf(m):
    """Truth transforms arrive already composed from the npz."""
    return m


def _sample(times, values, t):
    if len(times) == 1:
        return values[0]
    i = max(0, min(len(times) - 2, int(np.searchsorted(times, t, side="right")) - 1))
    span = times[i + 1] - times[i]
    u = 0.0 if span <= 0 else float((t - times[i]) / span)
    a, b = values[i], values[i + 1]
    if len(a) == 4 and float(np.dot(a, b)) < 0:
        b = -b
    return a * (1 - u) + b * u


def similarity_fit(src: np.ndarray, dst: np.ndarray):
    """Similarity map taking src onto dst, allowing REFLECTION, plus its RMS residual.

    Unreal is left-handed and glTF right-handed, so the mapping between them contains a mirror.
    Plain Kabsch forces a proper rotation (det +1) and simply cannot express that — it fitted
    with a 167mm residual, which the caller's gate correctly refused. Both determinants are
    tried and the better one wins.
    """
    sc, dc = src.mean(0), dst.mean(0)
    s0, d0 = src - sc, dst - dc
    covariance = s0.T @ d0 / len(src)
    U, S, Vt = np.linalg.svd(covariance)
    var = (s0 ** 2).sum() / len(src)
    best = None
    for det in (1.0, -1.0):
        D = np.diag([1.0, 1.0, det])
        R = Vt.T @ D @ U.T
        scale = float((S * np.array([1.0, 1.0, det])).sum() / var)
        t = dc - scale * R @ sc
        residual = float(np.sqrt((((src @ (scale * R).T + t) - dst) ** 2).sum(1)).mean())
        if best is None or residual < best[0]:
            M = np.eye(4)
            M[:3, :3] = scale * R
            M[:3, 3] = t
            best = (residual, M, det)
    residual, M, det = best
    return M, residual


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh-glb", required=True, help="stage 01 GLB (12 influences, UE names)")
    ap.add_argument("--truth", required=True, help="export_pose_transforms.py .npz output")
    ap.add_argument("--our-anim", required=True, help="the animation library the browser loads")
    ap.add_argument("--clip", required=True)
    ap.add_argument("--mesh-name", default=None)
    ap.add_argument("--truth-field", default="evaluated", choices=["evaluated", "raw"])
    args = ap.parse_args()

    tz = np.load(args.truth, allow_pickle=True)
    tmeta = json.loads(str(tz["meta"][0]))
    tbones = [str(b) for b in tz["bone_names"]]
    _log(f"truth: {tmeta['mesh']} / {Path(tmeta['anim']).name} — {len(tbones)} bones, "
         f"{tz['evaluated'].shape[0]} frames, ABP contributes up to "
         f"{tmeta['abp_delta_max_cm']:.2f}cm")
    def _t(row):
        return _compose(row[0:3], row[3:7], row[7:10])
    truth = {
        "length": tmeta["length"],
        "ref_pose_component": {n: _t(tz["ref"][i]) for i, n in enumerate(tbones)},
        "frames": [{"time": float(tz["times"][f]),
                    "bones": {n: _t(tz[args.truth_field][f, i]) for i, n in enumerate(tbones)}}
                   for f in range(tz[args.truth_field].shape[0])],
    }
    mesh_gltf, mesh_bin = _load_glb(Path(args.mesh_glb))
    our_gltf, our_bin = _load_glb(Path(args.our_anim))

    nodes = mesh_gltf["nodes"]
    skin = mesh_gltf["skins"][0]
    joints = skin["joints"]
    names = [nodes[j].get("name") for j in joints]
    index = {n: i for i, n in enumerate(names)}
    parent_of = {}
    for i, node in enumerate(nodes):
        for c in node.get("children", []):
            parent_of[c] = i
    parents = np.array([index.get(nodes[parent_of[j]].get("name"), -1) if j in parent_of else -1
                        for j in joints], dtype=np.int32)
    local_bind = np.stack([_compose(nodes[j].get("translation", [0, 0, 0]),
                                    nodes[j].get("rotation", [0, 0, 0, 1]),
                                    nodes[j].get("scale", [1, 1, 1])) for j in joints])
    world_bind = np.empty_like(local_bind)
    for i in range(len(names)):
        p = parents[i]
        world_bind[i] = local_bind[i] if p < 0 else world_bind[p] @ local_bind[i]

    # ---- solve the UE -> glTF mapping from the shared skeleton, and gate on its residual ----
    ref = truth["ref_pose_component"]
    shared = [n for n in names if n in ref]
    ue_pts = np.array([_xf(ref[n])[:3, 3] for n in shared])
    gl_pts = np.array([world_bind[index[n]][:3, 3] for n in shared])
    M, residual = similarity_fit(ue_pts, gl_pts)
    _log(f"UE->glTF similarity fit over {len(shared)} bones: residual {residual*1000:.3f} mm, "
         f"scale {np.linalg.norm(M[:3, 0]):.5f}, "
         f"det {np.linalg.det(M[:3, :3]):+.4f} (negative = handedness flip, expected)")
    if residual > 2e-3:
        raise SystemExit(f"[diff] ABORT: mapping residual {residual*1000:.2f}mm is too large; "
                         f"every number downstream would be meaningless")
    Minv = np.linalg.inv(M)

    # ---- mesh ----
    mesh = None
    for m in mesh_gltf["meshes"]:
        if args.mesh_name and m["name"] != args.mesh_name:
            continue
        if "WEIGHTS_0" in m["primitives"][0]["attributes"]:
            mesh = m
            break
    prim = mesh["primitives"][0]
    sets = sorted(k for k in prim["attributes"] if k.startswith("JOINTS_"))
    J = np.concatenate([_acc(mesh_gltf, mesh_bin, prim["attributes"][s]).astype(int) for s in sets], 1)
    W = np.concatenate([
        _acc(mesh_gltf, mesh_bin, prim["attributes"][s.replace("JOINTS", "WEIGHTS")])
        / _NORM[mesh_gltf["accessors"][prim["attributes"][s.replace("JOINTS", "WEIGHTS")]]["componentType"]]
        for s in sets], 1)
    P = _acc(mesh_gltf, mesh_bin, prim["attributes"]["POSITION"])
    W = W / np.maximum(W.sum(1, keepdims=True), 1e-9)
    homo = np.concatenate([P, np.ones((len(P), 1))], 1)
    _log(f"{mesh['name']}: {len(P)} verts, {W.shape[1]} influence slots")

    top4 = np.argsort(-W, axis=1)[:, :4]
    rows = np.arange(len(P))[:, None]
    W4 = W[rows, top4]
    W4 = W4 / np.maximum(W4.sum(1, keepdims=True), 1e-9)
    J4 = J[rows, top4]

    # ---- our animation ----
    our_node = {i: n.get("name") for i, n in enumerate(our_gltf["nodes"])}
    clip = next((a for a in our_gltf["animations"] if a.get("name") == args.clip), None)
    if clip is None:
        raise SystemExit(f"clip {args.clip} not in {args.our_anim}")
    alias = {v: k for k, v in CONTRACT.items()}
    chan, dur = {}, 0.0
    for ch in clip["channels"]:
        nm = our_node.get(ch["target"]["node"])
        nm = alias.get(nm, nm)                 # contract name -> UE name
        if nm not in index:
            continue
        smp = clip["samplers"][ch["sampler"]]
        times = _acc(our_gltf, our_bin, smp["input"])[:, 0]
        vals = _acc(our_gltf, our_bin, smp["output"])
        chan.setdefault(nm, {})[ch["target"]["path"]] = (times, vals)
        dur = max(dur, float(times[-1]))

    inv_world_bind = np.linalg.inv(world_bind)
    ref_ue = {n: _xf(ref[n]) for n in shared}

    def deform(S, ji, w):
        acc = np.zeros((len(P), 3))
        for c in range(ji.shape[1]):
            wc = w[:, c]
            if not np.any(wc):
                continue
            acc += wc[:, None] * np.einsum("vij,vj->vi", S[ji[:, c]], homo)[:, :3]
        return acc

    e10, e21, e20, e1b, e1a, e2a = [], [], [], [], [], []
    seam_v0, seam_v2 = [], []
    for fr in truth["frames"]:
        t = float(fr["time"])
        # V0: Unreal's own joint matrices, mapped into glTF space
        S_ue = np.tile(np.eye(4), (len(names), 1, 1))
        for n in shared:
            cs = _xf(fr["bones"][n])
            S_ue[index[n]] = M @ (cs @ np.linalg.inv(ref_ue[n])) @ Minv

        # V1/V2: the browser's joint matrices — clip rotation and scale, BIND translation
        local = local_bind.copy()
        local_b = local_bind.copy()
        tt = dur * (t / max(truth["length"], 1e-9))
        for n, paths in chan.items():
            i = index[n]
            base = local_bind[i]
            scale = np.linalg.norm(base[:3, :3], axis=0)
            rot = _sample(*paths["rotation"], tt) if "rotation" in paths else None
            if rot is None:
                continue
            if "scale" in paths:
                scale = _sample(*paths["scale"], tt)
            trans = base[:3, 3]
            if n in ROOT_BONES and "translation" in paths:
                trans = _sample(*paths["translation"], tt)
            local[i] = _compose(trans, rot, scale)
            local_b[i] = local[i]
            if n in ROOT_BONES and "translation" in paths:
                # VARIANT: rebase the root onto THIS CHARACTER's bind rather than onto the clip's
                # own first frame. The clips were baked against another body, so their pelvis
                # height is that body's, and importing it displaces everything above the feet.
                first = _sample(*paths["translation"], 0.0)
                local_b[i] = _compose(base[:3, 3] + (trans - first), rot, scale)
        world = np.empty_like(local)
        wb = np.empty_like(local_b)
        for i in range(len(names)):
            p = parents[i]
            world[i] = local[i] if p < 0 else world[p] @ local[i]
            wb[i] = local_b[i] if p < 0 else wb[p] @ local_b[i]
        S_ours = world @ inv_world_bind
        S_bind = wb @ inv_world_bind

        v0 = deform(S_ue, J, W)
        v1 = deform(S_ours, J, W)
        v2 = deform(S_ours, J4, W4)
        seam_v0.append(v0)
        seam_v2.append(v2)
        # Placement and deformation are different faults and want different fixes. A constant
        # offset means the character stands somewhere else; it cannot crease a shirt. Aligning
        # each frame by its mean bone offset removes the placement term so what remains is the
        # deformation difference alone.
        off = np.array([(world[index[n]][:3, 3] - S_ue[index[n]] @ np.append(world_bind[index[n]][:3, 3], 1.0))[:3]
                        for n in shared]).mean(0) if False else (v1 - v0).mean(0)
        e1a.append(np.linalg.norm((v1 - off) - v0, axis=1))
        e2a.append(np.linalg.norm((v2 - off) - v0, axis=1))
        e10.append(np.linalg.norm(v1 - v0, axis=1))
        e21.append(np.linalg.norm(v2 - v1, axis=1))
        e20.append(np.linalg.norm(v2 - v0, axis=1))
        e1b.append(np.linalg.norm(deform(S_bind, J, W) - v0, axis=1))

    # SEAM GAPS. Tearing is not mean error - it is two coincident vertices (the same point on
    # the garment, duplicated in the data) receiving different deformations and separating. If
    # V0 opens the seams, the tear exists in Unreal's own terms and no runtime change closes it;
    # if only V2 opens them, we introduced it.
    from scipy.spatial import cKDTree
    _pairs = np.array(sorted(cKDTree(P).query_pairs(5e-7)))
    if len(_pairs):
        for lbl, frames_v in (("UNREAL (V0)", seam_v0), ("ours as shipped (V2)", seam_v2)):
            g = np.max([np.linalg.norm(v[_pairs[:, 0]] - v[_pairs[:, 1]], axis=1)
                        for v in frames_v], 0) * 1000
            _log(f"SEAM GAP {lbl}: {len(_pairs)} coincident pairs   "
                 f"med {np.median(g):.2f}  p99 {np.percentile(g, 99):.2f}  max {g.max():.1f} mm"
                 f"   open>2mm: {(g > 2).sum()}")
    a, b, c, d1, aa, cc = (np.mean(x, 0) * 1000 for x in (e10, e21, e20, e1b, e1a, e2a))
    _log("")
    _log(f"deviation from UNREAL, mm, mean over {len(truth['frames'])} frames of {args.clip}")
    _log(f"{'component':<36} {'median':>8} {'p90':>8} {'p99':>8} {'max':>8}")
    for lbl, arr in (("our transform handling |V1-V0|", a),
                     ("4-influence limit      |V2-V1|", b),
                     ("TOTAL vs Unreal        |V2-V0|", c),
                     ("VARIANT root on own bind      ", d1),
                     ("--- placement removed ---------", None),
                     ("DEFORMATION only, 12 infl     ", aa),
                     ("DEFORMATION only, as shipped  ", cc)):
        if arr is None:
            _log(f"{lbl}")
            continue
        _log(f"{lbl:<36} {np.median(arr):>8.2f} {np.percentile(arr,90):>8.2f} "
             f"{np.percentile(arr,99):>8.2f} {arr.max():>8.2f}")
    # WHERE the transform error lives, per bone. A vertex diff says how bad; this says which
    # joint is responsible, which is the difference between a number and a lead.
    fr = truth["frames"][len(truth["frames"]) // 2]
    tt = dur * (float(fr["time"]) / max(truth["length"], 1e-9))
    local = local_bind.copy()
    for n, paths in chan.items():
        i = index[n]
        base = local_bind[i]
        scale = np.linalg.norm(base[:3, :3], axis=0)
        rot = _sample(*paths["rotation"], tt) if "rotation" in paths else None
        if rot is None:
            continue
        if "scale" in paths:
            scale = _sample(*paths["scale"], tt)
        trans = base[:3, 3]
        if n in ROOT_BONES and "translation" in paths:
            trans = _sample(*paths["translation"], tt)
        local[i] = _compose(trans, rot, scale)
    world = np.empty_like(local)
    for i in range(len(names)):
        p = parents[i]
        world[i] = local[i] if p < 0 else world[p] @ local[i]
    rows = []
    for n in shared:
        ours = world[index[n]][:3, 3]
        theirs = (M @ _xf(fr["bones"][n]))[:3, 3]
        rows.append((float(np.linalg.norm(ours - theirs)) * 1000, n))
    rows.sort(reverse=True)
    _log("")
    _log(f"per-BONE world position error at t={fr['time']:.2f}s (mm), worst 12:")
    for v, n in rows[:12]:
        _log(f"   {n:<28} {v:8.1f}")
    small = [r for r in rows if r[0] < 5]
    _log(f"   bones under 5mm: {len(small)} of {len(rows)}")
    # Constant offset, or real deformation? A near-constant vector means the whole skeleton is
    # displaced and nothing is deforming differently at all.
    vecs = np.array([(world[index[n]][:3, 3] - (M @ _xf(fr["bones"][n]))[:3, 3])
                     for n in shared]) * 1000
    mean = vecs.mean(0)
    resid = np.linalg.norm(vecs - mean, axis=1)
    _log(f"   offset vector: mean [{mean[0]:.1f}, {mean[1]:.1f}, {mean[2]:.1f}] mm")
    _log(f"   deviation from that constant: med {np.median(resid):.2f}mm "
         f"p90 {np.percentile(resid, 90):.2f} max {resid.max():.2f}")
    if np.median(resid) < 5:
        _log("   -> a CONSTANT OFFSET. The skeleton is displaced, not deformed differently.")

    # Which bones ROTATE differently. Position error accumulates down the chain, so a parent's
    # mistake shows up on every descendant; local rotation isolates the joint actually at fault.
    def _rot_of(m):
        r = m[:3, :3].copy()
        sc = np.linalg.norm(r, axis=0)
        sc[sc < 1e-12] = 1.0
        return r / sc
    rot_rows = []
    for n in shared:
        i = index[n]
        p = parents[i]
        ours = _rot_of(local[i])
        tw = M @ _xf(fr["bones"][n]) @ Minv
        tp = M @ _xf(fr["bones"][names[p]]) @ Minv if p >= 0 and names[p] in fr["bones"] else np.eye(4)
        theirs_local = np.linalg.inv(tp) @ tw
        theirs = _rot_of(theirs_local)
        rel = ours.T @ theirs
        ang = math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(rel) - 1) / 2))))
        rot_rows.append((ang, n))
    rot_rows.sort(reverse=True)
    angs = np.array([r[0] for r in rot_rows])
    _log("")
    _log(f"local ROTATION error vs Unreal (deg): med {np.median(angs):.2f} "
         f"p90 {np.percentile(angs, 90):.2f} max {angs.max():.2f}   "
         f"over 1deg: {(angs > 1).sum()}  over 10deg: {(angs > 10).sum()}")
    for v, n in rot_rows[:10]:
        _log(f"   {n:<28} {v:7.2f} deg")

    # Local TRANSLATION and SCALE. We drop non-root translation and keep clip scale; if the
    # rotations match Unreal and truncation is nil, whatever remains has to be here.
    tr_rows, sc_rows = [], []
    for n in shared:
        i = index[n]
        p = parents[i]
        tw = M @ _xf(fr["bones"][n]) @ Minv
        tp = M @ _xf(fr["bones"][names[p]]) @ Minv if p >= 0 and names[p] in fr["bones"] else np.eye(4)
        theirs_local = np.linalg.inv(tp) @ tw
        tr_rows.append((float(np.linalg.norm(local[i][:3, 3] - theirs_local[:3, 3])) * 1000, n))
        so = np.linalg.norm(local[i][:3, :3], axis=0)
        st = np.linalg.norm(theirs_local[:3, :3], axis=0)
        sc_rows.append((float(np.abs(so - st).max()), n))
    tr_rows.sort(reverse=True); sc_rows.sort(reverse=True)
    tr = np.array([r[0] for r in tr_rows]); sc = np.array([r[0] for r in sc_rows])
    _log("")
    _log(f"local TRANSLATION error vs Unreal (mm): med {np.median(tr):.3f} "
         f"p90 {np.percentile(tr, 90):.3f} max {tr.max():.3f}   over 1mm: {(tr > 1).sum()}")
    for v, n in tr_rows[:8]:
        _log(f"   {n:<28} {v:8.2f} mm")
    _log(f"local SCALE error vs Unreal: med {np.median(sc):.4f} "
         f"p90 {np.percentile(sc, 90):.4f} max {sc.max():.4f}   over 0.01: {(sc > 0.01).sum()}")
    for v, n in sc_rows[:5]:
        _log(f"   {n:<28} {v:8.4f}")

    _log("")
    _log(f"{'height band':<18} {'verts':>7} {'transforms':>11} {'4-infl':>8} {'total':>8}")
    ys = P[:, 1]
    lo = math.floor(ys.min() * 10) / 10
    while lo < ys.max():
        sel = (ys >= lo) & (ys < lo + 0.1)
        if sel.sum() >= 50:
            _log(f"  y {lo:.1f}-{lo+0.1:.1f}   {int(sel.sum()):>10} {np.median(a[sel]):>11.2f} "
                 f"{np.median(b[sel]):>8.2f} {np.median(c[sel]):>8.2f}")
        lo += 0.1


if __name__ == "__main__":
    main()
