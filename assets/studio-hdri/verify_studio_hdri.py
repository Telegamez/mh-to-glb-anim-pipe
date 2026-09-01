"""
Acceptance check for the authored studio HDRI (#3660).

Measures the rendered equirectangular file instead of trusting the scene that produced it:
finds the bright regions, works out which panel each one is, and reports the measured
positions, angular sizes and intensity ratios against studio-lighting.json.

    python verify_studio_hdri.py out/companion-studio-v1.hdr \
        --lighting-json <path to studio-lighting.json>

What it checks, and why each one is worth checking:

  panels present     all five, including `catchlight` — constraint 2 says it is small, dim
                     and load-bearing, which is exactly the profile of something that gets
                     silently lost in a render.
  elevation          measured against spec. Independent per panel, so it also identifies
                     which blob is which without relying on any orientation assumption.
  azimuth mapping    SOLVED from the data rather than assumed. The file's longitude
                     convention is whatever Blender's panoramic camera produced; this
                     reports it explicitly so the import step is written against a
                     measured fact.
  intensity ratios   the spec is emphatic that the ratios ARE the look and absolute
                     brightness is not. Measured relative to the key.
  headroom           values above 1.0 must survive — constraint 5, do not clamp.
  dark surround      constraint 1. The median of the whole sphere should sit far below
                     the key.
"""

import argparse
import json
import math
import os
import sys

import cv2
import numpy as np

EMBEDDED_PANELS = [
    {"id": "key",        "azimuthDeg":  -50.0, "elevationDeg":  33.0, "angularSizeDeg": 30.0, "intensity": 1.00},
    {"id": "fill",       "azimuthDeg":   46.0, "elevationDeg":  -4.0, "angularSizeDeg": 66.0, "intensity": 0.03},
    {"id": "rim",        "azimuthDeg":  156.0, "elevationDeg":  14.0, "angularSizeDeg": 20.0, "intensity": 1.80},
    {"id": "hair",       "azimuthDeg": -150.0, "elevationDeg":  48.0, "angularSizeDeg": 30.0, "intensity": 0.70},
    {"id": "catchlight", "azimuthDeg":   -7.0, "elevationDeg":  11.0, "angularSizeDeg": 12.0, "intensity": 0.18},
]


def load_hdr(path):
    img = cv2.imread(path, cv2.IMREAD_ANYDEPTH | cv2.IMREAD_ANYCOLOR)
    if img is None:
        raise SystemExit(f"could not read {path}")
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float64)


def luminance(rgb):
    return rgb @ np.array([0.2126, 0.7152, 0.0722])


def srgb_hex_to_linear(h):
    def f(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    h = h.lstrip("#")
    return [f(int(h[i:i + 2], 16) / 255.0) for i in (0, 2, 4)]


def find_blobs(lum, floor):
    """Connected bright regions, brightest first."""
    h, w = lum.shape
    mask = (lum > floor).astype(np.uint8)
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = []
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < 12:                       # denoiser speckle, not a panel
            continue
        sel = labels == i
        # Solid angle: each pixel covers (2pi/w)(pi/h)cos(elevation) steradians.
        rows = np.nonzero(sel)[0]
        el = (np.pi / 2) - np.pi * (rows + 0.5) / h
        omega = float((2 * np.pi / w) * (np.pi / h) * np.cos(el).sum())
        blobs.append({
            "omega": omega,
            "cx": centroids[i][0], "cy": centroids[i][1], "area": int(area),
            "mean": float(lum[sel].mean()), "peak": float(lum[sel].max()),
            "w": int(stats[i, cv2.CC_STAT_WIDTH]), "h": int(stats[i, cv2.CC_STAT_HEIGHT]),
        })
    return sorted(blobs, key=lambda b: -b["mean"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--lighting-json", default=None)
    args = ap.parse_args()

    panels = EMBEDDED_PANELS
    if args.lighting_json and os.path.exists(args.lighting_json):
        with open(args.lighting_json, "r", encoding="utf-8") as fh:
            panels = json.load(fh)["panels"]

    rgb = load_hdr(args.image)
    h, w = rgb.shape[:2]
    lum = luminance(rgb)

    print(f"file            {args.image}")
    print(f"resolution      {w} x {h}   aspect {w / h:.2f} (equirect wants 2.00)")
    print(f"value range     min {lum.min():.5f}   median {np.median(lum):.5f}   max {lum.max():.2f}")

    # Constraint 5 — the headroom above 1.0 is what the specular response uses.
    above1 = float((lum > 1.0).mean() * 100)
    print(f"headroom        {above1:.2f}% of pixels above 1.0   "
          f"{'OK - not clamped' if lum.max() > 1.0 else 'FAIL - clamped or tone-mapped'}")

    # Constraint 1 — the surround has to stay dark.
    print(f"surround        median/peak = {np.median(lum) / lum.max():.5f}  "
          f"({'dark' if np.median(lum) / lum.max() < 0.01 else 'TOO BRIGHT'})")

    blobs = find_blobs(lum, floor=max(np.median(lum) * 20, lum.max() * 0.002))
    print(f"\nbright regions found: {len(blobs)}")

    # Elevation comes straight out of the row and needs no longitude convention, so it is
    # what identifies each blob. The five spec elevations are all distinct.
    def elevation_of(cy):
        return 90.0 - 180.0 * (cy + 0.5) / h

    matched = {}
    for p in sorted(panels, key=lambda x: -x["intensity"]):
        best, err = None, 1e9
        for b in blobs:
            if id(b) in [id(v) for v in matched.values()]:
                continue
            e = abs(elevation_of(b["cy"]) - p["elevationDeg"])
            if e < err:
                best, err = b, e
        if best is not None and err < 12.0:
            matched[p["id"]] = best

    # A panel's expected LUMINANCE ratio is not its `intensity`. Intensity is a radiometric
    # scalar and the panels are different colours, so a rim at 1.8 in a cool blue does not
    # measure 1.8x a warm key. Compare against intensity * relative luminance of the colour,
    # or every run looks like it has drifted when it has not.
    def expected_lum(p):
        return p["intensity"] * luminance(np.array(srgb_hex_to_linear(p["color"])))

    key_spec = next(p for p in panels if p["id"] == "key")
    key_blob = matched.get("key")

    print(f"\n{'panel':<12} {'spec el':>8} {'meas el':>8} {'spec size':>10} {'meas size':>10} "
          f"{'want ratio':>11} {'meas ratio':>11} {'err':>7}")
    for p in panels:
        b = matched.get(p["id"])
        if b is None:
            print(f"{p['id']:<12}   MISSING - panel not found in the render")
            continue
        # Solid angle of the blob, so the comparison survives equirect's latitude stretch:
        # a bounding-box width is 1/cos(elevation) too wide for anything off the horizon.
        # Square-equivalent angular width, to match how angularSizeDeg is defined.
        omega = b["omega"]
        meas_size = math.degrees(math.sqrt(omega))
        want = expected_lum(p) / expected_lum(key_spec)
        got = b["mean"] / key_blob["mean"] if key_blob else float("nan")
        err = abs(got - want) / want * 100 if want else 0.0
        print(f"{p['id']:<12} {p['elevationDeg']:>8.1f} {elevation_of(b['cy']):>8.1f} "
              f"{p['angularSizeDeg']:>10.1f} {meas_size:>10.1f} "
              f"{want:>11.3f} {got:>11.3f} {err:>6.1f}%")

    # Solve the longitude convention from the matched panels rather than assuming one.
    print("\nazimuth mapping (solved from the data, not assumed):")
    for sign, label in ((1.0, "u = 0.5 + az/360"), (-1.0, "u = 0.5 - az/360")):
        offs = []
        for pid, b in matched.items():
            az = next(p["azimuthDeg"] for p in panels if p["id"] == pid)
            u = (b["cx"] + 0.5) / w
            offs.append(((u - sign * az / 360.0) % 1.0))
        # circular spread of the implied offset; a real convention gives one tight cluster
        ang = np.array(offs) * 2 * math.pi
        r = math.hypot(np.cos(ang).mean(), np.sin(ang).mean())
        centre = (math.atan2(np.sin(ang).mean(), np.cos(ang).mean()) / (2 * math.pi)) % 1.0
        print(f"  {label:<20} consistency {r:.4f}   offset u0 = {centre:.4f}"
              f"{'   <-- this is the file convention' if r > 0.99 else ''}")

    missing = [p["id"] for p in panels if p["id"] not in matched]
    print()
    if missing:
        print(f"RESULT: FAIL — panels not found: {', '.join(missing)}")
        sys.exit(1)
    print("RESULT: all five panels present and located")


if __name__ == "__main__":
    main()
