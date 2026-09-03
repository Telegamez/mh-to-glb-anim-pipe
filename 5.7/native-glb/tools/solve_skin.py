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
import re
import math
import struct
import time
from pathlib import Path

import numpy as np
from scipy.optimize import nnls

MAX_INFLUENCES = 4
# TELEGAMEZ (telegamez#3725): a garment vertex carrying at least this share of its weight on
# `Head` is part of a rigid head piece (a helmet), not a collar. The Head->Neck garment remap
# exists so collars do not ride the runtime's procedural nods; a helmet MUST ride them, exactly
# as the face and a head-bound accessory do. Tommy's helmet came out 79% Neck without this.
RIGID_HEAD_SHARE = 0.75
# ...and only on a mesh that actually carries a rigid head piece: at least this fraction of its
# vertices must be Head-dominated. A polo collar has a handful of such vertices (8 of 14,110 on
# tg-tom); a helmet has thousands (3,932 of 23,573 on Tommy). Below the fraction the collar rule
# applies to every vertex, so roster outfits solve exactly as before.
RIGID_HEAD_MESH_FRACTION = 0.05

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


def solve_mesh(positions, offsets, indices, weights, joints, joints_truth, live_map,
               max_influences=MAX_INFLUENCES, cand_remap=None, bone_seg=None,
               implausible_ratio=None):
    """One recipe per POSITION, not per vertex.

    The garment duplicates boundary vertices (2489 coincident pairs on Telly's outfit), and the
    source asset gives the twins DIFFERENT weights - measured against Unreal's own evaluated
    transforms with all 12 influences, Unreal itself opens those seams up to 58.7mm during
    Shrug. That is the tearing. Solving per vertex keeps the disagreement; solving per
    coincident CLUSTER (union of candidates, mean of truth targets) makes a seam tear
    impossible by construction, for every animation, on every character.
    """
    """`live_map[b]` is b's nearest LIVE ancestor - the constraint that makes the solve shippable.

    The basis is mathematically right about frozen bones: one that never moves is an exact alias
    of its live ancestor, so selecting it reproduces the motion perfectly. The EXPORTER is not,
    because a bone outside the shipped skeleton is not an alias there, it is absent, and its
    weight is simply dropped. The first solved build stranded 8.5% of the outfit's weight and
    71.6% of the head's on bones the runtime has never heard of, and the collar came apart.

    So candidates are mapped to live bones before selection. Truth is NOT mapped - it keeps the
    full rig logic, including motion from bones we cannot drive, which is what the weights are
    being asked to absorb.
    """
    n = len(positions)
    frames = joints.shape[0]
    out_idx = np.zeros((n, max_influences), dtype=np.int32)
    out_w = np.zeros((n, max_influences), dtype=np.float64)
    rigid_head_kept = [0]
    rigid_head_mesh = False
    if cand_remap is not None:
        moved_src = np.where(cand_remap != np.arange(len(cand_remap)))[0]
        if len(moved_src):
            dominated = 0
            for vv in range(n):
                tot = 0.0; head = 0.0
                for i in range(offsets[vv], offsets[vv + 1]):
                    ww = float(weights[i]); tot += ww
                    if live_map[int(indices[i])] in moved_src or int(indices[i]) in moved_src:
                        head += ww
                if tot > 0 and head / tot >= RIGID_HEAD_SHARE:
                    dominated += 1
            rigid_head_mesh = dominated / max(n, 1) >= RIGID_HEAD_MESH_FRACTION
            if dominated:
                _log(f"  head-dominated vertices: {dominated}/{n} "
                     f"({100 * dominated / max(n, 1):.1f}%) -> rigid head piece: {rigid_head_mesh}")
    err_trunc = np.zeros(n)
    err_solved = np.zeros(n)

    # Coincident vertices collapse to one representative; results are copied to every member.
    # Union-find over actual coincident PAIRS, not a rounding grid - a grid splits twins that
    # straddle a cell boundary, and 197 of Telly's 2489 seam pairs did exactly that.
    from scipy.spatial import cKDTree
    root = np.arange(n)
    def _find(x):
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x
    for a, b in cKDTree(positions).query_pairs(5e-7):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            root[max(ra, rb)] = min(ra, rb)
    cluster_of = np.array([_find(i) for i in range(n)])
    cluster_rep = np.unique(cluster_of)
    order = np.argsort(cluster_of, kind="stable")
    bounds = np.searchsorted(cluster_of[order], cluster_rep)
    bounds = np.append(bounds, n)

    homo = np.ones(4)
    for c, v in enumerate(cluster_rep):
        members = order[bounds[c]:bounds[c + 1]]
        # union of the members' influences, weights averaged over members
        merged = {}
        for m in members:
            for i in range(offsets[m], offsets[m + 1]):
                merged[int(indices[i])] = merged.get(int(indices[i]), 0.0) + float(weights[i])
        cand = np.array(list(merged.keys()), dtype=np.int64)
        w0 = np.array(list(merged.values())) / max(len(members), 1)
        if len(cand) == 0:
            continue
        homo[:3] = positions[v]

        # Implausible influences are dropped from the RECIPE, truth target included. The source
        # binds collar vertices up to 0.82 to `upperarm_out_r` - the crumpled-collar defect of
        # 00-START-HERE 6.16, in the asset itself. Faithfully reproducing that reproduces the
        # crumple, so the cleaning the fold used to do lands here, before the fit: any influence
        # whose bone sits more than `implausible_ratio` times as far from the vertex as the
        # nearest influence's bone is discarded and the recipe renormalised.
        if implausible_ratio is not None and len(cand) > 1:
            heads, tails = bone_seg
            hk = heads[live_map[cand]]
            tk = tails[live_map[cand]]
            seg = tk - hk
            tt = np.clip(np.einsum("kj,kj->k", positions[v] - hk, seg)
                         / np.maximum(np.einsum("kj,kj->k", seg, seg), 1e-12), 0.0, 1.0)
            dist = np.linalg.norm(positions[v] - (hk + tt[:, None] * seg), axis=1)
            keep = dist <= implausible_ratio * max(dist.min(), 1e-4)
            if keep.sum() >= 1 and not keep.all():
                cand, w0 = cand[keep], w0[keep]

        # Candidates live in the SHIPPED skeleton. Duplicates collapse: several frozen helpers
        # under one live parent are one candidate, which also frees slots for distinct bones.
        if cand_remap is None:
            mapped = live_map[cand]
        else:
            lm = live_map[cand]
            moved = cand_remap[lm] != lm                       # what the garment rule would remap (Head)
            share = float(w0[moved].sum() / max(w0.sum(), 1e-9)) if moved.any() else 0.0
            if rigid_head_mesh and share >= RIGID_HEAD_SHARE:
                mapped = lm                                     # rigid head piece: keep Head
                rigid_head_kept[0] += 1
            else:
                mapped = cand_remap[lm]
        cand_live, inverse = np.unique(mapped, return_inverse=True)
        w_live = np.bincount(inverse, weights=w0, minlength=len(cand_live))

        # basis[k] is where this vertex lands if driven purely by candidate k, per frame
        basis = np.einsum("fij,j->fi",
                          joints[:, cand_live, :, :].reshape(frames * len(cand_live), 4, 4),
                          homo).reshape(frames, len(cand_live), 4)[:, :, :3]
        truth_basis = np.einsum("fij,j->fi",
                                joints_truth[:, cand, :, :].reshape(frames * len(cand), 4, 4),
                                homo).reshape(frames, len(cand), 4)[:, :, :3]
        total = w0.sum()
        truth = np.einsum("fkj,k->fj", truth_basis, w0 / (total if total > 0 else 1.0))

        if len(cand_live) <= max_influences:
            chosen = np.arange(len(cand_live))
        else:
            # greedy: add the candidate that most reduces the residual, up to max_influences
            chosen: list[int] = []
            residual = truth.reshape(-1)
            for _ in range(max_influences):
                best, best_err = -1, None
                for k in range(len(cand_live)):
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
            sol = w_live[chosen] / max(w_live[chosen].sum(), 1e-9)
        else:
            sol = sol / s                      # glTF has no way to express "these do not sum to 1"
        err_solved[members] = np.linalg.norm(A @ sol - target) / math.sqrt(frames)

        # Baseline = what we SHIPPED before: fold frozen bones into live ancestors, then let the
        # exporter keep the four largest. Comparing against raw top-4 instead would flatter the
        # solve by crediting it with the fold's gains as well.
        top = np.argsort(-w_live)[:max_influences]
        At = basis[:, top, :].transpose(0, 2, 1).reshape(-1, len(top))
        wt = w_live[top] / max(w_live[top].sum(), 1e-9)
        err_trunc[members] = np.linalg.norm(At @ wt - target) / math.sqrt(frames)

        out_idx[members, :len(chosen)] = cand_live[chosen]
        out_w[members, :len(chosen)] = sol
    if rigid_head_kept[0]:
        _log(f"  rigid head piece: {rigid_head_kept[0]} vertex/vertices kept on Head "
             f"(>= {RIGID_HEAD_SHARE:.0%} Head-weighted; the garment Head->Neck rule is for collars)")
    return out_idx, out_w, err_trunc, err_solved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", required=True)
    parser.add_argument("--animations", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--frames-per-clip", type=int, default=4)
    parser.add_argument("--meshes", nargs="*", default=None)
    parser.add_argument("--implausible-ratio", type=float, default=None,
                        help="drop a garment influence whose bone is this many times farther "
                             "from the vertex than the nearest one. Omit to disable.")
    parser.add_argument("--live-bones", required=True,
                        help="_config/live_bones.json - the bones the shipped skeleton can drive")
    args = parser.parse_args()

    dump = np.load(args.dump, allow_pickle=True)
    joints, joints_truth, names = build_joint_matrices(
        dump, Path(args.animations), args.frames_per_clip)
    _log(f"{joints.shape[0]} sampled frames over {len(names)} bones "
         f"(truth carries rig-logic slide; basis is runtime-drivable only)")

    # Nearest live ancestor per bone. Walking up terminates at the root, which is always live;
    # a bone with no live ancestor at all would be a broken skeleton and is reported, not hidden.
    # LIVE = bones the shipped animation library actually moves. Everything else in the armature
    # is exported too (the GLB carries all ~1197 nodes, FACIAL_* included), so a frozen bone is
    # PRESENT and acts as an exact alias of its live ancestor rather than being dropped. Folding
    # to live ancestors is therefore not a correctness fix - it is a slot fix: several frozen
    # helpers under one live parent collapse into a single candidate, so the four slots hold four
    # DISTINCT drivable bones instead of aliases of the same one (00-START-HERE 6.19).
    live_names = set(json.loads(Path(args.live_bones).read_text(encoding="utf-8"))["live_bones"])
    _anim_gltf, _ = _load_glb(Path(args.animations))
    present = {n.get("name") for n in _anim_gltf["nodes"] if n.get("name")}
    _log(f"skeleton nodes present {len(present)}; of those {len(live_names & present)} move")
    parents = dump["bone_parents"]
    index = {n: i for i, n in enumerate(names)}
    live_map = np.arange(len(names), dtype=np.int64)
    orphans = []
    for i, n in enumerate(names):
        j = i
        while j >= 0 and names[j] not in live_names:
            j = int(parents[j])
        if j < 0:
            # No live ancestor. Keeping the bone is correct as long as it ships.
            if n not in present:
                orphans.append(n)
            j = i
        live_map[i] = j
    folded = int((live_map != np.arange(len(names))).sum())
    _log(f"live bones {len(live_names)}/{len(names)}; {folded} folded to a live ancestor"
         + (f"; {len(orphans)} ORPHANS with no live ancestor: {orphans[:5]}" if orphans else ""))

    # Stage 03's fold remapped garment weights off `Head` (GARMENT_BONE_REMAP) because the
    # runtime rotates the head procedurally - gaze, nods - far outside anything in the clip
    # library. A collar vertex the solve hangs on Head fits the sampled frames perfectly and
    # then rides a 23-degree procedural nod. Same rule here, applied BEFORE the fit so the
    # solve redistributes that motion instead of having it edited out afterwards.
    garment_re = re.compile(r"Outfit|Garment|Cloth", re.IGNORECASE)
    IMPLAUSIBLE_RATIO = args.implausible_ratio

    # bone segments for the implausibility test: head from bind, tail = mean of child heads,
    # a point for leaves. Coarse, but the test is a 2.5x ratio - it does not need precision.
    bind = dump["bone_bind"]
    heads = bind[:, :3, 3].copy()
    tails = heads.copy()
    child_sum = np.zeros_like(heads); child_n = np.zeros(len(names))
    for i, par in enumerate(dump["bone_parents"]):
        if par >= 0:
            child_sum[par] += heads[i]; child_n[par] += 1
    has = child_n > 0
    tails[has] = child_sum[has] / child_n[has][:, None]
    garment_remap = np.arange(len(names), dtype=np.int64)
    for src, dst in (("Head", "Neck"),):
        if src in index and dst in index:
            garment_remap[index[src]] = index[dst]

    result = {"bone_names": np.array(names, dtype=object)}
    stats = {}
    for key in [str(m) for m in dump["meshes"]]:
        if args.meshes and key not in args.meshes:
            continue
        positions = dump[f"mesh::{key}::positions"]
        offsets = dump[f"mesh::{key}::offsets"]
        indices = dump[f"mesh::{key}::indices"]
        weights = dump[f"mesh::{key}::weights"]
        start = time.time()
        idx, w, e_trunc, e_solved = solve_mesh(
            positions, offsets, indices, weights, joints, joints_truth, live_map,
            cand_remap=garment_remap if garment_re.search(key) else None,
            bone_seg=(heads, tails),
            implausible_ratio=IMPLAUSIBLE_RATIO if garment_re.search(key) else None)
        result[f"mesh::{key}::indices"] = idx
        result[f"mesh::{key}::weights"] = w
        better = float((e_solved < e_trunc - 1e-9).mean() * 100)
        stats[key] = {
            "verts": int(len(positions)),
            "mean_truncated_mm": round(float(e_trunc.mean()) * 1000, 4),
            "mean_solved_mm": round(float(e_solved.mean()) * 1000, 4),
            "worst_truncated_mm": round(float(e_trunc.max()) * 1000, 3),
            "worst_solved_mm": round(float(e_solved.max()) * 1000, 3),
            "improved_pct": round(better, 1),
        }
        _log(f"{key}: {len(positions)} verts in {time.time()-start:.1f}s   "
             f"mean deviation truncated {e_trunc.mean()*1000:.3f}mm -> solved "
             f"{e_solved.mean()*1000:.3f}mm   worst {e_trunc.max()*1000:.1f} -> "
             f"{e_solved.max()*1000:.1f}mm   improved on {better:.1f}% of verts")
    # Report weight that lands on bones the animation library never moves. This is NOT an error -
    # such a bone still ships and still aliases its parent - but a mesh whose weight sits mostly
    # on unanimated bones is inert, and that is worth seeing rather than discovering on screen.
    live_idx = {i for i, n in enumerate(names) if n in live_names}
    for key in [k.split("::")[1] for k in result
                if k.startswith("mesh::") and k.endswith("::indices")]:
        idx = result[f"mesh::{key}::indices"]
        w = result[f"mesh::{key}::weights"]
        mass = float(w.sum())
        inert = float(w[~np.isin(idx, list(live_idx))].sum())
        if mass > 0 and inert / mass > 0.01:
            _log(f"note: {key} carries {inert / mass * 100:.1f}% of its weight on bones the "
                 f"animation library does not move (present, frozen, aliasing their parent)")

    # Provenance travels WITH the weights. A solved npz that cannot say what it was fitted
    # against, or how much it actually bought, is indistinguishable from the two failed solves
    # sitting beside it named .v2-broken and .v3-worse.
    result["stats"] = np.array([json.dumps({
        "animations": str(args.animations),
        "frames_per_clip": int(args.frames_per_clip),
        "max_influences": MAX_INFLUENCES,
        "per_mesh": stats,
    })], dtype=object)
    result["meshes"] = np.array(
        [k.split("::")[1] for k in result if k.startswith("mesh::") and k.endswith("::indices")],
        dtype=object,
    )
    np.savez_compressed(args.out, **result)
    _log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
