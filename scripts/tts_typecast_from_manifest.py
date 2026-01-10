#!/usr/bin/env python3
import os, csv, json, argparse, requests, subprocess
from pathlib import Path


TYPECAST_API_URL = "https://api.typecast.ai/v1/text-to-speech"


# Internal defaults (used when CSV does not provide a value)
DEFAULT_EMOTION_PRESET = "normal"
DEFAULT_EMOTION_INTENSITY = 1.0
DEFAULT_TEMPO = 1.0
DEFAULT_PITCH = 0
DEFAULT_VOLUME = 100
DEFAULT_SEED = "05302020"


def require_api_key() -> str:
    api_key = os.environ.get("TYPECAST_API_KEY")
    if not api_key:
        raise SystemExit("Missing TYPECAST_API_KEY. Export it before running.")
    return api_key


def ensure_parent(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


def load_json(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"File not found: {path}")
    return json.loads(p.read_text())


def ffmpeg_normalize(in_path: Path, out_path: Path) -> None:
    ensure_parent(out_path)
    cmd = [
        "ffmpeg", "-y", "-i", str(in_path),
        "-ar", "48000", "-ac", "2", "-acodec", "pcm_s16le",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def download_to(path: Path, url: str):
    ensure_parent(path)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)


def tts_typecast(api_key: str, voice_id: str, text: str, out_wav: Path,
                 emotion: str = DEFAULT_EMOTION_PRESET, emotion_intensity: float = DEFAULT_EMOTION_INTENSITY,
                 tempo: float = DEFAULT_TEMPO, pitch: int = DEFAULT_PITCH, volume: int = DEFAULT_VOLUME):
    """Call Typecast TTS and save as 48kHz stereo WAV at out_wav.
    Attempts to request WAV; if API returns a URL or other format, handles it.
    """
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    # Clamp basics
    try:
        ei = max(0.0, min(2.0, float(emotion_intensity)))
    except Exception:
        ei = 1.0
    try:
        vol = int(max(0, min(100, int(volume))))
    except Exception:
        vol = 100

    payload = {
        "voice_id": voice_id,
        "text": text,
        # Model/language names may vary; these are common defaults
        "model": "ssfm-v21",
        "language": "eng",
        "seed": DEFAULT_SEED,
        "prompt": {
            "emotion_preset": emotion,
            "emotion_intensity": ei
        },
        "output": {
            "volume": vol,
            "audio_pitch": pitch,
            "audio_tempo": tempo,
            "audio_format": "wav"
        },
    }

    # Request synthesis
    r = requests.post(TYPECAST_API_URL, headers=headers, json=payload, timeout=120, stream=True)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise SystemExit(f"Typecast TTS error {r.status_code}: {detail}")

    content_type = r.headers.get("Content-Type", "")
    tmp_in = out_wav.with_suffix(".tc_in")  # raw response

    if "audio" in content_type:
        ensure_parent(tmp_in)
        with open(tmp_in, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    else:
        # Try JSON with an audio URL
        try:
            data = r.json()
        except Exception:
            data = None
        audio_url = None
        if isinstance(data, dict):
            audio_url = data.get("audio_url") or data.get("url")
        if not audio_url:
            # Fallback: write body and try ffmpeg anyway
            ensure_parent(tmp_in)
            with open(tmp_in, "wb") as f:
                f.write(r.content)
        else:
            tmp_in = out_wav.with_suffix(".tc_dl")
            download_to(tmp_in, audio_url)

    # Convert/normalize to 48kHz stereo WAV
    ffmpeg_normalize(tmp_in, out_wav)
    try:
        tmp_in.unlink()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest_csv", default=str(Path(__file__).resolve().parents[1] / "manifests/scene1.csv"))
    # New: prefer generator_inputs.json for per-role Typecast voice_id
    ap.add_argument("--generator_inputs_json", default=str(Path(__file__).resolve().parents[1] / "manifests/generator_inputs.json"))
    # Back-compat: optional legacy voice map JSON { "MediatorA": "tc_xxx", ... }
    ap.add_argument("--voice_map", default="")
    args = ap.parse_args()

    api_key = require_api_key()
    # Build voice map from generator_inputs.json characters.<Role>.typecast.voice_id
    voice_map: dict[str, str] = {}
    try:
        gen_inputs = load_json(args.generator_inputs_json)
        chars = (gen_inputs.get("characters") or {})
        for role, conf in chars.items():
            tc = (conf.get("typecast") or {})
            vid = (tc.get("voice_id") or "").strip()
            if vid:
                voice_map[role] = vid
    except Exception:
        voice_map = {}
    # Fallback to legacy voice_map JSON only if generator_inputs lacked entries and a path was provided
    if not voice_map and args.voice_map:
        try:
            legacy_map = load_json(args.voice_map)
            if isinstance(legacy_map, dict):
                voice_map = {str(k): str(v) for k, v in legacy_map.items() if str(v).strip()}
        except Exception:
            pass

    rows = []
    with open(args.manifest_csv, newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            rows.append(row)

    for row in rows:
        rid = row["id"].strip()
        speaker = row["speaker"].strip()
        audio_out = Path(row["audio"].strip())
        text = row["transcript"].strip()

        # Per-line overrides from CSV (fallback to internal defaults)
        r_emotion = (row.get("emotion_preset") or DEFAULT_EMOTION_PRESET).strip().lower()
        try:
            r_intensity = float(row.get("emotion_intensity") or DEFAULT_EMOTION_INTENSITY)
        except Exception:
            r_intensity = DEFAULT_EMOTION_INTENSITY
        try:
            r_tempo = float(row.get("tempo") or DEFAULT_TEMPO)
        except Exception:
            r_tempo = DEFAULT_TEMPO
        try:
            r_pitch = int(row.get("pitch") or DEFAULT_PITCH)
        except Exception:
            r_pitch = DEFAULT_PITCH
        try:
            r_volume = int(row.get("volume") or DEFAULT_VOLUME)
        except Exception:
            r_volume = DEFAULT_VOLUME

        # Skip if exists
        if audio_out.exists():
            print(f"[skip] {rid} {speaker} -> {audio_out.name} (exists)")
            continue

        # Resolve voice id by speaker/role name with a few common variants
        vid = (
            voice_map.get(speaker)
            or voice_map.get(speaker.capitalize())
            or voice_map.get(speaker.upper())
        )
        if not vid or "REPLACE_WITH_TYPECAST_VOICE_ID" in str(vid):
            raise SystemExit(
                "No valid Typecast voice_id for speaker "
                f"'{speaker}'. Ensure manifests/generator_inputs.json has "
                f"characters.{speaker}.typecast.voice_id populated."
            )

        print(f"[Typecast] {rid} {speaker} -> {audio_out.name}  voice_id={vid}  emo={r_emotion} inten={r_intensity} tempo={r_tempo} pitch={r_pitch} vol={r_volume}")
        tts_typecast(api_key, vid, text, audio_out,
                     emotion=r_emotion, emotion_intensity=r_intensity,
                     tempo=r_tempo, pitch=r_pitch, volume=r_volume)

    print(f"Done. Generated {len(rows)} wav files via Typecast.")


if __name__ == "__main__":
    main()


