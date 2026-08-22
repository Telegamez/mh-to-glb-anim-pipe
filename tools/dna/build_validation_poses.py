"""Build the control-vector table that UE's dump will validate the evaluator against.

    python build_validation_poses.py Telly_Head.dna viseme_poses.json -o tg_validation_poses.json

Emits a pose file in the same schema `author_viseme_anim.py` already consumes, so the table
goes through the *same* instrument F2's bake trusts:

    UE: author_viseme_anim.py --poses tg_validation_poses.json
        -> AnimSequence -> export_pose_transforms.py (post-process ABP) -> per-bone transforms

    here: riglogic_eval.py (the Python oracle) and riglogic_probe (Epic's compiled library)
          evaluated on the same control vectors

Three bands, each answering a different question about the evaluator:

1. **single controls** -- every raw control alone at 1.0. Maximal column coverage: each frame
   isolates one column of the joint matrix, so a disagreement names the control responsible
   instead of leaving a blended pose to be picked apart. This band alone would catch an
   off-by-one in the control indexing, which is the failure most likely to look plausible.

2. **the authored visemes** -- the poses F2 actually ships, so the validation covers what is
   in production rather than only synthetic input.

3. **co-activation pairs** -- where correctives live. Two sub-bands:
   * viseme x viseme at t=0.5, ranked by how many PSDs the combination fires
   * viseme x upper-face, which is the shape real speech takes when someone is also emoting

On the naming of band 3b
------------------------
These are labelled by the controls they move -- browRaise, browFurrow, squint, cheekRaise --
and NOT by emotion names. There is no emotion -> CTRL_expressions mapping in this repo yet;
building the face's response to an emotion is 4.2c's job and has not been done. Calling a
brow-raise frame "surprised" would be inventing the very mapping the project still owes, and
the table would then encode a guess as though it were a decision.

For validating an evaluator the distinction does not cost anything: what the band needs is
upper-face controls co-active with mouth controls, which is exactly what it has. When the
emotion mapping exists, the labels can be revisited; the coverage will not need to change.
"""
from __future__ import annotations

import argparse
import json
import sys

from parse_bhvr import Reader, read_sections
from riglogic_eval import RigLogic

RAW_CONTROL_COUNT = 263
DNA_PREFIX = "CTRL_expressions."

# Upper-face control fragments, by what they move. Matched against the DNA's own raw control
# names rather than hand-listed, so a rig with different naming yields an empty band loudly
# instead of a table that silently covers nothing.
UPPER_FACE_GROUPS = {
    "browRaise": ("browRaiseInL", "browRaiseInR", "browRaiseOuterL", "browRaiseOuterR"),
    "browFurrow": ("browDownL", "browDownR", "browLateralL", "browLateralR"),
    "squint": ("eyeSquintInnerL", "eyeSquintInnerR", "eyeCheekRaiseL", "eyeCheekRaiseR"),
    "eyeWide": ("eyeWidenL", "eyeWidenR"),
}


def raw_control_names(dna_path: str) -> list[str]:
    data = open(dna_path, "rb").read()
    sections = read_sections(data)
    r = Reader(data, sections["defn"]["offset"])
    for _ in range(4):
        r.vec_u16()
        for _ in range(r.u32()):
            r.vec_u16()

    def s() -> str:
        n = r.u32()
        v = r.d[r.o:r.o + n].decode("utf-8", "replace")
        r.o += n
        return v

    def sv() -> list[str]:
        return [s() for _ in range(r.u32())]

    sv()                       # gui control names
    return sv()                # raw control names


def bare(name: str) -> str:
    return name[len(DNA_PREFIX):] if name.startswith(DNA_PREFIX) else name


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dna")
    p.add_argument("viseme_poses")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--pairs", type=int, default=24,
                   help="how many viseme x viseme pairs to include, highest PSD count first")
    ns = p.parse_args()

    raw = raw_control_names(ns.dna)
    if len(raw) != RAW_CONTROL_COUNT:
        print(f"warning: {len(raw)} raw controls, expected {RAW_CONTROL_COUNT}", file=sys.stderr)

    visemes = json.load(open(ns.viseme_poses, encoding="utf-8"))["poses"]
    authored = [v for v in visemes if v.get("controls")]

    poses = []

    # ---- band 1: every raw control alone ------------------------------------------------
    for name in raw:
        poses.append({
            "name": f"single__{bare(name)}",
            "band": "single-control",
            "note": "one column of the joint matrix in isolation",
            "controls": {bare(name): 1.0},
        })

    # ---- band 2: the authored visemes ---------------------------------------------------
    for v in authored:
        poses.append({
            "name": f"viseme__{v['name']}",
            "band": "authored-viseme",
            "note": v.get("note", "")[:160],
            "controls": dict(v["controls"]),
        })

    # ---- band 3a: viseme x viseme, ranked by corrective activation -----------------------
    rig = RigLogic(ns.dna, lod=0)
    index = {n: i for i, n in enumerate(raw)}
    psd_end = RAW_CONTROL_COUNT + rig.c["psd_count"]

    def vector(controls: dict):
        import numpy as np
        v = np.zeros(rig.control_size, "float32")
        for k, val in controls.items():
            i = index.get(f"{DNA_PREFIX}{k}")
            if i is not None:
                v[i] = float(val)
        return v

    import numpy as np
    ranked = []
    for i in range(len(authored)):
        for j in range(i + 1, len(authored)):
            a, b = authored[i], authored[j]
            mixed = 0.5 * vector(a["controls"]) + 0.5 * vector(b["controls"])
            w = mixed.copy()
            rig.psds(w)
            ranked.append((int(np.count_nonzero(w[RAW_CONTROL_COUNT:psd_end])), a, b))
    ranked.sort(key=lambda t: -t[0])

    for npsd, a, b in ranked[:ns.pairs]:
        merged = {}
        for src in (a["controls"], b["controls"]):
            for k, val in src.items():
                merged[k] = round(merged.get(k, 0.0) + 0.5 * float(val), 4)
        poses.append({
            "name": f"pair__{a['name']}__{b['name']}",
            "band": "viseme-pair",
            "note": f"t=0.5 blend; fires {npsd} correctives",
            "controls": merged,
        })

    # ---- band 3b: viseme x upper-face ----------------------------------------------------
    available = {bare(n) for n in raw}
    groups = {}
    for label, fragments in UPPER_FACE_GROUPS.items():
        present = [f for f in fragments if f in available]
        if not present:
            print(f"warning: upper-face group '{label}' matched no controls in this DNA",
                  file=sys.stderr)
            continue
        groups[label] = present

    mouth_visemes = [v for v in authored if v["name"] in
                     ("viseme_aa", "viseme_O", "viseme_E", "viseme_PP", "viseme_CH")]
    for v in mouth_visemes:
        for label, controls in groups.items():
            merged = dict(v["controls"])
            for c in controls:
                merged[c] = 0.7
            poses.append({
                "name": f"upper__{v['name']}__{label}",
                "band": "viseme-x-upperface",
                "note": ("mouth and upper face co-active -- the shape speech takes when "
                         "someone is also emoting. Labelled by what it MOVES, not by an "
                         "emotion: no emotion->control mapping exists yet (4.2c owes it)."),
                "controls": merged,
            })

    out = {
        "$schema_note": ("Same schema as tg_viseme_poses.json, consumed by "
                         "author_viseme_anim.py. Built by tools/dna/build_validation_poses.py."),
        "asset_path": "/Game/TG_Face/AS_TG_Validation",
        "skeleton": "",
        "frame_rate": 30,
        "control_prefix": "ctrl_expressions_",
        "_purpose": ("Ground-truth table for slice 5.0/5.1. Bake through the post-process ABP "
                     "with export_pose_transforms.py; diff against riglogic_eval.py (the Python "
                     "oracle) and riglogic_probe (Epic's compiled library)."),
        "_bands": {
            "single-control": "every raw control alone at 1.0 -- isolates one matrix column",
            "authored-viseme": "the poses F2 ships",
            "viseme-pair": "t=0.5 blends ranked by corrective activation",
            "viseme-x-upperface": "mouth + upper face co-active",
        },
        "poses": poses,
    }

    with open(ns.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")

    counts = {}
    for pose in poses:
        counts[pose["band"]] = counts.get(pose["band"], 0) + 1
    print(f"wrote {ns.out}: {len(poses)} poses")
    for band, n in counts.items():
        print(f"  {band:<22} {n:>4}")
    print(f"  at {out['frame_rate']} fps that is {len(poses) / out['frame_rate']:.1f} s "
          f"plus a rest frame")
    return 0


if __name__ == "__main__":
    sys.exit(main())
