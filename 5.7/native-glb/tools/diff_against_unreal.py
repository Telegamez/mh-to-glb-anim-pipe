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


def _xf(d):
    return _compose(d["t"], d["q"], d["s"])


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
    ap.add_argument("--truth-json", required=True, help="export_pose_transforms.py output")
    ap.add_argument("--our-anim", required=True, help="the animation library the browser loads")
    ap.add_argument("--clip", required=True)
    ap.add_argument("--mesh-name", default=None)
    args = ap.parse_args()

    truth = json.loads(Path(args.truth_json).read_text(encoding="utf-8"))
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

    e10, e21, e20 = [], [], []
    for fr in truth["frames"]:
        t = float(fr["time"])
        # V0: Unreal's own joint matrices, mapped into glTF space
        S_ue = np.tile(np.eye(4), (len(names), 1, 1))
        for n in shared:
            cs = _xf(fr["bones"][n])
            S_ue[index[n]] = M @ (cs @ np.linalg.inv(ref_ue[n])) @ Minv

        # V1/V2: the browser's joint matrices — clip rotation and scale, BIND translation
        local = local_bind.copy()
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
        world = np.empty_like(local)
        for i in range(len(names)):
            p = parents[i]
            world[i] = local[i] if p < 0 else world[p] @ local[i]
        S_ours = world @ inv_world_bind

        v0 = deform(S_ue, J, W)
        v1 = deform(S_ours, J, W)
        v2 = deform(S_ours, J4, W4)
        e10.append(np.linalg.norm(v1 - v0, axis=1))
        e21.append(np.linalg.norm(v2 - v1, axis=1))
        e20.append(np.linalg.norm(v2 - v0, axis=1))

    a, b, c = (np.mean(x, 0) * 1000 for x in (e10, e21, e20))
    _log("")
    _log(f"deviation from UNREAL, mm, mean over {len(truth['frames'])} frames of {args.clip}")
    _log(f"{'component':<36} {'median':>8} {'p90':>8} {'p99':>8} {'max':>8}")
    for lbl, arr in (("our transform handling |V1-V0|", a),
                     ("4-influence limit      |V2-V1|", b),
                     ("TOTAL vs Unreal        |V2-V0|", c)):
        _log(f"{lbl:<36} {np.median(arr):>8.2f} {np.percentile(arr,90):>8.2f} "
             f"{np.percentile(arr,99):>8.2f} {arr.max():>8.2f}")
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
