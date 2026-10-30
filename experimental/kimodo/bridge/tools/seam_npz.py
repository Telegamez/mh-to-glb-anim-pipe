"""Source-side loop check on Kimodo NPZ takes: first vs last frame, before any UE time is spent.

S:\\Avatars\\metahuman\\kimodo\\.venv\\Scripts\\python seam_npz.py take_00.npz [take_01.npz ...]
"""
import os
import sys

import numpy as np
from scipy.spatial.transform import Rotation as R

for path in sys.argv[1:]:
    d = np.load(path)
    g = d["global_rot_mats"]
    seam = np.degrees((R.from_matrix(g[0]).inv() * R.from_matrix(g[-1])).magnitude()).max()
    excursion = max(np.degrees((R.from_matrix(g[0]).inv() * R.from_matrix(g[t])).magnitude()).max()
                    for t in range(len(g)))
    drift = np.linalg.norm(d["root_positions"][-1] - d["root_positions"][0]) * 100
    print(f"{os.path.basename(path):22s} seam {seam:5.2f} deg   max excursion {excursion:5.1f} deg   root drift {drift:4.1f} cm")
