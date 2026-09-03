"""Stage 03 / 5.7 native-glb - headless GLB export.

Invoked as:
    blender --background <char>.blend --python export_glb.py -- \
        --char <id> --workspace <abs>

Pipeline-specific notes vs 5.6/cinematic:
  * Stage 02 already wired hair / eyebrow / lash materials manually
    (sidecar atlases + synthesized hair color + alpha clip), so we
    don't need an mh_materials.json sidecar to flow through to a
    three.js viewer. The viewer in stage 04 is Bevy/WASM and just
    consumes the .glb directly.
  * Hair-card materials carry a Multiply node feeding Base Color
    (synth color * root-darkening ramp from Attribute.B). Blender's
    glTF exporter only writes a constant Base Color factor for
    materials whose BC graph isn't a direct Image Texture, so the
    ramp gets silently dropped. We pre-bake those small Multiply
    chains to a single PNG per material and rewire BC to the baked
    image before export.

Inputs:  characters/<id>/02-blend/<id>.blend (opened by Blender)
         _config/pipeline.yaml -> glb_constraints.{max_texture_px, draco_compression}
Outputs: characters/<id>/03-glb/<id>.glb
         characters/<id>/03-glb/glb_manifest.json
         Updates characters/<id>/manifest.json (stages.03_glb_export)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

import bpy


def _log(msg):
    print(f"[stage03-glb] {msg}", flush=True)


def _parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    p.add_argument("--workspace", required=True)
    return p.parse_args(argv)


def _iso_now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_glb_constraints(workspace):
    """Tiny flat YAML reader for the `glb_constraints:` block."""
    cfg = workspace / "_config" / "pipeline.yaml"
    # `max_texture_px` governs images the exporter EMBEDS. Sidecar textures are written to
    # disk beside the model and bypassed it entirely, which is how clothing shipped at
    # 4096x4096 while every embedded map was capped at 1024 -- a 16x inconsistency nobody
    # chose. So the sidecar cap INHERITS max_texture_px unless set explicitly; set
    # `sidecar_texture_px: 0` to opt back out and ship them at source resolution.
    out = {"max_texture_px": 1024, "draco_compression": True,
           "sidecar_texture_px": None, "sidecar_drop_unused_alpha": True,
           "target_tri_budget": 60000}
    if not cfg.exists():
        return out
    in_block = False
    for raw in cfg.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_block = line.strip().startswith("glb_constraints:")
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.split("#", 1)[0].strip().strip('"')
        if val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
        else:
            try:
                out[key] = int(val)
            except ValueError:
                out[key] = val
    return out


def _delete_hidden_meshes():
    """Drop meshes hidden from viewport/render so they don't get exported."""
    removed = []
    for obj in list(bpy.data.objects):
        if obj.type != "MESH":
            continue
        if obj.hide_viewport or obj.hide_render:
            removed.append(obj.name)
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.outliner.orphans_purge(do_recursive=True)
    return removed


# MetaHuman normal maps need DX -> GL conversion (+Y down -> +Y up) for
# glTF spec compliance, but only some of them. Empirically:
#
#   * MI_Face_Skin / MI_Body_Baked / MI_Eye*_Baked normals come through
#     UE's GLTFExporter ALREADY in OpenGL convention (the MH plugin's
#     bake handles the flip during export). Re-flipping here would
#     invert pores and wrinkles in browsers.
#   * Outfit normals (MID_M_DG_*) keep DirectX convention, so they DO
#     need flipping for spec-compliant rendering.
#
# Hair Attribute / Coverage atlases are NOT normal maps even though
# their names contain words like "Tangent" - skip them.
_NORMAL_HINTS = ("_n.png", "_n.tga", "_normal.png", "_normal.tga",
                 "_normal_map", "_normal_")
_NORMAL_SKIP = ("attribute", "coverage", "rootuvseed",
                "cardsatlas_tangent",  # tangent direction map, not normal
                "t_flatnormal", "t_skinmicronormal",
                # MH-baked skin/eye/body normals are already +Y up
                "mi_face_skin", "mi_body_baked",
                "mi_eyel_baked", "mi_eyer_baked")


def _is_normal_image(img):
    name = (img.name or "").lower()
    if any(s in name for s in _NORMAL_SKIP):
        return False
    if any(h in name for h in _NORMAL_HINTS):
        return True
    return name.endswith("_normal") or "_normal_" in name


def _flip_g_inplace(img):
    import numpy as np
    if img.size[0] == 0 or img.size[1] == 0:
        return False
    w, h = img.size[0], img.size[1]
    buf = np.empty(w * h * 4, dtype=np.float32)
    try:
        img.pixels.foreach_get(buf)
    except Exception as e:
        _log(f"  G-flip foreach_get failed for {img.name}: {e}")
        return False
    buf[1::4] = 1.0 - buf[1::4]
    img.pixels.foreach_set(buf)
    img.update()
    return True


def _flip_normal_maps_g():
    touched = 0
    for img in bpy.data.images:
        if not _is_normal_image(img):
            continue
        if _flip_g_inplace(img):
            _log(f"  normal G-flip: {img.name} ({img.size[0]}x{img.size[1]})")
            touched += 1
    return touched


def _per_image_cap(name, default_cap):
    n = name.lower()
    # Teeth fits ~2% of screen; 256 is plenty.
    if n.startswith("t_teeth"):
        return 256
    return default_cap


def _sidecar_cap(constraints):
    """Sidecar cap, inheriting max_texture_px when unset. Explicit 0 disables it."""
    v = constraints.get("sidecar_texture_px", None)
    if v is None:
        return int(constraints.get("max_texture_px", 1024))
    return int(v)


def _alpha_is_constant(img):
    """True when every alpha sample is identical, i.e. the channel carries nothing.

    Checked on the FULL image before any scaling. Checking afterwards would be cheaper and
    wrong: a handful of dissenting pixels can be averaged away by the downsample, and the
    channel would then be dropped on the strength of information the resize destroyed.
    """
    import numpy as np
    n = img.size[0] * img.size[1] * img.channels
    if img.channels < 4 or n == 0:
        return False
    buf = np.empty(n, dtype=np.float32)
    img.pixels.foreach_get(buf)
    a = buf[3::4]
    return bool(a.min() == a.max())


def _write_sidecar_png(img, dst, drop_alpha):
    """Write a PNG with exact channel control and no colour management.

    `save_render` is the only path that can force RGB rather than RGBA, and it applies the
    view transform — so that is pinned to Raw. These are normal, ORM and alpha-atlas maps:
    non-colour data, where a tone map would corrupt every value.
    """
    scene = bpy.context.scene
    s = scene.render.image_settings
    prev = (s.file_format, s.color_mode, s.color_depth, s.color_management)
    try:
        s.file_format = "PNG"
        s.color_mode = "RGB" if drop_alpha else "RGBA"
        s.color_depth = "8"
        s.color_management = "OVERRIDE"
        s.view_settings.view_transform = "Raw"
        s.view_settings.look = "None"
        s.view_settings.exposure = 0.0
        s.view_settings.gamma = 1.0
        img.save_render(filepath=str(dst), scene=scene)
    finally:
        (s.file_format, s.color_mode, s.color_depth, s.color_management) = prev


def _process_sidecar_texture(src, dst, cap, drop_alpha_allowed):
    """Downsample and de-alpha one sidecar texture. Returns a short note for the log.

    Sidecar textures bypass `max_texture_px` entirely — that constraint governs images the
    glTF exporter embeds, and these are copied to disk beside the model. They arrive from
    Unreal at 4096x4096 RGBA, so one companion's clothing is ~156 MB of PNG before anything
    downstream sees it, and every one of them carries a constant, unread alpha channel.
    """
    img = bpy.data.images.load(str(src))
    try:
        w, h = img.size[0], img.size[1]
        # Decided before scaling; see _alpha_is_constant.
        drop_alpha = bool(drop_alpha_allowed) and _alpha_is_constant(img)
        note = []
        if cap and max(w, h) > cap:
            factor = cap / max(w, h)
            nw, nh = max(1, int(w * factor)), max(1, int(h * factor))
            img.scale(nw, nh)
            note.append(f"{w}x{h} -> {nw}x{nh}")
        if drop_alpha:
            note.append("RGBA -> RGB")
        _write_sidecar_png(img, dst, drop_alpha)
        return ", ".join(note) if note else "unchanged"
    finally:
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass


def _downsample_images(max_px):
    """Scale every image > its cap down (preserving aspect). After
    scaling, repack the image so the glTF exporter uses the in-memory
    pixel buffer rather than re-reading the un-downsampled PNG from
    `filepath_raw`."""
    touched = 0
    for img in bpy.data.images:
        if img.size[0] == 0 or img.size[1] == 0:
            continue
        w, h = img.size[0], img.size[1]
        biggest = max(w, h)
        cap = _per_image_cap(img.name, max_px)
        if biggest <= cap:
            continue
        factor = cap / biggest
        new_w = max(1, int(w * factor))
        new_h = max(1, int(h * factor))
        _log(f"  downsample {img.name}: {w}x{h} -> {new_w}x{new_h} (cap={cap})")
        img.scale(new_w, new_h)
        # Detach from disk source so the glTF exporter can't fall back
        # to the original-size file. Pack the (now smaller) pixel
        # buffer into the .blend so it becomes the source of truth.
        try:
            img.filepath_raw = ""
            img.filepath = ""
            img.pack()
        except Exception as e:
            _log(f"    pack {img.name} failed: {e}")
        touched += 1
    return touched


def _flatten_card_basecolor(out_dir, max_px):
    """Pre-bake Multiply-node chains feeding Base Color into a single
    PNG per hair-card / eyebrow material so the glTF exporter ships
    the actual color we set up in stage 02 (synth tone with root
    darkening) instead of dropping back to a constant.

    For each *_CardMat material whose Base Color isn't a direct Image
    Texture node, we:
      1. Create a new sRGB image at the per-image cap size.
      2. Set Cycles bake target to that image.
      3. Bake the diffuse-color pass which evaluates the BC graph.
      4. Rewire BC <- new baked image. The Alpha link from the
         Attribute texture stays untouched.

    Returns the list of materials that got flattened."""
    flattened = []

    scene = bpy.context.scene
    prev_engine = scene.render.engine
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.render.bake.use_pass_direct = False
    scene.render.bake.use_pass_indirect = False
    scene.render.bake.use_pass_color = True
    scene.render.bake.margin = 4

    bake_dir = out_dir / "_baked"
    bake_dir.mkdir(parents=True, exist_ok=True)

    try:
        for mat in list(bpy.data.materials):
            if not mat.use_nodes or mat.users <= 0:
                continue
            nm = mat.name.lower()
            if not nm.endswith("_cardmat"):
                continue
            nt = mat.node_tree
            bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if bsdf is None:
                continue
            bc_input = bsdf.inputs.get("Base Color")
            if not bc_input or not bc_input.is_linked:
                continue
            src_node = bc_input.links[0].from_node
            if src_node.type == "TEX_IMAGE":
                continue  # already a direct texture; glTF handles it

            host_obj = None
            for obj in bpy.data.objects:
                if obj.type != "MESH":
                    continue
                if any(slot.material is mat for slot in obj.material_slots):
                    host_obj = obj
                    break
            if host_obj is None:
                _log(f"  flatten {mat.name}: no host mesh found, skipping")
                continue

            cap = _per_image_cap(mat.name, max_px)
            img_name = f"{mat.name}_BC_baked"
            img = bpy.data.images.new(img_name, width=cap, height=cap, alpha=False)
            img.colorspace_settings.name = "sRGB"

            target_node = nt.nodes.new("ShaderNodeTexImage")
            target_node.image = img
            target_node.location = (1000, -300)
            for n in nt.nodes:
                n.select = False
            target_node.select = True
            nt.nodes.active = target_node

            bpy.ops.object.select_all(action="DESELECT")
            host_obj.select_set(True)
            bpy.context.view_layer.objects.active = host_obj

            try:
                bpy.ops.object.bake(type="DIFFUSE", use_clear=True, margin=4)
            except Exception as e:
                _log(f"  flatten bake {mat.name} failed: {e}")
                nt.nodes.remove(target_node)
                continue

            try:
                img.filepath_raw = str(bake_dir / f"{img_name}.png")
                img.file_format = "PNG"
                img.save()
                img.pack()
            except Exception as e:
                _log(f"  flatten save {img_name} failed: {e}")

            for link in list(bc_input.links):
                nt.links.remove(link)
            nt.links.new(target_node.outputs["Color"], bc_input)

            _log(f"  flattened {mat.name} BC -> {img_name} ({cap}x{cap})")
            flattened.append(mat.name)
    finally:
        scene.render.engine = prev_engine

    return flattened


def _count_tris():
    total = 0
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        me = obj.data
        me.calc_loop_triangles()
        total += len(me.loop_triangles)
    return total


def _current_max_image_px():
    m = 0
    for img in bpy.data.images:
        if img.size[0] == 0:
            continue
        m = max(m, img.size[0], img.size[1])
    return m


def _emit_sidecar_for_viewer(char_dir, out_dir, sidecar_cap=0, drop_alpha=True):
    """Forward stage 02's mh_materials.json + the alpha textures it
    references from 01-glb/textures/ into 03-glb/. Stage 04's
    build_site.py copies whatever's in 03-glb/{mh_materials.json,
    textures/*} into docs/characters/<id>/, where viewer.js fetches
    them to drive the hair / lash shader injection."""
    import shutil
    src_map = char_dir / "02-blend" / "mh_materials.json"
    if not src_map.exists():
        _log("no 02-blend/mh_materials.json - skipping viewer sidecar")
        return
    dst_map = out_dir / "mh_materials.json"
    shutil.copy2(src_map, dst_map)

    # Walk the mapping, collect every alpha_stem + textures.alpha ref,
    # copy each from 01-glb/textures/ into 03-glb/textures/.
    mapping = json.loads(src_map.read_text(encoding="utf-8"))
    src_tex_dir = char_dir / "01-glb" / "textures"
    dst_tex_dir = out_dir / "textures"
    dst_tex_dir.mkdir(parents=True, exist_ok=True)
    needed = set()
    # Stems named by alpha_stem/tangent_stem drive the hair and lash shaders -- the alpha
    # atlas IS the hair silhouette, and card alpha is exactly where a downsample shows as
    # chewed edges. They are also the small ones (~8 MB against ~156 MB of clothing), so
    # capping them buys almost nothing and risks the thing most likely to be noticed.
    # Everything else is clothing BC/N/ORM, which is the actual weight.
    shader_critical = set()
    for m in mapping.get("materials", []):
        p = m.get("params") or {}
        t = m.get("textures") or {}
        if p.get("alpha_stem"):
            shader_critical.add(p["alpha_stem"])
        if p.get("tangent_stem"):
            shader_critical.add(p["tangent_stem"])
        # An `alpha` entry in the textures map is the same class of thing: the eyelash
        # coverage map. Thin alpha structures are precisely what a downsample chews, and it
        # is half a megabyte, so capping it risks a visible eyelash for no meaningful byte.
        alpha_ref = (t or {}).get("alpha")
        if isinstance(alpha_ref, str):
            shader_critical.add(os.path.splitext(os.path.basename(alpha_ref))[0])
    for m in mapping.get("materials", []):
        p = m.get("params") or {}
        t = m.get("textures") or {}
        if p.get("alpha_stem"):
            needed.add(p["alpha_stem"])
        # TELEGAMEZ: the strand-tangent atlas. Declared by stage 02; also
        # derived here by convention so a sidecar written before that change
        # still forwards it rather than silently dropping anisotropy.
        if p.get("tangent_stem"):
            needed.add(p["tangent_stem"])
        elif isinstance(p.get("alpha_stem"), str) and p["alpha_stem"].endswith("_Attribute"):
            needed.add(p["alpha_stem"].replace("_Attribute", "_Tangent"))
        for ref in t.values():
            if isinstance(ref, str) and "/" in ref:
                # "textures/foo.png" -> "foo"
                stem = os.path.splitext(os.path.basename(ref))[0]
                needed.add(stem)
            elif isinstance(ref, str):
                stem = os.path.splitext(ref)[0]
                needed.add(stem)
    copied = 0
    before_bytes = 0
    after_bytes = 0
    for stem in sorted(needed):
        src = src_tex_dir / f"{stem}.png"
        if not src.exists():
            _log(f"  sidecar miss: {src}")
            continue
        dst = dst_tex_dir / f"{stem}.png"
        before_bytes += src.stat().st_size
        cap = 0 if stem in shader_critical else int(sidecar_cap or 0)
        try:
            note = _process_sidecar_texture(src, dst, cap, drop_alpha)
        except Exception as e:
            # A texture that fails to process must still ship, or the character publishes
            # with a missing map and renders like a bad bake. Fall back to the raw copy and
            # say so rather than dropping it.
            _log(f"  sidecar process failed for {stem} ({e}); copying verbatim")
            shutil.copy2(src, dst)
            note = "copied verbatim"
        after_bytes += dst.stat().st_size
        _log(f"  sidecar {stem}: {note}")
        copied += 1
    _log(f"sidecar: {dst_map.name} + {copied} texture(s) -> {out_dir}"
         f"  [{before_bytes / 1048576:.1f} MB -> {after_bytes / 1048576:.1f} MB]")


def _propagate_mesh_target_names_to_primitives(glb_path):
    """Blender's glTF exporter writes morph target names at the mesh
    level (`mesh.extras.targetNames`), but three.js's GLTFLoader only
    reads them at the primitive level (`primitive.extras.targetNames`).
    Without this propagation, every imported mesh comes in with an
    empty `morphTargetDictionary` even though the morph data is
    correctly serialized - the blendshape panel ends up with zero
    sliders because there are no names to key off of.

    Rewrite the GLB chunk in place: parse JSON header, copy
    `mesh.extras.targetNames` into each `primitive.extras.targetNames`
    on that mesh, repack."""
    import io, struct
    with open(glb_path, "rb") as f:
        magic, version, length = struct.unpack("<III", f.read(12))
        json_len, json_type = struct.unpack("<II", f.read(8))
        json_bytes = f.read(json_len)
        bin_chunk = f.read()  # everything after the JSON chunk

    gltf = json.loads(json_bytes)
    propagated = 0
    for mesh in gltf.get("meshes", []):
        names = mesh.get("extras", {}).get("targetNames")
        if not names:
            continue
        for prim in mesh.get("primitives", []):
            if "targets" not in prim:
                continue
            extras = prim.setdefault("extras", {})
            if extras.get("targetNames") == names:
                continue
            extras["targetNames"] = names
            propagated += 1

    new_json = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    # JSON chunk must be 4-byte aligned, padded with spaces.
    pad = (4 - len(new_json) % 4) % 4
    new_json += b" " * pad
    new_bin = bin_chunk  # bin chunk header is included; its 4-byte
                         # alignment is already correct from Blender.
    new_length = 12 + 8 + len(new_json) + len(new_bin)
    with open(glb_path, "wb") as f:
        f.write(struct.pack("<III", magic, version, new_length))
        f.write(struct.pack("<II", len(new_json), json_type))
        f.write(new_json)
        f.write(new_bin)
    _log(f"propagated targetNames onto {propagated} primitive(s)")


def _update_char_manifest(char_dir, status, errors):
    path = char_dir / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    stage = data.setdefault("stages", {}).setdefault("03_glb_export", {})
    now = _iso_now()
    if status == "done":
        stage["started_at"] = stage.get("started_at") or now
        stage["completed_at"] = now
    else:
        stage["started_at"] = stage.get("started_at") or now
        stage["completed_at"] = None
    stage["status"] = status
    stage["errors"] = errors
    stage.setdefault("output_dir", "03-glb/")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _vertex_colour_stats():
    """Summarise the active colour attribute per mesh, before export.

    Recorded so the vertex-colour path can be checked by artifact rather than
    by trust. Stage 02's scalp darkening reported a healthy count for months
    while every shipped GLB carried a fake all-white COLOR_0 (see the
    export_vertex_color note below); the two facts never met, because nothing
    wrote down what was supposed to come out.

    `darkened` is the fraction below 0.95. A skin mesh reporting 0.0% means the
    darkening did not happen — that is the number to look at.
    """
    out = {}
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        ca = getattr(obj.data, "color_attributes", None)
        if not ca or len(ca) == 0:
            continue
        layer = ca.active_color
        if layer is None or len(layer.data) == 0:
            continue
        n = len(layer.data)
        step = max(1, n // 20000)      # sample: these meshes run to 144k loops
        lo, tot, dark, cnt = 1.0, 0.0, 0, 0
        for i in range(0, n, step):
            c = layer.data[i].color
            lum = (c[0] + c[1] + c[2]) / 3.0
            lo = min(lo, lum)
            tot += lum
            if lum < 0.95:
                dark += 1
            cnt += 1
        out[obj.name] = {
            "layer": layer.name,
            "domain": layer.domain,
            "min": round(lo, 4),
            "mean": round(tot / cnt, 4),
            "darkened_frac": round(dark / cnt, 4),
        }
        _log(f"  vcol: {obj.name} '{layer.name}' min={lo:.3f} "
             f"mean={tot/cnt:.3f} darkened={100.0*dark/cnt:.1f}%")
    if not out:
        # Log the empty case. An absent colour attribute and an unchecked one
        # look identical in silence, and that is how this was missed.
        _log("  vcol: no mesh carries an active colour attribute")
    return out


# Weight smoothing is applied to every skinned mesh EXCEPT these.
#
# The exclusions are the facial group: their shapes come from morph targets, their features are
# a millimetre or two across, and smoothing across a lid, lip or lash boundary would blur
# articulation that nothing else restores. Everything else — the garments AND the body — wants
# smoothing.
#
# The body was excluded on the first pass because its MEDIAN edge stretch measured 1.00, which
# looked healthy. The median was hiding it: ~900 individual triangles were still blowing up to
# 10-16x, all of them at the elbows, wrists and finger joints. That is exactly where the twist
# and bulge correctives (`lowerarm_twist_*`, `upperarm_twistCor_*`, `wrist_inner_*`, `*_bulge_*`)
# used to carry the deformation gradient; folding them into `LeftForeArm`/`LeftArm` collapses a
# smooth multi-ring falloff into a two-step one, and a two-step gradient shreds the moment the
# elbow passes ~90 degrees. It showed as torn, faceted forearms during Shrug, and as the arm
# pushing through the sleeve hem. Never judge this defect by a median.
NO_SMOOTH_MESH = re.compile(
    r"head|eye|lash|lacrimal|teeth|tongue|hide|cartilage|saliva|brow|beard|hair|card",
    re.IGNORECASE,
)
# An influence is implausible when its bone is more than this many times farther from the
# vertex than the nearest influencing bone. 2.5 drops chest-on-arm and waist-on-thigh while
# keeping sleeve-on-arm and hem-on-hip.
# Cloth is not attached to the skull. A collar weighted to `Head` is dragged by every head
# turn, which is what crumpled and flared the polo collar: the collar band measured 11% `Head`
# plus 17% `neck_02`, and at a 42-degree head turn it blew out to +33% wider than bind. Garment
# influence on `Head` is remapped to `Neck`, which is where a collar actually rides. Facial
# bones get the same treatment for the same reason. Body and face meshes are untouched.
GARMENT_MESH = re.compile(r"outfit|cloth|shirt|chino|trouser|pant|skirt|dress|jacket|boot|shoe",
                          re.IGNORECASE)
GARMENT_BONE_REMAP = {"Head": "Neck"}
# TELEGAMEZ (telegamez#3725): a garment vertex carrying at least this share of its bone weight
# on Head belongs to a rigid head piece (a helmet) and keeps Head; the remap is for collars.
RIGID_HEAD_SHARE = 0.75
RIGID_HEAD_MESH_FRACTION = 0.05     # ...on a mesh where at least this share of vertices is Head-dominated
# Skin meshes whose covered faces get removed.
#
# Head_Mesh is in here deliberately and it is not obvious: MetaHuman's head mesh does not stop at
# the jaw, it runs down through the neck into the upper chest and back (Telly's spans y 1.41 to
# 1.80, where the collar tops out around 1.57). That torso portion sits under the shirt and pokes
# through it, and because its UVs fall outside the face texture it renders GREY rather than skin
# — which is what the grey patch on the shoulder blade was. The opening margin is what keeps the
# visible neck: containment keeps it, because a ray cast outward from the neck above the collar
# escapes without hitting cloth.
SKIN_MESH = re.compile(r"^(Body|Head)_Mesh", re.IGNORECASE)
GARMENT_FORBIDDEN_BONE = re.compile(r"^(FACIAL_|jaw|tongue|teeth)", re.IGNORECASE)

# Which weight compensations run. Set MH_WEIGHT_COMP to A/B them:
#   full  fold + far-bone filter + smoothing   (shipped)
#   fold  fold only
#   none  raw MetaHuman weights, top-4 by the exporter
#
# All three exist to survive top-4 truncation, which was later measured against Unreal at a
# median of 0.00mm - so they may be solving a problem that is not there, at the cost of
# reshaping the garment's weight map (342 groups -> 87, influences dropped on ~11k verts).
WEIGHT_COMPENSATION = os.environ.get("MH_WEIGHT_COMP", "full").strip().lower()

IMPLAUSIBLE_DISTANCE_RATIO = 2.5
WEIGHT_SMOOTH_FACTOR = 0.5
WEIGHT_SMOOTH_REPEAT = 4
# Smoothing is ADAPTIVE: a vertex is only smoothed in proportion to how much its weight map
# disagrees with its neighbours. Below D0 it is left completely alone.
#
# Uniform smoothing was doing real damage. Linear-blend skinning loses volume whenever a vertex
# is shared between bones that rotate relative to each other, so spreading every vertex across
# more bones pinches the wrist (forearm vs hand) and the waist (the spine chain). It showed up
# as a monotonic slide in compression as passes went up - 428 triangles under half their bind
# length at 0 passes, 593 at 4, 741 at 10 - while the tearing number kept improving, which is
# how a one-sided metric talks you into wrecking a silhouette.
#
# The fold's damage is localised: hard steps where two neighbouring vertices aliased onto
# structurally distant bones. Coherent regions - the wrist gradient, the waist - were never
# broken and must not be touched. Reference disagreement medians: Head_Mesh 0.10 (healthy),
# Body_Mesh 0.26, outfit 0.44.
WEIGHT_SMOOTH_ADAPTIVE = False
WEIGHT_SMOOTH_D0 = 0.12
WEIGHT_SMOOTH_D1 = 0.40

# MEASURED TRADE-OFF - read this before changing any constant above.
#
# Tearing and compression move in OPPOSITE directions and no setting wins both, because the
# ~1130 corrective and twist joints that Unreal uses to prevent both artifacts are frozen here
# (6.14). Telly, Listening, peak over the clip; "tears" = triangles stretched over 3x, "squash"
# = triangles compressed under 0.5:
#
#   config                    shirt tears / worst    shirt squash   body tears   body squash
#   fold only (no smoothing)      4792 / 171x             428          1106          279
#   uniform x10                   1246 /  99x             741             0            -
#   uniform x4 + filter  <=       1505 /  49x             593            58           96
#   adaptive x6 (D0 0.25)         2952 / 150x             407            80          183
#   adaptive x8 (D0 0.12)         2033 /  87x             519            16          181
#
# Uniform x4 is shipped: lowest WORST-case tear (49x, the visible gashes) and lowest body
# compression (96, the wrist pinch). The adaptive path is kept because it is the better tool
# once the twist joints are driven - see the note in 6.16 about the real fix.


# How far to look for cloth along the skin's own normal. Generous: a loose shirt on a heavy
# build can sit several centimetres off the chest, and anything shorter leaves islands.
RAY_REACH = 0.25
# How far out through the fabric a piece of skin may have pushed and still be pulled back.
#
# This was 0.04 and that silently re-broke the fix: a heavy build pushes through the shirt by
# more than 4cm in places, those vertices were marked covered and then never moved, and the
# large chest patches came straight back. It has to cover the worst real poke-through without
# reaching the far side of the garment - a torso is ~25cm front to back, so 8cm is safe.
POKE_REACH = 0.08
EROSION_RINGS = 2
MAX_BODY_REMOVAL = 0.85
# A vertex whose nearest cloth is this close to a garment OPENING is not poking through it - it
# is simply outside the opening, like a chin in front of a collar or a forearm below a cuff.
# Without this veto the chin gets dragged back to the collar and the face deforms: Head_Mesh was
# tucking 4545 verts at a median "poke" of 51.6mm, which is not poke-through, it is a face.
OPENING_VETO = 0.02
# TELEGAMEZ (telegamez#3725): the veto above was written for collars and cuffs, where the
# nearest cloth to a chin or a forearm is the opening itself. It also swallows every small
# rigid piece: a helmet cheek guard or a sandal strap is a few centimetres across, so ALL of
# it is within 20mm of a rim and skin poking 2-3mm through the middle of the plate was never
# tucked (Tommy's cheeks showed through the helmet). The tuck therefore uses a RELATIVE veto:
# skin is "in front of an opening" only when the rim is closer than a few times its own poke.
# A chin 50mm in front of a collar rim (rim distance ~0) stays vetoed; a cheek 3mm through a
# plate whose rim is 15mm away is tucked. Deletion keeps the absolute veto unchanged - the
# holes it guards against were opened by deleting, not by tucking.
OPENING_VETO_FLOOR = 0.005
OPENING_VETO_RATIO = 3.0
# Skin that has pushed through the fabric is moved this far behind it rather than deleted, so
# the surviving fringe of a partly-covered face is hidden instead of leaving a gap.
TUCK_DEPTH = 0.004



def _hide_body_under_clothing():
    """Delete body faces that sit under a garment, so skin cannot poke through it.

    ## The measurement that justifies this

    Every "skin showing through the shirt" report is interpenetration, not torn cloth. Hiding
    `Body_Mesh` in the harness leaves the garment completely intact - no holes, clean collar,
    clean silhouette - and the skin patches are present with body animation OFF, at rest. So the
    body surface simply sits outside the garment surface in those regions, worst on the heaviest
    build (Tele2). Nothing about weights, rig logic or the 4-influence limit touches it.

    ## Why nothing is deleted

    The first version removed covered faces, which is what games normally do, and it produced
    holes: the underside of the jaw seen from below, transparent slices in the underarm during a
    Shrug. Both are the same flaw. Coverage is computed at BIND pose, but coverage CHANGES with
    pose - raise an arm and the armpit that was under the sleeve is suddenly on display. Deleting
    is only safe for geometry that is never visible in any pose, and one pose cannot establish
    that.

    So nothing is deleted. Skin that sits outside the fabric is moved just inside it instead. The
    visible result is the same - no skin through the cloth - but the geometry always exists, so
    no angle and no pose can open a hole. It costs the overdraw that deleting would have saved,
    which is the right trade for a defect class that only shows up after shipping.

    Coverage is decided by CONTAINMENT, not by distance to the cloth. Distance was the first
    attempt and it produces visibly arbitrary results: wherever a garment is loose - the armpit,
    the chest of a heavy build - the body sits beyond any fixed threshold and is left visible in
    islands. Casting along the skin's own normal does not care about fit. It also handles
    openings for free: below a sleeve hem the ray escapes and the arm stays visible, with no
    boundary-loop special case.

    Skin that has pushed THROUGH the fabric is the mirror case - cloth behind it rather than in
    front - and is tucked just inside instead of deleted. That matters because a face is only
    removed when all three of its vertices are covered, so the fringe of a partly-covered face
    survives; tucking hides it without opening a gap.
    """
    from mathutils import Vector  # noqa: PLC0415
    from mathutils.bvhtree import BVHTree  # noqa: PLC0415
    from mathutils.kdtree import KDTree  # noqa: PLC0415
    import bmesh  # noqa: PLC0415

    bodies = [o for o in bpy.data.objects if o.type == "MESH" and SKIN_MESH.search(o.name)]
    garments = [o for o in bpy.data.objects
                if o.type == "MESH" and GARMENT_MESH.search(o.name)]
    if not bodies or not garments:
        _log(f"hide-body: {len(bodies)} body / {len(garments)} garment meshes, nothing to do")
        return

    for body in bodies:
        covered = set()
        for garment in garments:
            gm = garment.data
            gmw = garment.matrix_world
            verts = [gmw @ v.co for v in gm.vertices]
            tris = []
            for poly in gm.polygons:
                idx = list(poly.vertices)
                for k in range(1, len(idx) - 1):
                    tris.append((idx[0], idx[k], idx[k + 1]))
            bvh = BVHTree.FromPolygons(verts, tris, all_triangles=True)

            # Boundary loops mark the garment's openings - collar, cuffs, hems.
            edge_use: dict[tuple[int, int], int] = {}
            for poly in gm.polygons:
                idx = list(poly.vertices)
                for k in range(len(idx)):
                    a, b = idx[k], idx[(k + 1) % len(idx)]
                    key = (min(a, b), max(a, b))
                    edge_use[key] = edge_use.get(key, 0) + 1
            boundary = {v for (a, b), n in edge_use.items() if n == 1 for v in (a, b)}
            kd = KDTree(len(boundary)) if boundary else None
            if kd is not None:
                for i, vi in enumerate(boundary):
                    kd.insert(verts[vi], i)
                kd.balance()

            bmw = body.matrix_world
            inv = bmw.inverted()
            nrm = bmw.to_3x3().inverted().transposed()
            hits = 0
            tucked = 0
            pokes = []
            for v in body.data.vertices:
                co = bmw @ v.co
                n = (nrm @ v.normal)
                if n.length < 1e-9:
                    continue
                n = n.normalized()
                # COVERED = cloth lies outside this skin along its own normal.
                #
                # Distance to the nearest cloth is the wrong test and it is what made coverage
                # look arbitrary: wherever a garment is loose - the armpit, the chest of a heavy
                # build - the body sits further away than any fixed threshold and simply is not
                # marked, leaving islands of skin. Containment does not care how loose the fit
                # is. Below a sleeve hem the ray escapes and the arm stays visible, which is the
                # behaviour the opening margin was approximating.
                hit = bvh.ray_cast(co + n * 1e-4, n, RAY_REACH)
                covered_here = hit is not None and hit[0] is not None
                nearest = bvh.find_nearest(co, POKE_REACH)
                outside = False
                near_opening = False
                tuck_veto = False
                if nearest is not None and nearest[3] is not None:
                    outside = (co - nearest[0]).dot(nearest[1]) > 0
                    if kd is not None:
                        _, _, bd = kd.find(nearest[0])
                        near_opening = bd is not None and bd < OPENING_VETO
                        # TELEGAMEZ (telegamez#3725): relative veto for the tuck, see above.
                        poke = (co - nearest[0]).length
                        tuck_veto = near_opening and bd < max(OPENING_VETO_FLOOR,
                                                              OPENING_VETO_RATIO * poke)
                if not covered_here:
                    # Cloth behind the skin rather than in front: it has pushed through. Decided
                    # by the nearest surface rather than a backward ray, which at this reach
                    # would start hitting the far side of the garment.
                    covered_here = outside
                if not covered_here:
                    continue
                # Deletable only well away from an opening. Near one, the skin is legitimately on
                # show - a chin in front of a collar, a forearm below a cuff - and removing it
                # was what opened holes under the jaw and through the underarm during a Shrug.
                # It can still be TUCKED, which is what stops it poking through the fabric.
                if not near_opening and v.index not in covered:
                    covered.add(v.index)
                    hits += 1
                # Only skin that is OUTSIDE the fabric moves. Anything already inside is left
                # exactly where it is - pulling it out to meet the cloth would inflate the body
                # to fill the garment.
                # The veto gates the TUCK as well as the deletion. It is measured from the
                # nearest CLOTH POINT to a boundary, which separates the two cases cleanly: a
                # chin's nearest cloth is the collar edge itself (distance ~0, vetoed), while a
                # chest's is mid-panel (far from any edge, tucked). Without this the chin gets
                # pulled back onto the collar and the face deforms.
                if tuck_veto or not outside or nearest is None or nearest[3] is None:
                    continue
                location, normal, _, _ = nearest
                pokes.append((co - location).length)
                v.co = inv @ (location - normal * TUCK_DEPTH)
                tucked += 1
            if pokes:
                pokes.sort()
                _log(f"hide-body:   poke-through at {garment.name} openings: "
                     f"med {pokes[len(pokes)//2]*1000:.1f}mm max {pokes[-1]*1000:.1f}mm "
                     f"(tuck depth {TUCK_DEPTH*1000:.0f}mm)")
            _log(f"hide-body: {garment.name} covers {hits} more {body.name} verts, "
                 f"tucked {tucked} that had pushed through it")

        if not covered:
            continue
        # Peel rings off the covered set so the cut edge retreats under the fabric, away from
        # the opening where the ray test is least certain and where a ragged edge would show.
        neighbours: dict[int, set[int]] = {}
        for edge in body.data.edges:
            a, b = edge.vertices
            neighbours.setdefault(a, set()).add(b)
            neighbours.setdefault(b, set()).add(a)
        before = len(covered)
        for _ in range(EROSION_RINGS):
            covered = {v for v in covered if neighbours.get(v, set()) <= covered}
        bm = bmesh.new()
        bm.from_mesh(body.data)
        doomed = [f for f in bm.faces if all(v.index in covered for v in f.verts)]
        share = len(doomed) / max(len(bm.faces), 1)
        if share > MAX_BODY_REMOVAL:
            bm.free()
            raise RuntimeError(f"hide-body: would delete {share:.0%} of {body.name} - refusing")
        bmesh.ops.delete(bm, geom=doomed, context="FACES")
        bm.to_mesh(body.data)
        bm.free()
        body.data.update()
        _log(f"hide-body: {body.name}: eroded {before - len(covered)}, removed {len(doomed)} "
             f"faces ({share:.1%}), {len(body.data.polygons)} remain")


def _read_live_bones(workspace):
    """Load the bones that must survive the fold. Missing file is fatal, by design."""
    path = Path(workspace) / "_config" / "live_bones.json"
    if not path.exists():
        raise RuntimeError(
            "live_bones.json not found at {}. Generate it from the animation "
            "library first:\n"
            "  python stages/05-animation-bake/tools/derive_live_bones.py "
            "--animations <mh-animations.glb> --out <workspace>/_config/live_bones.json\n"
            "Exporting without it ships a character whose clothing tears - see "
            "_fold_frozen_bones().".format(path)
        )
    return set(json.loads(path.read_text(encoding="utf-8"))["live_bones"])


def _drop_implausible_influences(obj, per_vertex, bone_span, ratio):
    """Remove influences from bones that are nowhere near the vertex they drive.

    ## Why a garment ends up bound to the wrong limb

    MetaHuman weights clothing across rig-logic helpers, and a helper hangs off whatever bone it
    corrects, not the bone it sits on. `upperarm_fwd_l` and `upperarm_bck_l` are children of
    `LeftArm` but sit on the CHEST; `thigh_bck_*` are children of `thigh_*` but sit on the
    PELVIS. Unreal drives them so the chest cloth only partly follows the arm. Frozen, they
    alias to their parent and the chest follows the arm completely.

    Measured on the shipped shirt, splitting the torso centre column (|x| < 0.12) from the
    sleeves so a short sleeve at chest height is not mistaken for chest cloth:

        collar  49% LeftShoulder/RightShoulder/RightArm     (should be neck and upper spine)
        chest   29% LeftArm/RightArm                        (should be Spine2/Spine1)
        waist   25% thigh_l/thigh_r                         (should be spine and hips)
        hem     62% thigh_l/thigh_r                         (should be hips)
        sleeve  64% LeftArm/RightArm                        (correct - leave alone)

    That is the collapsed waist, the crumpled collar and the placket being dragged sideways.

    Transferring the weights from the skin underneath is the obvious repair and is not available
    here: `Body_Mesh` has NO vertices in the chest or collar bands and `Head_Mesh` stops at the
    collarbone, because MetaHuman deletes the torso skin hidden by clothing. There is no donor
    for precisely the region that is broken.

    ## The rule

    Geometric plausibility. For each vertex, measure the distance to each influencing bone's
    BIND segment and keep only influences within `ratio` times the nearest one. A chest-centre
    vertex is ~20cm from the `LeftArm` segment and ~6cm from `Spine2`, so the arm goes. A sleeve
    vertex is ~4cm from `LeftArm` and ~20cm from `Spine2`, so the arm stays and the spine goes.
    The shirt hem at the hip stays partly on `thigh_*`, which is correct - it sits there.

    Only ever removes influences and renormalises, so it cannot invent motion. A vertex whose
    influences are all equally distant keeps all of them.
    """
    world = obj.matrix_world
    dropped = 0
    out = []
    for vert, weights in zip(obj.data.vertices, per_vertex):
        if len(weights) < 2:
            out.append(weights)
            continue
        co = world @ vert.co
        distances = {}
        for name in weights:
            span = bone_span.get(name)
            if span is None:
                distances[name] = 0.0        # unknown bone: never penalise it
                continue
            head, tail = span
            axis = tail - head
            length_sq = axis.dot(axis)
            if length_sq < 1e-12:
                distances[name] = (co - head).length
            else:
                t = max(0.0, min(1.0, (co - head).dot(axis) / length_sq))
                distances[name] = (co - (head + axis * t)).length
        nearest = min(distances.values())
        limit = max(nearest, 1e-4) * ratio
        kept = {n: w for n, w in weights.items() if distances[n] <= limit}
        if not kept:
            out.append(weights)
            continue
        if len(kept) < len(weights):
            dropped += 1
        total = sum(kept.values())
        out.append({n: w / total for n, w in kept.items()} if total > 0 else weights)
    return out, dropped


def _smooth_weights(obj, per_vertex):
    """Laplacian-smooth a sparse per-vertex weight map over the mesh's edges.

    The fold is exact, but it cannot repair what rig logic was hiding. MetaHuman spreads a
    garment across many helper joints that Unreal keeps moving together; frozen, each aliases to
    whatever real joint it happens to hang from, and structurally distant parents land on
    adjacent vertices — one vertex 0.98 on `LeftShoulder` (via `clavicle_out_l`) touching one
    0.90 on `LeftArm` (via `upperarm_fwd_l`). In Unreal those deform almost identically; here
    they diverge the instant the arm swings, and the triangle between them stretches 35x. That
    is the shoulder and placket tearing that survives the fold.

    Smoothing restores the continuous field the garment was authored to have. Done in Python
    rather than through `bpy.ops.object.vertex_group_smooth` because that operator polls for a
    paint context that a --background export does not have.

    Vertices are welded by position first: a garment is split along its UV and material seams,
    and smoothing the split copies independently is what would re-open the seam it is meant to
    close.
    """
    mesh = obj.data
    welded = {}
    representative = [0] * len(mesh.vertices)
    for vert in mesh.vertices:
        key = (round(vert.co.x, 5), round(vert.co.y, 5), round(vert.co.z, 5))
        representative[vert.index] = welded.setdefault(key, vert.index)

    neighbours = {}
    for edge in mesh.edges:
        a, b = representative[edge.vertices[0]], representative[edge.vertices[1]]
        if a == b:
            continue
        neighbours.setdefault(a, set()).add(b)
        neighbours.setdefault(b, set()).add(a)

    # one map per welded representative, seeded from its own copy
    def disagreement(a, b):
        """Half the L1 distance between two weight maps: 0 identical, 1 disjoint."""
        total = 0.0
        for name in set(a) | set(b):
            total += abs(a.get(name, 0.0) - b.get(name, 0.0))
        return total / 2.0

    current = {r: dict(per_vertex[r]) for r in set(representative)}
    touched = 0
    for _ in range(WEIGHT_SMOOTH_REPEAT):
        nxt = {}
        for r, weights in current.items():
            adjacent = neighbours.get(r)
            if not adjacent:
                nxt[r] = weights
                continue
            # How far out of step is this vertex with its neighbours?
            if WEIGHT_SMOOTH_ADAPTIVE:
                step = max(disagreement(weights, current[other]) for other in adjacent)
                alpha = (step - WEIGHT_SMOOTH_D0) / (WEIGHT_SMOOTH_D1 - WEIGHT_SMOOTH_D0)
                alpha = max(0.0, min(1.0, alpha)) * WEIGHT_SMOOTH_FACTOR
            else:
                alpha = WEIGHT_SMOOTH_FACTOR
            if alpha <= 0.0:
                nxt[r] = weights          # coherent already - leave it exactly as it is
                continue
            touched += 1
            averaged = {}
            for other in adjacent:
                for name, weight in current[other].items():
                    averaged[name] = averaged.get(name, 0.0) + weight
            count = float(len(adjacent))
            blended = {}
            for name in set(weights) | set(averaged):
                blended[name] = (
                    (1.0 - alpha) * weights.get(name, 0.0)
                    + alpha * (averaged.get(name, 0.0) / count)
                )
            total = sum(blended.values())
            if total > 0.0:
                blended = {n: w / total for n, w in blended.items() if w > 1e-6}
            nxt[r] = blended
        current = nxt

    _log(f"fold:   adaptive smoothing touched {touched} vertex-passes on {obj.name}")
    return [current[representative[i]] for i in range(len(mesh.vertices))]


WEIGHT_PROVENANCE = {"path": "unknown"}


def _apply_solved_weights(workspace, char):
    """Install skinning-decomposition weights, replacing the fold/filter/smooth stack.

    Those three exist to survive the exporter's top-4 truncation by rearranging the weight map
    around it. This does not need to: `solve_skin.py` finds the four influences that best
    reproduce the FULL-influence deformation over a range of motion, so the four slots carry the
    motion rather than merely the four largest numbers. Measured on Telly against the
    twelve-influence truth, mean per-vertex deviation over 60 sampled frames:

        Body_Mesh        0.582mm -> 0.112mm    worst 22.3mm -> 5.0mm
        Telly_Outfits    1.956mm -> 0.599mm    worst 34.4mm -> 8.2mm
        Head_Mesh        0.105mm -> 0.013mm    worst  4.7mm -> 2.4mm

    Returns True when solved weights were applied; the caller falls back to the fold if not, so
    a character that has not been solved yet still exports.
    """
    path = Path(workspace) / "characters" / char / "_solve" / "skin_solved.npz"
    if not path.exists():
        _log(f"no solved weights at {path} - falling back to the fold")
        WEIGHT_PROVENANCE.update(path="fold", reason="no skin_solved.npz")
        return False

    import numpy as np  # Blender ships numpy; scipy lives in the external solver

    data = np.load(str(path), allow_pickle=True)
    bone_names = [str(n) for n in data["bone_names"]]
    applied = 0
    for key in [str(m) for m in data["meshes"]]:
        obj = bpy.data.objects.get(key)
        if obj is None or obj.type != "MESH":
            _log(f"solved weights name {key} not in the scene - skipped")
            continue
        idx = data[f"mesh::{key}::indices"]
        wgt = data[f"mesh::{key}::weights"]
        if len(idx) != len(obj.data.vertices):
            raise RuntimeError(
                f"{key}: solved weights cover {len(idx)} verts, mesh has "
                f"{len(obj.data.vertices)} - the solve is stale, re-run run_weight_solve.ps1"
            )

        # Non-bone groups (shape masks and the like) are preserved untouched.
        bone_set = set(bone_names)
        keep = {}
        for vg in obj.vertex_groups:
            if vg.name in bone_set:
                continue
            keep[vg.name] = []
        if keep:
            for vert in obj.data.vertices:
                for g in vert.groups:
                    name = obj.vertex_groups[g.group].name
                    if name in keep and g.weight > 0.0:
                        keep[name].append((vert.index, g.weight))

        obj.vertex_groups.clear()
        groups = {}
        for v in range(len(idx)):
            for k in range(idx.shape[1]):
                w = float(wgt[v, k])
                if w <= 1e-6:
                    continue
                name = bone_names[int(idx[v, k])]
                vg = groups.get(name)
                if vg is None:
                    vg = obj.vertex_groups.new(name=name)
                    groups[name] = vg
                vg.add([v], w, "REPLACE")
        for name, entries in keep.items():
            vg = obj.vertex_groups.new(name=name)
            for index, weight in entries:
                vg.add([index], weight, "REPLACE")

        applied += 1
        _log(f"solved: {key} -> {len(groups)} bone groups, 4 influences by construction")

    if not applied:
        WEIGHT_PROVENANCE.update(path="fold", reason="solve matched no mesh in the scene")
        return False
    stats = {}
    if "stats" in data.files:
        try:
            stats = json.loads(str(data["stats"][0]))
        except Exception as e:  # noqa: BLE001
            _log(f"solved weights carry unreadable stats: {e}")
    WEIGHT_PROVENANCE.update(
        path="solved", meshes=applied, source=str(path.name), solve=stats)
    _log(f"solved weights applied to {applied} mesh(es); fold/filter/smooth skipped")
    for mesh_name, st in (stats.get("per_mesh") or {}).items():
        _log(f"  solve {mesh_name}: {st['mean_truncated_mm']}mm -> {st['mean_solved_mm']}mm "
             f"mean, worst {st['worst_truncated_mm']} -> {st['worst_solved_mm']}mm")
    return True


def _apply_body_tuck(workspace, char):
    """Push skin inward by the measured amount it punches through cloth under animation.

    Bind-pose coverage cannot see this: the arm inside a short sleeve at rest swings outside it
    while moving, and those vertices render as skin shards on the shoulder and sleeve. The
    offsets come from solve_body_tuck.py, which measures the actual penetration over the shipped
    clips, so this moves nothing that was not observed to break through.

    Runs BEFORE _hide_body_under_clothing(): tucked skin is more contained, so containment sees
    the corrected surface rather than the one that pokes.
    """
    path = Path(workspace) / "characters" / char / "_solve" / "body_tuck.npz"
    if not path.exists():
        _log(f"no body tuck at {path} - skipping (skin may punch through cloth in motion)")
        return None

    import numpy as np

    data = np.load(str(path), allow_pickle=True)
    key = str(data["mesh"][0])
    obj = bpy.data.objects.get(key)
    if obj is None or obj.type != "MESH":
        _log(f"body tuck names {key}, not in the scene - skipped")
        return None
    offsets = data["offsets"]
    if len(offsets) != len(obj.data.vertices):
        raise RuntimeError(
            f"{key}: body tuck covers {len(offsets)} verts, mesh has "
            f"{len(obj.data.vertices)} - stale, re-run solve_body_tuck.py")

    co = np.empty((len(obj.data.vertices), 3))
    obj.data.vertices.foreach_get("co", co.ravel())
    obj.data.vertices.foreach_set("co", (co + offsets).ravel())
    obj.data.update()

    stats = {}
    if "stats" in data.files:
        try:
            stats = json.loads(str(data["stats"][0]))
        except Exception as e:  # noqa: BLE001
            _log(f"body tuck carries unreadable stats: {e}")
    moved = int((np.linalg.norm(offsets, axis=1) > 1e-5).sum())
    _log(f"body tuck: moved {moved} verts on {key}, max "
         f"{np.linalg.norm(offsets, axis=1).max() * 1000:.2f} mm")
    return {"verts_moved": moved, "solve": stats}


def _fold_frozen_bones(workspace):
    """Fold every frozen bone's skin weight into its nearest LIVE ancestor.

    ## The defect this fixes

    glTF and three.js allow four bone influences per vertex. MetaHuman clothing wants twelve —
    the stage 01 exports carry JOINTS_0/1/2, and the outfit's median vertex has seven non-zero
    influences. The glTF exporter silently keeps the largest four and renormalises.

    That truncation was destroying the garments. Measured on Tele's outfit:

        weight discarded by top-4 truncation   median 21.9%   p90 45.4%   max 50.6%
        vertices losing more than 10%          69.0%

    And it discarded the WRONG ones. ~1130 of this skeleton's ~1185 joints are rig-logic
    helpers that Unreal drives procedurally and glTF cannot carry, so in the browser they never
    move. A frozen bone is an exact alias of its parent — jointMatrix(child) == jointMatrix(parent)
    when the child's local transform equals its bind (the localBind terms cancel against the
    inverse bind matrix). 73% of outfit vertices were spending two or more of their four slots
    on aliases of a single bone while genuinely distinct influences fell off the end.

    Folding first collapses the aliases, so the four slots hold four DISTINCT live bones. The
    median vertex then needs two. Same measurement after this function runs:

        weight discarded by top-4 truncation   median 0.0%   p90 0.0%   max 27.2%
        vertices losing more than 10%          5.1%

    Because the fold is an identity on the deformation, this is free: it cannot change how any
    vertex moves, only how efficiently its weights are stored.

    ## Not optional, and not derivable here

    "Live" comes from `_config/live_bones.json`, which is derived from the shipped animation
    library plus the platform's 16 contract bones. The contract half is load-bearing:
    `LeftEye`/`RightEye` are posed procedurally by gaze and no clip animates them, so a set
    derived from the clips alone would fold them into `Head` and kill eye movement with no
    error anywhere. Re-derive whenever the animation set changes.
    """
    live = _read_live_bones(workspace)

    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if not armatures:
        _log("fold: no armature, skipping")
        return {}

    # bone -> nearest ancestor that is live (itself if it is live)
    alias = {}
    for arm in armatures:
        for bone in arm.data.bones:
            node = bone
            while node is not None and node.name not in live:
                node = node.parent
            alias[bone.name] = node.name if node is not None else bone.name

    # Bind-space segment of every live bone, for the plausibility filter.
    bone_span = {}
    for arm in armatures:
        for bone in arm.data.bones:
            bone_span[bone.name] = (
                arm.matrix_world @ bone.head_local,
                arm.matrix_world @ bone.tail_local,
            )

    stats = {}
    for obj in bpy.data.objects:
        if obj.type != "MESH" or not obj.vertex_groups:
            continue
        if not any(m.type == "ARMATURE" for m in obj.modifiers):
            continue

        is_garment = bool(GARMENT_MESH.search(obj.name))
        index_to_name = {vg.index: vg.name for vg in obj.vertex_groups}
        # Groups that are not bones (shape masks and the like) are left exactly as they are.
        bone_indices = {i for i, n in index_to_name.items() if n in alias}
        if not bone_indices:
            continue

        per_vertex = []      # sparse {target group name -> weight} for every vertex
        untouched = {}       # non-bone group name -> [(vertex index, weight), ...]
        collapsed_verts = 0
        rigid_head_mesh = False
        if is_garment:
            dominated = 0
            for vert in obj.data.vertices:
                bw = [(alias[index_to_name[g.group]], g.weight) for g in vert.groups
                      if g.group in bone_indices and g.weight > 0.0]
                tw = sum(w for _, w in bw)
                if tw > 0.0 and sum(w for tg, w in bw if tg == "Head") / tw >= RIGID_HEAD_SHARE:
                    dominated += 1
            rigid_head_mesh = dominated / max(len(obj.data.vertices), 1) >= RIGID_HEAD_MESH_FRACTION
            if dominated:
                _log(f"fold:   {obj.name}: {dominated} Head-dominated vertices -> rigid head piece: {rigid_head_mesh}")
        for vert in obj.data.vertices:
            acc = {}
            distinct_before = 0
            rigid_head = False
            if is_garment and rigid_head_mesh:
                bone_w = [(alias[index_to_name[g.group]], g.weight) for g in vert.groups
                          if g.group in bone_indices and g.weight > 0.0]
                tot_w = sum(w for _, w in bone_w)
                head_w = sum(w for t, w in bone_w if t == "Head")
                rigid_head = tot_w > 0.0 and head_w / tot_w >= RIGID_HEAD_SHARE
            for g in vert.groups:
                weight = g.weight
                if weight <= 0.0:
                    continue
                name = index_to_name.get(g.group)
                if name is None:
                    continue
                if g.group in bone_indices:
                    distinct_before += 1
                    target = alias[name]
                    if is_garment and not rigid_head:
                        target = GARMENT_BONE_REMAP.get(target, target)
                        if GARMENT_FORBIDDEN_BONE.match(target):
                            target = "Neck"
                    acc[target] = acc.get(target, 0.0) + weight
                else:
                    untouched.setdefault(name, []).append((vert.index, weight))
            if len(acc) < distinct_before:
                collapsed_verts += 1
            per_vertex.append(acc)

        smoothed = 0
        dropped = 0
        if not NO_SMOOTH_MESH.search(obj.name):
            # Order matters: drop the implausible influences FIRST, then smooth. Smoothing a
            # chest vertex that is still 29% bound to the arm spreads that error into its
            # neighbours instead of removing it, which is how the waist ended up collapsing
            # harder at 10 passes than at 0.
            if WEIGHT_COMPENSATION == "full":
                per_vertex, dropped = _drop_implausible_influences(
                    obj, per_vertex, bone_span, IMPLAUSIBLE_DISTANCE_RATIO,
                )
                per_vertex = _smooth_weights(obj, per_vertex)
                smoothed = WEIGHT_SMOOTH_REPEAT

        folded = {}
        for index, acc in enumerate(per_vertex):
            for target, weight in acc.items():
                if weight > 1e-6:
                    folded.setdefault(target, []).append((index, weight))

        obj.vertex_groups.clear()
        for name, entries in list(folded.items()) + list(untouched.items()):
            vg = obj.vertex_groups.new(name=name)
            for index, weight in entries:
                vg.add([index], weight, "REPLACE")

        stats[obj.name] = {
            "verts": len(obj.data.vertices),
            "groups_before": len(index_to_name),
            "groups_after": len(obj.vertex_groups),
            "verts_with_collapsed_influences": collapsed_verts,
        }
        _log(
            f"fold: {obj.name} groups {len(index_to_name)} -> {len(obj.vertex_groups)}, "
            f"{collapsed_verts}/{len(obj.data.vertices)} verts had influences merge"
            + (f", dropped far-bone influences on {dropped}" if dropped else "")
            + (f", smoothed {smoothed} passes" if smoothed else "")
        )

    # Weight smoothing (see _smooth_weights) has already run per garment above.

    # Make the four-influence limit explicit here rather than leaving it to the exporter, so
    # any residual loss is applied and normalised once, where it can be logged.
    bpy.ops.object.select_all(action="DESELECT")
    view = bpy.context.view_layer.objects
    for name in stats:
        obj = bpy.data.objects[name]
        obj.select_set(True)
        view.active = obj
    if stats:
        bpy.ops.object.vertex_group_limit_total(limit=4)
        bpy.ops.object.vertex_group_normalize_all(lock_active=False)
        _log(f"fold: limited to 4 influences and normalised across {len(stats)} meshes")
    return stats


def main():
    args = _parse_args()
    workspace = Path(args.workspace)
    char_dir = workspace / "characters" / args.char
    out_dir = char_dir / "03-glb"
    out_dir.mkdir(parents=True, exist_ok=True)
    glb_path = out_dir / f"{args.char}.glb"
    manifest_path = out_dir / "glb_manifest.json"

    constraints = _read_glb_constraints(workspace)
    max_px = int(constraints.get("max_texture_px", 1024))
    draco = bool(constraints.get("draco_compression", True))

    try:
        removed = _delete_hidden_meshes()
        _log(f"removed {len(removed)} hidden meshes")

        # Weights BEFORE hide-body. The solve is indexed by the stage-02 vertex order, and
        # hide-body deletes geometry, so running it first shifts every index past the first
        # deletion and the stale-solve guard (correctly) refuses the file. Containment is a
        # ray-cast against bind geometry and does not read weights, so the order is free.
        _log(f"weight compensation: {WEIGHT_COMPENSATION}")
        if WEIGHT_COMPENSATION == "none":
            _log("weight compensation: DISABLED - raw MetaHuman weights, exporter takes top 4")
            WEIGHT_PROVENANCE.update(path="none", reason="MH_WEIGHT_COMP=none")
        elif not _apply_solved_weights(workspace, args.char):
            fold_stats = _fold_frozen_bones(workspace)

        # Measured pose-aware tuck first, then bind-pose containment on the corrected surface.
        tuck_stats = _apply_body_tuck(workspace, args.char)

        # Skin that is under cloth should not be drawn at all.
        _hide_body_under_clothing()

        flipped = _flip_normal_maps_g()
        _log(f"flipped G on {flipped} normal maps (DX -> GL)")

        downsampled = _downsample_images(max_px)
        _log(f"downsampled {downsampled} textures (max {max_px}px)")

        flattened = _flatten_card_basecolor(out_dir, max_px)
        _log(f"flattened {len(flattened)} card-material BC chains")

        kept_meshes = [o.name for o in bpy.data.objects if o.type == "MESH"]
        materials = [m.name for m in bpy.data.materials if m.users > 0]
        images = [i.name for i in bpy.data.images
                  if i.users > 0 and i.size[0] > 0]

        vcol_stats = _vertex_colour_stats()

        bpy.ops.object.select_all(action="DESELECT")
        for o in bpy.data.objects:
            if o.type in ("MESH", "ARMATURE"):
                o.select_set(True)

        _log(f"exporting {glb_path} (draco={draco})")
        bpy.ops.export_scene.gltf(
            filepath=str(glb_path),
            export_format="GLB",
            use_visible=True,
            export_apply=True,
            export_yup=True,
            export_image_format="AUTO",
            export_draco_mesh_compression_enable=draco,
            export_draco_mesh_compression_level=6,
            export_skins=True,
            export_morph=True,
            export_morph_normal=True,
            export_morph_tangent=False,
            export_cameras=False,
            export_lights=False,
            # TELEGAMEZ: export the real vertex colours, not a fake white set.
            #
            # Stage 02 bakes proximity scalp darkening into the face mesh's
            # `Col` attribute so skin seen through gaps between hair cards
            # reads as shadow instead of bright forehead. It works: in
            # telly.blend, Head_Mesh's Col is min 0.014 / mean 0.652 with 42%
            # of loops darkened, and the stage logs "darkened 12239/34657
            # face verts".
            #
            # None of that reached a single shipped GLB. Every companion's
            # Head_Mesh COLOR_0 measured pure 1.000 white across all 24,414
            # verts, because this export relied on the operator defaults —
            # export_vertex_color='MATERIAL' plus export_all_vertex_colors=True.
            # In that combination Blender only writes real colours when a
            # material uses them in a way the glTF material converter
            # recognises, and otherwise, in its own words, "a fake COLOR_0
            # will be created, in order to keep material unchanged". The skin
            # material does read Col, but through a Mix (Legacy) node the
            # converter does not follow — so the fake white won.
            #
            # The failure is the silent-success shape exactly: the bake ran,
            # the log reported a count, the attribute was present in the GLB,
            # and the values were meaningless. Assert on the artifact.
            #
            # 'ACTIVE' exports the active colour attribute regardless of what
            # the material converter can trace, which is what this pipeline
            # wants — stage 02 sets Col active and render on every mesh.
            export_vertex_color="ACTIVE",
        )

        # Forward the viewer sidecar (mh_materials.json + alpha textures)
        # from 02-blend/ + 01-glb/textures/ into 03-glb/. Stage 04's
        # build_site.py copies these into docs/ so viewer.js can load
        # them as alphaMap targets and run the hair-card / lash shader.
        _emit_sidecar_for_viewer(
            char_dir, out_dir,
            sidecar_cap=_sidecar_cap(constraints),
            drop_alpha=bool(constraints.get("sidecar_drop_unused_alpha", True)),
        )

        # Patch the GLB so three.js's GLTFLoader can find morph target
        # names (it looks per-primitive; Blender writes per-mesh).
        _propagate_mesh_target_names_to_primitives(glb_path)

        # Skeleton-uniqueness gate. The published GLB must carry ONE skeleton:
        # every skin's joints must resolve into a single shared node set, and
        # no two joint nodes may share a name. Three skins with 342 duplicated
        # bone names shipped for the whole life of the project and were only
        # exposed by the first real body-animation clip — three.js binds
        # tracks by node name, first match wins, so meshes skinned to the
        # spare skeletons held bind pose while the body moved. Head, hair and
        # clothing as unconnected layers.
        import struct as _struct
        with open(glb_path, "rb") as _fh:
            _struct.unpack("<III", _fh.read(12))
            _clen, _ = _struct.unpack("<II", _fh.read(8))
            _j = json.loads(_fh.read(_clen).decode("utf-8"))
        _names = [n.get("name", "") for n in _j.get("nodes", [])]
        _joint_ids = set()
        for _s in _j.get("skins", []):
            _joint_ids |= set(_s.get("joints", []))
        _jnames = [_names[i] for i in _joint_ids]
        _dups = sorted({n for n in _jnames if _jnames.count(n) > 1})
        if _dups:
            raise RuntimeError(
                f"skeleton gate: {len(_j.get('skins', []))} skins, "
                f"{len(_dups)} duplicated joint names (e.g. {_dups[:6]}) — "
                f"the armature merge did not hold; refusing to publish")
        _log(f"skeleton gate: {len(_j.get('skins', []))} skin(s), "
             f"{len(_joint_ids)} joints, all names unique")

        tri_count = _count_tris()
        max_img_px = _current_max_image_px()
        file_size = glb_path.stat().st_size

        manifest = {
            "character_id": args.char,
            "pipeline": "native-glb",
            "saved_at": _iso_now(),
            "glb_path": f"03-glb/{args.char}.glb",
            "file_size_bytes": file_size,
            "tri_count": tri_count,
            "mesh_count": len(kept_meshes),
            "material_count": len(materials),
            "image_count": len(images),
            "max_texture_px_used": max_img_px,
            "normal_maps_g_flipped": flipped,
            "downsampled_images": downsampled,
            "flattened_card_materials": flattened,
            "draco": draco,
            "weights": WEIGHT_PROVENANCE,
            "body_tuck": tuck_stats,
            # What the vertex colours looked like going in. Compare against the
            # GLB if the scalp ever reads bright through the hair again.
            "vertex_colours": vcol_stats,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _log(f"wrote {manifest_path}")
        _log(f"glb {file_size/1_000_000:.1f} MB, "
             f"{tri_count:,} tris, max {max_img_px}px, "
             f"{len(materials)} mats, {len(images)} imgs")

        _update_char_manifest(char_dir, "done", [])
    except Exception as exc:
        import traceback
        _log(f"FAILED: {exc}\n{traceback.format_exc()}")
        _update_char_manifest(char_dir, "failed", [str(exc)])
        raise


if __name__ == "__main__":
    main()
