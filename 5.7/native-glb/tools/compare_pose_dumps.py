"""Close gate 3 across two pose dumps, and refuse to do it if they are not a pair.

    python compare_pose_dumps.py <evaluated.npz> <original.npz>

## Why this file exists

`export_pose_transforms.py` normally proves three things in one process:

    1  the mesh has a post-process Anim Blueprint
    2  frames differ from one another
    3  the EVALUATED pose differs from the RAW AnimSequence pose

Gate 3 is the one that separates "an animation played" from "MetaHuman's rig logic ran".
It cannot run in-process any more: `evaluate_animation_blueprint_with_input_pose` aborts inside
the engine on the face mesh, so the evaluated pose now comes from the Sequencer bake instead and
`--raw-only` reads each sequence with `get_anim_pose_at_time` alone. Two files, one gate.

## The failure that split introduces

Inside one process the two poses could not disagree about where they came from. Across two
files they can: re-run one side, forget the other, and the comparison silently runs against a
stale dump. Nothing about the arrays themselves would look wrong.

So a pair must share a `run_id`, and **this tool refuses to compare dumps whose run ids differ
or are missing**. That refusal is the point of the file as much as the arithmetic is. It is not
a warning, because a warning printed above a green result is how the last several of these hid.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np

GATE3_MIN_CM = 1e-4


def _load(path: str) -> tuple[dict, np.lib.npyio.NpzFile]:
    d = np.load(path, allow_pickle=True)
    meta = json.loads(str(d["meta"][0]))
    return meta, d


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("evaluated", help="dump of the EVALUATED bake (A_*_evaluated)")
    p.add_argument("original", help="dump of the ORIGINAL sequence")
    p.add_argument("--json", help="write the verdict here")
    ns = p.parse_args()

    ev_meta, ev = _load(ns.evaluated)
    or_meta, og = _load(ns.original)

    # ---- provenance, before any arithmetic ----------------------------------------------
    ev_run, or_run = ev_meta.get("run_id"), or_meta.get("run_id")
    if not ev_run or not or_run:
        print(f"REFUSED: a dump has no run_id (evaluated={ev_run!r} original={or_run!r}).\n"
              f"  Both dumps must come from --raw-only runs, which require --run-id.",
              file=sys.stderr)
        return 2
    if ev_run != or_run:
        print(f"REFUSED: run_id mismatch — evaluated={ev_run!r} original={or_run!r}.\n"
              f"  These are not a pair. One of them is from an earlier run, and comparing them\n"
              f"  would answer a question nobody asked. Re-run both sides with one --run-id.",
              file=sys.stderr)
        return 2
    if ev_meta.get("source_anim") == or_meta.get("source_anim"):
        print(f"REFUSED: both dumps name the same source_anim "
              f"({ev_meta.get('source_anim')!r}). Gate 3 compares the evaluated bake against\n"
              f"  the ORIGINAL sequence; comparing a file with itself passes trivially.",
              file=sys.stderr)
        return 2

    ev_names = [str(b) for b in ev["bone_names"]]
    or_names = [str(b) for b in og["bone_names"]]
    if ev_names != or_names:
        print(f"REFUSED: bone lists differ ({len(ev_names)} vs {len(or_names)}). Different rigs.",
              file=sys.stderr)
        return 2
    if ev["raw"].shape != og["raw"].shape:
        print(f"REFUSED: shapes differ {ev['raw'].shape} vs {og['raw'].shape}.", file=sys.stderr)
        return 2

    print(f"run_id           {ev_run}")
    print(f"evaluated from   {ev_meta.get('source_anim')}")
    print(f"original from    {or_meta.get('source_anim')}")
    print(f"shape            {ev['raw'].shape[0]} frames x {ev['raw'].shape[1]} bones")

    # ---- gate 3 --------------------------------------------------------------------------
    # Translation only, matching what the in-process gate measured, and in the same units.
    a = ev["raw"][:, :, :3].astype(np.float64)
    b = og["raw"][:, :, :3].astype(np.float64)
    delta = np.abs(a - b)
    max_cm = float(delta.max())
    per_bone = delta.max(axis=(0, 2))
    moved = int((per_bone > GATE3_MIN_CM).sum())

    facial = [i for i, n in enumerate(ev_names) if n.upper().startswith("FACIAL")]
    facial_moved = int((per_bone[facial] > GATE3_MIN_CM).sum()) if facial else 0

    # Two different quantities, and conflating them is how a pass means the wrong thing.
    #
    #   PLACEMENT  |evaluated - original|, dominated by the ABP putting the facial hierarchy
    #              where it belongs. On telly this is ~156 cm, because the face SKM's reference
    #              pose holds FACIAL_C_FacialRoot at z=147 and the evaluated pose puts it at
    #              z=166. Body bones (root, pelvis, spine_01) are identical, so it is not a
    #              space mismatch — it is real, and it is not expression.
    #
    #   MOTION     how much the evaluated pose changes ACROSS FRAMES. This is what RigLogic
    #              produced from the curves, and the only half that validates an evaluator.
    #
    # A gate-3 that reported only the first would announce a 156 cm "ABP contribution" for a
    # face whose expressions move a few millimetres. Both are printed, and the motion figure
    # is checked separately below.
    motion = np.abs(a - a[0]).max(axis=(0, 2))
    motion_moved = int((motion > GATE3_MIN_CM).sum())
    motion_facial = int((motion[facial] > GATE3_MIN_CM).sum()) if facial else 0
    orig_motion = float(np.abs(b - b[0]).max())

    print(f"\nPLACEMENT — |evaluated - original|")
    print(f"  max                        {max_cm:.4f} cm")
    print(f"  bones that differ          {moved}/{len(ev_names)}")
    print(f"    of which FACIAL_*        {facial_moved}/{len(facial)}")
    print(f"\nMOTION — evaluated across frames (what RigLogic produced)")
    print(f"  max                        {motion.max():.4f} cm")
    print(f"  bones that move            {motion_moved}/{len(ev_names)}")
    print(f"    of which FACIAL_*        {motion_facial}/{len(facial)}")
    print(f"  original's own motion      {orig_motion:.4f} cm  (curve-driven: expect 0)")

    if max_cm < GATE3_MIN_CM:
        print(f"\nGATE 3 FAILED: the evaluated bake is identical to the original sequence "
              f"(max delta {max_cm:.6f} cm). The post-process ABP did not contribute, so this "
              f"is not MetaHuman's deformation.", file=sys.stderr)
        return 1

    # The gates ask whether SOMETHING moved. On a face rig the question is whether the FACE
    # did, and no gate has ever asked that — a dump that passed while moving only body bones
    # would be measuring the wrong thing entirely.
    if facial and facial_moved == 0:
        print(f"\nGATE 3 FAILED: {moved} bones differ but NOT ONE of the {len(facial)} FACIAL_* "
              f"bones does. Something moved; the face did not.", file=sys.stderr)
        return 1

    # The placement delta alone would pass for a rig that never expressed anything. A dump
    # whose face is correctly positioned and completely still is exactly the artifact this
    # programme keeps producing, so motion is checked on its own terms.
    if motion_facial == 0:
        print(f"\nGATE 3 FAILED: the facial hierarchy is placed ({facial_moved} FACIAL_* bones "
              f"differ from the original) but NOTHING MOVES across the {ev['raw'].shape[0]} "
              f"frames. RigLogic produced no expression.", file=sys.stderr)
        return 1

    print(f"\nGATE 3 PASSED")
    print(f"  the ABP placed the facial rig ({facial_moved}/{len(facial)} FACIAL_* bones differ "
          f"from the original by up to {max_cm:.2f} cm)")
    print(f"  and RigLogic drove it ({motion_facial} FACIAL_* bones move, up to "
          f"{motion.max():.2f} cm across the sequence, against an original that moves "
          f"{orig_motion:.2f} cm)")

    if ns.json:
        with open(ns.json, "w", encoding="utf-8") as f:
            json.dump({"run_id": ev_run,
                       "placement_max_cm": max_cm, "placement_bones": moved,
                       "placement_facial": facial_moved,
                       "motion_max_cm": float(motion.max()), "motion_bones": motion_moved,
                       "motion_facial": motion_facial,
                       "original_motion_cm": orig_motion,
                       "facial_total": len(facial),
                       "frames": int(ev["raw"].shape[0]), "bones": len(ev_names),
                       "evaluated_source": ev_meta.get("source_anim"),
                       "original_source": or_meta.get("source_anim"),
                       "gate3": "PASSED"}, f, indent=2)
        print(f"  wrote {ns.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
