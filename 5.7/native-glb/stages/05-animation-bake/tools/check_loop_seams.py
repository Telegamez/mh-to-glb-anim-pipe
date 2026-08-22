"""Assert that every looping clip in a built animation GLB actually loops.

Reads the GLB's own JSON and BIN chunks - no Blender, no UE, no three.js - and
compares each rotation channel's FIRST keyframe against its LAST. For a clip the
runtime plays with THREE.LoopRepeat, that pair is what the mixer jumps between
every cycle, so a large gap is a visible snap.

Written after telegamez#3321, where the published libraries shipped a 36 degree
snap on every looping clip. The cause was one spurious final keyframe: UE's glTF
exporter samples an AnimSequence's range INCLUSIVELY, so an N-frame clip ships
N+1 samples. `build_animations_glb.py` now trims that tail; this is how you check
it worked, before publishing rather than after someone notices.

Diagnosing, not just failing: it also reports the gap against the SECOND-TO-LAST
frame. If that number is near zero while the last-frame number is large, the clip
loops cleanly and the tail was not trimmed - exactly the signature of the original
defect.

    python check_loop_seams.py <built.glb> [--max-deg 2.0] [--clips Idle,Listening]

Exits non-zero if any checked clip exceeds the threshold.
"""

import argparse
import io
import json
import math
import struct
import sys

NUM_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
# Clips the runtime loops. Gestures play LoopOnce and their last frame is the
# pose they hold, so a seam there is correct, not a defect.
DEFAULT_LOOPING = ("Idle", "Listening", "Thinking", "Focused", "Speaking")


def _load(path):
    data = io.open(path, "rb").read()
    if data[:4] != b"glTF":
        raise SystemExit(f"{path}: not a GLB")
    json_len = struct.unpack("<I", data[12:16])[0]
    gltf = json.loads(data[20:20 + json_len].decode("utf-8"))
    offset = 20 + json_len
    bin_len = struct.unpack("<II", data[offset:offset + 8])[0]
    return gltf, data[offset + 8: offset + 8 + bin_len]


def _read(gltf, blob, accessor_index):
    acc = gltf["accessors"][accessor_index]
    if acc.get("componentType") != 5126:
        raise SystemExit("only float32 accessors are handled; got "
                         f"componentType={acc.get('componentType')}")
    view = gltf["bufferViews"][acc["bufferView"]]
    n = NUM_COMPONENTS[acc["type"]]
    base = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or 4 * n
    return [struct.unpack_from("<" + "f" * n, blob, base + i * stride)
            for i in range(acc["count"])]


def _quat_angle_deg(a, b):
    dot = min(1.0, abs(sum(x * y for x, y in zip(a, b))))
    return math.degrees(2 * math.acos(dot))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("glb")
    ap.add_argument("--max-deg", type=float, default=2.0,
                    help="largest tolerated first-to-last bone rotation, degrees")
    ap.add_argument("--clips", help="comma-separated clip names (default: the looping states)")
    args = ap.parse_args()

    gltf, blob = _load(args.glb)
    wanted = [c.strip() for c in args.clips.split(",")] if args.clips else list(DEFAULT_LOOPING)
    nodes = gltf.get("nodes", [])
    failures = []

    for anim in gltf.get("animations", []):
        name = anim.get("name")
        if name not in wanted:
            continue
        worst_last = (0.0, "")
        worst_prev = 0.0
        for ch in anim["channels"]:
            if ch["target"].get("path") != "rotation":
                continue
            values = _read(gltf, blob, anim["samplers"][ch["sampler"]]["output"])
            if len(values) < 3:
                continue
            gap = _quat_angle_deg(values[0], values[-1])
            if gap > worst_last[0]:
                node = ch["target"].get("node")
                worst_last = (gap, nodes[node].get("name", "?") if node is not None else "?")
            worst_prev = max(worst_prev, _quat_angle_deg(values[0], values[-2]))

        ok = worst_last[0] <= args.max_deg
        print(f"  {'ok  ' if ok else 'FAIL'} {name:<11} seam {worst_last[0]:7.3f} deg "
              f"({worst_last[1]})   vs 2nd-to-last {worst_prev:7.3f} deg")
        if not ok:
            failures.append(name)
            if worst_prev <= args.max_deg:
                print(f"       ^ the clip itself loops cleanly; the TAIL was not trimmed. "
                      f"See _trim_loop_tail in build_animations_glb.py")

    if failures:
        print(f"\n{len(failures)} clip(s) exceed {args.max_deg} deg: {', '.join(failures)}")
        sys.exit(1)
    print(f"\nall checked clips loop within {args.max_deg} deg")


if __name__ == "__main__":
    main()
