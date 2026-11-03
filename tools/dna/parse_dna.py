"""Parse a MetaHuman `.dna` file's header + definition layer. No engine needed.

    python parse_dna.py Telly_Head.dna [--json out.json]

Reports the section table and the counts Phase 5 (RigLogic → WASM) is sized
against: joints, raw controls, GUI controls, blendshape channels, animated
maps, per-LOD joint counts, and the mesh list.

Why a hand-rolled reader
------------------------
This is the *validation* half of `export_dna.py`, and validating an exporter
with the same engine that produced the file proves nothing about the file — it
proves the engine is self-consistent. Reading the bytes independently is what
makes "the DNA exists and parses" a claim about the artifact (§6.1).

It is deliberately partial: header, section index, and the definition layer.
The behaviour and geometry layers are the spike's job, not this tool's.

Binary format (DNA generation 2, version 5 — what UE 5.8 writes)
----------------------------------------------------------------
All integers big-endian.

    char[3]  "DNA"
    uint16   generation
    uint16   version
    uint32   sectionCount
    section[sectionCount]:
        char[4] id ("desc" "defn" "bhvr" "geom" "mlbh" "rbfb" "rbfe" "jbmd" "twsw")
        uint16  generation
        uint16  version
        uint32  offset      (absolute, from file start)
        uint32  size

    string      := uint32 length + length bytes, no terminator
    vector<T>   := uint32 count + count elements
    LODMapping  := vector<uint16> lods + vector<vector<uint16>> indices

    defn := LODMapping x4 (joint, blendshape, animatedmap, mesh)
            + vector<string> guiControlNames
            + vector<string> rawControlNames
            + vector<string> jointNames
            + vector<string> blendShapeChannelNames
            + vector<string> animatedMapNames
            + vector<string> meshNames
            + ...            (the rest is not read here)
"""
from __future__ import annotations

import argparse
import json
import struct
import sys

MAGIC = b"DNA"
SECTION_ENTRY_BYTES = 16
HEADER_BYTES = 11  # 3 magic + 2 generation + 2 version + 4 section count


class _Reader:
    """Big-endian cursor over the DNA byte string."""

    def __init__(self, data: bytes, offset: int):
        self.d = data
        self.o = offset

    def u16(self) -> int:
        v = struct.unpack_from(">H", self.d, self.o)[0]
        self.o += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from(">I", self.d, self.o)[0]
        self.o += 4
        return v

    def u16_vector(self) -> list[int]:
        n = self.u32()
        v = struct.unpack_from(f">{n}H", self.d, self.o)
        self.o += 2 * n
        return list(v)

    def string(self) -> str:
        n = self.u32()
        v = self.d[self.o:self.o + n].decode("utf-8", "replace")
        self.o += n
        return v

    def string_vector(self) -> list[str]:
        return [self.string() for _ in range(self.u32())]

    def lod_mapping(self) -> tuple[list[int], list[list[int]]]:
        lods = self.u16_vector()
        return lods, [self.u16_vector() for _ in range(self.u32())]


def parse(path: str) -> dict:
    with open(path, "rb") as f:
        data = f.read()

    if data[:3] != MAGIC:
        raise ValueError(f"{path}: not a DNA file (magic {data[:4]!r})")

    generation, version = struct.unpack_from(">HH", data, 3)
    count = struct.unpack_from(">I", data, 7)[0]

    sections = {}
    o = HEADER_BYTES
    for _ in range(count):
        sid = data[o:o + 4].decode("ascii", "replace")
        sgen, sver = struct.unpack_from(">HH", data, o + 4)
        soff, ssize = struct.unpack_from(">II", data, o + 8)
        sections[sid] = {"generation": sgen, "version": sver,
                         "offset": soff, "size": ssize}
        o += SECTION_ENTRY_BYTES

    out = {
        "path": path,
        "file_size": len(data),
        "generation": generation,
        "version": version,
        "sections": sections,
    }

    if "defn" not in sections:
        out["error"] = "no definition layer — cannot report rig counts"
        return out

    r = _Reader(data, sections["defn"]["offset"])
    lod_joint = r.lod_mapping()
    r.lod_mapping()  # blendshape LOD mapping — skipped
    r.lod_mapping()  # animated-map LOD mapping — skipped
    r.lod_mapping()  # mesh LOD mapping — skipped
    gui = r.string_vector()
    raw = r.string_vector()
    joints = r.string_vector()
    blendshapes = r.string_vector()
    animated_maps = r.string_vector()
    meshes = r.string_vector()

    out.update({
        "lod_count": len(lod_joint[0]),
        "joints_per_lod": [len(x) for x in lod_joint[1]],
        "gui_controls": len(gui),
        "raw_controls": len(raw),
        "joints": len(joints),
        "blend_shape_channels": len(blendshapes),
        "animated_maps": len(animated_maps),
        "meshes": meshes,
        # The tongue is the reason F2 exists: ARKit exposes `tongueOut` and
        # nothing else, while the rig underneath carries these. Reported by
        # name so the claim is checkable rather than asserted.
        "tongue_joints": [j for j in joints if "tongue" in j.lower()],
        "tongue_blend_shape_channels": [b for b in blendshapes
                                        if "tongue" in b.lower()],
        "joint_names_sample": joints[:16],
        "raw_control_names_sample": raw[:16],
    })
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dna", help="path to the .dna file")
    p.add_argument("--json", help="also write the full parse to this path")
    ns = p.parse_args()

    r = parse(ns.dna)
    print(f"{r['path']}")
    print(f"  DNA generation {r['generation']}.{r['version']}  "
          f"{r['file_size'] / 1_000_000:.1f} MB")
    print(f"  sections: {', '.join(r['sections'])}")
    if "joints" not in r:
        print(f"  {r.get('error')}")
        return 1
    print(f"  joints                 {r['joints']:5d}   "
          f"(per LOD: {r['joints_per_lod']})")
    print(f"  gui controls           {r['gui_controls']:5d}")
    print(f"  raw controls           {r['raw_controls']:5d}")
    print(f"  blendshape channels    {r['blend_shape_channels']:5d}")
    print(f"  animated maps          {r['animated_maps']:5d}")
    print(f"  meshes                 {len(r['meshes']):5d}")
    print(f"  tongue joints          {len(r['tongue_joints']):5d}   "
          f"{r['tongue_joints'][:6]}")
    print(f"  tongue bs channels     {len(r['tongue_blend_shape_channels']):5d}")
    if ns.json:
        with open(ns.json, "w", encoding="utf-8") as f:
            json.dump(r, f, indent=2)
        print(f"  wrote {ns.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
