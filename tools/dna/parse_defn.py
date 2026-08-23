"""The DNA definition layer past the name vectors: joint hierarchy and neutral transforms.

`parse_dna.py` reads the names and stops. Everything after them is what a consumer needs to
place a joint: its parent, and its neutral local transform.

## Why this matters more than it sounds

Two separate failures on this project came from borrowing a hierarchy instead of reading the
one that defines the rig:

  * UE's `AnimPoseSpaces.LOCAL` is not parent-relative on this rig — it reports
    FACIAL_L_ForeheadInB2 at 168 cm, a component-space magnitude.
  * The stage-02 skin dump's armature is MERGED by stage 02, so its parenting is not Unreal's.
    Deriving parent-relative transforms from it turned a real 1.1 cm lip motion into a 99 cm
    "disagreement" across 229 of 263 controls.

The DNA's hierarchy is neither borrowed nor derived: it is the definition. RigLogic's own
joint deltas are indexed against exactly these 870 joints in exactly this order.

## Layout, established the same way the behaviour layer was

Walked and checked against the section boundary — this parse ends on `defn`'s last byte
exactly, which is what makes the field order a claim about the file rather than about memory.

    defn := LODMapping x4
            vector<string> gui, raw, joint, blendShapeChannel, animatedMap, mesh   (names)
            vector<uint16> meshBlendShapeChannelMappingIndices                     (pairs)
            vector<uint16> meshBlendShapeChannelMappingLODs
            vector<uint16> jointHierarchy          parent index per joint
            vector<float>  neutralJointTranslationXs / Ys / Zs
            vector<float>  neutralJointRotationXs  / Ys / Zs                       (degrees)
"""
from __future__ import annotations

import struct

from parse_bhvr import Reader, read_sections


def parse_definition(path: str) -> dict:
    data = open(path, "rb").read()
    sections = read_sections(data)
    start = sections["defn"]["offset"]
    end = start + sections["defn"]["size"]
    r = Reader(data, start)

    for _ in range(4):                       # the four LOD mappings
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

    gui, raw, joints, blend_shapes, animated_maps, meshes = (sv() for _ in range(6))

    r.vec_u16()                              # mesh<->blendshape mapping indices
    r.vec_u16()                              # and its LOD vector

    n = r.u32()
    if n != len(joints):
        raise ValueError(f"{path}: joint hierarchy has {n} entries, {len(joints)} joints")
    parents = list(struct.unpack_from(f">{n}H", data, r.o))
    r.o += 2 * n

    def fvec(expect: int) -> list[float]:
        c = r.u32()
        if c != expect:
            raise ValueError(f"{path}: expected {expect} floats, found {c}")
        v = list(struct.unpack_from(f">{c}f", data, r.o))
        r.o += 4 * c
        return v

    nj = len(joints)
    tx, ty, tz = fvec(nj), fvec(nj), fvec(nj)
    rx, ry, rz = fvec(nj), fvec(nj), fvec(nj)

    if r.o != end:
        raise ValueError(
            f"{path}: definition parse ended at {r.o}, section ends at {end} "
            f"(delta {r.o - end:+}). Refusing to report a hierarchy from a desynchronised read.")

    return {
        "gui_controls": gui, "raw_controls": raw, "joints": joints,
        "blend_shape_channels": blend_shapes, "animated_maps": animated_maps,
        "meshes": meshes,
        "joint_parents": parents,
        "neutral_translation": list(zip(tx, ty, tz)),
        "neutral_rotation_deg": list(zip(rx, ry, rz)),
    }


if __name__ == "__main__":
    import sys

    d = parse_definition(sys.argv[1])
    print(f"{sys.argv[1]}")
    print(f"  joints            {len(d['joints'])}")
    print(f"  raw controls      {len(d['raw_controls'])}")
    print(f"  hierarchy         parents[:8] {d['joint_parents'][:8]}  "
          f"max {max(d['joint_parents'])}")
    t = d["neutral_translation"]
    print(f"  neutral translate x {min(p[0] for p in t):.2f}..{max(p[0] for p in t):.2f}  "
          f"y {min(p[1] for p in t):.2f}..{max(p[1] for p in t):.2f}  "
          f"z {min(p[2] for p in t):.2f}..{max(p[2] for p in t):.2f}")
    print("  parse ended exactly on the section boundary")
