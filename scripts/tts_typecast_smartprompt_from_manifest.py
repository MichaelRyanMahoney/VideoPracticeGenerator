#!/usr/bin/env python3
import argparse
import csv
import json
import math
import os
import subprocess
from pathlib import Path

import boto3
import requests

from workdir_utils import cleanup_work_dir, make_work_dir, should_keep_workdir

TYPECAST_API_URL = "https://api.typecast.ai/v1/text-to-speech"

DEFAULT_TEMPO = 1.0
DEFAULT_PITCH = 0
DEFAULT_VOLUME = 100
DEFAULT_SEED = 5302020
DEFAULT_MODEL = "ssfm-v30"
DEFAULT_CONTEXT_CHAR_LIMIT = 2000
DEFAULT_INCLUDE_SPEAKER_LABELS = False
DEFAULT_EMOTION_PRESET = "normal"
DEFAULT_EMOTION_INTENSITY = 1.0
VALID_EMOTION_PRESETS = {"normal", "happy", "sad", "angry", "whisper", "toneup", "tonedown"}


HESITANT_PREVIOUS_CONTEXT = (
    "The speaker is about to respond, but they hesitate. Their energy is noticeably low, and their tone is skeptical—"
    " as if they are not fully convinced by what was just said. They choose their words carefully, speaking softly"
    " with small pauses, sounding uncertain rather than assertive."
)

HESITANT_NEXT_CONTEXT = (
    "The other person hears that tentative, skeptical remark and replies carefully. They keep their own energy low"
    " and measured, responding as if the conversation is delicate and the speaker is unsure. The exchange stays cautious,"
    " with an undercurrent of doubt and restrained emotion."
)


def _boolish(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    return s in {"1", "true", "yes", "y", "t", "on"}


def _try_load_json(path: str) -> dict:
    try:
        p = Path(path)
        if not p.exists():
            return {}
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_hesitant_contexts(delivery_contexts_json: str) -> tuple[str, str]:
    """
    Load smart-prompt delivery contexts (NOT spoken text) from JSON.
    Falls back to the built-in defaults if missing/invalid.
    """
    data = _try_load_json(delivery_contexts_json)
    sp = (data.get("smart_prompt") or {}) if isinstance(data, dict) else {}
    hes = (sp.get("hesitant") or {}) if isinstance(sp, dict) else {}
    prev = (hes.get("previous_text") or "").strip()
    nxt = (hes.get("next_text") or "").strip()
    if not prev:
        prev = HESITANT_PREVIOUS_CONTEXT
    if not nxt:
        nxt = HESITANT_NEXT_CONTEXT
    return prev, nxt


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


def parse_pitch_to_semitones(value: object) -> float:
    """Accept either semitone pitch or multiplier ratio.

    - If pitch is in [0.5, 2.0], treat it as a ratio where 1.0 means neutral.
    - Otherwise treat it as semitones directly.
    """
    try:
        raw = float(value)
    except Exception:
        return float(DEFAULT_PITCH)
    if 0.5 <= raw <= 2.0:
        semitones = 12.0 * math.log(raw, 2)
    else:
        semitones = raw
    return max(-12.0, min(12.0, semitones))


def normalize_emotion_preset(value: str) -> str:
    emotion = (value or DEFAULT_EMOTION_PRESET).strip().lower()
    return emotion if emotion in VALID_EMOTION_PRESETS else DEFAULT_EMOTION_PRESET


def parse_float_or_default(value: object, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def parse_int_or_default(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def parse_character_typecast_defaults(tc: dict) -> dict[str, object]:
    defaults: dict[str, object] = {}
    if not isinstance(tc, dict):
        return defaults

    pitch = tc.get("pitch")
    if pitch is not None and str(pitch).strip() != "":
        defaults["pitch"] = parse_float_or_default(pitch, DEFAULT_PITCH)

    speed = tc.get("speed")
    if speed is None or str(speed).strip() == "":
        speed = tc.get("tempo")
    if speed is not None and str(speed).strip() != "":
        defaults["tempo"] = parse_float_or_default(speed, DEFAULT_TEMPO)

    volume = tc.get("volume")
    if volume is not None and str(volume).strip() != "":
        defaults["volume"] = parse_int_or_default(volume, DEFAULT_VOLUME)

    emotion = tc.get("emotion")
    if emotion is not None and str(emotion).strip() != "":
        defaults["emotion_preset"] = normalize_emotion_preset(str(emotion))

    return defaults


def pick_csv_or_character_default(
    row: dict,
    row_key: str,
    character_defaults: dict[str, object],
    character_key: str,
    fallback: object,
    parser,
):
    row_value = row.get(row_key)
    if row_value is not None and str(row_value).strip() != "":
        return parser(row_value, fallback)
    if character_key in character_defaults:
        return character_defaults.get(character_key)
    return fallback


def _is_s3_uri(s: str) -> bool:
    return isinstance(s, str) and s.startswith("s3://")


def _s3_parse_uri(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://"), f"Not an s3 uri: {uri}"
    no = uri[5:]
    b, k = no.split("/", 1)
    return b, k


def _s3_exists(s3, uri: str) -> bool:
    b, k = _s3_parse_uri(uri)
    try:
        s3.head_object(Bucket=b, Key=k)
        return True
    except Exception:
        return False


def _s3_upload_file(s3, src: Path, uri: str) -> None:
    b, k = _s3_parse_uri(uri)
    s3.upload_file(str(src), b, k)


def _speaker_label(
    speaker: str,
    role_to_name: dict[str, str],
    include_speaker_labels: bool,
) -> str:
    if not include_speaker_labels:
        return ""
    display = role_to_name.get(speaker) or speaker
    if display != speaker:
        return f"{speaker} ({display})"
    return speaker


def _format_context_line(
    speaker: str,
    transcript: str,
    role_to_name: dict[str, str],
    include_speaker_labels: bool,
) -> str:
    text = (transcript or "").strip()
    if not text:
        return ""
    label = _speaker_label(speaker, role_to_name, include_speaker_labels)
    if label:
        return f"{label}: {text}"
    return text


def _clip_context(lines: list[str], char_limit: int) -> str:
    if not lines:
        return ""
    out: list[str] = []
    total = 0
    for line in lines:
        chunk_len = len(line) + (1 if out else 0)
        if total + chunk_len > char_limit:
            break
        out.append(line)
        total += chunk_len
    return "\n".join(out)


def build_smart_prompt_context(
    rows: list[dict],
    row_idx: int,
    role_to_name: dict[str, str],
    char_limit: int,
    include_speaker_labels: bool,
) -> tuple[str, str]:
    # Collect previous rows from nearest backwards, then restore chronological order.
    prev_candidates: list[str] = []
    for i in range(row_idx - 1, -1, -1):
        line = _format_context_line(
            rows[i].get("speaker", "").strip(),
            rows[i].get("transcript", "").strip(),
            role_to_name,
            include_speaker_labels,
        )
        if line:
            prev_candidates.append(line)
    prev_candidates.reverse()
    previous_text = _clip_context(prev_candidates, char_limit)

    # Collect next rows in chronological order.
    next_candidates: list[str] = []
    for i in range(row_idx + 1, len(rows)):
        line = _format_context_line(
            rows[i].get("speaker", "").strip(),
            rows[i].get("transcript", "").strip(),
            role_to_name,
            include_speaker_labels,
        )
        if line:
            next_candidates.append(line)
    next_text = _clip_context(next_candidates, char_limit)

    return previous_text, next_text


def tts_typecast_smart_prompt(
    api_key: str,
    voice_id: str,
    text: str,
    out_wav: Path,
    previous_text: str,
    next_text: str,
    tempo: float = DEFAULT_TEMPO,
    pitch: float = DEFAULT_PITCH,
    volume: int = DEFAULT_VOLUME,
):
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    try:
        vol = int(max(0, min(200, int(volume))))
    except Exception:
        vol = 100
    pitch_semitones = parse_pitch_to_semitones(pitch)
    tempo_out = max(0.5, min(2.0, float(tempo)))

    payload = {
        "voice_id": voice_id,
        "text": text,
        "model": DEFAULT_MODEL,
        "language": "eng",
        "seed": DEFAULT_SEED,
        "prompt": {
            "emotion_type": "smart",
            "previous_text": previous_text,
            "next_text": next_text,
        },
        "output": {
            "volume": vol,
            "audio_pitch": pitch_semitones,
            "audio_tempo": tempo_out,
            "audio_format": "wav",
        },
    }

    r = requests.post(TYPECAST_API_URL, headers=headers, json=payload, timeout=120, stream=True)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise SystemExit(f"Typecast smart-prompt TTS error {r.status_code}: {detail}")

    content_type = r.headers.get("Content-Type", "")
    tmp_in = out_wav.with_suffix(".tc_in")

    if "audio" in content_type:
        ensure_parent(tmp_in)
        with open(tmp_in, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    else:
        try:
            data = r.json()
        except Exception:
            data = None
        audio_url = None
        if isinstance(data, dict):
            audio_url = data.get("audio_url") or data.get("url")
        if not audio_url:
            ensure_parent(tmp_in)
            with open(tmp_in, "wb") as f:
                f.write(r.content)
        else:
            tmp_in = out_wav.with_suffix(".tc_dl")
            download_to(tmp_in, audio_url)

    ffmpeg_normalize(tmp_in, out_wav)
    try:
        tmp_in.unlink()
    except Exception:
        pass


def tts_typecast_preset(
    api_key: str,
    voice_id: str,
    text: str,
    out_wav: Path,
    emotion: str = DEFAULT_EMOTION_PRESET,
    emotion_intensity: float = DEFAULT_EMOTION_INTENSITY,
    tempo: float = DEFAULT_TEMPO,
    pitch: float = DEFAULT_PITCH,
    volume: int = DEFAULT_VOLUME,
):
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    try:
        ei = max(0.0, min(2.0, float(emotion_intensity)))
    except Exception:
        ei = DEFAULT_EMOTION_INTENSITY
    try:
        vol = int(max(0, min(200, int(volume))))
    except Exception:
        vol = DEFAULT_VOLUME
    emotion_preset = normalize_emotion_preset(emotion)
    pitch_semitones = parse_pitch_to_semitones(pitch)
    tempo_out = max(0.5, min(2.0, float(tempo)))

    payload = {
        "voice_id": voice_id,
        "text": text,
        "model": DEFAULT_MODEL,
        "language": "eng",
        "seed": DEFAULT_SEED,
        "prompt": {
            "emotion_type": "preset",
            "emotion_preset": emotion_preset,
            "emotion_intensity": ei,
        },
        "output": {
            "volume": vol,
            "audio_pitch": pitch_semitones,
            "audio_tempo": tempo_out,
            "audio_format": "wav",
        },
    }

    r = requests.post(TYPECAST_API_URL, headers=headers, json=payload, timeout=120, stream=True)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise SystemExit(f"Typecast preset TTS error {r.status_code}: {detail}")

    content_type = r.headers.get("Content-Type", "")
    tmp_in = out_wav.with_suffix(".tc_in")

    if "audio" in content_type:
        ensure_parent(tmp_in)
        with open(tmp_in, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    else:
        try:
            data = r.json()
        except Exception:
            data = None
        audio_url = None
        if isinstance(data, dict):
            audio_url = data.get("audio_url") or data.get("url")
        if not audio_url:
            ensure_parent(tmp_in)
            with open(tmp_in, "wb") as f:
                f.write(r.content)
        else:
            tmp_in = out_wav.with_suffix(".tc_dl")
            download_to(tmp_in, audio_url)

    ffmpeg_normalize(tmp_in, out_wav)
    try:
        tmp_in.unlink()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest_csv", default=str(Path(__file__).resolve().parents[1] / "manifests/scene1.csv"))
    ap.add_argument("--generator_inputs_json", default=str(Path(__file__).resolve().parents[1] / "manifests/generator_inputs.json"))
    ap.add_argument(
        "--delivery_contexts_json",
        default=(
            os.environ.get("VPG_TYPECAST_DELIVERY_CONTEXTS_JSON")
            or str(Path(__file__).resolve().parents[1] / "manifests/typecast_delivery_contexts.json")
        ),
        help="Optional JSON defining Typecast smart-prompt delivery contexts (not spoken).",
    )
    ap.add_argument("--voice_map", default="")
    ap.add_argument("--context_char_limit", type=int, default=DEFAULT_CONTEXT_CHAR_LIMIT)
    ap.add_argument(
        "--include_speaker_labels",
        default="true" if DEFAULT_INCLUDE_SPEAKER_LABELS else "false",
        help="true/false for speaker labels in context",
    )
    args = ap.parse_args()

    include_speaker_labels = (
        str(args.include_speaker_labels).strip().lower() in {"1", "true", "yes", "y"}
    )
    context_char_limit = max(1, min(DEFAULT_CONTEXT_CHAR_LIMIT, int(args.context_char_limit)))

    api_key = require_api_key()
    hesitant_prev_ctx, hesitant_next_ctx = _load_hesitant_contexts(args.delivery_contexts_json)

    voice_map: dict[str, str] = {}
    role_to_name: dict[str, str] = {}
    typecast_defaults_by_role: dict[str, dict[str, object]] = {}
    try:
        gen_inputs = load_json(args.generator_inputs_json)
        chars = (gen_inputs.get("characters") or {})
        for role, conf in chars.items():
            tc = (conf.get("typecast") or {})
            vid = (tc.get("voice_id") or "").strip()
            if vid:
                voice_map[role] = vid
            display_name = (conf.get("name") or "").strip()
            if display_name:
                role_to_name[role] = display_name
            typecast_defaults_by_role[role] = parse_character_typecast_defaults(tc)
    except Exception:
        voice_map = {}
        role_to_name = {}
        typecast_defaults_by_role = {}

    if not voice_map and args.voice_map:
        try:
            legacy_map = load_json(args.voice_map)
            if isinstance(legacy_map, dict):
                voice_map = {str(k): str(v) for k, v in legacy_map.items() if str(v).strip()}
        except Exception:
            pass

    rows: list[dict] = []
    with open(args.manifest_csv, newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            rows.append(row)

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")) if any(
        (_is_s3_uri((r.get("audio") or "").strip()) for r in rows)
    ) else None

    for i, row in enumerate(rows):
        rid = row["id"].strip()
        speaker = row["speaker"].strip()
        audio_raw = (row.get("audio") or "").strip()
        audio_hash = (row.get("audio_hash") or "").strip()
        text = row["transcript"].strip()
        is_pause_row = speaker.upper() in {"PAUSE", "BREAK"}

        if is_pause_row:
            print(f"[skip] {rid} {speaker} (pause row)")
            continue
        if not audio_raw:
            raise SystemExit(
                f"Manifest speech row id={rid} speaker={speaker} missing audio destination."
            )
        if not text:
            raise SystemExit(
                f"Manifest speech row id={rid} speaker={speaker} missing transcript."
            )
        typecast_mode = (row.get("typecast_mode") or "smart").strip().lower()
        hesitant = _boolish(row.get("hesitant"))
        character_defaults = (
            typecast_defaults_by_role.get(speaker)
            or typecast_defaults_by_role.get(speaker.capitalize())
            or typecast_defaults_by_role.get(speaker.upper())
            or {}
        )
        character_has_emotion_override = "emotion_preset" in character_defaults
        use_preset_mode = (typecast_mode == "preset") or character_has_emotion_override
        # Force smart prompt when hesitant is enabled, regardless of any preset emotion overrides.
        if hesitant:
            use_preset_mode = False

        r_tempo = pick_csv_or_character_default(
            row=row,
            row_key="tempo",
            character_defaults=character_defaults,
            character_key="tempo",
            fallback=DEFAULT_TEMPO,
            parser=parse_float_or_default,
        )
        r_pitch = pick_csv_or_character_default(
            row=row,
            row_key="pitch",
            character_defaults=character_defaults,
            character_key="pitch",
            fallback=DEFAULT_PITCH,
            parser=parse_float_or_default,
        )
        r_volume = pick_csv_or_character_default(
            row=row,
            row_key="volume",
            character_defaults=character_defaults,
            character_key="volume",
            fallback=DEFAULT_VOLUME,
            parser=parse_int_or_default,
        )
        r_emotion = (
            (row.get("emotion_preset") or "").strip().lower()
            or str(character_defaults.get("emotion_preset") or DEFAULT_EMOTION_PRESET)
        )
        r_emotion = normalize_emotion_preset(r_emotion)
        r_intensity = parse_float_or_default(
            row.get("emotion_intensity") or DEFAULT_EMOTION_INTENSITY,
            DEFAULT_EMOTION_INTENSITY,
        )

        audio_is_s3 = _is_s3_uri(audio_raw)
        if audio_is_s3:
            if not s3:
                raise SystemExit("audio is s3://... but boto3 client could not be created (missing AWS_REGION/AWS creds?)")
            if _s3_exists(s3, audio_raw):
                print(f"[skip] {rid} {speaker} -> {audio_hash or audio_raw} (s3 exists)")
                continue
            tmp_dir = make_work_dir("vpg_tts_smart_")
            audio_out = tmp_dir / f"{audio_hash or (speaker + '_' + rid)}.wav"
        else:
            audio_out = Path(audio_raw)
            if audio_out.exists():
                print(f"[skip] {rid} {speaker} -> {audio_out.name} (exists)")
                continue

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

        if use_preset_mode:
            pitch_st = parse_pitch_to_semitones(r_pitch)
            print(
                f"[Typecast Preset] {rid} {speaker} -> {audio_out.name}  voice_id={vid}  "
                f"model={DEFAULT_MODEL} emo={r_emotion} inten={r_intensity} "
                f"tempo={r_tempo} pitch={r_pitch} ({pitch_st:.2f}st) vol={r_volume}"
            )
            tts_typecast_preset(
                api_key=api_key,
                voice_id=vid,
                text=text,
                out_wav=audio_out,
                emotion=r_emotion,
                emotion_intensity=r_intensity,
                tempo=r_tempo,
                pitch=r_pitch,
                volume=r_volume,
            )
        else:
            if hesitant:
                previous_text, next_text = hesitant_prev_ctx, hesitant_next_ctx
            else:
                previous_text, next_text = build_smart_prompt_context(
                    rows=rows,
                    row_idx=i,
                    role_to_name=role_to_name,
                    char_limit=context_char_limit,
                    include_speaker_labels=include_speaker_labels,
                )

            print(
                f"[Typecast SmartPrompt] {rid} {speaker} -> {audio_out.name}  voice_id={vid}  "
                f"model={DEFAULT_MODEL} prev_chars={len(previous_text)} next_chars={len(next_text)} "
                f"tempo={r_tempo} pitch={r_pitch} vol={r_volume}"
            )
            tts_typecast_smart_prompt(
                api_key=api_key,
                voice_id=vid,
                text=text,
                out_wav=audio_out,
                previous_text=previous_text,
                next_text=next_text,
                tempo=r_tempo,
                pitch=r_pitch,
                volume=r_volume,
            )

        if audio_is_s3:
            assert s3 is not None
            print(f"[s3] upload -> {audio_raw}")
            _s3_upload_file(s3, audio_out, audio_raw)
            if not should_keep_workdir():
                cleanup_work_dir(tmp_dir)

    print(f"Done. Generated {len(rows)} wav files via Typecast smart prompt.")


if __name__ == "__main__":
    main()

