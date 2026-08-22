"""Run export_pose_transforms over every clip in animset.json, in one editor session.

Starting Unreal costs about a minute; ten clips should not pay it ten times.
"""
import json, os, subprocess, sys

STAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.abspath(os.path.join(STAGE, "..", ".."))


def main():
    import unreal  # noqa: F401  (import proves we are inside the editor)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    cfg = json.load(open(os.path.join(STAGE, "animset.json"), encoding="utf-8"))
    clips = {}
    clips.update(cfg.get("required") or {})
    clips.update(cfg.get("gestures") or {})

    mesh = None
    out_dir = None
    fps = "30"
    for a in sys.argv:
        if a.startswith("--mesh="):
            mesh = a.split("=", 1)[1]
        if a.startswith("--out-dir="):
            out_dir = a.split("=", 1)[1]
        if a.startswith("--fps="):
            fps = a.split("=", 1)[1]
    if not (mesh and out_dir):
        raise RuntimeError("need --mesh= --out-dir=")
    os.makedirs(out_dir, exist_ok=True)

    import importlib
    for name, asset in clips.items():
        out = os.path.join(out_dir, f"{name}.npz")
        sys.argv = ["export_pose_transforms.py", f"--mesh={mesh}", f"--anim={asset}",
                    f"--out={out}", f"--fps={fps}"]
        mod = importlib.import_module("export_pose_transforms")
        importlib.reload(mod)
        print(f"[batch] {name} -> {out}", flush=True)


main()
