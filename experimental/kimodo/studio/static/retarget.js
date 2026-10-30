// Kimodo (SOMA77) -> any humanoid GLB, in the browser.
//
// Same rules as the validated UE retarget (kimodo-bridge/README.md):
//   * world-space rotation transfer: Kimodo's global rotations are deltas from SOMA's T-pose
//     (verified to 1e-7 m), so target_world(t) = G(t) * A * target_rest_world
//   * A aligns ONLY the limbs (T-pose -> the character's A-pose); the torso keeps the
//     character's own rest - aligning it produced a constant 8 deg backward lean in UE
//   * fingers are left at rest: Kimodo does not animate them (somaskel30)
import * as THREE from 'three';

// [soma bone, target candidates, soma child for alignment | null, target child candidates]
// Candidates list MetaHuman contract names first, raw UE names next, Mixamo-style last.
const MAP = [
  ['Hips', ['Hips', 'pelvis', 'mixamorig:Hips'], null],
  ['Spine1', ['Spine', 'spine_01', 'mixamorig:Spine'], null],
  ['Spine2', ['Spine1', 'spine_03', 'mixamorig:Spine1'], null],
  ['Chest', ['Spine2', 'spine_05', 'mixamorig:Spine2'], null],
  ['Neck1', ['Neck', 'neck_01', 'mixamorig:Neck'], null],
  ['Neck2', ['neck_02'], null],
  ['Head', ['Head', 'head', 'mixamorig:Head'], null],
];
for (const [S, s, m] of [['Left', 'l', 'Left'], ['Right', 'r', 'Right']]) {
  MAP.push(
    [`${S}Shoulder`, [`${m}Shoulder`, `clavicle_${s}`], `${S}Arm`, [`${m}Arm`, `upperarm_${s}`]],
    [`${S}Arm`, [`${m}Arm`, `upperarm_${s}`], `${S}ForeArm`, [`${m}ForeArm`, `lowerarm_${s}`]],
    [`${S}ForeArm`, [`${m}ForeArm`, `lowerarm_${s}`], `${S}Hand`, [`${m}Hand`, `hand_${s}`]],
    [`${S}Hand`, [`${m}Hand`, `hand_${s}`], `${S}HandMiddle1`, [`middle_01_${s}`, `${m}HandMiddle1`]],
    [`${S}Leg`, [`thigh_${s}`, `${m}UpLeg`], `${S}Shin`, [`calf_${s}`, `${m}Leg`]],
    [`${S}Shin`, [`calf_${s}`, `${m}Leg`], `${S}Foot`, [`foot_${s}`, `${m}Foot`]],
    [`${S}Foot`, [`foot_${s}`, `${m}Foot`], `${S}ToeBase`, [`ball_${s}`, `${m}ToeBase`]],
    [`${S}ToeBase`, [`ball_${s}`, `${m}ToeBase`], null],
  );
}
const SOMA_STANDING_HIPS = 1.0; // metres; SOMA's BVH hip offset

const _v = new THREE.Vector3(), _q = new THREE.Quaternion(), _q2 = new THREE.Quaternion();

function depth(o) { let d = 0; while (o.parent) { d++; o = o.parent; } return d; }

export class Retargeter {
  constructor(characterRoot, soma) {
    this.root = characterRoot;
    this.soma = soma; // {names, parents, rest}
    this.somaIndex = Object.fromEntries(soma.names.map((n, i) => [n, i]));
    const bones = {};
    characterRoot.traverse((o) => { if (o.isBone && !(o.name in bones)) bones[o.name] = o; });
    this.bones = bones;
    const find = (cands) => cands.map((c) => bones[c.replace(/[:]/g, '')] || bones[c]).find(Boolean);

    characterRoot.updateMatrixWorld(true);
    // Rest pose for every bone, so switching clips never inherits a previous clip's fingers.
    this.rest = new Map();
    characterRoot.traverse((o) => { if (o.isBone) this.rest.set(o, { q: o.quaternion.clone(), p: o.position.clone() }); });

    // Facing: SOMA's left is +X (facing +Z). Yaw F maps SOMA's frame onto the character's.
    const la = find(['LeftArm', 'upperarm_l', 'mixamorig:LeftArm']);
    const ra = find(['RightArm', 'upperarm_r', 'mixamorig:RightArm']);
    if (!la || !ra) throw new Error('character has no recognisable arm bones');
    const left = la.getWorldPosition(new THREE.Vector3()).sub(ra.getWorldPosition(new THREE.Vector3()));
    left.y = 0; left.normalize();
    this.F = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(1, 0, 0), left);
    this.Finv = this.F.clone().invert();

    this.entries = [];
    this.missing = [];
    for (const [s, tc, sc, tcc] of MAP) {
      const bone = find(tc);
      const si = this.somaIndex[s];
      if (!bone || si === undefined) { this.missing.push(s); continue; }
      const restWorld = bone.getWorldQuaternion(new THREE.Quaternion());
      let A = new THREE.Quaternion();
      if (sc) {
        const child = find(tcc);
        const sci = this.somaIndex[sc];
        if (child && sci !== undefined) {
          const dt = child.getWorldPosition(new THREE.Vector3()).sub(bone.getWorldPosition(new THREE.Vector3())).normalize();
          const r = soma.rest;
          const ds = new THREE.Vector3(r[sci][0] - r[si][0], r[sci][1] - r[si][1], r[sci][2] - r[si][2])
            .normalize().applyQuaternion(this.F);
          A.setFromUnitVectors(dt, ds);
        }
      }
      this.entries.push({ soma: s, si, bone, restWorld, A, depth: depth(bone) });
    }
    this.entries.sort((a, b) => a.depth - b.depth);
    this.hips = this.entries.find((e) => e.soma === 'Hips');
    if (!this.hips) throw new Error('character has no hips bone');
    this.hipsRestWorld = this.hips.bone.getWorldPosition(new THREE.Vector3());
    this.scale = this.hipsRestWorld.y / SOMA_STANDING_HIPS;
  }

  resetPose() {
    for (const [b, r] of this.rest) { b.quaternion.copy(r.q); b.position.copy(r.p); }
    this.root.updateMatrixWorld(true);
  }

  // Pose the character at one Kimodo frame.
  applyFrame(motion, f) {
    const gq = motion.global_quats[f];
    for (const e of this.entries) {
      const s = gq[e.si];
      _q.set(s[0], s[1], s[2], s[3]);
      _q.premultiply(this.F).multiply(this.Finv);               // G' = F G F^-1
      const world = _q.multiply(e.A).multiply(e.restWorld);      // G' A R0
      e.bone.parent.updateWorldMatrix(true, false);
      e.bone.parent.getWorldQuaternion(_q2).invert();
      e.bone.quaternion.copy(_q2.multiply(world));
    }
    const rp = motion.root_positions[f], r0 = motion.root_positions[0];
    _v.set((rp[0] - r0[0]) * this.scale, rp[1] * this.scale, (rp[2] - r0[2]) * this.scale)
      .applyQuaternion(this.F);
    _v.x += this.hipsRestWorld.x; _v.z += this.hipsRestWorld.z;
    const hb = this.hips.bone;
    hb.parent.updateWorldMatrix(true, false);
    hb.position.copy(hb.parent.worldToLocal(_v.clone()));
  }

  // Bake a Kimodo motion into an AnimationClip on this character (mapped bones + hips).
  bakeClip(motion, name) {
    const T = motion.frames, times = new Float32Array(T);
    const qs = this.entries.map(() => new Float32Array(T * 4));
    const ps = new Float32Array(T * 3);
    this.resetPose();
    for (let f = 0; f < T; f++) {
      times[f] = f / motion.fps;
      this.applyFrame(motion, f);
      this.entries.forEach((e, i) => {
        const q = e.bone.quaternion, o = f * 4, a = qs[i];
        let sgn = 1;
        if (f > 0 && q.x * a[o - 4] + q.y * a[o - 3] + q.z * a[o - 2] + q.w * a[o - 1] < 0) sgn = -1;
        a[o] = q.x * sgn; a[o + 1] = q.y * sgn; a[o + 2] = q.z * sgn; a[o + 3] = q.w * sgn;
      });
      const p = this.hips.bone.position;
      ps[f * 3] = p.x; ps[f * 3 + 1] = p.y; ps[f * 3 + 2] = p.z;
    }
    this.resetPose();
    const tracks = this.entries.map((e, i) => new THREE.QuaternionKeyframeTrack(`${e.bone.uuid}.quaternion`, times, qs[i]));
    tracks.push(new THREE.VectorKeyframeTrack(`${this.hips.bone.uuid}.position`, times, ps));
    return new THREE.AnimationClip(name, (T - 1) / motion.fps, tracks);
  }

  // Inverse retarget: the character's CURRENT pose -> SOMA global rotations (deltas from T-pose,
  // SOMA frame) + hip height. Forward is world = F G F^-1 A R0, so G = F^-1 (world R0^-1 A^-1) F.
  // SOMA joints with no character bone (fingers, jaw, eyes, ends) inherit their parent's global
  // rotation, i.e. stay at rest locally - the same thing Kimodo's 30->77 expansion does.
  capturePose() {
    this.root.updateMatrixWorld(true);
    const n = this.soma.names.length, G = new Array(n).fill(null);
    for (const e of this.entries) {
      const w = e.bone.getWorldQuaternion(new THREE.Quaternion());
      const g = w.multiply(e.restWorld.clone().invert()).multiply(e.A.clone().invert());
      G[e.si] = this.Finv.clone().multiply(g).multiply(this.F);
    }
    for (let j = 0; j < n; j++) {
      if (!G[j]) G[j] = j === 0 ? new THREE.Quaternion() : G[this.soma.parents[j]].clone();
    }
    const hips = this.hips.bone.getWorldPosition(new THREE.Vector3());
    return { global_quats: G.map((q) => [q.x, q.y, q.z, q.w]), hips_y: hips.y / this.scale };
  }

  // 07 §3 clip contract, measured on THIS character (metres, world space), same quantities as
  // scripts/motion/audit_clips.py: maxHandRise = max(hand.y - hips.y) <= 0.50,
  // minHandHeadDist = min |hand - head| >= 0.30, duration 1.5-9 s.
  audit(motion) {
    const b = (n) => this.bones[n];
    const lh = b('LeftHand') || b('hand_l'), rh = b('RightHand') || b('hand_r');
    const head = b('Head') || b('head'), hips = this.hips.bone;
    if (!lh || !rh || !head) return null;
    let rise = -Infinity, dist = Infinity;
    const p = new THREE.Vector3(), h = new THREE.Vector3(), hp = new THREE.Vector3();
    this.resetPose();
    for (let f = 0; f < motion.frames; f++) {
      this.applyFrame(motion, f);
      this.root.updateMatrixWorld(true);
      hips.getWorldPosition(hp); head.getWorldPosition(h);
      for (const hand of [lh, rh]) {
        hand.getWorldPosition(p);
        rise = Math.max(rise, p.y - hp.y);
        dist = Math.min(dist, p.distanceTo(h));
      }
    }
    this.resetPose();
    const duration = (motion.frames - 1) / motion.fps;
    return {
      maxHandRise: +rise.toFixed(3), minHandHeadDist: +dist.toFixed(3), duration: +duration.toFixed(2),
      pass: { handRise: rise <= 0.5, handHead: dist >= 0.3, duration: duration >= 1.5 && duration <= 9 },
    };
  }

  // Per mapped bone, angle (deg) between the character's current world rotation and a stored one.
  worldQuats() { return this.entries.map((e) => e.bone.getWorldQuaternion(new THREE.Quaternion())); }

  // SOMA skeleton ghost positions in the character's frame (for side-by-side checking).
  ghostPoints(motion, f, offset) {
    const P = motion.posed_joints[f], r0 = motion.root_positions[0], pts = [];
    for (let j = 1; j < P.length; j++) {
      const p = this.soma.parents[j];
      for (const k of [p, j]) {
        pts.push(new THREE.Vector3(P[k][0] - r0[0], P[k][1], P[k][2] - r0[2]).multiplyScalar(this.scale)
          .applyQuaternion(this.F).add(offset));
      }
    }
    return pts;
  }
}
