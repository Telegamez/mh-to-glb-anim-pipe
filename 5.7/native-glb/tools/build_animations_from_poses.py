"""Author the animation library from Unreal's EVALUATED transforms, not from AnimSequences.

## Why the previous two attempts were wrong

v2 exported each `AnimSequence` to glTF. An AnimSequence is the INPUT to MetaHuman's deformation
machine, so the post-process ABPs never ran and all ~1130 corrective joints shipped frozen.

v3 baked the evaluated pose through `SequencerTools.export_anim_sequence`, which moved 309 bones
instead of 53 and looked like the fix. Measured against Unreal's own component-space transforms
it is still wrong, and wrong precisely where it matters — local rotation delta from rest, ours vs
Unreal, on one Shrug frame:

    lowerarm_correctiveRoot_l   95.6 deg      wrist_inner_l          31.6 deg
    lowerarm_correctiveRoot_r   71.2 deg      upperarm_twist_01_r    47.7 deg
    upperarm_correctiveRoot_r   60.0 deg      37 bones over 10 deg

Main bones are fine (median 0.005 deg). Those corrective bones are the wrist indent, the shoulder
and the collar. A round trip through AnimSequence + Sequencer + Blender does not preserve them.

## What this does instead

Reads `export_pose_transforms.py` output — Unreal's component-space transform per bone per frame,
plus the reference pose read from the same component with the ABP disabled — and converts it to
local TRS against that reference. Three facts were measured before building this, all on Telly
and Telie, and each one removes a decision:

  * recomposing local -> world reproduces Unreal's CS exactly: **0.000000 mm**
  * local ROTATIONS are identical between characters: **0.000 deg**, so ONE library serves the
    roster and no per-character bake is needed
  * local TRANSLATIONS never move from the reference pose: slide is **0.000 mm** everywhere.
    Unreal's body rig logic is rotation and scale only. Dropping non-root translation at runtime
    is therefore CORRECT, not a compromise — which retires the whole retarget question.

So the output carries rotation and scale tracks for every bone, and translation only for the
root, where it is genuine motion.

Bone names are rewritten through the platform's 16-pair contract map, the same one stage 02
applies to the characters. It is duplicated here deliberately - see CONTRACT_BONE_RENAMES in
stages/02-blender-assemble/tools/import_glb.py, and change both together.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import struct
from pathlib import Path

import numpy as np

# Mirrors stage 02's CONTRACT_BONE_RENAMES. MetaHuman name -> platform contract name.
CONTRACT_BONE_RENAMES = {
    "pelvis": "Hips",
    "spine_01": "Spine",
    "spine_03": "Spine1",
    "spine_05": "Spine2",
    "neck_01": "Neck",
    "head": "Head",
    "clavicle_l": "LeftShoulder",
    "upperarm_l": "LeftArm",
    "lowerarm_l": "LeftForeArm",
    "hand_l": "LeftHand",
    "clavicle_r": "RightShoulder",
    "upperarm_r": "RightArm",
    "lowerarm_r": "RightForeArm",
    "hand_r": "RightHand",
    "eye_l": "LeftEye",
    "eye_r": "RightEye",
}

ROOT_BONES = {"root", "pelvis"}


def similarity_fit(src, dst):
    """UE -> glTF map, SOLVED from the shared skeleton. Reflection allowed: Unreal is
    left-handed and glTF right-handed, and a proper-rotation-only fit misses by 167mm.

    Hardcoding this as a guessed permutation is what made the first version of this file produce
    a 441mm deviation. Solve it, and gate on the residual.
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
        res = float(np.sqrt((((src @ (scale * R).T + t) - dst) ** 2).sum(1)).mean())
        if best is None or res < best[0]:
            M = np.eye(4)
            M[:3, :3] = scale * R
            M[:3, 3] = t
            best = (res, M)
    return best[1], best[0]


def _log(m):
    print(f"[build_anims] {m}", flush=True)


def compose(t, q, s):
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    m = np.eye(4)
    m[0, 0] = 1 - 2 * (y * y + z * z); m[0, 1] = 2 * (x * y - z * w); m[0, 2] = 2 * (x * z + y * w)
    m[1, 0] = 2 * (x * y + z * w); m[1, 1] = 1 - 2 * (x * x + z * z); m[1, 2] = 2 * (y * z - x * w)
    m[2, 0] = 2 * (x * z - y * w); m[2, 1] = 2 * (y * z + x * w); m[2, 2] = 1 - 2 * (x * x + y * y)
    m[:3, 0] *= s[0]; m[:3, 1] *= s[1]; m[:3, 2] *= s[2]
    m[:3, 3] = t
    return m


def decompose(m):
    t = m[:3, 3].copy()
    s = np.linalg.norm(m[:3, :3], axis=0)
    s = np.where(s < 1e-12, 1.0, s)
    r = m[:3, :3] / s
    if np.linalg.det(r) < 0:
        s[0] = -s[0]
        r[:, 0] = -r[:, 0]
    tr = r[0, 0] + r[1, 1] + r[2, 2]
    if tr > 0:
        k = math.sqrt(tr + 1.0) * 2
        q = [(r[2, 1] - r[1, 2]) / k, (r[0, 2] - r[2, 0]) / k, (r[1, 0] - r[0, 1]) / k, 0.25 * k]
    elif r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
        k = math.sqrt(1.0 + r[0, 0] - r[1, 1] - r[2, 2]) * 2
        q = [0.25 * k, (r[0, 1] + r[1, 0]) / k, (r[0, 2] + r[2, 0]) / k, (r[2, 1] - r[1, 2]) / k]
    elif r[1, 1] > r[2, 2]:
        k = math.sqrt(1.0 + r[1, 1] - r[0, 0] - r[2, 2]) * 2
        q = [(r[0, 1] + r[1, 0]) / k, 0.25 * k, (r[1, 2] + r[2, 1]) / k, (r[0, 2] - r[2, 0]) / k]
    else:
        k = math.sqrt(1.0 + r[2, 2] - r[0, 0] - r[1, 1]) * 2
        q = [(r[0, 2] + r[2, 0]) / k, (r[1, 2] + r[2, 1]) / k, 0.25 * k, (r[1, 0] - r[0, 1]) / k]
    q = np.array(q, dtype=np.float64)
    q /= np.linalg.norm(q) or 1.0
    return t, q, s


def bind_from_glb(path: Path):
    """Bone order, parent map, and BIND WORLD matrices from the character GLB."""
    data = path.read_bytes()
    off = 12
    gltf = None
    while off < len(data):
        length, kind = struct.unpack_from("<II", data, off)
        if kind == 0x4E4F534A:
            gltf = json.loads(data[off + 8: off + 8 + length].decode("utf-8"))
            break
        off += 8 + length + ((4 - length % 4) % 4)
    nodes = gltf["nodes"]
    joints = gltf["skins"][0]["joints"]
    name = {j: nodes[j].get("name") for j in joints}
    parent = {}
    for i, n in enumerate(nodes):
        for c in n.get("children", []):
            if c in name and i in name:
                parent[name[c]] = name[i]
    order = [name[j] for j in joints]
    local = {name[j]: compose(nodes[j].get("translation", [0, 0, 0]),
                              nodes[j].get("rotation", [0, 0, 0, 1]),
                              nodes[j].get("scale", [1, 1, 1])) for j in joints}
    world = {}
    for n in order:
        p = parent.get(n)
        world[n] = local[n] if p not in world else world[p] @ local[n]
    return order, parent, local, world


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pose-dir", required=True)
    ap.add_argument("--reference-glb", required=True, help="stage 01 GLB, for bone parentage")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    order, parent, glb_local, glb_world = bind_from_glb(Path(args.reference_glb))

    clips = sorted(Path(args.pose_dir).glob("*.npz"))
    if not clips:
        raise SystemExit(f"no .npz in {args.pose_dir}")

    buffers: list[bytes] = []
    accessors: list[dict] = []
    views: list[dict] = []
    animations: list[dict] = []
    node_index: dict[str, int] = {}
    nodes: list[dict] = []

    def add_accessor(arr: np.ndarray, kind: str) -> int:
        raw = arr.astype(np.float32).tobytes()
        offset = sum(len(b) for b in buffers)
        pad = (4 - offset % 4) % 4
        if pad:
            buffers.append(b"\0" * pad)
            offset += pad
        buffers.append(raw)
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(raw)})
        acc = {"bufferView": len(views) - 1, "componentType": 5126,
               "count": int(arr.shape[0]), "type": kind}
        if kind == "SCALAR":
            acc["min"] = [float(arr.min())]
            acc["max"] = [float(arr.max())]
        accessors.append(acc)
        return len(accessors) - 1

    for clip_path in clips:
        data = np.load(clip_path, allow_pickle=True)
        bones = [str(b) for b in data["bone_names"]]
        ref = data["ref"].astype(np.float64)
        poses = data["poses"].astype(np.float64)
        times = data["times"].astype(np.float64)
        bi = {n: i for i, n in enumerate(bones)}

        ref_cs = {n: compose(ref[bi[n], 0:3], ref[bi[n], 3:7], ref[bi[n], 7:10]) for n in bones}

        # Solve the UE -> glTF map from THIS character's reference pose against the GLB bind,
        # then gate. A transform is carried across by CONJUGATION on the WORLD matrix - applying
        # only the rotation part to a local transform silently drops the translation component of
        # the basis change, which is the other half of what broke the first version.
        shared = [n for n in order if n in ref_cs]
        M, residual = similarity_fit(
            np.array([ref_cs[n][:3, 3] for n in shared]),
            np.array([glb_world[n][:3, 3] for n in shared]))
        Minv = np.linalg.inv(M)
        if residual > 2e-3:
            raise SystemExit(f"[build_anims] ABORT {clip_path.stem}: UE->glTF residual "
                             f"{residual*1000:.2f}mm")

        # self-check: the reference pose must round-trip to the GLB's own local bind
        worst = 0.0
        for n in shared:
            g = M @ ref_cs[n] @ Minv
            p = parent.get(n)
            loc = g if p not in shared else np.linalg.inv(M @ ref_cs[p] @ Minv) @ g
            worst = max(worst, float(np.abs(loc - glb_local[n]).max()))
        if worst > 1e-3:
            raise SystemExit(f"[build_anims] ABORT {clip_path.stem}: reference pose does not "
                             f"round-trip to the GLB bind (max {worst*1000:.3f}mm)")

        channels, samplers = [], []
        time_acc = add_accessor(times, "SCALAR")
        moved = 0
        for n in bones:
            if n not in shared:
                continue
            rots = np.empty((len(times), 4))
            trans = np.empty((len(times), 3))
            scales = np.empty((len(times), 3))
            for f in range(len(times)):
                idx = bi[n]
                cs = compose(poses[f, idx, 0:3], poses[f, idx, 3:7], poses[f, idx, 7:10])
                g = M @ cs @ Minv
                p = parent.get(n)
                if p in bi:
                    pidx = bi[p]
                    pcs = compose(poses[f, pidx, 0:3], poses[f, pidx, 3:7], poses[f, pidx, 7:10])
                    local = np.linalg.inv(M @ pcs @ Minv) @ g
                else:
                    local = g
                t, q, sc3 = decompose(local)
                rots[f] = q
                trans[f] = t
                scales[f] = sc3
            # keep the shortest arc so the runtime's slerp does not take the long way
            for f in range(1, len(times)):
                if float(np.dot(rots[f - 1], rots[f])) < 0:
                    rots[f] = -rots[f]

            name = CONTRACT_BONE_RENAMES.get(n, n)
            if name not in node_index:
                node_index[name] = len(nodes)
                nodes.append({"name": name})
            target = node_index[name]

            samplers.append({"input": time_acc, "output": add_accessor(rots, "VEC4"),
                             "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1,
                             "target": {"node": target, "path": "rotation"}})
            if np.abs(scales - 1.0).max() > 1e-4:
                samplers.append({"input": time_acc, "output": add_accessor(scales, "VEC3"),
                                 "interpolation": "LINEAR"})
                channels.append({"sampler": len(samplers) - 1,
                                 "target": {"node": target, "path": "scale"}})
            # Translation ONLY for the root. Measured: every other bone's local translation is
            # identical to its reference pose in every frame (slide 0.000mm), so a translation
            # track elsewhere could only carry the bake body's proportions.
            if n in ROOT_BONES and np.abs(trans - trans[0]).max() > 1e-6:
                samplers.append({"input": time_acc, "output": add_accessor(trans, "VEC3"),
                                 "interpolation": "LINEAR"})
                channels.append({"sampler": len(samplers) - 1,
                                 "target": {"node": target, "path": "translation"}})
                moved += 1

        animations.append({"name": clip_path.stem, "channels": channels, "samplers": samplers})
        _log(f"{clip_path.stem}: {len(times)} frames, {len(channels)} channels, "
             f"{len(bones)} bones, root translation on {moved}")

    binary = b"".join(buffers)
    gltf = {
        "asset": {"version": "2.0", "generator": "build_animations_from_poses"},
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "scene": 0,
        "nodes": nodes,
        "animations": animations,
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(binary)}],
    }
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * ((4 - len(js) % 4) % 4)
    binary += b"\0" * ((4 - len(binary) % 4) % 4)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(binary)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A)); f.write(js)
        f.write(struct.pack("<II", len(binary), 0x004E4942)); f.write(binary)
    _log(f"wrote {out} ({out.stat().st_size/1e6:.1f} MB, {len(animations)} clips, "
         f"{len(nodes)} nodes)")


if __name__ == "__main__":
    main()
