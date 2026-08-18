"""Skinning decomposition — solve 4-influence weights against the full-influence deformation.

## The problem, stated exactly

glTF and three.js allow four bone influences per vertex. MetaHuman's weight map wants twelve.
The glTF exporter resolves that by keeping the four largest and renormalising, which discards a
median 21.9% of each garment vertex's weight and produces the tearing, the collar gash and the
forearm pinch that four rounds of Blender and runtime heuristics could only partly hide.

Truncation asks "which four weights are biggest". That is the wrong question. The right one is
"which four influences best reproduce the motion", and the two have different answers: a vertex
driven by six helpers that all move together is served perfectly by one representative bone,
while a vertex split between two limbs cannot be served by its four largest at all.

## Ground truth

The twelve-influence deformation AS THE RUNTIME WOULD PRODUCE IT: clip rotations and scale, the
character's own bind translations, all bones. Not "what Unreal renders" — the runtime provably
cannot reach that, because non-root translation carries the bake body's proportions and has to
be dropped (00-START-HERE §6.18). Aiming at an unreachable target would make the solve
unfalsifiable; aiming at this one isolates the truncation loss, which is the whole remaining gap.

    p_truth(t) = sum_j w_j . J_j(t) . v_bind          over all influences
    J_j(t)     = W_j(t) . inverse(bind_j)

## The solve

Per vertex, over F sampled frames, position is LINEAR in the weights, so each vertex is a small
non-negative least-squares problem in its own candidate bones:

    minimise || A w - b ||   subject to w >= 0,  A is (3F x k), b is the truth trajectory

Candidates are the vertex's own influences — the bones that already move it. Selection is greedy
(orthogonal-matching-pursuit style): repeatedly add whichever candidate most reduces the
residual, up to four, then re-solve on the chosen four and renormalise to sum 1. Greedy rather
than exhaustive because C(12,4) is 495 subsets per vertex and there are ~175k vertices across
the roster; greedy is four small solves and lands within a hair of exhaustive on this data.

Renormalising to 1 is not free — it re-introduces a little error the unconstrained solve had
removed — but a weight map that does not sum to 1 makes the mesh shrink under any rotation, and
glTF has no way to say "these weights are deliberate". The measured cost is reported per mesh.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import time
from pathlib import Path

import numpy as np
from scipy.optimize import nnls

MAX_INFLUENCES = 4

# Bones whose translation is root motion, not a corrective. The runtime pins these.
ROOT_BONES = {"Hips", "root", "Root", "Armature"}

_COMPONENT = {5120: "b", 5121: "B", 5122: "h", 5123: "H", 5125: "I", 5126: "f"}
_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _log(msg: str) -> None:
    print(f"[solve_skin] {msg}", flush=True)


# ---------------------------------------------------------------- glTF reading


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
    dtype = np.dtype("<" + {"b": "i1", "B": "u1", "h": "i2", "H": "u2",
                            "I": "u4", "f": "f4"}[_COMPONENT[acc["componentType"]]])
    view = gltf["bufferViews"][acc["bufferView"]]
    base = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or dtype.itemsize * count
    if stride == dtype.itemsize * count:
        return np.frombuffer(binary, dtype=dtype, count=acc["count"] * count,
                             offset=base).reshape(acc["count"], count).astype(np.float64)
    rows = [np.frombuffer(binary, dtype=dtype, count=count, offset=base + i * stride)
            for i in range(acc["count"])]
    return np.stack(rows).astype(np.float64)


def _sample(times: np.ndarray, values: np.ndarray, t: float) -> np.ndarray:
    """Linear sample; quaternions are slerped by normalising the lerp, which is close enough at
    30fps and avoids a special case for the sign flip."""
    if len(times) == 1:
        return values[0]
    i = int(np.searchsorted(times, t, side="right")) - 1
    i = max(0, min(len(times) - 2, i))
    span = times[i + 1] - times[i]
    u = 0.0 if span <= 0 else float((t - times[i]) / span)
    a, b = values[i], values[i + 1]
    if len(a) == 4 and float(np.dot(a, b)) < 0:
        b = -b
    out = a * (1 - u) + b * u
    return out


# ---------------------------------------------------------------- pose building


def _compose(translation, rotation, scale) -> np.ndarray:
    x, y, z, w = rotation
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    m = np.eye(4)
    m[0, 0] = 1 - 2 * (y * y + z * z); m[0, 1] = 2 * (x * y - z * w); m[0, 2] = 2 * (x * z + y * w)
    m[1, 0] = 2 * (x * y + z * w); m[1, 1] = 1 - 2 * (x * x + z * z); m[1, 2] = 2 * (y * z - x * w)
    m[2, 0] = 2 * (x * z - y * w); m[2, 1] = 2 * (y * z + x * w); m[2, 2] = 1 - 2 * (x * x + y * y)
    m[:3, 0] *= scale[0]; m[:3, 1] *= scale[1]; m[:3, 2] *= scale[2]
    m[:3, 3] = translation
    return m


def build_joint_matrices(dump, anim_path: Path, frames_per_clip: int):
    """Two sets of joint matrices per sampled frame: what we AIM AT, and what we can DRIVE.

    This split is the whole point of the decomposition, and getting it wrong wastes the method.

      truth  full rig logic — the correctives' translation and scale included, retargeted onto
             this character's bind so the bake body's proportions do not come with them.
      basis  what the runtime can actually produce — rotation and scale, bind translation, since
             non-root translation has to be dropped (00-START-HERE §6.18).

    Solving `basis . w ~= truth` lets the weights ABSORB deformation the runtime cannot drive
    directly: a corrective that holds the wrist open by sliding becomes a different distribution
    across the bones that are driven. Making truth and basis identical — the first version of
    this file — only recovers truncation loss and leaves every rig-logic effect on the floor.
    """
    names = [str(n) for n in dump["bone_names"]]
    parents = dump["bone_parents"]
    bind = dump["bone_bind"]                       # (B,4,4) bind in armature space
    index = {n: i for i, n in enumerate(names)}
    inv_bind = np.linalg.inv(bind)

    # local bind TRS, from the bind world matrices
    local_bind = np.empty_like(bind)
    for i in range(len(names)):
        p = parents[i]
        local_bind[i] = bind[i] if p < 0 else np.linalg.inv(bind[p]) @ bind[i]

    gltf, binary = _load_glb(anim_path)
    node_name = {i: n.get("name") for i, n in enumerate(gltf["nodes"])}

    poses = []
    for anim in gltf.get("animations", []):
        channels = {}
        duration = 0.0
        for ch in anim["channels"]:
            name = node_name.get(ch["target"]["node"])
            if name not in index:
                continue
            sampler = anim["samplers"][ch["sampler"]]
            times = _accessor(gltf, binary, sampler["input"])[:, 0]
            values = _accessor(gltf, binary, sampler["output"])
            channels.setdefault(name, {})[ch["target"]["path"]] = (times, values)
            duration = max(duration, float(times[-1]))
        if not channels:
            continue
        # Per-bone reference translation = the MEAN of its own track over the clip.
        #
        # Not the node's declared rest. A corrective's translation track routinely sits at a
        # constant that is NOT its node rest — those constants are the bake body's proportions
        # (§6.18), and measuring slide against the node rest bakes that difference in as a
        # permanent 6-70mm displacement. It showed up as absurd 33-70mm solve deviations, and a
        # head does not move 70mm. Mean-centring keeps the VARIATION, which is the rig logic, and
        # discards the constant, which is the other body.
        anim_rest = {}
        for name, paths in channels.items():
            if "translation" in paths:
                anim_rest[name] = paths["translation"][1].mean(axis=0)
        for k in range(frames_per_clip):
            t = duration * (k + 0.5) / frames_per_clip
            local_basis = local_bind.copy()
            local_truth = local_bind.copy()
            for name, paths in channels.items():
                i = index[name]
                trs = local_bind[i]
                bind_translation = trs[:3, 3]
                rot = None
                scale = np.linalg.norm(trs[:3, :3], axis=0)
                if "rotation" in paths:
                    rot = _sample(*paths["rotation"], t)
                if "scale" in paths:
                    scale = _sample(*paths["scale"], t)
                if rot is None:
                    continue
                local_basis[i] = _compose(bind_translation, rot, scale)
                # truth keeps the slide, rebased onto this character and scaled by the ratio of
                # rest offsets so a longer limb slides proportionally further
                truth_translation = bind_translation
                # Root motion is NOT rig logic. Hips carries ~14cm of travel that
                # `stabilizeRootMotion` pins at runtime; letting it into truth but not basis
                # offsets the entire skeleton and swamps the solve — it showed up as a uniform
                # ~100mm deviation on every mesh.
                if name in ROOT_BONES:
                    pass
                elif "translation" in paths and name in anim_rest:
                    source_rest = anim_rest[name]
                    slide = _sample(*paths["translation"], t) - source_rest
                    src_len = float(np.linalg.norm(source_rest))
                    ratio = (float(np.linalg.norm(bind_translation)) / src_len) if src_len > 1e-5 else 1.0
                    truth_translation = bind_translation + slide * min(ratio, 2.0)
                local_truth[i] = _compose(truth_translation, rot, scale)
            for local, bucket in ((local_basis, 0), (local_truth, 1)):
                world = np.empty_like(local)
                for i in range(len(names)):
                    p = parents[i]
                    world[i] = local[i] if p < 0 else world[p] @ local[i]
                poses.append((bucket, world @ inv_bind))
    basis = np.stack([m for b, m in poses if b == 0])
    truth = np.stack([m for b, m in poses if b == 1])
    return basis, truth, names          # each (F,B,4,4)


# ---------------------------------------------------------------- the solve


def solve_mesh(positions, offsets, indices, weights, joints, joints_truth,
               max_influences=MAX_INFLUENCES):
    n = len(positions)
    frames = joints.shape[0]
    out_idx = np.zeros((n, max_influences), dtype=np.int32)
    out_w = np.zeros((n, max_influences), dtype=np.float64)
    err_trunc = np.zeros(n)
    err_solved = np.zeros(n)

    homo = np.ones(4)
    for v in range(n):
        a, b = offsets[v], offsets[v + 1]
        cand = indices[a:b]
        w0 = weights[a:b]
        if len(cand) == 0:
            continue
        homo[:3] = positions[v]
        # basis[k] is where this vertex lands if driven purely by candidate k, per frame
        basis = np.einsum("fij,j->fi", joints[:, cand, :, :].reshape(frames * len(cand), 4, 4),
                          homo).reshape(frames, len(cand), 4)[:, :, :3]
        truth_basis = np.einsum("fij,j->fi",
                                joints_truth[:, cand, :, :].reshape(frames * len(cand), 4, 4),
                                homo).reshape(frames, len(cand), 4)[:, :, :3]
        total = w0.sum()
        truth = np.einsum("fkj,k->fj", truth_basis, w0 / (total if total > 0 else 1.0))

        if len(cand) <= max_influences:
            chosen = np.arange(len(cand))
        else:
            # greedy: add the candidate that most reduces the residual, up to max_influences
            chosen: list[int] = []
            residual = truth.reshape(-1)
            for _ in range(max_influences):
                best, best_err = -1, None
                for k in range(len(cand)):
                    if k in chosen:
                        continue
                    trial = chosen + [k]
                    A = basis[:, trial, :].reshape(frames * 3, len(trial), order="C") \
                        if False else basis[:, trial, :].transpose(0, 2, 1).reshape(-1, len(trial))
                    sol, err = nnls(A, truth.reshape(-1))
                    if best_err is None or err < best_err:
                        best, best_err = k, err
                if best < 0:
                    break
                chosen.append(best)
            chosen = np.array(chosen, dtype=int)

        A = basis[:, chosen, :].transpose(0, 2, 1).reshape(-1, len(chosen))
        target = truth.reshape(-1)
        sol, _ = nnls(A, target)
        s = sol.sum()
        if s <= 1e-9:
            sol = w0[chosen] / max(w0[chosen].sum(), 1e-9)
        else:
            sol = sol / s                      # glTF has no way to express "these do not sum to 1"
        err_solved[v] = np.linalg.norm(A @ sol - target) / math.sqrt(frames)

        # what plain truncation would have produced, for the report
        top = np.argsort(-w0)[:max_influences]
        At = basis[:, top, :].transpose(0, 2, 1).reshape(-1, len(top))
        wt = w0[top] / max(w0[top].sum(), 1e-9)
        err_trunc[v] = np.linalg.norm(At @ wt - target) / math.sqrt(frames)

        out_idx[v, :len(chosen)] = cand[chosen]
        out_w[v, :len(chosen)] = sol
    return out_idx, out_w, err_trunc, err_solved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", required=True)
    parser.add_argument("--animations", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--frames-per-clip", type=int, default=4)
    parser.add_argument("--meshes", nargs="*", default=None)
    args = parser.parse_args()

    dump = np.load(args.dump, allow_pickle=True)
    joints, joints_truth, names = build_joint_matrices(
        dump, Path(args.animations), args.frames_per_clip)
    _log(f"{joints.shape[0]} sampled frames over {len(names)} bones "
         f"(truth carries rig-logic slide; basis is runtime-drivable only)")

    result = {"bone_names": np.array(names, dtype=object)}
    for key in [str(m) for m in dump["meshes"]]:
        if args.meshes and key not in args.meshes:
            continue
        positions = dump[f"mesh::{key}::positions"]
        offsets = dump[f"mesh::{key}::offsets"]
        indices = dump[f"mesh::{key}::indices"]
        weights = dump[f"mesh::{key}::weights"]
        start = time.time()
        idx, w, e_trunc, e_solved = solve_mesh(
            positions, offsets, indices, weights, joints, joints_truth)
        result[f"mesh::{key}::indices"] = idx
        result[f"mesh::{key}::weights"] = w
        better = float((e_solved < e_trunc - 1e-9).mean() * 100)
        _log(f"{key}: {len(positions)} verts in {time.time()-start:.1f}s   "
             f"mean deviation truncated {e_trunc.mean()*1000:.3f}mm -> solved "
             f"{e_solved.mean()*1000:.3f}mm   worst {e_trunc.max()*1000:.1f} -> "
             f"{e_solved.max()*1000:.1f}mm   improved on {better:.1f}% of verts")
    result["meshes"] = np.array(
        [k.split("::")[1] for k in result if k.startswith("mesh::") and k.endswith("::indices")],
        dtype=object,
    )
    np.savez_compressed(args.out, **result)
    _log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
