"""Refuse clips that move a bone further than the production loops ever do.

Written for telegamez#4908. The first Kimodo loops passed every other gate (clip set, units, loop
seam, 07 §3 hands) and still tore the polo collar on panda: the retarget swung clavicle_l/r ~19 deg
from the Idle pose where production loops stay under 12-15 deg, and rotated neck_02, which no
production loop moves at all. The garment is skinned for the motion range the production clips
use, so that range is the envelope.

For every bone, the envelope is the largest rotation away from the library's own Idle frame 0
reached by any REFERENCE clip, plus a margin. Each CHECKED clip must stay inside it on every bone
the garment is skinned to (GATED, the polo: pelvis, spine, collarbones, neck, head, upper arms and
their corrective/twist joints). Other bones outside the envelope (fingers, forearms, legs) are
reported but do not fail the clip: they do not move the polo, and the source pack's fingers and
feet routinely differ from the five reference loops (owner's call, 2026-10-23).

    python check_bone_envelope.py <built.glb> --reference Idle,Listening,Thinking,Focused,Speaking \
        --check BreathingIdle,Listening2,Listening3 [--margin-deg 3.0]

Reads the GLB's JSON and BIN chunks directly, like check_loop_seams.py. Exits non-zero on any
violation, naming the clip, bone, reach and envelope.
"""

import argparse
import io
import re
import json
import math
import struct
import sys


def _load(path):
    data = io.open(path, 'rb').read()
    if data[:4] != b'glTF':
        raise SystemExit(f'{path}: not a GLB')
    jlen = struct.unpack_from('<I', data, 12)[0]
    return json.loads(data[20:20 + jlen]), data[20 + jlen + 8:]


def _accessor(gltf, binc, idx):
    a = gltf['accessors'][idx]
    bv = gltf['bufferViews'][a['bufferView']]
    n = {'SCALAR': 1, 'VEC3': 3, 'VEC4': 4}[a['type']]
    off = bv.get('byteOffset', 0) + a.get('byteOffset', 0)
    vals = struct.unpack_from('<%df' % (a['count'] * n), binc, off)
    return [vals[i * n:(i + 1) * n] for i in range(a['count'])]


def _angle(a, b):
    return math.degrees(2 * math.acos(min(1.0, abs(sum(x * y for x, y in zip(a, b))))))


def rotation_tracks(path):
    gltf, binc = _load(path)
    names = {i: n.get('name', str(i)) for i, n in enumerate(gltf['nodes'])}
    clips = {}
    for anim in gltf.get('animations', []):
        for ch in anim['channels']:
            if ch['target']['path'] == 'rotation':
                q = _accessor(gltf, binc, anim['samplers'][ch['sampler']]['output'])
                clips.setdefault(anim['name'], {})[names[ch['target']['node']]] = q
    return clips


GATED = re.compile(r'^(Hips|pelvis|Spine\d*|spine_\d+.*|Neck|neck_\d+|Head|head|LeftShoulder|RightShoulder|'
                   r'clavicle.*|LeftArm|RightArm|upperarm.*)$')


def reach(track, ref):
    return max(_angle(q, ref) for q in track)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('glb')
    ap.add_argument('--reference', required=True, help='comma-separated production loops')
    ap.add_argument('--check', required=True, help='comma-separated clips to hold to the envelope')
    ap.add_argument('--margin-deg', type=float, default=3.0)
    args = ap.parse_args()

    clips = rotation_tracks(args.glb)
    if 'Idle' not in clips:
        raise SystemExit('FAIL no Idle clip: the envelope is measured from Idle frame 0')
    base = {bone: q[0] for bone, q in clips['Idle'].items()}
    refs = [c.strip() for c in args.reference.split(',') if c.strip()]
    checks = [c.strip() for c in args.check.split(',') if c.strip()]
    missing = [c for c in refs + checks if c not in clips]
    if missing:
        raise SystemExit(f'FAIL clip(s) missing from the GLB: {", ".join(missing)}')

    envelope = {}
    for c in refs:
        for bone, q in clips[c].items():
            if bone in base:
                envelope[bone] = max(envelope.get(bone, 0.0), reach(q, base[bone]))

    failures = []
    for c in checks:
        worst, info = [], []
        for bone, q in clips[c].items():
            if bone not in base:
                continue
            r = reach(q, base[bone])
            limit = envelope.get(bone, 0.0) + args.margin_deg
            if r > limit:
                (worst if GATED.match(bone) else info).append((r - limit, bone, r, limit))
        worst.sort(reverse=True)
        info.sort(reverse=True)
        note = (f'   (not gated, outside: ' + ', '.join(f'{b} +{d:.1f}' for d, b, _, _ in info[:5])
                + (f' and {len(info) - 5} more' if len(info) > 5 else '') + ')') if info else ''
        if worst:
            failures.append(c)
            print(f'  FAIL {c}: {len(worst)} bone(s) outside the envelope; worst: '
                  + '; '.join(f'{b} {r:.1f} deg > {lim:.1f}' for _, b, r, lim in worst[:4]) + note)
        else:
            print(f'  ok   {c}{note}')
    if failures:
        print(f'  bone-envelope gate FAILED for {", ".join(failures)} '
              f'(reference {", ".join(refs)}, margin {args.margin_deg:.1f} deg)')
        sys.exit(1)


if __name__ == '__main__':
    main()
