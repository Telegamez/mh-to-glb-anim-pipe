"""Derive the set of bones a 4-influence target must keep, and write live_bones.json.

## Why this file exists

MetaHuman rigs the body with ~1200 joints, but only a few dozen of them ever MOVE once the
asset leaves Unreal. The rest are rig-logic helpers (`clavicle_out_*`, `upperarm_fwd_*`,
`thigh_bck_*`, `*_twistCor_*`, `*_bulge_*`) that UE drives procedurally from the current pose.
glTF cannot carry rig logic, so in the browser they sit frozen at bind.

A frozen bone is an ALIAS of its parent, exactly:

    worldLive(b) = worldLive(p) . localBind(b)
    IBM(b)       = inv(worldBind(p) . localBind(b))
    jointMatrix(b) = worldLive(p) . localBind(b) . inv(localBind(b)) . inv(worldBind(p))
                   = jointMatrix(p)

So folding a frozen bone's skin weight into its nearest live ancestor cannot change the
deformation by even a float. It can only change how many DISTINCT influences a vertex needs —
and that is the whole game, because glTF/three.js allow four and MetaHuman clothing wants
twelve. Measured on Tele's outfit before any fold, the exporter's top-4 truncation was throwing
away a median 21.9% of each vertex's weight (max 50.6%, 69% of vertices losing more than 10%),
and it kept redundant aliases of one bone while discarding genuinely distinct influences. That
is what tore the clothing apart. After the fold the same truncation discards a median of 0.0%.

## What counts as live

Three sources, unioned:

  1. Bones any clip in the library actually moves. Not bones that merely HAVE a track — the
     bake writes translation/rotation/scale channels for all 342 body joints and 341 of the
     translation channels are constant, carrying the source skeleton's proportions rather than
     any motion.
  2. `CONTRACT_BONES` — the 16 the platform asset contract names. `LeftEye`/`RightEye` are the
     reason this list is not optional: gaze poses them procedurally at runtime and NO clip
     animates them, so deriving liveness from the clips alone would fold them into `Head` and
     silently kill eye movement.
  3. Anything named in `extra_live` in the stage config, as an escape hatch.

Re-run this whenever the animation set changes. `export_glb.py` fails the export if the file is
missing rather than silently shipping an unfolded — that is, torn — character.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import struct
from pathlib import Path

# The platform asset contract's 16 bones. Present in every companion, referenced by name from
# the runtime motion code, and therefore live whether or not a clip animates them.
CONTRACT_BONES = [
    "Hips", "Spine", "Spine1", "Spine2", "Neck", "Head",
    "LeftEye", "RightEye",
    "LeftShoulder", "LeftArm", "LeftForeArm", "LeftHand",
    "RightShoulder", "RightArm", "RightForeArm", "RightHand",
]

# Twist joints are driven procedurally at runtime by naturalMotion's twistDriver, not by any
# clip, so they must survive the fold with their weights intact. They are load-bearing: 45.2% of
# the forearm and wrist skin is weighted to them, and folding them into their parent is what
# made the wrists candy-wrapper. Their `*_twistCor_*` children sit at zero offset and fold onto
# them, so those come along for free.
TWIST_BONE = re.compile(r"^(lowerarm|upperarm|thigh|calf)_twist_\d+_[lr]$", re.IGNORECASE)


# A channel counts as motion only past these. Anything under is a constant track.
ROT_EPSILON_DEG = 0.5
POS_EPSILON_M = 1e-3
SCALE_EPSILON = 1e-3

_COMPONENT = {5120: "b", 5121: "B", 5122: "h", 5123: "H", 5125: "I", 5126: "f"}
_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _load_glb(path: Path):
    data = path.read_bytes()
    if data[:4] != b"glTF":
        raise SystemExit(f"not a GLB: {path}")
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


def _read_accessor(gltf, binary, index):
    accessor = gltf["accessors"][index]
    count = _COUNT[accessor["type"]]
    fmt = _COMPONENT[accessor["componentType"]]
    size = struct.calcsize("<" + fmt)
    view = gltf["bufferViews"][accessor["bufferView"]]
    base = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    stride = view.get("byteStride") or size * count
    return [
        struct.unpack_from("<" + fmt * count, binary, base + i * stride)
        for i in range(accessor["count"])
    ]


def _quat_angle_deg(a, b):
    dot = min(1.0, abs(sum(x * y for x, y in zip(a, b))))
    return math.degrees(2 * math.acos(dot))


def _moving_bones(gltf, binary) -> set[str]:
    moving: set[str] = set()
    for animation in gltf.get("animations", []):
        for channel in animation["channels"]:
            target = channel["target"]
            name = gltf["nodes"][target["node"]].get("name")
            if not name:
                continue
            values = _read_accessor(gltf, binary, animation["samplers"][channel["sampler"]]["output"])
            if not values:
                continue
            path = target["path"]
            if path == "rotation":
                if any(_quat_angle_deg(values[0], v) > ROT_EPSILON_DEG for v in values):
                    moving.add(name)
            elif path == "translation":
                spread = max(
                    max(v[i] for v in values) - min(v[i] for v in values) for i in range(3)
                )
                if spread > POS_EPSILON_M:
                    moving.add(name)
            elif path == "scale":
                if any(abs(c - 1.0) > SCALE_EPSILON for v in values for c in v):
                    moving.add(name)
    return moving


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--animations", required=True, help="path to mh-animations.glb")
    parser.add_argument("--out", required=True, help="path to write live_bones.json")
    parser.add_argument("--extra-live", nargs="*", default=[], help="additional bones to keep")
    args = parser.parse_args()

    gltf, binary = _load_glb(Path(args.animations))
    moving = _moving_bones(gltf, binary)
    clips = [a.get("name", "?") for a in gltf.get("animations", [])]
    twist = {n["name"] for n in gltf.get("nodes", []) if TWIST_BONE.match(n.get("name") or "")}
    live = sorted(moving | set(CONTRACT_BONES) | twist | set(args.extra_live))

    missing = [b for b in CONTRACT_BONES if b not in moving]
    payload = {
        "source": Path(args.animations).name,
        "clips": clips,
        "live_bones": live,
        "moved_by_clips": sorted(moving),
        "contract_bones_no_clip_moves_them": sorted(missing),
        "twist_bones_driven_at_runtime": sorted(twist),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"[live_bones] clips: {len(clips)} -> {', '.join(clips)}")
    print(f"[live_bones] moved by clips: {len(moving)}")
    print(f"[live_bones] + contract bones no clip moves: {', '.join(missing) or '(none)'}")
    print(f"[live_bones] + twist bones driven at runtime: {len(twist)}")
    print(f"[live_bones] live total: {len(live)}  ->  {out}")


if __name__ == "__main__":
    main()
