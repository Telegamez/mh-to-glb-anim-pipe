"""Kimodo Studio - local server.

Keeps Kimodo loaded on the GPU, stores a library of generated takes, serves the three.js studio.
Run with the Kimodo venv (see start-studio.ps1):

    python server.py   ->  http://localhost:7870

Library layout (one folder per take):
    library/<id>/meta.json     name, prompt, params, tags, notes, created, bake status
    library/<id>/motion.npz    Kimodo output (somaskel77)
The browser retargets onto whichever character GLB is loaded; production bakes go through
kimodo-bridge/tools/run_kimodo_take.ps1 (headless UE + stage 05).
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
LIBRARY = Path(os.environ.get("KIMODO_STUDIO_LIBRARY", HERE / "library")).resolve()
STATIC = HERE / "static"
DATA = Path(os.environ.get("KIMODO_STUDIO_DATA", HERE / "data")).resolve()
CHARACTERS = Path(os.environ.get("KIMODO_CHARACTERS", DATA / "characters")).resolve()
PRODUCTION = Path(os.environ.get("KIMODO_ANIMATIONS", DATA / "animations")).resolve()
BRIDGE = HERE.parent / "bridge" / "tools" / "run_kimodo_take.ps1"
BRIDGE_WORK = HERE.parent / "bridge" / "_work"
TARGET_MESH = os.environ.get("KIMODO_TARGET_MESH", "")
MODEL_NAME = os.environ.get("KIMODO_STUDIO_MODEL", "Kimodo-SOMA-RP-v1.1")
PORT = int(os.environ.get("KIMODO_STUDIO_PORT", "7870"))
FPS = 30

POSES = LIBRARY / "_poses"
PACKS = LIBRARY / "_packs"
for _d in (LIBRARY, POSES, PACKS):
    _d.mkdir(parents=True, exist_ok=True)

# The Telegames companion emotion states (owner's list, 2026-10-05). Prompts are starting points:
# standing, in place, conversational body language, so every clip can share the Idle neutral.
DEFAULT_PACK = {
    "name": "telegames-emotions",
    "neutral_pose": None,
    "duration": 4.0,
    # Measured 2026-10-05 ("excited", seed 21, 3 takes): pinning both ends to the Idle neutral at
    # constraint weight 2.0 cut wrist travel to 6-9 cm (free: 51-118 cm); at 1.0 it was 15-99 cm and
    # the seam was still 0.00 deg (post-processing enforces the end keys). So packs use 1.0.
    "constraint_weight": 1.0,
    "states": [
        {"state": "neutral", "intent": "No particular emotion",
         "prompt": "A person stands relaxed in place, breathing calmly with small natural weight shifts."},
        {"state": "excited", "intent": "Enthusiasm or anticipation",
         "prompt": "A person stands in place, bouncing lightly on their toes and pumping their fists with excitement."},
        {"state": "frustrated", "intent": "Irritation or dissatisfaction",
         "prompt": "A person stands in place, sighs heavily, throws their hands down in frustration and shakes their head."},
        {"state": "amused", "intent": "Finding something funny",
         "prompt": "A person stands in place chuckling, shoulders shaking, briefly covering their mouth with one hand."},
        {"state": "skeptical", "intent": "Doubt or questioning",
         "prompt": "A person stands with arms crossed, tilting their head and leaning back slightly in doubt."},
        {"state": "surprised", "intent": "An unexpected reaction",
         "prompt": "A person stands and is suddenly surprised, flinching back with raised open hands, then settles."},
        {"state": "contemplative", "intent": "Thoughtful reflection",
         "prompt": "A person stands thinking, one hand touching their chin, looking slightly down, shifting weight slowly."},
        {"state": "heated", "intent": "Intense, argumentative engagement",
         "prompt": "A person stands arguing intensely, leaning forward and gesturing forcefully with both hands."},
        {"state": "agreeing", "intent": "Agreement or approval",
         "prompt": "A person stands nodding in agreement, with a small open-palm gesture of approval."},
        {"state": "concerned", "intent": "Worry or care",
         "prompt": "A person stands with hands clasped in front, leaning slightly forward with a worried posture."},
        {"state": "sympathetic", "intent": "Empathy toward someone's difficulty",
         "prompt": "A person stands, places one hand on their chest and tilts their head gently with empathy."},
        {"state": "happy", "intent": "Warmth or joy",
         "prompt": "A person stands happily with a light bounce, relaxed open arms and an upbeat posture."},
        {"state": "sad", "intent": "Sadness or sorrow",
         "prompt": "A person stands with slumped shoulders and head lowered, sighing sadly."},
    ],
}
for _s in DEFAULT_PACK["states"]:
    _s.update(candidates=[], approved=None)
app = FastAPI(title="Kimodo Studio")

# --------------------------------------------------------------------------- model
STATE = {"status": "loading", "error": None, "model": None, "loaded_at": None}
GEN_LOCK = threading.Lock()


def _load_model():
    try:
        from kimodo import load_model
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        model = load_model(MODEL_NAME, device=device, default_family="Kimodo")
        STATE.update(model=model, status="ready", loaded_at=time.time())
        print(f"[studio] model {MODEL_NAME} ready on {device}", flush=True)
    except Exception as e:  # noqa: BLE001
        STATE.update(status="error", error=f"{e}\n{traceback.format_exc()}")
        print(f"[studio] model load failed: {e}", flush=True)


if os.environ.get("KIMODO_STUDIO_SKIP_MODEL") != "1":
    threading.Thread(target=_load_model, daemon=True).start()


def _soma():
    from kimodo.skeleton import SOMASkeleton77
    return SOMASkeleton77()


SOMA = _soma()

# --------------------------------------------------------------------------- library helpers


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:40] or "take"


def _meta_path(aid):
    return LIBRARY / aid / "meta.json"


def _read_meta(aid):
    p = _meta_path(aid)
    if not p.exists():
        raise HTTPException(404, f"no animation {aid}")
    return json.loads(p.read_text(encoding="utf-8"))


def _write_meta(aid, meta):
    _meta_path(aid).write_text(json.dumps(meta, indent=1), encoding="utf-8")


def _all_meta():
    out = []
    for d in LIBRARY.iterdir():
        if (d / "meta.json").exists():
            try:
                out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                pass
    return sorted(out, key=lambda m: m.get("created", 0), reverse=True)


def _quat_xyzw(mats: np.ndarray) -> np.ndarray:
    from scipy.spatial.transform import Rotation as R
    shp = mats.shape[:-2]
    q = R.from_matrix(mats.reshape(-1, 3, 3)).as_quat()  # xyzw
    return q.reshape(*shp, 4)


def _seam_deg(npz) -> float:
    from scipy.spatial.transform import Rotation as R
    g = npz["global_rot_mats"]
    return float(np.degrees((R.from_matrix(g[0]).inv() * R.from_matrix(g[-1])).magnitude()).max())


# --------------------------------------------------------------------------- API models


class GenerateReq(BaseModel):
    prompt: str
    duration: float = 4.0
    samples: int = 1
    seed: Optional[int] = None
    steps: int = 100
    text_weight: float = 2.0
    constraint_weight: float = 2.0
    loop: str = "none"            # none | self | pose
    loop_from: Optional[str] = None   # library id when loop == "pose"
    loop_frame: int = 0
    hold: int = 2
    name: Optional[str] = None
    tags: list[str] = []


class PatchReq(BaseModel):
    name: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    favorite: Optional[bool] = None
    audit: Optional[dict] = None


# --------------------------------------------------------------------------- generation


def _bookend_constraint(rot_mats_77, hips_y, n_frames, hold):
    from kimodo.geometry import matrix_to_axis_angle
    aa = matrix_to_axis_angle(torch.tensor(rot_mats_77, dtype=torch.float32)).numpy().tolist()
    last = n_frames - 1
    frames = sorted(set(list(range(0, hold + 1)) + list(range(last - hold, last + 1))))
    root = [0.0, float(hips_y), 0.0]
    return {"type": "fullbody", "frame_indices": frames, "local_joints_rot": [aa] * len(frames),
            "root_positions": [root] * len(frames), "smooth_root_2d": [[0.0, 0.0]] * len(frames)}


def _run_model(texts, n_frames, samples, steps, cfg, constraints):
    from kimodo.constraints import load_constraints_lst
    model = STATE["model"]
    lst = load_constraints_lst(constraints, model.skeleton) if constraints else []
    return model(texts, [n_frames], constraint_lst=lst, num_denoising_steps=steps, num_samples=samples,
                 multi_prompt=True, num_transition_frames=5, post_processing=True, return_numpy=True,
                 cfg_type="separated", cfg_weight=cfg)


def _local_rots_77(global_rot_mats_frame):
    from kimodo.skeleton import global_rots_to_local_rots
    g = torch.tensor(global_rot_mats_frame[None], dtype=torch.float32)
    return global_rots_to_local_rots(g, SOMA)[0].numpy()


@app.post("/api/generate")
def generate(req: GenerateReq):
    if STATE["status"] != "ready":
        raise HTTPException(503, f"model is {STATE['status']}")
    from kimodo.exports.motion_io import save_kimodo_npz
    from kimodo.tools import seed_everything

    n_frames = max(30, int(round(req.duration * FPS)))
    texts = [req.prompt.strip().rstrip(".") + "."]
    cfg = [req.text_weight, req.constraint_weight]
    seed = req.seed if req.seed is not None else int(time.time()) % 100000
    t0 = time.time()
    with GEN_LOCK:
        constraints = None
        if req.loop == "neutral":
            pose = _read_pose(req.loop_from)
            constraints = [_bookend_constraint(np.array(pose["local_rot_mats"]), pose["hips_y"], n_frames, req.hold)]
        elif req.loop == "pose":
            if not req.loop_from:
                raise HTTPException(400, "loop=pose needs loop_from")
            src = np.load(LIBRARY / req.loop_from / "motion.npz")
            f = min(req.loop_frame, len(src["global_rot_mats"]) - 1)
            constraints = [_bookend_constraint(_local_rots_77(src["global_rot_mats"][f]),
                                               src["root_positions"][f][1], n_frames, req.hold)]
        seed_everything(seed)
        out = _run_model(texts, n_frames, req.samples, req.steps, cfg, constraints)
        if req.loop == "self":
            # Two passes: each free sample donates its own first frame as the bookend pose.
            outs = []
            for i in range(req.samples):
                c = [_bookend_constraint(_local_rots_77(out["global_rot_mats"][i][0]),
                                         out["root_positions"][i][0][1], n_frames, req.hold)]
                seed_everything(seed + i)
                o = _run_model(texts, n_frames, 1, req.steps, cfg, c)
                outs.append({k: (v[0] if hasattr(v, "shape") and v.ndim and v.shape[0] == 1 else v) for k, v in o.items()})
        else:
            outs = [{k: (v[i] if hasattr(v, "shape") and v.ndim and v.shape[0] == req.samples else v)
                     for k, v in out.items()} for i in range(req.samples)]

    created = []
    base = req.name or _slug(req.prompt)
    for i, sample in enumerate(outs):
        aid = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        d = LIBRARY / aid
        d.mkdir()
        save_kimodo_npz(str(d / "motion.npz"), sample)
        npz = np.load(d / "motion.npz")
        meta = {
            "id": aid, "name": base if req.samples == 1 else f"{base} #{i + 1}",
            "prompt": req.prompt, "duration": n_frames / FPS, "frames": n_frames, "fps": FPS,
            "seed": seed + i if req.loop == "self" else seed, "sample": i, "steps": req.steps,
            "cfg": cfg, "loop": req.loop, "loop_from": req.loop_from, "hold": req.hold,
            "seam_deg": round(_seam_deg(npz), 3), "tags": list(req.tags), "notes": "",
            "favorite": False, "created": time.time(), "model": MODEL_NAME,
            "gen_seconds": round(time.time() - t0, 1), "bake": None,
        }
        _write_meta(aid, meta)
        created.append(meta)
    print(f"[studio] generated {len(created)} take(s) for {req.prompt!r} in {time.time() - t0:.1f}s", flush=True)
    return created


# --------------------------------------------------------------------------- library CRUD


@app.get("/api/status")
def status():
    free = total = None
    if torch.cuda.is_available():
        free, total = torch.cuda.mem_get_info()
    return {"status": STATE["status"], "error": STATE["error"], "model": MODEL_NAME,
            "vram_free_gb": round(free / 2**30, 1) if free else None,
            "vram_total_gb": round(total / 2**30, 1) if total else None}


@app.get("/api/animations")
def list_animations():
    return _all_meta()


@app.get("/api/animations/{aid}")
def get_animation(aid: str):
    return _read_meta(aid)


@app.get("/api/animations/{aid}/motion")
def get_motion(aid: str):
    _read_meta(aid)
    d = np.load(LIBRARY / aid / "motion.npz")
    g = d["global_rot_mats"]
    return {
        "fps": FPS, "frames": int(g.shape[0]),
        # Global rotations as deltas from SOMA's T-pose (identity at rest), xyzw, [T][77][4].
        "global_quats": np.round(_quat_xyzw(g), 5).tolist(),
        "root_positions": np.round(d["root_positions"], 5).tolist(),
        "posed_joints": np.round(d["posed_joints"], 4).tolist(),
        "foot_contacts": d["foot_contacts"].astype(int).tolist() if "foot_contacts" in d.files else None,
    }


@app.patch("/api/animations/{aid}")
def patch_animation(aid: str, req: PatchReq):
    meta = _read_meta(aid)
    for k, v in req.model_dump(exclude_none=True).items():
        meta[k] = v
    _write_meta(aid, meta)
    return meta


@app.post("/api/animations/{aid}/duplicate")
def duplicate_animation(aid: str):
    meta = _read_meta(aid)
    new = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    shutil.copytree(LIBRARY / aid, LIBRARY / new)
    meta.update(id=new, name=f"{meta['name']} copy", created=time.time(), bake=None)
    _write_meta(new, meta)
    return meta


@app.delete("/api/animations/{aid}")
def delete_animation(aid: str):
    _read_meta(aid)
    shutil.rmtree(LIBRARY / aid)
    return {"deleted": aid}


# --------------------------------------------------------------------------- neutral poses


class PoseReq(BaseModel):
    name: str
    global_quats: list[list[float]]   # [77][4] xyzw, SOMA frame, deltas from T-pose
    hips_y: float
    source: str = ""


def _read_pose(name):
    p = POSES / f"{_slug(name or '')}.json"
    if not p.exists():
        raise HTTPException(404, f"no neutral pose {name}")
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/poses")
def list_poses():
    out = []
    for p in sorted(POSES.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out.append({k: d[k] for k in ("name", "hips_y", "source", "created")})
    return out


@app.post("/api/poses")
def save_pose(req: PoseReq):
    from scipy.spatial.transform import Rotation as R
    from kimodo.skeleton import global_rots_to_local_rots
    if len(req.global_quats) != SOMA.nbjoints:
        raise HTTPException(400, f"need {SOMA.nbjoints} joints, got {len(req.global_quats)}")
    g = torch.tensor(R.from_quat(np.array(req.global_quats)).as_matrix()[None], dtype=torch.float32)
    local = global_rots_to_local_rots(g, SOMA)[0].numpy()
    name = _slug(req.name)
    data = {"name": name, "source": req.source, "hips_y": req.hips_y, "created": time.time(),
            "local_rot_mats": np.round(local, 6).tolist(), "global_quats": req.global_quats}
    (POSES / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    return {k: data[k] for k in ("name", "hips_y", "source", "created")}


# --------------------------------------------------------------------------- packs (state sets)


def _pack_path(name):
    return PACKS / f"{_slug(name)}.json"


@app.get("/api/packs")
def list_packs():
    if not any(PACKS.glob("*.json")):
        _pack_path(DEFAULT_PACK["name"]).write_text(json.dumps(DEFAULT_PACK, indent=1), encoding="utf-8")
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(PACKS.glob("*.json"))]


PACK_LOCK = threading.Lock()


def _load_pack(name):
    p = _pack_path(name)
    if not p.exists():
        raise HTTPException(404, f"no pack {name}")
    return json.loads(p.read_text(encoding="utf-8"))


@app.put("/api/packs/{name}")
def save_pack(name: str, pack: dict):
    with PACK_LOCK:
        pack["name"] = _slug(name)
        if _pack_path(name).exists():
            # The export job owns "export"; a browser holding a stale copy must not clobber it.
            pack["export"] = _load_pack(name).get("export")
        _pack_path(name).write_text(json.dumps(pack, indent=1), encoding="utf-8")
    return pack


def _set_export(name, **kw):
    with PACK_LOCK:
        pack = _load_pack(name)
        pack["export"] = {**(pack.get("export") or {}), **kw}
        _pack_path(name).write_text(json.dumps(pack, indent=1), encoding="utf-8")


EXPORTS = HERE / "exports"


def _export_worker(name):
    pack = _load_pack(name)
    chosen = [s for s in pack["states"] if s.get("approved") and _meta_path(s["approved"]).exists()]
    try:
        for i, st in enumerate(chosen):
            meta = _read_meta(st["approved"])
            b = meta.get("bake") or {}
            if not (b.get("status") == "done" and b.get("glb") and Path(b["glb"]).exists()):
                _set_export(name, progress=f"baking {i + 1}/{len(chosen)}: {st['state']}")
                take = "KM_" + re.sub(r"[^A-Za-z0-9]", "", (st.get("clip") or st["state"]).title()) + "_" + meta["id"][-4:]
                meta["bake"] = {"status": "running", "take": take, "started": time.time()}
                _write_meta(meta["id"], meta)
                _bake_worker(meta["id"], take)
        out = EXPORTS / pack["name"]
        out.mkdir(parents=True, exist_ok=True)
        gestures, manifest = {}, []
        for st in chosen:
            meta = _read_meta(st["approved"])
            b = meta.get("bake") or {}
            clip = st.get("clip") or st["state"].capitalize()
            entry = {"state": st["state"], "intent": st.get("intent"), "clip": clip, "behavior": "one_shot",
                     "take": meta["id"], "prompt": meta["prompt"], "seed": meta["seed"], "frames": meta["frames"],
                     "seam_deg": meta["seam_deg"], "neutral_pose": pack.get("neutral_pose"), "audit": meta.get("audit"),
                     "bake": b.get("status"), "ue_asset": b.get("ue_asset"), "baked_glb": b.get("glb"),
                     # 07 §3: one-shot emotion clips are exempt from the hand thresholds but must say so.
                     "hand_threshold_exempt": "one-shot emotion clip (07 §3); measured values recorded in audit"}
            manifest.append(entry)
            if b.get("status") == "done":
                gestures[clip] = b["ue_asset"]
        animset = {"_comment": f"Kimodo Studio export of pack '{pack['name']}' ({time.strftime('%Y-%m-%d %H:%M')}). "
                               "Emotion one-shots, every clip bookended on the neutral pose "
                               f"'{pack.get('neutral_pose')}'. NOT a runtime set on its own: merge the gestures into "
                               "animset.male.json / animset.female.json deliberately, then rebake with run_animbake.ps1.",
                   "required": {}, "gestures": gestures, "output": f"mh-animations-{pack['name']}.glb"}
        (out / f"animset.{pack['name']}.json").write_text(json.dumps(animset, indent=1), encoding="utf-8")
        (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        failed = [m["state"] for m in manifest if m["bake"] != "done"]
        _set_export(name, status="done" if not failed else "partial", progress=None,
                    animset=str(out / f"animset.{pack['name']}.json"), failed=failed, finished=time.time())
    except Exception as e:  # noqa: BLE001
        _set_export(name, status="failed", error=f"{e}", progress=None, finished=time.time())


@app.post("/api/packs/{name}/export")
def export_pack(name: str):
    pack = _load_pack(name)
    if (pack.get("export") or {}).get("status") == "running":
        raise HTTPException(409, "export already running")
    _set_export(name, status="running", progress="starting", started=time.time(), animset=None, error=None)
    threading.Thread(target=_export_worker, args=(name,), daemon=True).start()
    return _load_pack(name)


# --------------------------------------------------------------------------- production bake


BAKE_LOCK = threading.Lock()  # one headless UE at a time: concurrent runs fight over the project


def _bake_worker(aid, take):
    with BAKE_LOCK:
        _bake_worker_locked(aid, take)


def _bake_worker_locked(aid, take):
    d = LIBRARY / aid
    log = d / "bake.log"
    meta = _read_meta(aid)
    try:
        from kimodo.exports.bvh import save_motion_bvh
        from kimodo.skeleton import global_rots_to_local_rots
        npz = np.load(d / "motion.npz")
        g = torch.tensor(npz["global_rot_mats"], dtype=torch.float32)
        local = global_rots_to_local_rots(g, SOMA)
        root = torch.tensor(npz["posed_joints"][:, SOMA.root_idx, :], dtype=torch.float32)
        bvh = d / "motion.bvh"
        save_motion_bvh(str(bvh), local, root, skeleton=SOMA, fps=FPS, standard_tpose=True)
        if not TARGET_MESH:
            raise HTTPException(400, "Set KIMODO_TARGET_MESH to your Unreal SkeletalMesh asset path before baking")
        cmd = ["pwsh", "-NoProfile", "-File", str(BRIDGE), "-Bvh", str(bvh), "-Take", take, "-TargetMesh", TARGET_MESH]
        with open(log, "w", encoding="utf-8") as f:
            rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode
        text = log.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"\[run\] baked (.+?\.glb)", text)
        meta = _read_meta(aid)
        meta["bake"] = {"status": "done" if rc == 0 and m else "failed", "take": take,
                        "ue_asset": f"/Game/Kimodo/Retargeted/{take}", "glb": m.group(1) if m else None,
                        "finished": time.time(), "log": str(log)}
    except Exception as e:  # noqa: BLE001
        meta = _read_meta(aid)
        meta["bake"] = {"status": "failed", "take": take, "error": str(e), "finished": time.time()}
    _write_meta(aid, meta)


@app.post("/api/animations/{aid}/bake")
def bake(aid: str):
    meta = _read_meta(aid)
    if meta.get("bake") and meta["bake"].get("status") == "running":
        raise HTTPException(409, "already baking")
    take = "KM_" + re.sub(r"[^A-Za-z0-9]", "", meta["name"].title())[:32] + "_" + aid[-4:]
    meta["bake"] = {"status": "running", "take": take, "started": time.time()}
    _write_meta(aid, meta)
    threading.Thread(target=_bake_worker, args=(aid, take), daemon=True).start()
    return meta


# --------------------------------------------------------------------------- assets


@app.get("/api/soma")
def soma():
    return {"names": list(SOMA.bone_order_names),
            "parents": [int(p) for p in SOMA.joint_parents],
            "rest": np.round(SOMA.neutral_joints.numpy(), 5).tolist()}


@app.get("/api/characters")
def characters():
    out = []
    if CHARACTERS.is_dir():
        for g in sorted(CHARACTERS.glob("*/*.glb")):
            if g.stem == g.parent.name:
                out.append({"id": g.stem, "url": f"/characters/{g.parent.name}/{g.name}",
                            "mb": round(g.stat().st_size / 2**20, 1)})
    return out


@app.get("/api/production")
def production():
    out = [{"id": p.stem, "url": f"/production/{p.name}"} for p in sorted(PRODUCTION.glob("*.glb"))]
    # Kimodo takes already baked for production (headless UE retarget + stage 05 evaluated bake):
    # the exact clip the platform would play, correctives and twists included.
    bridge = BRIDGE_WORK
    for p in sorted(bridge.glob("*/bake/*.glb")):
        out.append({"id": f"baked: {p.stem}", "url": f"/bridge/{p.relative_to(bridge).as_posix()}"})
    return out


# Optional inputs may be absent until the user supplies them.
for directory in (CHARACTERS, PRODUCTION, BRIDGE_WORK):
    directory.mkdir(parents=True, exist_ok=True)
app.mount("/characters", StaticFiles(directory=CHARACTERS), name="characters")
app.mount("/production", StaticFiles(directory=PRODUCTION, check_dir=False), name="production")
app.mount("/bridge", StaticFiles(directory=BRIDGE_WORK, check_dir=False), name="bridge")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


if __name__ == "__main__":
    print(f"[studio] http://localhost:{PORT}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
