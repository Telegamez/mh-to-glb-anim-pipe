"""Build a Kimodo constraints.json that pins one full-body pose to the first and last frame.

Run with the Kimodo venv:
    S:\\Avatars\\metahuman\\kimodo\\.venv\\Scripts\\python make_bookends.py \\
        --pose-npz take.npz [--pose-frame 0] --duration 4.0 --out constraints.json [--hold 2]

Looping clips must end where they start (stage 05's loop-seam gate allows 2 deg). Freely
generated motion does not: the first test take seamed at 12.6 deg on the neck. Pinning the same
pose at both ends makes the clip loop by construction. Taking that pose from the Idle neutral
(rather than from the take itself) is what later puts every clip in one pose family.

--hold N also pins the N frames after the start and before the end, which gives the blend
into and out of the loop a still moment instead of a velocity kink.
"""
import argparse
import json

import numpy as np
import torch

from kimodo.geometry import matrix_to_axis_angle

FPS = 30


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pose-npz", required=True, help="Kimodo NPZ whose frame supplies the bookend pose")
    ap.add_argument("--pose-frame", type=int, default=0)
    ap.add_argument("--duration", type=float, required=True, help="seconds; must match kimodo_gen --duration")
    ap.add_argument("--hold", type=int, default=0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    d = np.load(a.pose_npz)
    rots = torch.tensor(d["local_rot_mats"][a.pose_frame], dtype=torch.float32)  # [77, 3, 3]
    root = d["root_positions"][a.pose_frame].astype(float)                        # [3], metres, Y-up
    aa = matrix_to_axis_angle(rots).numpy()                                         # [77, 3]

    n = int(round(a.duration * FPS))
    last = n - 1
    frames = sorted(set(list(range(0, a.hold + 1)) + list(range(last - a.hold, last + 1))))
    # Stand in place: the same XZ at both ends, relative to the canonical origin at frame 0.
    root = [0.0, float(root[1]), 0.0]
    constraint = {
        "type": "fullbody",
        "frame_indices": frames,
        "local_joints_rot": [aa.tolist()] * len(frames),
        "root_positions": [root] * len(frames),
        "smooth_root_2d": [[0.0, 0.0]] * len(frames),
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump([constraint], f)
    print(f"[bookends] pose {a.pose_npz}#{a.pose_frame} pinned at frames {frames} of {n}; hip height {root[1]:.3f} m -> {a.out}")


if __name__ == "__main__":
    main()
