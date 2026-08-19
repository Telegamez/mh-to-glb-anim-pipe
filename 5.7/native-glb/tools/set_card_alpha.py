"""Switch hair/eyebrow/lash CARD materials between BLEND and MASK in a finished GLB.

Card hair is a stack of overlapping alpha-textured quads. Unreal renders those with a MASKED
material: a texel below the clip value is discarded outright. Our export emits BLEND, which
keeps every low-alpha texel as a faint semi-transparent sliver, and layers dozens of them
per pixel with no per-triangle sort. The visible result is stray strands drifting across the
face that Unreal never draws.

MASK is not strictly better everywhere — it hardens the silhouette of fine flyaway strands —
so this exists to make the comparison cheap rather than to declare a winner. It rewrites only
the JSON chunk, so it takes a second and needs no re-export; Draco touches vertex data, not
materials.

DECIDED 2026-08-19: the roster ships BLEND. Both modes were built and compared side by side in
the harness on Telee and Telie, and BLEND was preferred — the faint stray strands it keeps cost
less than the cut-out silhouette MASK produces on fine hair. Do not "fix" the exporter to emit
MASK; that comparison has been made. This tool stays for re-running it on a future hairstyle
where the trade may land the other way.

    python tools/set_card_alpha.py --glb <in.glb> --out <out.glb> --mode MASK --cutoff 0.33
    python tools/set_card_alpha.py --glb <in.glb> --out <out.glb> --mode BLEND
"""
from __future__ import annotations

import argparse
import json
import re
import struct
from pathlib import Path

JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942

# Card meshes only. The skin, garment and eye materials are deliberately untouched.
CARD_MATERIAL = re.compile(r"CardMat|LashMat", re.IGNORECASE)


def _log(msg):
    print(f"[card_alpha] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glb", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["MASK", "BLEND", "OPAQUE"], default="MASK")
    ap.add_argument("--cutoff", type=float, default=0.33,
                    help="alphaCutoff for MASK; Unreal's default opacity mask clip is 0.333")
    args = ap.parse_args()

    raw = Path(args.glb).read_bytes()
    magic, version, _ = struct.unpack("<III", raw[:12])
    if magic != 0x46546C67:
        raise RuntimeError(f"{args.glb} is not a GLB")

    chunks, off = [], 12
    while off < len(raw):
        length, ctype = struct.unpack("<II", raw[off:off + 8])
        chunks.append((ctype, raw[off + 8:off + 8 + length]))
        off += 8 + length

    gltf = None
    for ctype, data in chunks:
        if ctype == JSON_CHUNK:
            gltf = json.loads(data)
    if gltf is None:
        raise RuntimeError("no JSON chunk")

    touched = []
    for mat in gltf.get("materials", []):
        name = mat.get("name", "")
        if not CARD_MATERIAL.search(name):
            continue
        before = mat.get("alphaMode", "OPAQUE")
        mat["alphaMode"] = args.mode
        if args.mode == "MASK":
            mat["alphaCutoff"] = args.cutoff
        else:
            mat.pop("alphaCutoff", None)
        touched.append(f"{name}: {before} -> {args.mode}")
    if not touched:
        raise RuntimeError("no card materials matched - nothing to do, refusing to write a copy")
    for t in touched:
        _log(t)

    new_json = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    new_json += b" " * ((4 - len(new_json) % 4) % 4)          # chunks are 4-byte aligned
    out = bytearray()
    body = bytearray()
    body += struct.pack("<II", len(new_json), JSON_CHUNK) + new_json
    for ctype, data in chunks:
        if ctype == JSON_CHUNK:
            continue
        pad = (4 - len(data) % 4) % 4
        data = data + (b"\x00" * pad)
        body += struct.pack("<II", len(data), ctype) + data
    out += struct.pack("<III", 0x46546C67, version, 12 + len(body)) + body
    Path(args.out).write_bytes(bytes(out))
    _log(f"wrote {args.out} ({len(out)/1e6:.1f} MB, {len(touched)} materials)")


if __name__ == "__main__":
    main()
