"""Ground-truth deformation check — how far is our GLB from what Unreal actually does?

## Why this exists

Every other measurement in this pipeline compares our output against our own assumptions about
what it should be doing. That is marking your own homework, and it is how five separate real
defects got "fixed" without any of them changing what a person sees. This compares against
Unreal.

It also DECOMPOSES the error instead of attributing it, which is the part that matters. There
are exactly two candidate causes for the browser's deformation differing from Unreal's, and they
call for completely different work:

    V0   Unreal's own result       12 influences, full transforms (translation included)
    V1   unlimited-influence web   12 influences, rotation+scale, BIND translations
    V2   what the exporter ships    4 influences, rotation+scale, bind translations

    |V1 - V0|   the cost of dropping non-root translation   (a retarget problem)
    |V2 - V1|   the cost of the 4-influence limit           (a weight-solve problem)
    |V2 - V0|   the total gap

If the first dominates, no amount of weight solving helps. If the second dominates, the solve is
the right investment. Guessing between them has already cost days.

## Ground truth is per-character on purpose

Rig logic is evaluated against a specific body. All six companions share one skeleton
(`metahuman_base_skel`) and one `ABP_Body_PostProcess`, but each has its own mesh with its own
reference pose, and `bake_evaluated_anims.py` picks the first ABP-carrying mesh in the registry
unless told otherwise — which meant every shipped clip was baked against Tele's body. For a
shared animation library that is defensible, because rotations transfer across same-topology
skeletons and translations are dropped anyway. For ground truth it is not: bake with
`--mesh=/Game/MetaHumans/<Char>/Body/SKM_<Char>_BodyMesh` so the correctives are that
character's own.

## What it does not cover

Morph targets and cloth simulation. The body and clothing deform by linear blend skinning over
these joints, which is what this reproduces; the face is driven by ARKit morphs that the browser
already receives directly.
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


def _log(msg: str) -> None:
    print(f"[verify] {msg}", flush=True)


def _load_glb(path: Path):
    data = path.read_bytes()
    offset, gltf, binary = 12, None, b""
    while offset < len(data):
        length, kind = struct.unpack_from("<II", data, offset)
        chunk = data[offset + 8: offset + 8 + length]
        if kind == 0x4E4F534A:
            gltf = json.loads(chunk.decode("utf-8"))
        elif kind == 0x004E4942:
            binary = chunk
        offset += 8 + length + ((4 - length % 4) % 4)
    if gltf is None:
        raise SystemExit(f"no JSON chunk in {path}")
    return gltf, binary


def _accessor(gltf, binary, index) -> np.ndarray:
    acc = gltf["accessors"][index]
    count = _COUNT[acc["type"]]
    dtype = np.dtype("<" + _COMPONENT[acc["componentType"]])
    view = gltf["bufferViews"][acc["bufferView"]]
    base = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or dtype.itemsize * count
    if stride == dtype.itemsize * count:
        out = np.frombuffer(binary, dtype=dtype, count=acc["count"] * count,
                            offset=base).reshape(acc["count"], count)
    else:
        out = np.stack([np.frombuffer(binary, dtype=dtype, count=count, offset=base + i * stride)
                        for i in range(acc["count"])])
    return out.astype(np.float64)


def _compose(t, r, s) -> np.ndarray:
    x, y, z, w = r
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    m = np.eye(4)
    m[0, 0] = 1 - 2 * (y * y + z * z); m[0, 1] = 2 * (x * y - z * w); m[0, 2] = 2 * (x * z + y * w)
    m[1, 0] = 2 * (x * y + z * w); m[1, 1] = 1 - 2 * (x * x + z * z); m[1, 2] = 2 * (y * z - x * w)
    m[2, 0] = 2 * (x * z - y * w); m[2, 1] = 2 * (y * z + x * w); m[2, 2] = 1 - 2 * (x * x + y * y)
    m[:3, 0] *= s[0]; m[:3, 1] *= s[1]; m[:3, 2] *= s[2]
    m[:3, 3] = t
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


ROOT_BONES = {"root", "Root", "pelvis", "Hips"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh-glb", required=True, help="stage 01 GLB: 12 influences, UE bone names")
    ap.add_argument("--truth-anim", required=True, help="evaluated bake against THIS character")
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--mesh-name", default=None)
    args = ap.parse_args()

    mesh_gltf, mesh_bin = _load_glb(Path(args.mesh_glb))
    anim_gltf, anim_bin = _load_glb(Path(args.truth_anim))

    nodes = mesh_gltf["nodes"]
    skin = mesh_gltf["skins"][0]
    joints = skin["joints"]
    names = [nodes[j].get("name") for j in joints]
    index = {n: i for i, n in enumerate(names)}
    ibm = _accessor(mesh_gltf, mesh_bin, skin["inverseBindMatrices"]).reshape(-1, 4, 4)
    ibm = np.transpose(ibm, (0, 2, 1))                     # glTF stores column-major

    parent_of = {}
    for i, node in enumerate(nodes):
        for c in node.get("children", []):
            parent_of[c] = i
    parents = np.array([index.get(nodes[parent_of[j]].get("name"), -1) if j in parent_of else -1
                        for j in joints], dtype=np.int32)
    local_bind = np.stack([
        _compose(nodes[j].get("translation", [0, 0, 0]),
                 nodes[j].get("rotation", [0, 0, 0, 1]),
                 nodes[j].get("scale", [1, 1, 1])) for j in joints])

    mesh = None
    for m in mesh_gltf["meshes"]:
        if args.mesh_name and m["name"] != args.mesh_name:
            continue
        if "WEIGHTS_0" in m["primitives"][0]["attributes"]:
            mesh = m
            break
    if mesh is None:
        raise SystemExit("no skinned mesh found")
    prim = mesh["primitives"][0]
    sets = sorted(k for k in prim["attributes"] if k.startswith("JOINTS_"))
    J = np.concatenate([_accessor(mesh_gltf, mesh_bin, prim["attributes"][s]).astype(int)
                        for s in sets], axis=1)
    W = np.concatenate([
        _accessor(mesh_gltf, mesh_bin, prim["attributes"][s.replace("JOINTS", "WEIGHTS")])
        / _NORM[mesh_gltf["accessors"][prim["attributes"][s.replace("JOINTS", "WEIGHTS")]]["componentType"]]
        for s in sets], axis=1)
    P = _accessor(mesh_gltf, mesh_bin, prim["attributes"]["POSITION"])
    _log(f"{mesh['name']}: {len(P)} verts, {W.shape[1]} influence slots, {len(names)} joints")

    # The animation's rest pose is the shared Skeleton asset's reference pose; the mesh's is
    # that CHARACTER's. MetaHuman overrides the ref pose per mesh to give each body its
    # proportions, so the two disagree by up to 70mm (pelvis 0.9716 vs 0.9014, calf 0.4538 vs
    # 0.4106) on 295 of 342 bones. An animated translation is therefore only meaningful as a
    # DELTA from the animation's own rest — read absolutely it deforms the character into the
    # base skeleton's body, which is what made the first version of this tool report a uniform
    # 70mm "error" that was really a rest-pose mismatch.
    anim_rest = {n.get("name"): np.array(n.get("translation", [0, 0, 0]), dtype=np.float64)
                 for n in anim_gltf["nodes"] if n.get("name")}
    anim_node_name = {i: n.get("name") for i, n in enumerate(anim_gltf["nodes"])}
    anim = anim_gltf["animations"][0]
    channels, duration = {}, 0.0
    for ch in anim["channels"]:
        name = anim_node_name.get(ch["target"]["node"])
        if name not in index:
            continue
        sampler = anim["samplers"][ch["sampler"]]
        times = _accessor(anim_gltf, anim_bin, sampler["input"])[:, 0]
        values = _accessor(anim_gltf, anim_bin, sampler["output"])
        channels.setdefault(name, {})[ch["target"]["path"]] = (times, values)
        duration = max(duration, float(times[-1]))
    _log(f"truth clip '{anim.get('name')}': {len(channels)} driven bones, {duration:.2f}s")

    homo = np.concatenate([P, np.ones((len(P), 1))], axis=1)
    top4 = np.argsort(-W, axis=1)[:, :4]
    rows = np.arange(len(P))[:, None]
    W4 = W[rows, top4]
    W4 = W4 / np.maximum(W4.sum(axis=1, keepdims=True), 1e-9)
    J4 = J[rows, top4]

    tot_drop, tot_trunc, tot_all = [], [], []
    for k in range(args.frames):
        t = duration * (k + 0.5) / args.frames
        local_full = local_bind.copy()
        local_rot = local_bind.copy()
        for name, paths in channels.items():
            i = index[name]
            base = local_bind[i]
            bind_t = base[:3, 3]
            scale = np.linalg.norm(base[:3, :3], axis=0)
            rot = _sample(*paths["rotation"], t) if "rotation" in paths else None
            if rot is None:
                continue
            if "scale" in paths:
                scale = _sample(*paths["scale"], t)
            # V0 = this character's bind PLUS the rig-logic slide, which is the animated
            # translation measured against the animation's own rest. That is what Unreal draws.
            slide = np.zeros(3)
            if "translation" in paths:
                slide = _sample(*paths["translation"], t) - anim_rest.get(name, bind_t)
            local_full[i] = _compose(bind_t + slide, rot, scale)
            # V1 = the browser: root keeps its motion, every other bone sits at bind.
            local_rot[i] = _compose(bind_t + slide if name in ROOT_BONES else bind_t, rot, scale)

        def skin_matrices(local):
            world = np.empty_like(local)
            for i in range(len(names)):
                p = parents[i]
                world[i] = local[i] if p < 0 else world[p] @ local[i]
            return world @ ibm

        Sf, Sr = skin_matrices(local_full), skin_matrices(local_rot)

        def deform(S, joints_idx, weights):
            acc = np.zeros((len(P), 3))
            for c in range(joints_idx.shape[1]):
                w = weights[:, c]
                if not np.any(w):
                    continue
                M = S[joints_idx[:, c]]
                acc += w[:, None] * np.einsum("vij,vj->vi", M, homo)[:, :3]
            return acc

        v0 = deform(Sf, J, W / np.maximum(W.sum(axis=1, keepdims=True), 1e-9))
        v1 = deform(Sr, J, W / np.maximum(W.sum(axis=1, keepdims=True), 1e-9))
        v2 = deform(Sr, J4, W4)
        tot_drop.append(np.linalg.norm(v1 - v0, axis=1))
        tot_trunc.append(np.linalg.norm(v2 - v1, axis=1))
        tot_all.append(np.linalg.norm(v2 - v0, axis=1))

    drop = np.mean(tot_drop, axis=0) * 1000
    trunc = np.mean(tot_trunc, axis=0) * 1000
    total = np.mean(tot_all, axis=0) * 1000
    q = lambda a, p: float(np.percentile(a, p))
    _log("")
    _log(f"deviation from UNREAL, mm, averaged over {args.frames} frames")
    _log(f"{'component':<34} {'median':>8} {'p90':>8} {'p99':>8} {'max':>8}")
    for label, arr in (("dropping translation  |V1-V0|", drop),
                       ("4-influence limit     |V2-V1|", trunc),
                       ("TOTAL                 |V2-V0|", total)):
        _log(f"{label:<34} {np.median(arr):>8.2f} {q(arr,90):>8.2f} {q(arr,99):>8.2f} {arr.max():>8.2f}")
    share = 100 * np.median(drop) / max(np.median(drop) + np.median(trunc), 1e-9)
    _log("")
    _log(f"at the median, dropping translation accounts for {share:.0f}% of the recoverable error, "
         f"the 4-influence limit for {100-share:.0f}%")

    # where the error lives, by height, so the shape of the failure is visible
    _log("")
    _log(f"{'height band':<16} {'verts':>7} {'dropTrans':>10} {'trunc':>8} {'total':>8}")
    ys = P[:, 1]
    for lo in np.arange(math.floor(ys.min() * 10) / 10, ys.max(), 0.1):
        sel = (ys >= lo) & (ys < lo + 0.1)
        if sel.sum() < 50:
            continue
        _log(f"  y {lo:.1f}-{lo+0.1:.1f}      {int(sel.sum()):>7} {np.median(drop[sel]):>10.2f} "
             f"{np.median(trunc[sel]):>8.2f} {np.median(total[sel]):>8.2f}")


if __name__ == "__main__":
    main()
