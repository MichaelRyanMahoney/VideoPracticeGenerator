#!/usr/bin/env python3
"""
Insert freeze + slate overlay pauses into a base MP4, driven by [OVERLAY] markers
in script.txt and aligned to the director_visemes.json timeline.

What it does
- Scans script.txt for tokens: spoken blocks, [PAUSE], [OVERLAY], and [ProcessFormSwap].
- Aligns token order to director_visemes.json beats (speech vs pause).
- For each [OVERLAY], picks the tc_in (seconds) of the next beat as insertion time.
- Builds a final MP4 by:
  - Transcoding pre/post segments from the base video with consistent encoding
  - Creating a pause clip at each insertion time by freezing the exact frame
    and overlaying the provided slate PNG (with fade in/out) over silence
  - Concatenating all segments losslessly via concat demuxer (-c copy)

Notes
- This preserves A/V sync by inserting silence during the pause so subsequent
  content stays aligned.
- Requires ffmpeg in PATH.
"""
import argparse
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
from typing import Any, Dict
import shutil
import math


def parse_timecode_to_seconds(tc: str) -> float:
    tc = (tc or "").strip()
    if not tc:
        return 0.0
    hh, mm, ss = tc.split(":")
    return float(hh) * 3600.0 + float(mm) * 60.0 + float(ss)


SPEAKER_LINE = re.compile(r'^\s*([A-Z0-9 ]+?)(?:\s*\(([A-Z \.]+)\))?\s*(?:\{[^}]*\})?\s*$')
OVERLAY_MARKER = re.compile(r'^\[OVERLAY(\d+)?\]$', re.IGNORECASE)


def parse_script_tokens(script_text: str) -> list[str]:
    """
    Return a list of tokens in chronological order:
      - "line" for a spoken block (speaker line followed by 1+ text lines)
      - "pause" for a standalone [PAUSE]
      - "overlay" for a standalone [OVERLAY] or [OVERLAYn]
      - "pf_swap" for a standalone [ProcessFormSwap]
    """
    lines = script_text.splitlines()
    i = 0
    tokens: list[str] = []
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        stripped = raw.strip()
        # Simple stage directives
        if stripped == "[PAUSE]":
            tokens.append("pause")
            i += 1
            continue
        if OVERLAY_MARKER.match(stripped):
            tokens.append("overlay")
            i += 1
            continue
        if stripped == "[ProcessFormSwap]":
            tokens.append("pf_swap")
            i += 1
            continue
        # Speaker blocks
        m = SPEAKER_LINE.match(stripped)
        if m and i + 1 < len(lines):
            j = i + 1
            spoken_found = False
            while j < len(lines):
                t = lines[j].strip()
                if not t:
                    break
                if t.startswith("[") and t.endswith("]"):
                    break
                if SPEAKER_LINE.match(t):
                    break
                # treat as spoken content or inline directives
                spoken_found = True
                j += 1
            if spoken_found:
                tokens.append("line")
            i = j
        else:
            i += 1
    return tokens


@dataclass
class BeatToken:
    kind: str  # "line" or "pause"
    tc_in_sec: float


def build_beats_tokens(director: dict) -> list[BeatToken]:
    tokens: list[BeatToken] = []
    for b in director.get("beats", []):
        if b.get("type") == "pause":
            tokens.append(BeatToken("pause", parse_timecode_to_seconds(b.get("tc_in", "00:00:00.000"))))
        else:
            tokens.append(BeatToken("line", parse_timecode_to_seconds(b.get("tc_in", "00:00:00.000"))))
    return tokens


def extract_header_value(script_text: str, key_prefix: str) -> str:
    """
    Find a line like 'CONFLICT DESCRIPTION: something' and return the value trimmed.
    """
    for line in script_text.splitlines():
        if line.upper().startswith(key_prefix.upper()):
            # Split on first colon
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return ""


def wrap_text(src: str, max_chars: int = 64) -> str:
    """
    Simple hard wrap at whitespace boundaries for ffmpeg drawtext.
    """
    words = src.split()
    lines: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for w in words:
        if cur_len + (1 if cur else 0) + len(w) > max_chars:
            if cur:
                lines.append(" ".join(cur))
            cur = [w]
            cur_len = len(w)
        else:
            cur.append(w)
            cur_len += (1 if cur_len > 0 else 0) + len(w)
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)

def map_overlays_to_times(script_tokens: list[str], beat_tokens: list[BeatToken], anchor: str = "prev_end") -> list[float]:
    """
    Align script token sequence to beat token sequence by kind ("line"/"pause").
    For each "overlay":
      - anchor == "prev_end": use the END of the previous beat, which is the START of the next beat
      - anchor == "next_start": use the START of the next beat
    Fallbacks:
      - if no next beat exists, drop overlay (can't place after end)
    """
    overlay_times: list[float] = []
    si = 0
    bi = 0
    while si < len(script_tokens):
        st = script_tokens[si]
        if st in ("line", "pause"):
            # advance beats until we match the same kind
            while bi < len(beat_tokens) and beat_tokens[bi].kind != st:
                bi += 1
            # consume one matched beat if present
            if bi < len(beat_tokens):
                bi += 1
            si += 1
            continue
        if st == "overlay":
            # overlay timing based on requested anchor
            if bi < len(beat_tokens):
                if anchor == "prev_end":
                    # previous beat end == next beat start
                    overlay_times.append(beat_tokens[bi].tc_in_sec)
                else:
                    # next_start (legacy behavior)
                    overlay_times.append(beat_tokens[bi].tc_in_sec)
            # If we're at end, we can't place overlay; silently skip
            si += 1
            continue
        # unknown token, skip
        si += 1
    return overlay_times


def map_pf_swaps_to_times(script_tokens: list[str], beat_tokens: list[BeatToken], anchor: str = "prev_end") -> list[float]:
    """
    Align script token sequence to beat token sequence; collect times for [ProcessFormSwap] markers.
    Uses same anchoring strategy as overlays.
    """
    times: list[float] = []
    si = 0
    bi = 0
    while si < len(script_tokens):
        st = script_tokens[si]
        if st in ("line", "pause"):
            while bi < len(beat_tokens) and beat_tokens[bi].kind != st:
                bi += 1
            if bi < len(beat_tokens):
                bi += 1
            si += 1
            continue
        if st == "pf_swap":
            if bi < len(beat_tokens):
                # For prev_end and next_start, this resolves to next beat start
                times.append(beat_tokens[bi].tc_in_sec)
            si += 1
            continue
        si += 1
    return times


def parse_overlay_ids(script_text: str) -> list[int | None]:
    """
    Extract overlay numeric IDs in order of appearance in the script:
    - [OVERLAY] => None
    - [OVERLAY7] => 7
    """
    ids: list[int | None] = []
    for line in script_text.splitlines():
        m = OVERLAY_MARKER.match(line.strip())
        if m:
            g = m.group(1)
            ids.append(int(g) if g and g.isdigit() else None)
    return ids


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as e:
        raise SystemExit(f"YAML config requested but PyYAML is not installed. Install with: pip install pyyaml\nDetails: {e}")
    with path.open("r") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise SystemExit("Top-level YAML config must be a mapping/object.")
    return data


def load_config(config_path: Path) -> Dict[str, Any]:
    """
    Load configuration from JSON (.json) or YAML (.yml/.yaml).
    """
    suffix = config_path.suffix.lower()
    if suffix == ".json":
        return json.loads(config_path.read_text() or "{}")
    if suffix in (".yml", ".yaml"):
        return _load_yaml(config_path)
    raise SystemExit(f"Unsupported config file extension: {suffix}. Use .json, .yml, or .yaml.")


def _resolve_path(base_dir: Path, value: Any) -> Any:
    if isinstance(value, str):
        p = Path(value)
        return str(p if p.is_absolute() else (base_dir / p))
    if isinstance(value, list):
        return [_resolve_path(base_dir, v) for v in value]
    return value


def resolve_paths_in_config(cfg: Dict[str, Any], cfg_dir: Path) -> Dict[str, Any]:
    """
    Resolve relative paths in known path-like fields relative to the config file directory.
    """
    path_keys = {
        "script", "director", "base", "overlay_image", "out",
        "pf_icon", "labels_bubble", "labels_fontfile",
        "intro_fontfile"
    }
    # intro_bg is a list of paths
    resolved = dict(cfg)
    for k in list(resolved.keys()):
        if k in path_keys:
            resolved[k] = _resolve_path(cfg_dir, resolved[k])
        if k == "intro_bg":
            resolved[k] = _resolve_path(cfg_dir, resolved[k])
        # Resolve new intro2 nested assets
        if k == "intro2" and isinstance(resolved[k], dict):
            intro2 = dict(resolved[k])
            if "bg" in intro2:
                intro2["bg"] = _resolve_path(cfg_dir, intro2.get("bg"))
            if "process_form_overlay" in intro2:
                intro2["process_form_overlay"] = _resolve_path(cfg_dir, intro2.get("process_form_overlay"))
            if "chars" in intro2 and isinstance(intro2["chars"], list):
                new_chars = []
                for item in intro2["chars"]:
                    if isinstance(item, dict) and "image" in item:
                        it = dict(item)
                        it["image"] = _resolve_path(cfg_dir, it.get("image"))
                        new_chars.append(it)
                    else:
                        new_chars.append(item)
                intro2["chars"] = new_chars
            resolved[k] = intro2
        if k == "overlays" and isinstance(resolved[k], list):
            new_list = []
            for item in resolved[k]:
                if isinstance(item, dict) and "image" in item:
                    it = dict(item)
                    it["image"] = _resolve_path(cfg_dir, item.get("image"))
                    new_list.append(it)
                else:
                    new_list.append(item)
            resolved[k] = new_list
    return resolved


def build_overlay_config_lookup(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a lookup:
      - 'by_id': {int -> dict(settings)}
      - 'default': dict(settings) for id == 'default' (optional)
    Accepts config schema:
      overlays: [{id: 1, image: "...", duration: 10.0, fade: 0.5, overlay_alpha: 0.9, pre_roll_sec: 0.0}, ...]
    """
    result = {"by_id": {}, "default": {}}
    lst = cfg.get("overlays")
    if not isinstance(lst, list):
        return result
    for item in lst:
        if not isinstance(item, dict):
            continue
        oid = item.get("id")
        if isinstance(oid, int):
            result["by_id"][int(oid)] = item
        elif isinstance(oid, str) and oid.lower() == "default":
            result["default"] = item
    return result


def find_overlay_image_for_id(
    overlay_id: int | None,
    args_overlay_image: str,
    cfg_lookup: Dict[str, Any],
    cfg_dir: Path | None,
    script_path: Path,
    base_path: Path,
) -> str:
    """
    Resolve the overlay image path for a given overlay_id.
    Priority:
      1) Config overlays[id].image
      2) Config overlays['default'].image
      3) File named Overlay{n}.png in candidate folders (if id is not None)
      4) Fallback to args.overlay_image
    """
    # 1) per-id config
    by_id = cfg_lookup.get("by_id", {})
    default_cfg = cfg_lookup.get("default", {})
    img = None
    if overlay_id is not None and overlay_id in by_id:
        img = by_id[overlay_id].get("image")
        if img and Path(img).exists():
            return str(Path(img))
    # 2) default config image
    img = default_cfg.get("image")
    if img and Path(img).exists():
        return str(Path(img))
    # 3) auto-discovery Overlay{n}.png
    if overlay_id is not None:
        name = f"Overlay{overlay_id}.png"
        candidates: list[Path] = []
        args_img_dir = Path(args_overlay_image).parent
        candidates += [
            args_img_dir / name,
            script_path.parent / "assets" / name,
            script_path.parent / "scenes" / name,
            base_path.parent / "assets" / name,
            base_path.parent / "scenes" / name,
        ]
        if cfg_dir:
            candidates += [
                cfg_dir / name,
                cfg_dir / "assets" / name,
                cfg_dir / "scenes" / name,
            ]
        for c in candidates:
            if c.exists():
                return str(c)
    # 4) fallback to global overlay_image
    return str(Path(args_overlay_image))


def run(cmd: list[str]) -> None:
    print(" ", shlex.join(cmd))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def ffprobe_stream_durations(path: str) -> dict:
    """
    Return {'video': seconds_or_None, 'audio': seconds_or_None, 'format': seconds_or_None}
    """
    if not shutil.which("ffprobe"):
        return {"video": None, "audio": None, "format": None}
    try:
        # Get per-stream durations
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", path],
            capture_output=True, text=True
        )
        info = json.loads(p.stdout or "{}")
        vdur = None
        adur = None
        for s in (info.get("streams") or []):
            try:
                d = float(s.get("duration")) if s.get("duration") is not None else None
            except Exception:
                d = None
            if s.get("codec_type") == "video":
                vdur = d if vdur is None else vdur
            if s.get("codec_type") == "audio":
                adur = d if adur is None else adur
        # Fallback to format duration
        pf = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True
        )
        try:
            fdur = float(pf.stdout.strip())
        except Exception:
            fdur = None
        return {"video": vdur, "audio": adur, "format": fdur}
    except Exception:
        return {"video": None, "audio": None, "format": None}


def main():
    # First parse only --config to seed defaults
    ap0 = argparse.ArgumentParser(add_help=False)
    ap0.add_argument("--config", help="Path to settings file (JSON or YAML)")
    known, _ = ap0.parse_known_args()
    cfg_path = Path(known.config).resolve() if known.config else None
    cfg: Dict[str, Any] = {}
    if cfg_path:
        if not cfg_path.exists():
            raise SystemExit(f"Config file not found: {cfg_path}")
        cfg = load_config(cfg_path)
        # Normalize intro_bg to list if provided as a single string
        if isinstance(cfg.get("intro_bg"), str):
            cfg["intro_bg"] = [cfg["intro_bg"]]
        # Resolve relative paths in config
        cfg = resolve_paths_in_config(cfg, cfg_path.parent)

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="Path to settings file (JSON or YAML)")
    ap.add_argument("--script", help="Path to script.txt containing [OVERLAY] markers")
    ap.add_argument("--director", help="Path to director_visemes.json")
    ap.add_argument("--base", help="Path to base MP4 (with audio) to insert overlays into")
    ap.add_argument("--overlay_image", help="Path to full-screen PNG (can include transparency)")
    ap.add_argument("--out", help="Output MP4 with overlays inserted")
    ap.add_argument("--duration", type=float, default=12.0, help="Overlay (pause) duration seconds (default 12)")
    ap.add_argument("--fade", type=float, default=0.5, help="Fade in/out seconds for the slate (default 0.5)")
    ap.add_argument("--overlay_alpha", type=float, default=0.9, help="Max opacity of overlay image (0.0-1.0, default 0.9)")
    ap.add_argument("--anchor", choices=["prev_end", "next_start"], default="prev_end", help="Time anchoring for [OVERLAY] markers (default prev_end)")
    ap.add_argument("--pre_roll_frames", type=int, default=2, help="Shift overlay earlier by N frames (default 2)")
    ap.add_argument("--pre_roll_sec", type=float, default=0.0, help="Shift overlay earlier by seconds (overrides frames if > 0)")
    # ProcessForm moving icon options
    ap.add_argument("--pf_icon", help="Path to ProcessFormIcon.png (small floating icon)")
    ap.add_argument("--pf_width", type=int, default=200, help="Icon width in px (height auto)")
    ap.add_argument("--pf_dx", type=int, default=300, help="Horizontal delta between right and left positions")
    ap.add_argument("--pf_margin", type=int, default=60, help="Margin from edges in px")
    ap.add_argument("--pf_y", type=int, default=60, help="Y position from top in px")
    ap.add_argument("--pf_anim_sec", type=float, default=0.5, help="Animation duration per swap in seconds")
    # Always-on character labels (text above heads)
    ap.add_argument("--labels", action="store_true", help="Enable character labels above heads")
    ap.add_argument("--labels_fontfile", help="Path to font file for labels (e.g., Inter.ttf)")
    ap.add_argument("--labels_fontsize", type=int, default=42, help="Label font size (default 42)")
    ap.add_argument("--labels_color", default="white", help="Label font color (default white)")
    ap.add_argument("--labels_y", type=int, default=120, help="Y position for labels in px from top (default 120)")
    ap.add_argument("--labels_bubble", help="Path to NameBubble.png to place behind labels")
    ap.add_argument("--labels_bubble_width", type=int, default=420, help="Bubble width in px (height auto)")
    ap.add_argument("--labels_bubble_y", type=int, default=80, help="Bubble top y in px (default 80)")
    ap.add_argument("--labels_text_offset_y", type=int, default=10, help="Text offset inside bubble in px (default 10)")
    ap.add_argument("--crf", type=int, default=18, help="libx264 quality (default 18)")
    ap.add_argument("--audio_bitrate", default="192k", help="AAC bitrate (default 192k)")
    ap.add_argument("--fps", type=int, default=0, help="Override FPS; defaults to director fps")
    # Intro slate from conflict description
    ap.add_argument("--intro_bg", action="append", help="Path to intro background image (PNG/JPG). If multiple provided, first is used for now.")
    ap.add_argument("--intro_duration", type=float, default=5.0, help="Intro duration seconds (default 5)")
    ap.add_argument("--intro_fontfile", help="Path to .ttf/.otf font file (e.g., Inter.ttf)")
    ap.add_argument("--intro_fontsize", type=int, default=48, help="Intro text font size (default 48)")
    ap.add_argument("--intro_fontcolor", default="white", help="Intro text color (default white)")
    ap.add_argument("--intro_boxcolor", default="black@0.6", help="Background box color behind intro text (default black@0.6)")
    ap.add_argument("--intro_fade", type=float, default=0.5, help="Intro fade in/out seconds (default 0.5)")
    # Seed config defaults so CLI still overrides
    if cfg:
        ap.set_defaults(**cfg)
    args = ap.parse_args()

    # Post-parse validation so config can satisfy required values
    missing = [k for k in ["script", "director", "base", "overlay_image", "out"] if not getattr(args, k, None)]
    if missing:
        hint = f"Missing required options: {', '.join('--'+m for m in missing)}"
        if cfg_path:
            hint += f"\nValues can be provided via --config {cfg_path} or CLI flags."
        raise SystemExit(hint)

    # Preflight summary and sanity checks
    base_input = str(Path(args.base))
    pr = ffprobe_stream_durations(base_input)
    base_video_s = pr.get("video")
    base_audio_s = pr.get("audio")
    base_format_s = pr.get("format")
    # Count overlays
    try:
        script_text_raw = Path(args.script).read_text()
        num_overlays = script_text_raw.count("[OVERLAY]")
    except Exception:
        num_overlays = None
    est_added = (float(args.duration) * (num_overlays or 0)) + (float(args.intro_duration) if (args.intro_bg and len(args.intro_bg) >= 1) else 0.0)
    if base_format_s:
        print(f"[preflight] Base duration (format): {base_format_s:.3f}s")
    if base_video_s is not None or base_audio_s is not None:
        print(f"[preflight] Stream durations  video={base_video_s if base_video_s is not None else 'n/a'}s  audio={base_audio_s if base_audio_s is not None else 'n/a'}s")
    if num_overlays is not None:
        print(f"[preflight] Overlay markers: {num_overlays}  pause_d={float(args.duration):.3f}s  intro={'yes' if (args.intro_bg and len(args.intro_bg) >= 1) else 'no'}")
    if base_format_s:
        print(f"[preflight] Estimated final duration ≈ {base_format_s + est_added:.3f}s (base + overlays + intro)")
    # Warn if audio is significantly longer than video (may cause extension if not shortest)
    if (base_audio_s and base_video_s) and (base_audio_s - base_video_s > 1.0):
        print(f"[preflight] Warning: base audio ({base_audio_s:.3f}s) > video ({base_video_s:.3f}s) by >1s. Capping pre-slate to shortest to avoid unintended extension.")

    script_text = Path(args.script).read_text()
    director = json.loads(Path(args.director).read_text())
    fps = int(args.fps or director.get("fps", 24))
    base = str(Path(args.base))
    slate = str(Path(args.overlay_image))
    out_path = str(Path(args.out))
    pause_d = float(args.duration)
    fade_d = float(args.fade)
    overlay_alpha = max(0.0, min(1.0, float(args.overlay_alpha)))
    # Always-on logo config (hardcoded)
    # Prefer top-level assets; fallback to common project subfolders if needed
    primary_logo = Path("/Users/michaelmahoney/Desktop/MediatorSPARK/assets/SPARKLogoFinal.png")
    fallback_logos = [
        Path("/Users/michaelmahoney/Desktop/MediatorSPARK/MockMediationGenerator/Testing/assets/SPARKLogoFinal.png"),
        Path("/Users/michaelmahoney/Desktop/MediatorSPARK/MockMediationGenerator/Testing_FourHeads/assets/SPARKLogoFinal.png"),
        Path("/Users/michaelmahoney/Desktop/MediatorSPARK/MockMediationGenerator/Testing_FourHeads/scenes/SPARKLogoFinal.png"),
    ]
    logo_path = str(primary_logo if primary_logo.exists() else next((p for p in fallback_logos if p.exists()), primary_logo))
    have_logo = Path(logo_path).exists()
    logo_w = 261
    logo_mx = 20
    logo_my = 20
    # Determine pre-roll in seconds
    if float(args.pre_roll_sec) > 0.0:
        pre_roll_sec = float(args.pre_roll_sec)
    else:
        pre_roll_sec = max(0, int(args.pre_roll_frames)) / float(fps or 24)

    script_tokens = parse_script_tokens(script_text)
    beat_tokens = build_beats_tokens(director)
    overlay_times = map_overlays_to_times(script_tokens, beat_tokens, anchor=args.anchor)
    pf_swap_times = map_pf_swaps_to_times(script_tokens, beat_tokens, anchor=args.anchor)
    overlay_ids = parse_overlay_ids(script_text)

    # Phase 1: optional pre-slate compositing (ProcessForm icon + labels)
    base_for_pause = base
    if (args.pf_icon and pf_swap_times) or args.labels:
        # Build x(t) expression for right<->left moves with animation (will be overridden below)
        ANIM = max(0.01, float(args.pf_anim_sec))
        # Process Form icon positions and animation:
        # Absolute X positions for left/right, and Y = 75px from bottom
        left_px = 694
        right_px = 1035
        def right_x():
            return f"{right_px}"
        def left_x():
            return f"{left_px}"
        def ramp_rl(t0: float):
            # move from right -> left
            return f"({right_px} + (t - {t0:.3f})/{ANIM} * ({left_px - right_px}))"
        def ramp_lr(t0: float):
            # move from left -> right
            return f"({left_px} + (t - {t0:.3f})/{ANIM} * ({right_px - left_px}))"

        # Build icon animation expression only if icon requested
        xexpr = None
        if args.pf_icon and pf_swap_times:
            swaps = sorted(float(t) for t in pf_swap_times)
            end_state = "left" if (len(swaps) % 2 == 1) else "right"
            end_expr = left_x() if end_state == "left" else right_x()
            nested = end_expr
            state = end_state
            for t0 in reversed(swaps):
                prev_state = "right" if state == "left" else "left"
                anim_expr = ramp_rl(t0) if prev_state == "right" else ramp_lr(t0)
                nested = f"if(between(t,{t0:.3f},{t0+ANIM:.3f}), {anim_expr}, {nested})"
                hold_expr = right_x() if prev_state == "right" else left_x()
                nested = f"if(lt(t,{t0:.3f}), {hold_expr}, {nested})"
                state = prev_state
            xexpr = nested.replace(",", r"\,")
        # Y position expression: 75px from bottom
        pf_y_expr = f"(main_h - overlay_h - 75)"

        # Prepare label texts from script header
        d1_name = extract_header_value(script_text, "DISPUTANT 1 NAME:")
        d2_name = extract_header_value(script_text, "DISPUTANT 2 NAME:")
        ma_name = extract_header_value(script_text, "MEDIATOR A NAME:")
        mb_name = extract_header_value(script_text, "MEDIATOR B NAME:")
        def first_name(full: str) -> str:
            full = (full or "").strip()
            if not full:
                return ""
            return full.split()[0].title()
        d1 = f"Disputant 1\n{first_name(d1_name) or 'Unknown'}"
        d2 = f"Disputant 2\n{first_name(d2_name) or 'Unknown'}"
        ma = f"Mediator A\n{first_name(ma_name) or 'Unknown'}"
        mb = f"Mediator B\n{first_name(mb_name) or 'Unknown'}"

        # Write label textfiles to avoid escaping issues
        tmp_labels_dir = Path(tempfile.mkdtemp(prefix="labels_"))
        tf_d1 = tmp_labels_dir / "d1.txt"; tf_d1.write_text(d1)
        tf_ma = tmp_labels_dir / "ma.txt"; tf_ma.write_text(ma)
        tf_mb = tmp_labels_dir / "mb.txt"; tf_mb.write_text(mb)
        tf_d2 = tmp_labels_dir / "d2.txt"; tf_d2.write_text(d2)

        # Positions (fractions of width for center points)
        # Left to right: D1, Mediator A, Mediator B, D2
        # Hard-coded geometry from user spec
        bubble_width = 289
        cx_px = [483, 771, 1124, 1443]  # D1, MA, MB, D2 centerlines
        bottom_px = [694, 812, 812, 694]  # bubble bottoms (original positions)
        title_size = 34  # ~10% larger
        name_size = 23   # ~10% larger
        line_spacing = 6
        # Title fonts: try Inter Bold, fallback to Inter
        default_inter = Path("/Library/Fonts/Inter.ttf")
        bold_candidates = [
            Path("/Library/Fonts/Inter Bold.ttf"),
            Path("/Library/Fonts/Inter-Bold.ttf"),
            Path("/System/Library/Fonts/Supplemental/Inter-Bold.ttf"),
        ]
        if args.labels_fontfile:
            provided_font = Path(args.labels_fontfile).resolve()
            bold_font = provided_font
            regular_font = provided_font
        else:
            bold_font = next((p for p in bold_candidates if p.exists()), default_inter)
            regular_font = default_inter

        # Build filter graph
        filter_parts = []
        input_args = ["ffmpeg", "-y", "-i", base]
        next_input_idx = 1
        # Optional icon input
        if args.pf_icon and pf_swap_times:
            input_args += ["-loop", "1", "-i", str(Path(args.pf_icon))]
            filter_parts.append(f"[{next_input_idx}:v]scale={int(args.pf_width)}:-1,format=rgba[ic]")
            filter_parts.append(f"[0:v][ic]overlay=x={xexpr}:y={pf_y_expr}:format=auto[v0]")
            next_input_idx += 1
        else:
            # normalize pixel format to avoid odd overlay behavior
            filter_parts.append(f"[0:v]format=rgba[v0]")
        # Optional bubble input
        have_bubble = bool(args.labels_bubble)
        if have_bubble:
            input_args += ["-loop", "1", "-i", str(Path(args.labels_bubble))]
            # Scale to requested width (hard-coded 289) then split for reuse
            filter_parts.append(f"[{next_input_idx}:v]scale={bubble_width}:-1,format=rgba,split=4[nb1][nb2][nb3][nb4]")
            next_input_idx += 1

        # Add four overlays (bubble if provided) + drawtext sequentially
        # Helper to add one label (bubble + two lines), index i maps to above arrays
        def add_label(prev_stream: str, i: int, title_text: str, name_text: str, nb_tag: str) -> str:
            # Bubble placement: center on cx_px[i], y from top using bottom distance
            if have_bubble:
                filter_parts.append(
                    f"[{prev_stream}][{nb_tag}]overlay=x=({cx_px[i]}-overlay_w/2):y=(main_h-{bottom_px[i]}-overlay_h):format=auto[vb{i}]"
                )
                s = f"vb{i}"
            else:
                s = prev_stream
            # Title (bold), aligned center on cx, vertical approx centered in bubble using bottom distance
            title_y = f"(main_h - {bottom_px[i]} - ({title_size}+{line_spacing}+{name_size})/2 - {name_size} - 35)"
            draw_title = ":".join([
                f"fontfile='{bold_font}'",
                "fontcolor=white",
                f"fontsize={title_size}",
                f"text='{title_text}'",
                f"x=({cx_px[i]}-text_w/2)",
                f"y={title_y}",
            ])
            filter_parts.append(f"[{s}]drawtext={draw_title}[vt{i}]")
            # Name (regular), centered under title
            name_y = f"({title_y}+{title_size}+{line_spacing})"
            draw_name = ":".join([
                f"fontfile='{regular_font}'",
                "fontcolor=white",
                f"fontsize={name_size}",
                f"text='({name_text})'",
                f"x=({cx_px[i]}-text_w/2)",
                f"y={name_y}",
            ])
            out_tag = f"vo{i}"
            filter_parts.append(f"[vt{i}]drawtext={draw_name}[{out_tag}]")
            return out_tag

        # D1
        if have_bubble:
            next_stream = add_label("v0", 0, "Disputant 1", first_name(d1_name) or "Unknown", "nb1")
        else:
            next_stream = add_label("v0", 0, "Disputant 1", first_name(d1_name) or "Unknown", "nb1")
        # Mediator A
        # Mediator A
        next_stream = add_label(next_stream, 1, "Mediator A", first_name(ma_name) or "Unknown", "nb2")
        # Mediator B
        # Mediator B
        next_stream = add_label(next_stream, 2, "Mediator B", first_name(mb_name) or "Unknown", "nb3")
        # D2
        # D2
        final_stream = add_label(next_stream, 3, "Disputant 2", first_name(d2_name) or "Unknown", "nb4")

        base_labels = str(Path(out_path).with_suffix(".pre_slates.mp4"))
        print("[apply_overlays] Compositing labels/icon (pre-slate) →", base_labels)
        cmd = input_args + [
            "-filter_complex", ";".join(filter_parts),
            "-map", f"[{final_stream}]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", str(int(args.crf)), "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-shortest",
            base_labels
        ]
        run(cmd)
        base_for_pause = base_labels

    # Phase 2: pause slate insertion (top-most layer)
    if not overlay_times:
        if have_logo:
            print("[apply_overlays] No [OVERLAY] markers; applying permanent logo overlay → out")
            run([
                "ffmpeg", "-y",
                "-i", base_for_pause,
                "-loop", "1", "-i", logo_path,
                "-filter_complex", f"[1:v]scale={logo_w}:-1,format=rgba[lg];[0:v][lg]overlay=x=(main_w-overlay_w-{logo_mx}):y=(main_h-overlay_h-{logo_my}):format=auto[v]",
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-crf", str(int(args.crf)), "-pix_fmt", "yuv420p", "-r", str(int(fps)),
                "-c:a", "copy",
                out_path
            ])
        else:
            print("[apply_overlays] No [OVERLAY] markers found or could not align; copying base → out")
            run(["ffmpeg", "-y", "-i", base_for_pause, "-c", "copy", out_path])
    else:
        # Build segments
        workdir = Path(tempfile.mkdtemp(prefix="overlays_"))
        concat_list = workdir / "concat.txt"
        seg_index = 0
        t_cursor = 0.0
        # Encode settings for uniformity across segments
        v_enc = ["-c:v", "libx264", "-crf", str(int(args.crf)), "-pix_fmt", "yuv420p", "-r", str(int(fps))]
        a_enc = ["-c:a", "aac", "-b:a", args.audio_bitrate, "-ar", "48000", "-ac", "2"]

        def add_file_to_concat(p: Path):
            with concat_list.open("a") as f:
                f.write(f"file '{p.as_posix()}'\n")

        print(f"[apply_overlays] Inserting {len(overlay_times)} overlay pause(s)")

        # Prepare per-overlay config lookup
        cfg_lookup = build_overlay_config_lookup(cfg)
        for idx, t_overlay in enumerate(overlay_times):
            ov_id = overlay_ids[idx] if idx < len(overlay_ids) else None
            # Resolve per-overlay parameters
            # Base defaults from global args
            this_duration = float(args.duration)
            this_fade = float(args.fade)
            this_alpha = overlay_alpha
            this_pre_roll = pre_roll_sec
            # Apply config overrides if present
            if ov_id is not None and ov_id in cfg_lookup.get("by_id", {}):
                ov_cfg = cfg_lookup["by_id"][ov_id]
            else:
                ov_cfg = cfg_lookup.get("default", {})
            if isinstance(ov_cfg, dict):
                if "duration" in ov_cfg:
                    try:
                        this_duration = float(ov_cfg.get("duration"))
                    except Exception:
                        pass
                if "fade" in ov_cfg:
                    try:
                        this_fade = float(ov_cfg.get("fade"))
                    except Exception:
                        pass
                if "overlay_alpha" in ov_cfg:
                    try:
                        this_alpha = float(ov_cfg.get("overlay_alpha"))
                    except Exception:
                        pass
                if "pre_roll_sec" in ov_cfg:
                    try:
                        this_pre_roll = float(ov_cfg.get("pre_roll_sec"))
                    except Exception:
                        pass
                elif "pre_roll_frames" in ov_cfg:
                    try:
                        this_pre_roll = max(0, int(ov_cfg.get("pre_roll_frames"))) / float(fps or 24)
                    except Exception:
                        pass
            # Choose image for this overlay
            slate_img = find_overlay_image_for_id(
                ov_id,
                slate,
                cfg_lookup,
                cfg_path.parent if 'cfg_path' in locals() and cfg_path else None,
                Path(args.script),
                Path(args.base),
            )
            # Apply pre-roll (shift earlier)
            t_ins = max(0.0, t_overlay - this_pre_roll)
            # Enforce monotonic timeline
            if t_ins < t_cursor:
                t_ins = t_cursor
            # Pre segment: [t_cursor, t_ins)
            if t_ins > t_cursor:
                seg_path = workdir / f"seg_{seg_index:03d}.mp4"
                seg_index += 1
                # Re-encode segment to unify parameters
                if have_logo:
                    seg_dur = max(0.0, t_ins - t_cursor)
                    cmd = [
                        "ffmpeg", "-y",
                        "-ss", f"{t_cursor:.3f}",
                        "-to", f"{t_ins:.3f}",
                        "-i", base_for_pause,
                        "-loop", "1", "-t", f"{seg_dur:.3f}", "-i", logo_path,
                        "-filter_complex", f"[1:v]scale={logo_w}:-1,format=rgba[lg];[0:v][lg]overlay=x=(main_w-overlay_w-{logo_mx}):y=(main_h-overlay_h-{logo_my}):shortest=1:format=auto[vout]",
                        "-map", "[vout]", "-map", "0:a?",
                    ] + v_enc + a_enc + [str(seg_path)]
                else:
                    cmd = [
                        "ffmpeg", "-y",
                        "-ss", f"{t_cursor:.3f}",
                        "-to", f"{t_ins:.3f}",
                        "-i", base_for_pause,
                    ] + v_enc + a_enc + [str(seg_path)]
                run(cmd)
                add_file_to_concat(seg_path)

            # Pause segment: freeze exact frame at t_ins and overlay slate with fade, add silence audio
            still = workdir / f"still_{idx:03d}.png"
            cmd_still = [
                "ffmpeg", "-y",
                "-ss", f"{t_ins:.3f}",
                "-i", base_for_pause,
                "-frames:v", "1",
                str(still),
            ]
            run(cmd_still)

            pause_mp4 = workdir / f"pause_{idx:03d}.mp4"
            st = 0.0
            ft_in = st
            ft_out = max(0.0, this_duration - this_fade)
            if have_logo:
                filter_complex = (
                    f"[1:v]format=rgba,fade=in:st={ft_in}:d={this_fade}:alpha=1,"
                    f"fade=out:st={ft_out}:d={this_fade}:alpha=1,"
                    f"colorchannelmixer=aa={this_alpha}[sl];"
                    f"[0:v][sl]overlay=x=0:y=0:shortest=1:format=auto[bg];"
                    f"[3:v]scale={logo_w}:-1,format=rgba[lg];"
                    f"[bg][lg]overlay=x=(main_w-overlay_w-{logo_mx}):y=(main_h-overlay_h-{logo_my}):shortest=1:format=auto[vout]"
                )
                cmd_pause = [
                    "ffmpeg", "-y",
                    "-loop", "1", "-t", f"{this_duration:.3f}", "-i", str(still),
                    "-loop", "1", "-t", f"{this_duration:.3f}", "-i", slate_img,
                    "-f", "lavfi", "-t", f"{this_duration:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-loop", "1", "-t", f"{pause_d:.3f}", "-i", logo_path,
                    "-filter_complex", filter_complex,
                    "-map", "[vout]", "-map", "2:a",
                ] + v_enc + a_enc + [str(pause_mp4)]
            else:
                filter_complex = (
                    f"[1:v]format=rgba,fade=in:st={ft_in}:d={this_fade}:alpha=1,"
                    f"fade=out:st={ft_out}:d={this_fade}:alpha=1,"
                    f"colorchannelmixer=aa={this_alpha}[sl];"
                    f"[0:v][sl]overlay=x=0:y=0:shortest=1:format=auto[vout]"
                )
                cmd_pause = [
                    "ffmpeg", "-y",
                    "-loop", "1", "-t", f"{this_duration:.3f}", "-i", str(still),
                    "-loop", "1", "-t", f"{this_duration:.3f}", "-i", slate_img,
                    "-f", "lavfi", "-t", f"{this_duration:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-filter_complex", filter_complex,
                    "-map", "[vout]", "-map", "2:a",
                ] + v_enc + a_enc + [str(pause_mp4)]
            run(cmd_pause)
            # Optional: overlay countdown timer (TimerCircle + NumbersBold) during dwell (post fade-in to pre fade-out)
            try:
                scenes_dir = Path(args.script).parent / "scenes"
                timer_circle_path = scenes_dir / "TimerCircle.png"
                numbers_dir = scenes_dir / "NumbersBold"
                digit_files = {str(d): numbers_dir / f"{d}.png" for d in range(10)}
                assets_ok = timer_circle_path.exists() and all(p.exists() for p in digit_files.values())
            except Exception:
                assets_ok = False
            # Compute dwell window
            hold_seconds = max(0.0, float(this_duration) - 2.0 * float(this_fade))
            show_timer = assets_ok and hold_seconds > 0.0
            if show_timer:
                # Number PNG dimensions (w,h) in px as provided
                digit_size: dict[str, tuple[int,int]] = {
                    "1": (58, 130),
                    "2": (94, 132),
                    "3": (99, 134),
                    "4": (105, 130),
                    "5": (97, 132),
                    "6": (101, 134),
                    "7": (92, 130),
                    "8": (101, 134),
                    "9": (101, 134),
                    "0": (105, 134),
                }
                circle_src_w = 701
                circle_src_h = 701
                circle_w = 220
                circle_h = 220
                scale_factor = float(circle_w) / float(circle_src_w)
                # Make digits 25% larger than the circle's scale
                digit_scale_factor = scale_factor * 1.25
                gap = 7
                # Countdown windows:
                # - Start showing first number at t=0 (during fade-in)
                # - Show 0 starting 1s before fade-out starts, and keep 0 through fade-out to the end
                zero_start = max(0.0, float(ft_out) - 1.0)
                # Build 1-second windows ending at zero_start and going backward in 1s blocks, clipped at t=0
                steps: list[tuple[float, float]] = []
                t_end = float(zero_start)
                while t_end > 0.0:
                    t_start = max(0.0, t_end - 1.0)
                    steps.insert(0, (t_start, t_end))
                    if t_start <= 0.0:
                        break
                    t_end = t_start
                # Build inputs: 0 = pause clip, 1 = timer circle, 2..11 = digits 0..9
                input_args = [
                    "ffmpeg", "-y",
                    "-i", str(pause_mp4),
                    "-loop", "1", "-t", f"{this_duration:.3f}", "-i", str(timer_circle_path),
                ]
                # Maintain deterministic order 0..9 for mapping
                for d in range(10):
                    input_args += ["-loop", "1", "-t", f"{this_duration:.3f}", "-i", str(digit_files[str(d)])]
                # Start filter graph
                filter_parts: list[str] = []
                # Normalize base format
                filter_parts.append("[0:v]format=rgba[v0]")
                # Timer circle visible for entire pause (including fades); scale to 220x220
                circle_enable = f"between(t,0,{float(this_duration):.3f})"
                # Apply overlay-level fade to circle if fade > 0
                if float(this_fade) > 0.0:
                    filter_parts.append(
                        f"[1:v]scale={circle_w}:{circle_h},format=rgba,"
                        f"fade=t=in:st=0:d={float(this_fade):.3f}:alpha=1,"
                        f"fade=t=out:st={float(ft_out):.3f}:d={float(this_fade):.3f}:alpha=1[tc]"
                    )
                else:
                    filter_parts.append(f"[1:v]scale={circle_w}:{circle_h},format=rgba[tc]")
                # Place circle 60px from left, 45px from bottom
                circle_x = 60
                # y uses main_h to keep relative to bottom
                circle_y_expr = f"(main_h-{circle_h}-45)"
                filter_parts.append(f"[v0][tc]overlay=x={circle_x}:y={circle_y_expr}:format=auto:enable='{circle_enable}'[vc]")
                cur = "vc"
                # Map digit char -> input index (2..11)
                digit_input_idx = {str(d): (2 + d) for d in range(10)}
                # Helper to compute positions per value string
                def layout_for_value(val: int) -> list[tuple[str, int, str, int, int]]:
                    s = str(max(0, int(val)))
                    # Compute total width including gap
                    scaled_widths = [int(round(digit_size[ch][0] * digit_scale_factor)) for ch in s]
                    scaled_heights = [int(round(digit_size[ch][1] * digit_scale_factor)) for ch in s]
                    total_w = sum(scaled_widths) + gap * (len(s) - 1 if len(s) > 1 else 0)
                    x_left = circle_x + int((circle_w - total_w) / 2)
                    # circle top y expression
                    y_top_expr = f"(main_h-{circle_h}-45)"
                    placements: list[tuple[str, int, str, int, int]] = []
                    x_cursor = x_left
                    for i, ch in enumerate(s):
                        sw = scaled_widths[i]
                        sh = scaled_heights[i]
                        # y to vertically center this digit within the circle
                        y_expr = f"({y_top_expr}+{int((circle_h - sh) / 2)})"
                        placements.append((ch, x_cursor, y_expr, sw, sh))
                        x_cursor += sw + gap
                    return placements
                # For each second window, overlay appropriate digits with quick crossfades
                num_steps = len(steps)
                for i, (start_t, end_t) in enumerate(steps):
                    if end_t - start_t <= 0.0:
                        continue
                    # Value counts down to 1 for the last pre-zero window
                    value = num_steps - i
                    placements = layout_for_value(value)
                    enable = f"between(t,{start_t:.3f},{end_t:.3f})"
                    # Quick fade durations (0.1s each side, but clamp to half the window)
                    win = float(end_t - start_t)
                    fi = min(0.1, max(0.0, win / 2.0))
                    fo = fi
                    _overlay_seq_counter = 0
                    for idx_p, (ch, x_pos, y_expr, sw, sh) in enumerate(placements):
                        tag_in = cur
                        tag_out = f"v_step{i}_{_overlay_seq_counter}"
                        didx = digit_input_idx.get(ch, 2)  # default to '0' if somehow missing
                        # Scale this digit instance to match circle scale, keep 7px gap unchanged
                        dscaled = f"sd_step{i}_{_overlay_seq_counter}"
                        # Build fade filters: global overlay-level fades (if any) + per-window quick crossfades
                        if float(this_fade) > 0.0:
                            if fi > 0.0 and fo > 0.0:
                                filter_parts.append(
                                    f"[{didx}:v]scale={int(sw)}:{int(sh)},format=rgba,"
                                    f"fade=t=in:st=0:d={float(this_fade):.3f}:alpha=1,"
                                    f"fade=t=out:st={float(ft_out):.3f}:d={float(this_fade):.3f}:alpha=1,"
                                    f"fade=t=in:st={start_t:.3f}:d={fi:.3f}:alpha=1,"
                                    f"fade=t=out:st={(end_t - fo):.3f}:d={fo:.3f}:alpha=1[{dscaled}]"
                                )
                            else:
                                filter_parts.append(
                                    f"[{didx}:v]scale={int(sw)}:{int(sh)},format=rgba,"
                                    f"fade=t=in:st=0:d={float(this_fade):.3f}:alpha=1,"
                                    f"fade=t=out:st={float(ft_out):.3f}:d={float(this_fade):.3f}:alpha=1[{dscaled}]"
                                )
                        else:
                            if fi > 0.0 and fo > 0.0:
                                filter_parts.append(
                                    f"[{didx}:v]scale={int(sw)}:{int(sh)},format=rgba,"
                                    f"fade=t=in:st={start_t:.3f}:d={fi:.3f}:alpha=1,"
                                    f"fade=t=out:st={(end_t - fo):.3f}:d={fo:.3f}:alpha=1[{dscaled}]"
                                )
                            else:
                                filter_parts.append(f"[{didx}:v]scale={int(sw)}:{int(sh)},format=rgba[{dscaled}]")
                        filter_parts.append(f"[{tag_in}][{dscaled}]overlay=x={int(x_pos)}:y={y_expr}:format=auto:enable='{enable}'[{tag_out}]")
                        cur = tag_out
                        _overlay_seq_counter += 1
                # Final zero: from 1s before fade-out starts, through fade-out until end
                if float(this_duration) > 0.0:
                    z_start = max(0.0, float(zero_start))
                    z_end = float(this_duration)
                    if z_end > z_start:
                        placements = layout_for_value(0)
                        enable = f"between(t,{z_start:.3f},{z_end:.3f})"
                        _overlay_seq_counter = 0
                        for idx_p, (ch, x_pos, y_expr, sw, sh) in enumerate(placements):
                            tag_in = cur
                            tag_out = f"v_zero_{_overlay_seq_counter}"
                            didx = digit_input_idx.get(ch, 2)
                            dscaled = f"sd_zero_{_overlay_seq_counter}"
                            # Fade-in only (keep 0 visible through fade-out)
                            fi0 = min(0.1, max(0.0, (z_end - z_start) / 2.0))
                            if float(this_fade) > 0.0:
                                if fi0 > 0.0:
                                    filter_parts.append(
                                        f"[{didx}:v]scale={int(sw)}:{int(sh)},format=rgba,"
                                        f"fade=t=in:st=0:d={float(this_fade):.3f}:alpha=1,"
                                        f"fade=t=out:st={float(ft_out):.3f}:d={float(this_fade):.3f}:alpha=1,"
                                        f"fade=t=in:st={z_start:.3f}:d={fi0:.3f}:alpha=1[{dscaled}]"
                                    )
                                else:
                                    filter_parts.append(
                                        f"[{didx}:v]scale={int(sw)}:{int(sh)},format=rgba,"
                                        f"fade=t=in:st=0:d={float(this_fade):.3f}:alpha=1,"
                                        f"fade=t=out:st={float(ft_out):.3f}:d={float(this_fade):.3f}:alpha=1[{dscaled}]"
                                    )
                            else:
                                if fi0 > 0.0:
                                    filter_parts.append(
                                        f"[{didx}:v]scale={int(sw)}:{int(sh)},format=rgba,"
                                        f"fade=t=in:st={z_start:.3f}:d={fi0:.3f}:alpha=1[{dscaled}]"
                                    )
                                else:
                                    filter_parts.append(f"[{didx}:v]scale={int(sw)}:{int(sh)},format=rgba[{dscaled}]")
                            filter_parts.append(f"[{tag_in}][{dscaled}]overlay=x={int(x_pos)}:y={y_expr}:format=auto:enable='{enable}'[{tag_out}]")
                            cur = tag_out
                            _overlay_seq_counter += 1
                # Map video and preserve audio from original pause clip
                pause_with_timer = Path(str(pause_mp4).replace(".mp4", "_timer.mp4"))
                cmd_timer = input_args + [
                    "-filter_complex", ";".join(filter_parts),
                    "-map", f"[{cur}]", "-map", "0:a",
                    "-c:v", "libx264", "-crf", str(int(args.crf)), "-pix_fmt", "yuv420p", "-r", str(int(fps)),
                    "-c:a", "aac", "-b:a", args.audio_bitrate,
                    str(pause_with_timer)
                ]
                run(cmd_timer)
                pause_mp4 = pause_with_timer
            # Add final pause segment (with timer if applied)
            add_file_to_concat(pause_mp4)

            # Move cursor forward
            t_cursor = t_ins

        # Tail segment: from last cursor to end
        tail_path = workdir / f"seg_{seg_index:03d}.mp4"
        seg_index += 1
        if have_logo:
            cmd_tail = [
                "ffmpeg", "-y",
                "-ss", f"{t_cursor:.3f}",
                "-i", base_for_pause,
                "-loop", "1", "-i", logo_path,
                "-filter_complex", f"[1:v]scale={logo_w}:-1,format=rgba[lg];[0:v][lg]overlay=x=(main_w-overlay_w-{logo_mx}):y=(main_h-overlay_h-{logo_my}):shortest=1:format=auto[vout]",
                "-map", "[vout]", "-map", "0:a?",
            ] + v_enc + a_enc + [str(tail_path)]
        else:
            cmd_tail = [
                "ffmpeg", "-y",
                "-ss", f"{t_cursor:.3f}",
                    "-i", base_for_pause,
            ] + v_enc + a_enc + [str(tail_path)]
        run(cmd_tail)
        add_file_to_concat(tail_path)

        # Concat all with demuxer (streams are identical → copy)
        print("[apply_overlays] Concatenating segments →", out_path)
        run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            # Re-encode final output to eliminate AAC priming/timestamp discontinuities
        ] + v_enc + a_enc + [out_path])
        print("[apply_overlays] Done.")

    # Phase 3: optional intro sequence
    # New animated intro (intro2) takes precedence if present; otherwise fallback to legacy single-image intro.
    intro2_cfg = cfg.get("intro2") if cfg else None
    if isinstance(intro2_cfg, dict):
        # Defaults and parameters
        intro_bg = str(Path(intro2_cfg.get("bg", ""))) if intro2_cfg.get("bg") else ""
        intro_total = float(intro2_cfg.get("duration", 8.0))
        intro_fade = float(intro2_cfg.get("fade", 0.5))
        placement = str(intro2_cfg.get("placement", "camera")).lower()  # "camera" or "slots"
        conflict_start = float(intro2_cfg.get("conflict_text_time", 2.0))
        conflict_duration = float(intro2_cfg.get("conflict_text_duration", 2.5))
        # Global/default bubbles delay
        bubble_delay = float(intro2_cfg.get("bubbles_delay", 0.6))
        # Optional timeline-style controls
        d_appear = float(intro2_cfg.get("d_appear", 0.5))
        d_bubbles_delay = float(intro2_cfg.get("d_bubbles_delay", bubble_delay))
        conflict_after_d_bubbles = float(intro2_cfg.get("conflict_after_d_bubbles", 0.5))
        m_appear_after_conflict = float(intro2_cfg.get("m_appear_after_conflict", 1.0))
        m_bubbles_delay = float(intro2_cfg.get("m_bubbles_delay", bubble_delay))
        process_after_m_bubbles = float(intro2_cfg.get("process_after_m_bubbles", 1.0))
        process_overlay = str(Path(intro2_cfg.get("process_form_overlay", ""))) if intro2_cfg.get("process_form_overlay") else ""
        process_time = float(intro2_cfg.get("process_form_time", max(0.0, intro_total - 2.0)))
        process_duration = float(intro2_cfg.get("process_form_duration", 1.5))
        char_width = int(intro2_cfg.get("char_width", 620))
        intro_logo_scale = float(intro2_cfg.get("intro_logo_scale", 1.0))

        # Prepare conflict description text (prefixed)
        conflict = extract_header_value(script_text, "CONFLICT DESCRIPTION:")
        if not conflict:
            conflict = "Conflict description not found."
        conflict_prefixed = f"Conflict Description: {conflict}"
        intro_text_wrapped = wrap_text(conflict_prefixed, max_chars=64)
        tmp_dir = Path(tempfile.mkdtemp(prefix="intro2_"))
        txt_file = tmp_dir / "intro_text.txt"
        txt_file.write_text(intro_text_wrapped)

        # Resolution target
        try:
            render_res = director.get("render", {}).get("resolution", [1920, 1080])
            tgt_w, tgt_h = int(render_res[0]), int(render_res[1])
        except Exception:
            tgt_w, tgt_h = 1920, 1080

        # Character layout defaults by role
        # Horizontal centers for D1, MA, MB, D2
        cx_px = [483, 771, 1124, 1443]
        # Bubbles baseline distances (from bottom)
        bottom_px = [694, 812, 812, 694]
        role_to_idx = {"d1": 0, "ma": 1, "mb": 2, "d2": 3}

        # Parse characters (role + image + appear)
        chars = []
        for entry in (intro2_cfg.get("chars") or []):
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role", "")).lower()
            img = entry.get("image")
            appear = float(entry.get("appear", 0.5))
            if not img or role not in role_to_idx:
                continue
            idx = role_to_idx[role]
            chars.append({"idx": idx, "role": role, "image": str(Path(img)), "appear": appear})
        # If none provided, fall back to simple timing: D1/D2 at 0.5, MA/MB at 3.5 (images must exist to be used)
        if not chars:
            # Try to infer from labels (not guaranteed)
            pass

        # Apply timeline-derived times if requested (presence of any timeline keys triggers overrides)
        timeline_keys = {"d_appear", "d_bubbles_delay", "conflict_after_d_bubbles", "m_appear_after_conflict", "m_bubbles_delay", "process_after_m_bubbles"}
        has_timeline_overrides = any(k in intro2_cfg for k in timeline_keys)
        if has_timeline_overrides:
            # 1) Disputants appear
            for c in chars:
                if c["role"] in ("d1", "d2"):
                    c["appear"] = d_appear
            # 2) Conflict start (after disputant bubbles start + hold)
            conflict_start = float(intro2_cfg.get("conflict_text_time", d_appear + d_bubbles_delay + conflict_after_d_bubbles))
            # 3) Mediators appear after conflict ends + hold
            m_appear_abs = conflict_start + conflict_duration + m_appear_after_conflict
            for c in chars:
                if c["role"] in ("ma", "mb"):
                    c["appear"] = m_appear_abs
            # 4) Process overlay after mediator bubbles start + hold
            mediators_bubbles_start = m_appear_abs + m_bubbles_delay
            process_time_default = mediators_bubbles_start + process_after_m_bubbles
            # Only override if not explicitly set
            if "process_form_time" not in intro2_cfg:
                process_time = process_time_default
        else:
            # If no overrides, set a sensible mediator appear time for downstream calculations
            try:
                m_appear_abs = max(c["appear"] for c in chars if c["role"] in ("ma", "mb"))
            except Exception:
                m_appear_abs = 0.0
        # If a title slide is configured, offset all intro events so they start after the title finishes
        try:
            _title_d = float(intro2_cfg.get("title_duration", 0.0))
            _has_title = bool(intro2_cfg.get("title_slide")) and _title_d > 0.0
            title_offset = _title_d if _has_title else 0.0
        except Exception:
            title_offset = 0.0
        if title_offset > 0.0:
            conflict_start = conflict_start + title_offset
            for c in chars:
                c["appear"] = float(c["appear"]) + title_offset
            try:
                process_time = float(process_time) + title_offset  # type: ignore
            except Exception:
                pass

        # Auto-compute duration if not explicitly provided in config
        if "duration" not in intro2_cfg:
            # Candidate end times:
            # - Last character fade-in completes
            try:
                char_end = max((float(c["appear"]) + float(intro_fade)) for c in chars) if chars else 0.0
            except Exception:
                char_end = 0.0
            # - Disputant bubbles fade-in completes
            d_bubbles_end = (d_appear + d_bubbles_delay + intro_fade) + title_offset
            # - Mediator bubbles fade-in completes (only if mediators exist)
            m_bubbles_end = ((m_appear_abs + m_bubbles_delay + intro_fade) if any(c["role"] in ("ma", "mb") for c in chars) else 0.0) + (title_offset if any(c["role"] in ("ma", "mb") for c in chars) else 0.0)
            # - Conflict text end
            conflict_end = conflict_start + conflict_duration
            # - Process overlay end (if present)
            process_end = (process_time + process_duration) if process_overlay else 0.0
            # Pad a bit to avoid abrupt cuts
            tail_pad = max(0.25, float(intro_fade) * 0.5)
            computed_total = max(char_end, d_bubbles_end, m_bubbles_end, conflict_end, process_end) + tail_pad
            intro_total = round(computed_total, 3)

        # Build inputs and filter graph
        input_args = ["ffmpeg", "-y"]
        # 0: background (or solid color if absent)
        if intro_bg:
            input_args += ["-loop", "1", "-framerate", str(int(fps)), "-t", f"{intro_total:.3f}", "-i", intro_bg]
            bg_tag = "0:v"
            next_idx = 1
        else:
            # fallback to color source background
            input_args += ["-f", "lavfi", "-t", f"{intro_total:.3f}", "-i", f"color=c=black:s={tgt_w}x{tgt_h}:r={int(fps)}"]
            bg_tag = "0:v"
            next_idx = 1
        # Optional title slide (loops for its own duration)
        title_tag = None
        title_d = float(intro2_cfg.get("title_duration", 0.0))
        title_fade = float(intro2_cfg.get("title_fade", intro_fade))
        title_path = str(Path(intro2_cfg.get("title_slide"))) if intro2_cfg.get("title_slide") else ""
        if title_path and title_d > 0.0:
            input_args += ["-loop", "1", "-framerate", str(int(fps)), "-t", f"{title_d:.3f}", "-i", title_path]
            title_tag = f"{next_idx}:v"
            next_idx += 1
        # 1: optional bubble image (reused)
        have_bubble = bool(args.labels_bubble)
        if have_bubble:
            input_args += ["-loop", "1", "-t", f"{intro_total:.3f}", "-i", str(Path(args.labels_bubble))]
            bubble_idx = next_idx
            next_idx += 1
        else:
            bubble_idx = None
        # Character inputs
        char_inputs = []
        for c in chars:
            input_args += ["-loop", "1", "-t", f"{intro_total:.3f}", "-i", c["image"]]
            c["input_idx"] = next_idx
            char_inputs.append(c)
            next_idx += 1
        # ProcessForm overlay
        pf_idx = None
        if process_overlay:
            input_args += ["-loop", "1", "-t", f"{intro_total:.3f}", "-i", process_overlay]
            pf_idx = next_idx
            next_idx += 1
        # Optional logo overlay (match permanent logo position/size)
        logo_idx = None
        if have_logo:
            input_args += ["-loop", "1", "-t", f"{intro_total:.3f}", "-i", logo_path]
            logo_idx = next_idx
            next_idx += 1
        # Silent audio
        input_args += ["-f", "lavfi", "-t", f"{intro_total:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        audio_input_idx = next_idx

        filter_parts = []
        # Start by scaling/cropping bg to target and convert to rgba
        filter_parts.append(
            f"[{bg_tag}]scale=w={tgt_w}:h={tgt_h}:force_original_aspect_ratio=increase,"
            f"crop={tgt_w}:{tgt_h},format=rgba[vbg]"
        )
        # If title slide present, scale it and crossfade into background; else use background directly
        if title_tag:
            filter_parts.append(
                f"[{title_tag}]scale=w={tgt_w}:h={tgt_h}:force_original_aspect_ratio=increase,"
                f"crop={tgt_w}:{tgt_h},format=rgba[vt]"
            )
            # Crossfade: title → bg, fade at end of title
            xf_off = max(0.0, title_d - title_fade)
            filter_parts.append(
                f"[vt][vbg]xfade=transition=fade:duration={title_fade:.3f}:offset={xf_off:.3f}[v0]"
            )
            cur = "v0"
        else:
            cur = "vbg"

        # Prepare bubbles: scale and split
        if bubble_idx is not None:
            filter_parts.append(f"[{bubble_idx}:v]scale=289:-1,format=rgba,split=4[nb1][nb2][nb3][nb4]")

        # Overlay characters
        # placement == "camera": assume PNGs match camera framing; optionally scale to char_width and center-bottom align
        # placement == "slots": legacy fixed slots with char_width scaling
        for j, c in enumerate(char_inputs):
            ci = c["input_idx"]
            start = max(0.0, float(c["appear"]))
            if placement == "camera":
                if int(char_width) > 0:
                    # Scale to requested width (e.g., 1400), keep aspect; center horizontally, bottom align
                    filter_parts.append(f"[{ci}:v]scale={int(char_width)}:-1,format=rgba[ch{j}]")
                    # Fade in alpha on the character
                    filter_parts.append(f"[ch{j}]fade=t=in:st={start:.3f}:d={intro_fade:.3f}:alpha=1[ch{j}f]")
                    filter_parts.append(
                        f"[{cur}][ch{j}f]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h):format=auto:enable='between(t,{start:.3f},{intro_total:.3f})'[v{j+1}]"
                    )
                else:
                    # No scaling: overlay full-frame at 0,0
                    filter_parts.append(f"[{ci}:v]format=rgba[ch{j}]")
                    filter_parts.append(f"[ch{j}]fade=t=in:st={start:.3f}:d={intro_fade:.3f}:alpha=1[ch{j}f]")
                    filter_parts.append(
                        f"[{cur}][ch{j}f]overlay=x=0:y=0:format=auto:enable='between(t,{start:.3f},{intro_total:.3f})'[v{j+1}]"
                    )
            else:
                # slots placement
                cx = cx_px[c["idx"]]
                filter_parts.append(f"[{ci}:v]scale={char_width}:-1,format=rgba[ch{j}]")
                filter_parts.append(f"[ch{j}]fade=t=in:st={start:.3f}:d={intro_fade:.3f}:alpha=1[ch{j}f]")
                filter_parts.append(
                    f"[{cur}][ch{j}f]overlay=x=({cx}-overlay_w/2):y=(main_h-overlay_h-150):format=auto:enable='between(t,{start:.3f},{intro_total:.3f})'[v{j+1}]"
                )
            cur = f"v{j+1}"

        # Add bubbles + name text
        # Build name strings from script header; reuse first_name helper
        d1_name = extract_header_value(script_text, "DISPUTANT 1 NAME:")
        d2_name = extract_header_value(script_text, "DISPUTANT 2 NAME:")
        ma_name = extract_header_value(script_text, "MEDIATOR A NAME:")
        mb_name = extract_header_value(script_text, "MEDIATOR B NAME:")
        def first_name(full: str) -> str:
            full = (full or "").strip()
            if not full:
                return ""
            return full.split()[0].title()
        titles = ["Disputant 1", "Mediator A", "Mediator B", "Disputant 2"]
        names = [first_name(d1_name) or "Unknown", first_name(ma_name) or "Unknown", first_name(mb_name) or "Unknown", first_name(d2_name) or "Unknown"]
        title_size = 34
        name_size = 23
        line_spacing = 6
        # Fonts (reuse labels font if provided)
        if args.labels_fontfile:
            provided_font = Path(args.labels_fontfile).resolve()
            bold_font = provided_font
            regular_font = provided_font
        else:
            default_inter = Path("/Library/Fonts/Inter.ttf")
            bold_candidates = [
                Path("/Library/Fonts/Inter Bold.ttf"),
                Path("/Library/Fonts/Inter-Bold.ttf"),
                Path("/System/Library/Fonts/Supplemental/Inter-Bold.ttf"),
            ]
            bold_font = next((p for p in bold_candidates if p.exists()), default_inter)
            regular_font = default_inter
        # For each role present, compute bubble start = char start + bubble_delay
        # We'll overlay bubble and draw two text lines (title/name)
        used_roles = {c["idx"]: c for c in char_inputs}
        for ridx in range(4):
            if ridx not in used_roles:
                continue
            # Per-role bubble delays (fall back to global bubble_delay)
            role_delay = d_bubbles_delay if ridx in (0, 3) else m_bubbles_delay
            start = max(0.0, float(used_roles[ridx]["appear"]) + role_delay)
            # bubble
            if bubble_idx is not None:
                nb_tag = f"nb{ridx+1}"
                # Fade in bubble before overlay
                filter_parts.append(f"[{nb_tag}]fade=t=in:st={start:.3f}:d={intro_fade:.3f}:alpha=1[{nb_tag}f]")
                filter_parts.append(
                    f"[{cur}][{nb_tag}f]overlay=x=({cx_px[ridx]}-overlay_w/2):y=(main_h-{bottom_px[ridx]}-overlay_h):format=auto:enable='between(t,{start:.3f},{intro_total:.3f})'[vb{ridx}]"
                )
                cur = f"vb{ridx}"
            # title text
            title_y = f"(main_h - {bottom_px[ridx]} - ({title_size}+{line_spacing}+{name_size})/2 - {name_size} - 35)"
            draw_title = ":".join([
                f"fontfile='{bold_font}'",
                "fontcolor=white",
                f"fontsize={title_size}",
                f"text='{titles[ridx]}'",
                f"x=({cx_px[ridx]}-text_w/2)",
                f"y={title_y}",
                f"enable='between(t,{start:.3f},{intro_total:.3f})'",
            ])
            filter_parts.append(f"[{cur}]drawtext={draw_title}[vt{ridx}]")
            # name text
            name_y = f"({title_y}+{title_size}+{line_spacing})"
            draw_name = ":".join([
                f"fontfile='{regular_font}'",
                "fontcolor=white",
                f"fontsize={name_size}",
                f"text='({names[ridx]})'",
                f"x=({cx_px[ridx]}-text_w/2)",
                f"y={name_y}",
                f"enable='between(t,{start:.3f},{intro_total:.3f})'",
            ])
            filter_parts.append(f"[vt{ridx}]drawtext={draw_name}[vo{ridx}]")
            cur = f"vo{ridx}"

        # Conflict description (top center), appear then disappear
        fontopt = []
        if args.intro_fontfile:
            fontopt = [f"fontfile='{str(Path(args.intro_fontfile))}'"]
        drawtext_opts = ":".join([
            *fontopt,
            f"textfile='{str(txt_file)}'",
            f"fontcolor={args.intro_fontcolor}",
            f"fontsize={int(args.intro_fontsize)}",
            "line_spacing=10",
            "x=(w-text_w)/2",
            "y=80",
            f"enable='between(t,{conflict_start:.3f},{(conflict_start+conflict_duration):.3f})'",
        ])
        filter_parts.append(f"[{cur}]drawtext={drawtext_opts}[vconf]")
        cur = "vconf"

        # Process form overlay on top near end
        if pf_idx is not None:
            filter_parts.append(
                f"[{pf_idx}:v]format=rgba,fade=t=in:st={process_time:.3f}:d={intro_fade:.3f}:alpha=1[pf]"
            )
            filter_parts.append(
                f"[{cur}][pf]overlay=x=0:y=0:format=auto:enable='between(t,{process_time:.3f},{min(intro_total, process_time+process_duration):.3f})'[vout]"
            )
            cur = "vout"

        # Permanent logo overlay across the intro (bottom-right)
        if logo_idx is not None:
            scaled_logo_w = max(1, int(logo_w * max(0.05, intro_logo_scale)))
            filter_parts.append(
                f"[{logo_idx}:v]scale={scaled_logo_w}:-1,format=rgba[lg]"
            )
            filter_parts.append(
                f"[{cur}][lg]overlay=x=(main_w-overlay_w-{logo_mx}):y=(main_h-overlay_h-{logo_my}):format=auto:enable='between(t,0,{intro_total:.3f})'[vlogo]"
            )
            cur = "vlogo"

        intro_mp4 = tmp_dir / "intro2.mp4"
        cmd = input_args + [
            "-filter_complex", ";".join(filter_parts),
            "-map", f"[{cur}]", "-map", f"{audio_input_idx}:a",
            "-c:v", "libx264", "-crf", str(int(args.crf)), "-pix_fmt", "yuv420p", "-r", str(int(fps)),
            "-c:a", "aac", "-b:a", args.audio_bitrate,
            "-shortest",
            str(intro_mp4)
        ]
        print("[apply_overlays] Building animated intro (intro2) →", intro_mp4)
        run(cmd)

        # Crossfade intro → main
        xf_d = max(0.1, min(intro_fade, intro_total - 0.1))
        tmp_joined = tmp_dir / "intro2_merged.mp4"
        run([
            "ffmpeg", "-y",
            "-i", str(intro_mp4),
            "-i", str(out_path),
            "-filter_complex",
            f"[0:v][1:v]xfade=transition=fade:duration={xf_d:.3f}:offset={max(0.0, intro_total - xf_d):.3f}[v];"
            f"[0:a][1:a]acrossfade=d={xf_d:.3f}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-crf", str(int(args.crf)), "-pix_fmt", "yuv420p", "-r", str(int(fps)),
            "-c:a", "aac", "-b:a", args.audio_bitrate,
            str(tmp_joined)
        ])
        Path(tmp_joined).replace(out_path)
    elif args.intro_bg and len(args.intro_bg) >= 1:
        # Legacy single-image intro (existing behavior)
        intro_bg = str(Path(args.intro_bg[0]))
        intro_d = max(0.5, float(args.intro_duration))
        intro_fade = max(0.0, float(args.intro_fade))
        # Extract conflict description and wrap
        conflict = extract_header_value(script_text, "CONFLICT DESCRIPTION:")
        if not conflict:
            conflict = "Conflict description not found."
        intro_text_wrapped = wrap_text(conflict, max_chars=64)
        # Write to a temp text file for drawtext
        tmp_dir = Path(tempfile.mkdtemp(prefix="intro_"))
        txt_file = tmp_dir / "intro_text.txt"
        txt_file.write_text(intro_text_wrapped)
        # Build intro clip
        intro_mp4 = tmp_dir / "intro.mp4"
        fontopt = []
        if args.intro_fontfile:
            fontopt = [f"fontfile='{str(Path(args.intro_fontfile))}'"]
        drawtext_opts = ":".join([
            *fontopt,
            f"textfile='{str(txt_file)}'",
            f"fontcolor={args.intro_fontcolor}",
            f"fontsize={int(args.intro_fontsize)}",
            "line_spacing=10",
            "box=1",
            f"boxcolor={args.intro_boxcolor}",
            "boxborderw=20",
            "x=(w-text_w)/2",
            "y=(h-text_h)/2"
        ])
        try:
            render_res = director.get("render", {}).get("resolution", [1920, 1080])
            tgt_w, tgt_h = int(render_res[0]), int(render_res[1])
        except Exception:
            tgt_w, tgt_h = 1920, 1080
        filter_intro = (
            f"[0:v]scale=w={tgt_w}:h={tgt_h}:force_original_aspect_ratio=increase,"
            f"crop={tgt_w}:{tgt_h},format=rgba,"
            f"fade=t=in:st=0:d={intro_fade},"
            f"fade=t=out:st={max(0.0,intro_d-intro_fade)}:d={intro_fade}[v0];"
            f"[v0]drawtext={drawtext_opts}[v1]"
        )
        run([
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(int(fps)), "-t", f"{intro_d:.3f}", "-i", intro_bg,
            "-f", "lavfi", "-t", f"{intro_d:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-filter_complex", filter_intro,
            "-map", "[v1]", "-map", "1:a",
            "-c:v", "libx264", "-crf", str(int(args.crf)), "-pix_fmt", "yuv420p", "-r", str(int(fps)),
            "-c:a", "aac", "-b:a", args.audio_bitrate,
            "-shortest",
            str(intro_mp4)
        ])
        # Concat intro + current out_path without re-encode
        main_after = Path(out_path)
        concat_list = tmp_dir / "concat.txt"
        concat_list.write_text(f"file '{str(intro_mp4)}'\nfile '{str(main_after)}'\n")
        final_out = str(main_after)
        tmp_joined = tmp_dir / "joined.mp4"
        run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(tmp_joined)
        ])
        Path(tmp_joined).replace(final_out)


if __name__ == "__main__":
    main()


