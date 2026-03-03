# scripts/whisperx_to_director_visemes.py
#
# Build a director.json that carries viseme events using your rig's viseme
# shape key names (e.g., "viseme_PP","FF","TH","DD","kk","CH","SS","nn","RR","aa","E","I","O").
#
import argparse, json, wave, csv, re, os, tempfile, hashlib
from pathlib import Path
import boto3

import torch
import whisperx
import nltk
from g2p_en import G2p
from ovr_viseme_map import phoneme_to_viseme

VOWELS = {"AA","AE","AH","AO","AW","AY","EH","ER","EY","IH","IY","OW","OY","UH","UW","AX"}
STRONG_ONSET_GROUPS = {"viseme_PP","viseme_FF","viseme_TH","viseme_CH","viseme_SS"}  # visual consonants

def _ensure_nltk_resource(resource_path: str, download_id: str) -> None:
    try:
        nltk.data.find(resource_path)
        return
    except LookupError:
        pass
    print(f"[nltk] downloading {download_id}...")
    nltk.download(download_id, quiet=True)
    # Re-check so we fail early with a clear message if download failed.
    nltk.data.find(resource_path)


def ensure_nltk_for_g2p() -> None:
    """Ensure runtime NLTK assets required by g2p_en are available."""
    # Keep NLTK assets in-repo by default when no explicit NLTK_DATA is provided.
    if not (os.environ.get("NLTK_DATA") or "").strip():
        local_nltk = Path(__file__).resolve().parents[1] / "nltk_data"
        local_nltk.mkdir(parents=True, exist_ok=True)
        os.environ["NLTK_DATA"] = str(local_nltk)
    # Required for POS tagging in g2p_en.
    try:
        _ensure_nltk_resource("taggers/averaged_perceptron_tagger_eng", "averaged_perceptron_tagger_eng")
    except LookupError:
        # Older NLTK distributions still use the legacy name.
        _ensure_nltk_resource("taggers/averaged_perceptron_tagger", "averaged_perceptron_tagger")
    # Required for dictionary-backed phoneme lookup in g2p_en.
    _ensure_nltk_resource("corpora/cmudict", "cmudict")


def make_g2p() -> G2p:
    ensure_nltk_for_g2p()
    return G2p()


def word_to_phones_with_stress(word, g2p):
    """
    Return list of (base_phone, stress_digit_or_None), e.g., [("AE","1"), ("T",None)]
    """
    try:
        tokens = g2p(word)
    except LookupError:
        # Defensive fallback if NLTK data is missing at runtime.
        ensure_nltk_for_g2p()
        tokens = g2p(word)
    out = []
    for t in tokens:
        t = t.strip()
        if not t or t == " ":
            continue
        if not any(c.isalpha() for c in t):
            continue
        base = "".join([c for c in t if not c.isdigit()]).upper()
        stress = "".join([c for c in t if c.isdigit()]) or None
        if base and any(ch.isalpha() for ch in base):
            out.append((base, stress))
    return out or [("AE","1")]

def distribute_times(start, end, n):
    if n <= 0:
        return []
    if end <= start:
        return [start]
    step = (end - start) / n
    return [start + (i + 0.5) * step for i in range(n)]

def load_aligner():
    if torch.cuda.is_available():
        device_align = "cuda"
    elif torch.backends.mps.is_available():
        device_align = "mps"
    else:
        device_align = "cpu"
    print(f"[whisperx] align device: {device_align}")

    # Keep alignment model downloads out of container root by using a configurable cache directory.
    # WhisperX passes model_dir through to torchaudio's download utilities.
    model_dir = (os.environ.get("VPG_ALIGN_MODEL_DIR") or "").strip()
    if not model_dir:
        torch_home = (os.environ.get("TORCH_HOME") or "").strip()
        if torch_home:
            model_dir = str(Path(torch_home) / "torchaudio")
    kwargs = {}
    if model_dir:
        p = Path(model_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        kwargs["model_dir"] = str(p)
        print(f"[whisperx] align model_dir: {p}")

    align_model, metadata = whisperx.load_align_model(language_code="en", device=device_align, **kwargs)
    return align_model, metadata, device_align

def get_wav_duration_seconds(audio_path: str) -> float:
    try:
        with wave.open(audio_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return frames / float(rate)
    except Exception:
        return 0.0

def align_line(audio_path, transcript, align_model, metadata, device_align):
    dur = max(0.01, get_wav_duration_seconds(audio_path))
    segs = [{"start": 0.0, "end": float(dur), "text": transcript}]
    return whisperx.align(segs, align_model, metadata, audio_path, device_align)

def collapse_adjacent_identical(visemes):
    if not visemes:
        return visemes
    out = [visemes[0]]
    for v in visemes[1:]:
        if v["p"] == out[-1]["p"]:
            # keep earlier or later? keep earlier; ignore duplicate
            continue
        out.append(v)
    return out

def enforce_min_gap(visemes, min_gap_sec):
    if not visemes or min_gap_sec <= 0:
        return visemes
    out = []
    last_t = None
    for v in visemes:
        if last_t is None or (v["t"] - last_t) >= min_gap_sec:
            out.append(v)
            last_t = v["t"]
        else:
            # too close: prefer vowel over consonant; else keep earlier
            is_vowel = any(v["p"].endswith(suf) for suf in ("_aa","_E","_I","_O"))
            if is_vowel and out:
                out[-1] = v
                last_t = v["t"]
    return out

def select_visemes_for_word(word, t0, t1, g2p, strategy="onset_plus_vowel", max_events_per_word=2):
    """
    strategy:
      - "vowel_only": pick primary stressed vowel (or first vowel), 1 event
      - "onset_plus_vowel": optional strong onset consonant + main vowel (<=2)
      - "all": map all phones (legacy behavior, not recommended)
    """
    phones = word_to_phones_with_stress(word, g2p)
    bases = [p for p,_ in phones]
    # Find vowels with their indices and stress
    vowel_idxs = [(i, p, stress) for i,(p,stress) in enumerate(phones) if p in VOWELS]
    main_vowel_idx = None
    if vowel_idxs:
        # prefer primary stress '1', else secondary '2', else first vowel
        for i,p,s in vowel_idxs:
            if s == "1":
                main_vowel_idx = i
                break
        if main_vowel_idx is None:
            for i,p,s in vowel_idxs:
                if s == "2":
                    main_vowel_idx = i
                    break
        if main_vowel_idx is None:
            main_vowel_idx = vowel_idxs[0][0]
    # Choose candidate visemes
    chosen = []
    if strategy in ("vowel_only","onset_plus_vowel"):
        # main vowel
        if main_vowel_idx is not None:
            chosen.append(main_vowel_idx)
        # optional salient onset before the vowel
        if strategy == "onset_plus_vowel" and main_vowel_idx is not None:
            # scan left from vowel to find first strong consonant
            for j in range(main_vowel_idx - 1, -1, -1):
                vkey = phoneme_to_viseme(bases[j])
                if vkey in STRONG_ONSET_GROUPS:
                    chosen.insert(0, j)
                    break
    else:  # "all"
        chosen = list(range(len(bases)))
    # Map indices to visemes and times
    chosen = sorted(set(chosen))
    if not chosen:
        # fallback: first phone's mapping
        chosen = [0]
    times = distribute_times(t0, t1, len(chosen))
    visemes = [{"p": phoneme_to_viseme(bases[idx]), "t": times[k]} for k, idx in enumerate(chosen)]
    # Cap events per word
    if max_events_per_word and len(visemes) > max_events_per_word:
        # keep vowel and, if present, onset; else keep first two
        if strategy in ("vowel_only","onset_plus_vowel"):
            # if we had two chosen, they already are onset+vowel; truncate otherwise
            visemes = visemes[:max_events_per_word]
        else:
            visemes = visemes[:max_events_per_word]
    return visemes

def _load_allowed_roles(generator_inputs_path: str) -> set[str]:
    p = Path(generator_inputs_path)
    data = json.loads(p.read_text())
    roles = set((data.get("characters") or {}).keys())
    if not roles:
        raise RuntimeError(f"No roles found in {generator_inputs_path}/characters")
    return roles


def _is_s3_uri(s: str) -> bool:
    return isinstance(s, str) and s.startswith("s3://")


def _s3_parse_uri(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://"), f"Not an s3 uri: {uri}"
    no = uri[5:]
    bucket, key = no.split("/", 1)
    return bucket, key


def _s3_download(s3, uri: str, dest: Path) -> None:
    b, k = _s3_parse_uri(uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(b, k, str(dest))


def _ensure_local_audio(audio_ref: str, cache_dir: Path, s3) -> str:
    """
    Return a local filesystem path to a WAV for whisperx alignment.
    If audio_ref is s3://..., download into cache_dir.
    """
    if not _is_s3_uri(audio_ref):
        return str(Path(audio_ref).resolve())
    if s3 is None:
        raise RuntimeError("Audio is s3:// but no boto3 client was provided")
    # Deterministic cache name based on S3 key
    _b, key = _s3_parse_uri(audio_ref)
    safe_name = key.replace("/", "__")
    local = cache_dir / safe_name
    if not local.exists():
        print(f"[s3] download {audio_ref} -> {local}")
        _s3_download(s3, audio_ref, local)
    return str(local.resolve())

def batch_mode(manifest_csv, generator_inputs_json, fps, out_path, gap_sec=0.75, strategy="onset_plus_vowel", max_events_per_word=2, min_event_gap_sec=0.08, collapse_adjacent=True, audio_cache_dir: str = ""):
    align_model, metadata, device_align = load_aligner()
    g2p = make_g2p()

    allowed_roles = _load_allowed_roles(generator_inputs_json)
    beats = []
    t_cursor = 0.0

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    cache_dir = Path(audio_cache_dir) if audio_cache_dir else Path(tempfile.gettempdir()) / "vpg_audio_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    with open(manifest_csv, newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            speaker = row["speaker"].strip()
            if speaker not in allowed_roles:
                raise RuntimeError(f"Speaker '{speaker}' is not a valid role. Use one of: {sorted(allowed_roles)}")
            audio_ref = (row.get("audio") or "").strip()
            if not audio_ref:
                raise RuntimeError("Manifest row missing audio")
            audio_local = _ensure_local_audio(audio_ref, cache_dir, s3 if _is_s3_uri(audio_ref) else None)
            transcript = row["transcript"].strip()

            aligned = align_line(audio_local, transcript, align_model, metadata, device_align)

            words = []
            for seg in aligned.get("segments", []):
                for w in seg.get("words", []):
                    if "start" in w and "end" in w and w.get("word"):
                        words.append((w["word"], float(w["start"]), float(w["end"])))

            vis = []
            for wd, t0, t1 in words:
                word_vis = select_visemes_for_word(
                    wd, t0, t1, g2p,
                    strategy=strategy,
                    max_events_per_word=max_events_per_word
                )
                # shift by running cursor
                for ev in word_vis:
                    ev["t"] = round(t_cursor + ev["t"], 3)
                vis += word_vis
            if collapse_adjacent:
                vis = collapse_adjacent_identical(vis)
            if min_event_gap_sec and min_event_gap_sec > 0:
                vis = enforce_min_gap(vis, min_event_gap_sec)

            beats.append({
                "tc_in": f"00:00:{t_cursor:06.3f}",
                "char": speaker,
                # Keep the audio reference as-is (local path or s3:// URI). Downstream workers can fetch if needed.
                "audio": audio_ref,
                "visemes": vis,
            })

            if words:
                t_cursor += words[-1][2] + float(gap_sec)
            else:
                t_cursor += float(gap_sec)

    director = {
        "project": "FourHeadDemo",
        "fps": fps,
        "render": {"resolution": [1920, 1080], "engine": "BLENDER_EEVEE", "output": "out/demo.mp4"},
        # Note: assets and mesh mapping are now defined via generator_inputs.json; this 'assets' field is intentionally omitted.
        "beats": beats
    }
    Path(out_path).write_text(json.dumps(director, indent=2))
    print(f"[out] Wrote {out_path} with {len(beats)} beats")

SPEAKER_LINE = re.compile(r'^\s*([A-Z0-9 ]+?)(?:\s*\(([A-Z \.]+)\))?\s*(?:\{([^}]*)\})?\s*$')
PAUSE_TOKEN = re.compile(r'\[PAUSE\]', re.IGNORECASE)
SHOW_TOKEN = re.compile(r'^\s*\[\s*SHOW\s+MEDIATOR\s+([AB])\s*\]\s*$', re.IGNORECASE)

def _parse_stage_from_script(script_path: str) -> tuple[dict, str, int]:
    """
    Return:
      - pauses_after_by_idx: map of 1-based speech index -> number of [PAUSE] tokens that appear
        immediately after that speech block (before the next speaker).
      - start_mediator: 'A' or 'B' if a [SHOW MEDIATOR X] directive appears before the first speech;
        defaults to 'A' otherwise.
      - pauses_before_first: number of [PAUSE] tokens encountered before the first speech block
        (used to insert an initial delay at the timeline start).
    """
    p = Path(script_path)
    if not p.exists():
        return {}, "A", 0
    lines = p.read_text().splitlines()
    pauses_after: dict[int, int] = {}
    start_mediator = "A"
    pauses_before_first = 0
    idx = 0
    i = 0
    # Detect first global SHOW before any speech
    while i < len(lines):
        t = lines[i].strip()
        if not t:
            i += 1
            continue
        m_show = SHOW_TOKEN.match(t)
        if m_show and idx == 0:
            start_mediator = (m_show.group(1) or "A").upper()
            i += 1
            continue
        # Count any [PAUSE] directives before first speech as initial delay
        if idx == 0 and t.startswith("[") and t.endswith("]"):
            pauses_before_first += len(PAUSE_TOKEN.findall(t))
            i += 1
            continue
        m = SPEAKER_LINE.match(t)
        if m:
            # consume this speech block
            idx += 1
            j = i + 1
            # Walk until blank, another speaker header, or bracketed directive
            while j < len(lines):
                u = lines[j].strip()
                if not u:
                    break
                if SPEAKER_LINE.match(u):
                    break
                if u.startswith("[") and u.endswith("]"):
                    # Accumulate all PAUSE tokens on bracketed lines after this speech
                    # (including multiple PAUSE tokens on one line)
                    count = len(PAUSE_TOKEN.findall(u))
                    if count:
                        pauses_after[idx] = pauses_after.get(idx, 0) + count
                    # Also allow SHOW directives mid-script (ignored for start)
                    j += 1
                    # keep scanning subsequent bracket lines
                    continue
                # normal spoken text line
                j += 1
            i = j
        else:
            i += 1
    return pauses_after, start_mediator, pauses_before_first

def batch_mode_with_stage(manifest_csv, generator_inputs_json, fps, out_path, script_txt=None, gap_sec=0.75, strategy="onset_plus_vowel", max_events_per_word=2, min_event_gap_sec=0.08, collapse_adjacent=True, pause_seconds=0.5, audio_cache_dir: str = ""):
    align_model, metadata, device_align = load_aligner()
    g2p = make_g2p()

    allowed_roles = _load_allowed_roles(generator_inputs_json)
    beats = []
    t_cursor = 0.0
    pauses_map: dict[int, int] = {}
    start_mediator = "A"
    pauses_before_first = 0

    # If script is provided or found at project root, parse stage directions
    st_path = None
    if script_txt:
        st_path = Path(script_txt)
    else:
        # default fallback: project/script.txt
        st_path = Path(__file__).resolve().parents[1] / "script.txt"
    if st_path and st_path.exists():
        pauses_map, start_mediator, pauses_before_first = _parse_stage_from_script(str(st_path))

    rows: list[dict] = []
    with open(manifest_csv, newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            rows.append(row)

    def _is_manifest_pause_row(row: dict) -> bool:
        speaker = (row.get("speaker") or "").strip()
        transcript = (row.get("transcript") or "").strip()
        return (
            speaker.upper() in {"PAUSE", "BREAK"} or
            PAUSE_TOKEN.search(transcript) is not None
        )

    manifest_has_pause_rows = any(_is_manifest_pause_row(r) for r in rows)
    if manifest_has_pause_rows:
        # Pause timing is now encoded directly in the manifest; avoid script-derived duplication.
        pauses_map = {}
        pauses_before_first = 0

    # Apply any initial [PAUSE]s before first speech as explicit pause beats
    if pauses_before_first and pause_seconds:
        try:
            for _ in range(int(pauses_before_first)):
                # Create a dedicated pause beat (no audio, no visemes)
                beats.append({
                    "type": "pause",
                    "tc_in": f"00:00:{t_cursor:06.3f}",
                    "duration": float(pause_seconds)
                })
                t_cursor += float(pause_seconds)
        except Exception:
            pass

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    cache_dir = Path(audio_cache_dir) if audio_cache_dir else Path(tempfile.gettempdir()) / "vpg_audio_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    speech_idx = 0  # counts only spoken lines (aligns [PAUSE]s from script)
    for row in rows:
        speaker = (row.get("speaker") or "").strip()
        transcript = (row.get("transcript") or "").strip()
        audio_ref = (row.get("audio") or "").strip()
        manifest_id = str(row.get("id") or "").strip()
        audio_hash = str(row.get("audio_hash") or "").strip()
        # Stable transcript hash (useful for change detection without bloating director JSON).
        transcript_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest() if transcript else ""
        # Allow explicit pauses in the manifest:
        # - speaker "PAUSE" or "BREAK"
        # - transcript contains [PAUSE]
        is_manifest_pause = (
            speaker.upper() in {"PAUSE", "BREAK"} or
            PAUSE_TOKEN.search(transcript) is not None
        )
        if is_manifest_pause:
            try:
                dur = float(row.get("duration") or pause_seconds or 1.0)
            except Exception:
                dur = float(pause_seconds or 1.0)
            beats.append({
                "type": "pause",
                "tc_in": f"00:00:{t_cursor:06.3f}",
                "duration": float(dur),
                "manifest_id": manifest_id or None,
            })
            t_cursor += float(dur)
            # Do not increment speech_idx for pause rows
            continue

        if speaker not in allowed_roles:
            raise RuntimeError(f"Speaker '{speaker}' is not a valid role. Use one of: {sorted(allowed_roles)}")
        if not audio_ref:
            raise RuntimeError(
                f"Manifest speech row id={row.get('id', '?')} speaker={speaker} is missing audio. "
                "Use a PAUSE/BREAK row for non-spoken timeline gaps."
            )
        audio_local = _ensure_local_audio(audio_ref, cache_dir, s3 if _is_s3_uri(audio_ref) else None)

        aligned = align_line(audio_local, transcript, align_model, metadata, device_align)
        # Use actual WAV duration to space beats (prevents overlap eating pauses)
        wav_dur = get_wav_duration_seconds(audio_local)

        words = []
        for seg in aligned.get("segments", []):
            for w in seg.get("words", []):
                if "start" in w and "end" in w and w.get("word"):
                    words.append((w["word"], float(w["start"]), float(w["end"])))

        vis = []
        for wd, t0, t1 in words:
            word_vis = select_visemes_for_word(
                wd, t0, t1, g2p,
                strategy=strategy,
                max_events_per_word=max_events_per_word
            )
            # shift by running cursor
            for ev in word_vis:
                ev["t"] = round(t_cursor + ev["t"], 3)
            vis += word_vis
        if collapse_adjacent:
            vis = collapse_adjacent_identical(vis)
        if min_event_gap_sec and min_event_gap_sec > 0:
            vis = enforce_min_gap(vis, min_event_gap_sec)

        beats.append({
            "tc_in": f"00:00:{t_cursor:06.3f}",
            "char": speaker,
            "audio": audio_ref,
            "manifest_id": manifest_id or None,
            "audio_hash": audio_hash or None,
            "transcript_hash": transcript_hash or None,
            "visemes": vis,
        })

        # Advance by the full WAV duration to avoid overlaps, then add default gap
        try:
            t_cursor += float(wav_dur) + float(gap_sec)
        except Exception:
            t_cursor += float(gap_sec)
        # Insert additional explicit pause beat(s) for [PAUSE] markers after this speech
        speech_idx += 1
        if pauses_map and pause_seconds:
            try:
                num_pauses = int(pauses_map.get(speech_idx, 0))
            except Exception:
                num_pauses = 0
            for _ in range(num_pauses):
                beats.append({
                    "type": "pause",
                    "tc_in": f"00:00:{t_cursor:06.3f}",
                    "duration": float(pause_seconds)
                })
                t_cursor += float(pause_seconds)

    director = {
        "project": "FourHeadDemo",
        "fps": fps,
        "render": {"resolution": [1920, 1080], "engine": "BLENDER_EEVEE", "output": "out/demo.mp4"},
        # Stage configuration for the renderer (used for initial visibility/fades)
        "stage": {
            "start_mediator": start_mediator,
            "fade_disputants_sec": 1.0
        },
        "beats": beats
    }
    Path(out_path).write_text(json.dumps(director, indent=2))
    print(f"[out] Wrote {out_path} with {len(beats)} beats")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest_csv", required=True, help="CSV with id,speaker,audio,transcript")
    ap.add_argument("--generator_inputs_json", help="Path to manifests/generator_inputs.json; used to validate roles", default=str(Path(__file__).resolve().parents[1] / "manifests" / "generator_inputs.json"))
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--out", default="director_visemes.json")
    ap.add_argument("--gap_sec", type=float, default=0.75, help="Pause inserted between lines (seconds)")
    ap.add_argument("--strategy", choices=["vowel_only","onset_plus_vowel","all"], default="onset_plus_vowel", help="Event selection per word")
    ap.add_argument("--max_events_per_word", type=int, default=2, help="Cap number of viseme events per word")
    ap.add_argument("--min_event_gap_sec", type=float, default=0.08, help="Minimum time between consecutive viseme events")
    ap.add_argument("--no_collapse_adjacent", action="store_true", help="Disable collapsing adjacent identical visemes")
    ap.add_argument("--script_txt", help="Optional path to script.txt to parse [PAUSE] and [SHOW MEDIATOR X] directives")
    ap.add_argument("--pause_seconds", type=float, default=0.5, help="Seconds to insert per [PAUSE] directive")
    ap.add_argument("--audio_cache_dir", default="", help="Directory to cache downloaded audio when manifest uses s3:// URIs")
    args = ap.parse_args()

    # Use enhanced builder that incorporates stage directions from script (if provided)
    batch_mode_with_stage(
        args.manifest_csv,
        args.generator_inputs_json,
        args.fps,
        args.out,
        script_txt=args.script_txt,
        gap_sec=args.gap_sec,
        strategy=args.strategy,
        max_events_per_word=args.max_events_per_word,
        min_event_gap_sec=args.min_event_gap_sec,
        collapse_adjacent=(not args.no_collapse_adjacent),
        pause_seconds=args.pause_seconds,
        audio_cache_dir=args.audio_cache_dir,
    )

if __name__ == "__main__":
    main()


