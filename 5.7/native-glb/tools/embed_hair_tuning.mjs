/**
 * Bake per-character hair calibration INTO the sidecar, so it stops depending on the name.
 *
 *   node embed_hair_tuning.mjs --sidecar <mh_materials.json> --char <id>
 *                              [--source <asset name>] [--manifest <manifest.json>]
 *                              [--profiles <hairProfile.ts>]
 *
 * ## The problem this removes
 *
 * The runtime looks up hair calibration by character id — and derives that id from the model
 * FILENAME (`characterIdOf` in metaHumanSurface.ts). A companion whose id has no entry in
 * `HAIR_PROFILES` renders with default hair: straw instead of its tuned groom, with no error
 * anywhere. That is not hypothetical. It has happened twice:
 *
 *   - Loading telly from the CDN, where the content-addressed path made the id a bake hash.
 *   - Staging a model as `model.glb` in a dev harness, which made the id "model".
 *
 * It also means a bake cannot be renamed. Wanting to keep two builds of one MetaHuman side by
 * side — an A/B, a compressed variant — costs the calibration, silently.
 *
 * ## Why writing it to the sidecar is the fix
 *
 * `resolveHairTuning` merges in this order:
 *
 *     DEFAULT -> accessory -> HAIR_PROFILES[id] -> mh_materials.json params -> live override
 *
 * The sidecar ALREADY wins over the id-keyed table, and `sanitizeTuning` reads the same
 * snake_case keys. So the values simply need to be in the file. Nothing in the runtime
 * changes; this uses a path that already exists and a precedence that is already correct.
 * hairProfile.ts says as much: "one vocabulary runs end to end".
 *
 * After this, `HAIR_PROFILES` becomes a fallback for assets baked before this step, and the
 * id is just a label.
 *
 * ## The rename is exactly when the lookup must NOT use the new name
 *
 * This tool exists so a bake can be renamed. It was then keyed on the renamed id, which meant
 * the rename it enables is the thing that breaks it: bake `Tele` as `tim` and the lookup asks
 * for HAIR_PROFILES['tim'], finds nothing, embeds nothing, and the companion renders with
 * straw hair — the same silent failure, reintroduced by the feature meant to remove it, and
 * only visible by looking at the render.
 *
 * The calibration belongs to the MetaHuman, not to the label on this build. So the source
 * asset's own name is tried when the id has no profile. `bootstrap_character.py` records it as
 * `output_name` in the character's manifest — `--id` moves only the id and leaves `mh_folder`
 * and `output_name` describing the asset — and the manifest sits one level above the sidecar,
 * so nothing has to be passed in for the common case.
 *
 * ## Where the numbers come from
 *
 * `hairProfile.ts` in the telegamez checkout, which is the live source. It is read rather
 * than copied, because a second copy of numbers that were dialled in by eye is a copy that
 * silently goes stale. If the checkout is not there, this says so and changes nothing —
 * baking without calibration is the behaviour we already had, so it is a warning, not a
 * failure.
 */

import fs from 'node:fs';
import path from 'node:path';

const argv = process.argv.slice(2);
const flag = (n, d) => { const i = argv.indexOf(`--${n}`); return i === -1 ? d : argv[i + 1]; };

const sidecarPath = flag('sidecar');
const charId = (flag('char') || '').toLowerCase();
if (!sidecarPath || !charId) { console.error('--sidecar and --char are required'); process.exit(2); }

/**
 * The name of the MetaHuman this was baked from, when it differs from the id.
 *
 * Read from the character's manifest by default: the sidecar lives at
 * `characters/<id>/03-glb/mh_materials.json`, so the manifest is one directory up. Explicit
 * flags win, for a caller that knows better or a layout that moves.
 */
function sourceName() {
  const explicit = flag('source');
  if (explicit) return explicit.toLowerCase();
  const manifestPath = flag('manifest')
    ?? path.resolve(path.dirname(sidecarPath), '..', 'manifest.json');
  try {
    const m = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    return String(m.output_name || '').toLowerCase() || null;
  } catch {
    // No manifest is not an error. It means the id is all we know, which is the behaviour
    // this had before the fallback existed.
    return null;
  }
}

/** Same rules as hairClassOf() in hairProfile.ts. Kept in step with it deliberately. */
function hairClassOf(materialName) {
  const n = (materialName || '').toLowerCase();
  if (/lash/.test(n)) return 'lashes';
  const token = n.split('_')[0];
  if (token === 'eyebrows' || token === 'brows' || token === 'eyebrow') return 'brows';
  if (token === 'beard' || token === 'stubble') return 'beard';
  if (token === 'mustache' || token === 'moustache') return 'mustache';
  if (token === 'goatee') return 'goatee';
  return 'hair';
}

/** Locate the sister checkout's hairProfile.ts without hard-coding one machine's layout. */
function findProfiles(explicit) {
  if (explicit) return fs.existsSync(explicit) ? explicit : null;
  const rel = 'packages/web-next/src/services/RoomManager/Agent/materials/hairProfile.ts';
  const here = path.resolve('.');
  const guesses = [];
  for (let dir = here, i = 0; i < 8; i++, dir = path.dirname(dir)) {
    guesses.push(path.join(dir, 'telegamez', rel));
    if (path.dirname(dir) === dir) break;
  }
  return guesses.find((p) => fs.existsSync(p)) ?? null;
}

/**
 * Pull HAIR_PROFILES out of the TypeScript.
 *
 * The declaration is a plain object literal — numbers, strings and nested objects, with JS
 * comments — so evaluating it is exact where a regex would be a guess. Braces are matched
 * rather than assuming the closing one is the last in the file.
 */
function readProfiles(file) {
  const src = fs.readFileSync(file, 'utf8');
  const marker = 'HAIR_PROFILES: Record<string, HairProfile> = ';
  const at = src.indexOf(marker);
  if (at === -1) throw new Error(`HAIR_PROFILES not found in ${file}`);
  const start = at + marker.length;
  let depth = 0;
  let end = -1;
  for (let i = start; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  if (end === -1) throw new Error('HAIR_PROFILES literal is unbalanced');
  // eslint-disable-next-line no-eval
  return eval(`(${src.slice(start, end)})`);
}

const profilesFile = findProfiles(flag('profiles'));
if (!profilesFile) {
  console.log('[hair] hairProfile.ts not found — skipping. The companion will fall back to');
  console.log('[hair] HAIR_PROFILES at runtime, which works only while its id stays put.');
  process.exit(0);
}

let profiles;
try {
  profiles = readProfiles(profilesFile);
} catch (e) {
  console.log(`[hair] could not read ${profilesFile}: ${e.message} — skipping`);
  process.exit(0);
}

// The id first, so a character tuned under its own name is unaffected. Then the asset it was
// baked from, which is what a renamed bake needs and the only place the calibration survives.
const source = sourceName();
let tuningKey = charId;
let profile = profiles[charId];
if (!profile && source && source !== charId && profiles[source]) {
  tuningKey = source;
  profile = profiles[source];
  console.log(`[hair] '${charId}' has no profile of its own; using '${source}', the MetaHuman`);
  console.log('[hair] it was baked from. Calibration belongs to the asset, not to the name.');
}
if (!profile) {
  const known = Object.keys(profiles).join(', ');
  console.log(`[hair] no tuned profile for '${charId}'${source && source !== charId ? ` or '${source}'` : ''} — nothing to embed.`);
  console.log(`[hair] tuned ids: ${known}`);
  process.exit(0);
}

const sidecar = JSON.parse(fs.readFileSync(sidecarPath, 'utf8'));
let touched = 0;
const notes = [];

for (const m of sidecar.materials || []) {
  // Only hair-ish materials carry this tuning. `face_accessory` is how lashes are declared.
  if (m.kind !== 'hair' && m.kind !== 'face_accessory') continue;
  const cls = m.kind === 'face_accessory' ? 'lashes' : hairClassOf(m.material_name || '');
  const tuned = { ...(profile.global ?? {}), ...(profile.materials?.[cls] ?? {}) };
  if (!Object.keys(tuned).length) continue;

  m.params = m.params || {};
  // The sidecar's own values win: they describe THIS bake (colour, alpha channel, atlas
  // stems), whereas the profile is calibration. Overwriting them would restyle a character
  // as a side effect of recording how its hair was tuned.
  let added = 0;
  for (const [k, v] of Object.entries(tuned)) {
    if (!(k in m.params)) { m.params[k] = v; added++; }
  }
  if (added) { touched++; notes.push(`${m.material_name} [${cls}] +${added}`); }
}

if (!touched) {
  console.log(`[hair] '${tuningKey}' has a profile but no hair material needed it — unchanged.`);
  process.exit(0);
}

sidecar.hair_tuning_source = {
  character_id: charId,
  // Which profile the values came from. Worth recording separately: on a renamed bake these
  // differ, and "why does Tim's hair look like Tele's" has no answer without it.
  tuned_as: tuningKey,
  from: 'hairProfile.ts HAIR_PROFILES',
  embedded_at: new Date().toISOString(),
  note: 'Embedded so calibration survives a rename. The sidecar wins over HAIR_PROFILES in '
      + 'resolveHairTuning, so the character id no longer decides how this hair renders.',
};
fs.writeFileSync(sidecarPath, `${JSON.stringify(sidecar, null, 2)}\n`);
console.log(`[hair] embedded '${tuningKey}' calibration into ${path.basename(sidecarPath)} — ${touched} material(s)`);
for (const n of notes) console.log(`[hair]   ${n}`);
