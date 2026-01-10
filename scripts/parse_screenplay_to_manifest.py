# scripts/parse_screenplay_to_manifest.py
import re, csv, argparse
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
SPEAKER_LINE = re.compile(r'^\s*([A-Z0-9 ]+?)(?:\s*\(([A-Z \.]+)\))?\s*(?:\{([^}]*)\})?\s*$')

# Standalone defaults directive: {DEFAULTS emotion=normal intensity=1.0 tempo=1.0 pitch=0 volume=100}
DEFAULTS_LINE = re.compile(r'^\s*\{\s*DEFAULTS\s+([^}]*)\}\s*$')

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
    """Parse a simple 'key=value' blob separated by spaces or commas.
    Values can be quoted; whitespace around '=' is allowed.
    """
    if not blob:
        return {}
    out = {}
    # split by spaces or commas that are not inside quotes
    tokens = re.findall(r'(\w+)\s*=\s*("[^"]*"|\'[^\']*\'|[^,\s]+)', blob)
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
            out["emotion_preset"] = str(v).lower()
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
                out["pitch"] = int(v)
            except Exception:
                pass
        elif kk == "volume":
            try:
                out["volume"] = int(v)
            except Exception:
                pass
    return out

def parse_script(lines):
    entries=[]
    i=0; cur_speaker=None
    current_defaults = {  # applied to every entry unless overridden
        "emotion_preset": "normal",
        "emotion_intensity": 1.0,
        "tempo": 1.0,
        "pitch": 0,
        "volume": 100,
    }
    while i < len(lines):
        line = lines[i].rstrip("\n")
        # Update defaults if a DEFAULTS line is encountered
        dm = DEFAULTS_LINE.match(line.strip())
        if dm:
            kv = _coerce_types(_parse_kv_blob(dm.group(1) or ""))
            current_defaults.update(kv)
            i += 1
            continue

        m = SPEAKER_LINE.match(line)
        if m and i+1 < len(lines):
            # Next non-empty line(s) until blank or bracketed stage dir
            cur_speaker = normalize_speaker(m.group(1), m.group(2))
            inline_kv = _coerce_types(_parse_kv_blob(m.group(3) or ""))
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
                    kv3 = _coerce_types(_parse_kv_blob(t.strip()[1:-1]))
                    row_kv.update(kv3)
                    j += 1
                    continue
                spoken.append(t)
                j += 1
            if spoken:
                # Merge defaults -> row overrides
                attrs = dict(current_defaults)
                attrs.update(row_kv)
                entries.append((cur_speaker, " ".join(spoken), attrs))
            i = j
        else:
            i += 1
    return entries

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in_txt", required=True)
    ap.add_argument("--out_csv", required=True)
    args = ap.parse_args()

    text = Path(args.in_txt).read_text()
    lines = text.splitlines()
    entries = parse_script(lines)

    Path(args.out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "id","speaker","audio","transcript",
            "emotion_preset","emotion_intensity","tempo","pitch","volume"
        ])
        for idx, (spk, txt, attrs) in enumerate(entries, start=1):
            rid = f"{idx:03d}"
            audio = f"audio/{spk.upper()}_{rid}.wav"
            w.writerow([
                rid,
                spk,
                audio,
                txt,
                attrs.get("emotion_preset","normal"),
                attrs.get("emotion_intensity",1.0),
                attrs.get("tempo",1.0),
                attrs.get("pitch",0),
                attrs.get("volume",100),
            ])
    print(f"Wrote {args.out_csv} with {len(entries)} rows")

if __name__ == "__main__":
    main()
