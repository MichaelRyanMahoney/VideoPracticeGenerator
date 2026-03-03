# scripts/parse_screenplay_to_manifest.py
import re, csv, argparse, hashlib, json, os
from pathlib import Path

# Map variants (names or labels) to canonical ROLE KEYS
# We now use roles only: MediatorA, MediatorB, Disputant1, Disputant2
ALIASES = {
    # Full names -> roles
    "EMILY":"MEDIATORA",
    "EMILY JOHNSON":"MEDIATORA",
    "MICHAEL":"MEDIATORB",
    "MICHAEL NGUYEN":"MEDIATORB",
    "CALEB":"DISPUTANT1",
    "CALEB WARD":"DISPUTANT1",
    "ARIA":"DISPUTANT2",
    "ARIA LOPEZ":"DISPUTANT2",
    # Role labels -> roles
    "MEDIATOR A":"MEDIATORA",
    "MEDIATOR B":"MEDIATORB",
    "DISPUTANT 1":"DISPUTANT1",
    "DISPUTANT 2":"DISPUTANT2",
    # Already-canonical
    "MEDIATORA":"MEDIATORA",
    "MEDIATORB":"MEDIATORB",
    "DISPUTANT1":"DISPUTANT1",
    "DISPUTANT2":"DISPUTANT2",
}

# Allow digits in role labels (e.g., DISPUTANT 1); support optional inline {key=value ...}
SPEAKER_LINE = re.compile(r'^\s*([A-Z0-9 ]+)(?:\s*\(([A-Z \.]+)\))?\s*(?:\{([^}]*)\})?\s*$')
# One-line speaker + dialogue format:
# MEDIATOR A (EMILY) {emotion=normal ...} "Hello there."
INLINE_SPEAKER_LINE = re.compile(
    r'^\s*([A-Z0-9 ]+)(?:\s*\(([A-Z \.]+)\))?\s*(?:\{([^}]*)\})?\s+(.+?)\s*$'
)

# Standalone defaults directive:
# {DEFAULTS emotion=normal intensity=1.0 tempo=1.0 pitch=1.0 volume=100}
# pitch accepts either semitone shift (0 neutral) or ratio (1.0 neutral).
DEFAULTS_LINE = re.compile(r'^\s*\{\s*DEFAULTS\s+([^}]*)\}\s*$')
PAUSE_TOKEN = re.compile(r'\[PAUSE\]', re.IGNORECASE)
VALID_EMOTION_PRESETS = {"normal", "happy", "sad", "angry", "whisper", "toneup", "tonedown"}
EMOTION_KEYS = {"emotion", "emotion_preset"}

def _safe_path_segment(value: str) -> str:
    """
    Make a string safe to use as a single path segment.
    Keeps common project ids like 'VIDEO-01' intact.
    """
    s = (value or "").strip()
    if not s:
        return ""
    s = re.sub(r"[\\/]+", "_", s)
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s.strip("._-")

def normalize_speaker(raw_role, raw_name):
    """
    Return canonical role key strictly in {MediatorA, MediatorB, Disputant1, Disputant2}.
    Any legacy name usage is mapped via ALIASES; unknowns raise an error.
    """
    name = (raw_name or "").strip().upper()
    role = (raw_role or "").strip().upper()
    key = None
    if name and name in ALIASES:
        key = ALIASES[name]
    elif role and role in ALIASES:
        key = ALIASES[role]
    if not key:
        raise ValueError(f"Unknown speaker '{raw_name or raw_role}'. Use role labels MediatorA, MediatorB, Disputant1, Disputant2 (or known aliases).")
    # Convert canonical to proper case format
    if key == "MEDIATORA": return "MediatorA"
    if key == "MEDIATORB": return "MediatorB"
    if key == "DISPUTANT1": return "Disputant1"
    if key == "DISPUTANT2": return "Disputant2"
    raise ValueError(f"Unrecognized canonical role '{key}'")

def _parse_kv_blob(blob: str) -> dict:
    """Parse inline attrs with either 'key=value' or 'key: value' syntax.
    Values can be quoted; attributes may be space- or comma-separated.
    """
    if not blob:
        return {}
    out = {}
    tokens = re.findall(r'(\w+)\s*[:=]\s*("[^"]*"|\'[^\']*\'|[^,\s]+)', blob)
    for k, v in tokens:
        v = v.strip().strip('"\'')
        key = k.strip().lower()
        out[key] = v
    return out

def _coerce_types(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        kk = k.lower()
        if kk in {"emotion", "emotion_preset"}:
            emo = str(v).strip().lower()
            out["emotion_preset"] = emo if emo in VALID_EMOTION_PRESETS else "normal"
        elif kk in {"intensity", "emotion_intensity"}:
            try:
                out["emotion_intensity"] = float(v)
            except Exception:
                pass
        elif kk == "tempo":
            try:
                out["tempo"] = float(v)
            except Exception:
                pass
        elif kk == "pitch":
            try:
                out["pitch"] = float(v)
            except Exception:
                pass
        elif kk == "volume":
            try:
                out["volume"] = int(v)
            except Exception:
                pass
        elif kk == "hesitant":
            # Used to steer Typecast smart-prompt delivery only (NOT part of spoken text).
            raw = str(v).strip().lower()
            out["hesitant"] = raw in {"1", "true", "yes", "y", "t", "on"}
    return out


def _has_explicit_emotion_key(raw_kv: dict) -> bool:
    return any(str(k).strip().lower() in EMOTION_KEYS for k in raw_kv.keys())

def parse_script(lines):
    entries = []
    i=0; cur_speaker=None
    # Only attributes explicitly provided via {DEFAULTS ...} should be emitted into the CSV.
    # If a field is absent in the manifest row, downstream TTS will fall back to per-character
    # defaults from generator_inputs.json (preferred) and then internal defaults.
    current_defaults: dict = {}
    while i < len(lines):
        line = lines[i].rstrip("\n")
        stripped = line.strip()
        # Update defaults if a DEFAULTS line is encountered
        dm = DEFAULTS_LINE.match(stripped)
        if dm:
            kv = _coerce_types(_parse_kv_blob(dm.group(1) or ""))
            current_defaults.update(kv)
            i += 1
            continue
        # Preserve standalone [PAUSE] directives as explicit manifest rows.
        if stripped.startswith("[") and stripped.endswith("]"):
            pause_count = len(PAUSE_TOKEN.findall(stripped))
            if pause_count:
                for _ in range(pause_count):
                    entries.append({
                        "kind": "pause",
                        "speaker": "PAUSE",
                        "transcript": "",
                        "duration": 0.5,
                        "attrs": {},
                    })
                i += 1
                continue

        # Handle one-line entries where speaker label and spoken text are on the same line.
        mi = INLINE_SPEAKER_LINE.match(line)
        if mi:
            try:
                cur_speaker = normalize_speaker(mi.group(1), mi.group(2))
            except ValueError:
                cur_speaker = None
            if cur_speaker:
                raw_inline_kv = _parse_kv_blob(mi.group(3) or "")
                inline_kv = _coerce_types(raw_inline_kv)
                has_explicit_emotion = _has_explicit_emotion_key(raw_inline_kv)
                spoken_inline = (mi.group(4) or "").strip()
                has_inline_attrs = bool((mi.group(3) or "").strip())
                looks_like_quote = spoken_inline.startswith(("“", "\"", "'"))
                # Avoid misclassifying metadata lines like "DISPUTANT 1 NAME: Caleb".
                if spoken_inline and (has_inline_attrs or looks_like_quote):
                    attrs = dict(current_defaults)
                    attrs.update(inline_kv)
                    attrs["typecast_mode"] = "preset" if has_explicit_emotion else "smart"
                    if bool(attrs.get("hesitant")) is True:
                        attrs["typecast_mode"] = "smart"
                    entries.append({
                        "kind": "speech",
                        "speaker": cur_speaker,
                        "transcript": spoken_inline,
                        "duration": "",
                        "attrs": attrs,
                    })
                    i += 1
                    continue

        m = SPEAKER_LINE.match(line)
        if m and i+1 < len(lines):
            # Next non-empty line(s) until blank or bracketed stage dir
            cur_speaker = normalize_speaker(m.group(1), m.group(2))
            raw_inline_kv = _parse_kv_blob(m.group(3) or "")
            inline_kv = _coerce_types(raw_inline_kv)
            has_explicit_emotion = _has_explicit_emotion_key(raw_inline_kv)
            j = i+1
            spoken=[]
            row_kv = dict(inline_kv)
            while j < len(lines):
                t = lines[j].strip()
                if not t: break
                if t.startswith("[") and t.endswith("]"): break
                # stop on another all-caps label too
                if SPEAKER_LINE.match(t): break
                # allow a standalone {key=value} directive line before spoken text
                dm2 = DEFAULTS_LINE.match(t)
                if dm2:
                    # This would change global defaults mid-script; avoid here
                    kv2 = _coerce_types(_parse_kv_blob(dm2.group(1) or ""))
                    current_defaults.update(kv2)
                    j += 1
                    continue
                if t.startswith("{") and t.endswith("}") and "=" in t:
                    raw_kv3 = _parse_kv_blob(t.strip()[1:-1])
                    if _has_explicit_emotion_key(raw_kv3):
                        has_explicit_emotion = True
                    kv3 = _coerce_types(raw_kv3)
                    row_kv.update(kv3)
                    j += 1
                    continue
                spoken.append(t)
                j += 1
            if spoken:
                # Merge defaults -> row overrides
                attrs = dict(current_defaults)
                attrs.update(row_kv)
                attrs["typecast_mode"] = "preset" if has_explicit_emotion else "smart"
                if bool(attrs.get("hesitant")) is True:
                    attrs["typecast_mode"] = "smart"
                entries.append({
                    "kind": "speech",
                    "speaker": cur_speaker,
                    "transcript": " ".join(spoken),
                    "duration": "",
                    "attrs": attrs,
                })
            i = j
        else:
            i += 1
    return entries

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in_txt", required=True)
    ap.add_argument("--out_csv", required=True)
    ap.add_argument(
        "--project_id",
        default="",
        help="Optional project id like VIDEO-01. Included in audio_hash to isolate caches per-project.",
    )
    ap.add_argument("--audio_s3_prefix", default="", help="Optional S3 prefix like s3://bucket/projects/Video-01/audio (no trailing slash required). If set, manifest audio will be written as S3 URIs.")
    args = ap.parse_args()

    text = Path(args.in_txt).read_text()
    lines = text.splitlines()
    entries = parse_script(lines)

    Path(args.out_csv).parent.mkdir(parents=True, exist_ok=True)
    project_id = (args.project_id or "").strip()
    safe_project_id = _safe_path_segment(project_id)
    audio_s3_prefix = (args.audio_s3_prefix or "").strip()
    if audio_s3_prefix and not audio_s3_prefix.startswith("s3://"):
        raise SystemExit("--audio_s3_prefix must be an s3:// URI")

    # Optional: load generator_inputs.json (next to out_csv or in_txt folder) to include voice_id in hash
    # This makes audio cache robust to voice changes.
    voice_ids: dict[str, str] = {}
    typecast_defaults: dict[str, dict] = {}
    try:
        # Prefer env override (useful in Batch) else look beside script/out_csv.
        gen_path = os.environ.get("VPG_GENERATOR_INPUTS_JSON")
        if gen_path:
            gen_inputs_path = Path(gen_path)
        else:
            # Try sibling manifests/generator_inputs.json next to out_csv; else next to in_txt
            out_dir = Path(args.out_csv).resolve().parent
            cand1 = out_dir / "generator_inputs.json"
            cand2 = out_dir.parent / "inputs" / "generator_inputs.json"
            cand3 = Path(args.in_txt).resolve().parent / "generator_inputs.json"
            gen_inputs_path = next((p for p in (cand1, cand2, cand3) if p.exists()), None)
        if gen_inputs_path and Path(gen_inputs_path).exists():
            gi = json.loads(Path(gen_inputs_path).read_text())
            for role, conf in (gi.get("characters") or {}).items():
                tc = ((conf.get("typecast") or {}) if isinstance(conf, dict) else {})
                vid = (tc.get("voice_id") or "").strip()
                if vid:
                    voice_ids[str(role)] = vid
                # Capture per-role defaults so audio_hash (and caching) changes when these change.
                d: dict = {}
                emo = tc.get("emotion")
                if emo is not None and str(emo).strip() != "":
                    e = str(emo).strip().lower()
                    d["emotion_preset"] = e if e in VALID_EMOTION_PRESETS else "normal"
                tempo = tc.get("tempo")
                if tempo is None or str(tempo).strip() == "":
                    tempo = tc.get("speed")
                if tempo is not None and str(tempo).strip() != "":
                    try:
                        d["tempo"] = float(tempo)
                    except Exception:
                        pass
                pitch = tc.get("pitch")
                if pitch is not None and str(pitch).strip() != "":
                    try:
                        d["pitch"] = float(pitch)
                    except Exception:
                        pass
                volume = tc.get("volume")
                if volume is not None and str(volume).strip() != "":
                    try:
                        d["volume"] = int(volume)
                    except Exception:
                        pass
                if d:
                    typecast_defaults[str(role)] = d
    except Exception:
        voice_ids = {}
        typecast_defaults = {}

    def _effective_attr(role: str, attrs: dict, key: str, fallback):
        if isinstance(attrs, dict) and key in attrs:
            return attrs.get(key)
        return (typecast_defaults.get(role, {}) or {}).get(key, fallback)

    def audio_hash_for(role: str, transcript: str, attrs: dict) -> str:
        vid = voice_ids.get(role, "")
        effective_hesitant = bool((attrs or {}).get("hesitant")) is True
        base_mode = str((attrs or {}).get("typecast_mode", "smart")).strip().lower() or "smart"
        if effective_hesitant:
            effective_mode = "smart"
        else:
            character_has_emotion = "emotion_preset" in (typecast_defaults.get(role, {}) or {})
            effective_mode = "preset" if (base_mode == "preset" or character_has_emotion) else "smart"
        payload: dict = {
            "v": 1,
            "project_id": project_id,
            "role": role,
            "voice_id": vid,
            "transcript": transcript,
            "typecast_mode": effective_mode,
            # include the per-line delivery attrs so cache invalidates correctly
            "emotion_preset": str(_effective_attr(role, attrs, "emotion_preset", "normal")),
            "emotion_intensity": float(_effective_attr(role, attrs, "emotion_intensity", 1.0)),
            "tempo": float(_effective_attr(role, attrs, "tempo", 1.0)),
            "pitch": float(_effective_attr(role, attrs, "pitch", 0.0)),
            "volume": int(_effective_attr(role, attrs, "volume", 100)),
        }
        # Only include hesitant when explicitly enabled so existing hashes remain stable.
        if effective_hesitant:
            payload["hesitant"] = True
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
    with open(args.out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "id",
            "speaker",
            "audio",        # either relative file path or s3:// uri (depending on args)
            "audio_hash",   # sha256 of stable audio identity
            "transcript",
            "duration",     # seconds for pause rows; empty for speech rows
            "typecast_mode",
            "hesitant",
            "emotion_preset","emotion_intensity","tempo","pitch","volume"
        ])
        for idx, entry in enumerate(entries, start=1):
            rid = f"{idx:03d}"
            kind = entry.get("kind", "speech")
            spk = str(entry.get("speaker", "")).strip()
            txt = str(entry.get("transcript", "")).strip()
            duration = entry.get("duration", "")
            attrs = entry.get("attrs", {}) or {}
            if kind == "pause":
                ah = ""
                audio = ""
            else:
                ah = audio_hash_for(spk, txt, attrs)
                if audio_s3_prefix:
                    # Store as audio/<hash>.wav (hash-only naming prevents renumber churn across edits)
                    audio = f"{audio_s3_prefix.rstrip('/')}/{ah}.wav"
                else:
                    # Local default: content-addressed naming for robust caching across script edits.
                    # If project_id is provided, use a per-project subfolder for cleanliness.
                    if safe_project_id:
                        audio = f"audio/{safe_project_id}/{ah}.wav"
                    else:
                        audio = f"audio/{ah}.wav"
            # Keep the manifest aligned with the actual synthesis behavior: if a character has an emotion
            # override in generator_inputs.json, the smartprompt TTS pipeline will use preset mode.
            typecast_mode_out = attrs.get("typecast_mode","smart") if kind != "pause" else ""
            if kind != "pause":
                if bool(attrs.get("hesitant")) is True:
                    typecast_mode_out = "smart"
                else:
                    if str(typecast_mode_out).strip().lower() != "preset" and "emotion_preset" in (typecast_defaults.get(spk, {}) or {}):
                        typecast_mode_out = "preset"
            w.writerow([
                rid,
                spk,
                audio,
                ah,
                txt,
                duration,
                typecast_mode_out,
                "true" if (kind != "pause" and bool(attrs.get("hesitant")) is True) else "",
                attrs.get("emotion_preset","") if kind != "pause" else "",
                attrs.get("emotion_intensity","") if kind != "pause" else "",
                attrs.get("tempo","") if kind != "pause" else "",
                attrs.get("pitch","") if kind != "pause" else "",
                attrs.get("volume","") if kind != "pause" else "",
            ])
    print(f"Wrote {args.out_csv} with {len(entries)} rows")

if __name__ == "__main__":
    main()
