#!/usr/bin/env python3
"""
Local (Mac) sharded render runner.

Flow:
1) Prepare a single render-ready scene once (optional).
2) Split frame range into N shards.
3) Render shards in parallel Blender processes.
4) Optionally mux frames + audio into MP4.
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())

def _project_id_from_generator_inputs(generator_inputs_json: Path) -> str:
    try:
        gi = _load_json(generator_inputs_json)
        run_cfg = gi.get("run") or {}
        pid = (
            (run_cfg.get("project_name") or "")
            or (run_cfg.get("projectId") or "")
            or (run_cfg.get("project_id") or "")
            or (run_cfg.get("project") or "")
        )
        return str(pid).strip()
    except Exception:
        return ""

def _project_id_from_script(script_txt: Path) -> str:
    try:
        for line in script_txt.read_text().splitlines():
            if line.strip().upper().startswith("PROJECT:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""

def _resolve_project_id(generator_inputs_json: Path, script_txt: Path) -> str:
    return _project_id_from_generator_inputs(generator_inputs_json) or _project_id_from_script(script_txt)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _run(cmd: list[str], env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
    print("[run]", " ".join(cmd), flush=True)
    res = subprocess.run(cmd, env=env, cwd=str(cwd) if cwd else None)
    if res.returncode != 0:
        raise SystemExit(res.returncode)


def _need_tts(manifest_csv: Path) -> bool:
    if not manifest_csv.exists():
        return True
    with open(manifest_csv, newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            audio = (row.get("audio") or "").strip()
            if not audio:
                continue
            if not Path(audio).exists():
                return True
    return False


def _parse_tc_seconds(tc: str) -> float:
    try:
        h, m, s = (tc or "00:00:00.000").split(":")
        return float(h) * 3600.0 + float(m) * 60.0 + float(s)
    except Exception:
        return 0.0


def _estimate_end_seconds_from_director(director: dict) -> float:
    end_s = 0.0
    for b in director.get("beats", []) or []:
        t0 = _parse_tc_seconds(str(b.get("tc_in") or "00:00:00.000"))
        if (b.get("type") or "").lower() == "pause" or not b.get("audio"):
            try:
                dur = float(b.get("duration") or 1.0)
            except Exception:
                dur = 1.0
            end_s = max(end_s, t0 + max(0.0, dur))
            continue
        vmax = t0
        for ev in b.get("visemes", []) or []:
            try:
                vmax = max(vmax, float(ev.get("t") or t0))
            except Exception:
                pass
        end_s = max(end_s, max(vmax, t0 + 1.0))
    return max(0.0, float(end_s))

def _parse_tc_seconds(tc: str) -> float:
    try:
        h, m, s = (tc or "00:00:00.000").split(":")
        return float(h) * 3600.0 + float(m) * 60.0 + float(s)
    except Exception:
        return 0.0

def _sec_to_frame_1based(t_sec: float, fps: int) -> int:
    """
    Convert timeline seconds to a 1-based frame index consistent with our PNG naming
    convention (<stem>_0001.png corresponds to frame 1).
    """
    try:
        f0 = int(round(float(t_sec) * float(fps)))
    except Exception:
        f0 = 0
    return max(1, f0 + 1)

def _stable_hash(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    items = sorted(((int(s), int(e)) for s, e in ranges if int(e) >= int(s)), key=lambda x: x[0])
    out: list[tuple[int, int]] = []
    cur_s, cur_e = items[0]
    for s, e in items[1:]:
        if s <= cur_e + 1:
            cur_e = max(cur_e, e)
        else:
            out.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    out.append((cur_s, cur_e))
    return out

def _beat_change_ranges_frames(
    *,
    old_director: dict | None,
    new_director: dict,
    fps: int,
    pad_frames: int,
    frame_start: int,
    frame_end: int,
) -> list[tuple[int, int]]:
    """
    Compute frame ranges that should be rerendered based on beat-level diffs.

    This is intentionally conservative:
    - If a beat's identity or timing changes, we rerender that beat's span (plus padding).
    - If timing shifts ripple forward, subsequent beats will also be detected as changed.
    """
    beats_new = list(new_director.get("beats") or [])
    beats_old = list((old_director or {}).get("beats") or [])

    def key_for(b: dict, idx: int) -> str:
        mid = b.get("manifest_id")
        if mid is None:
            mid = ""
        mid = str(mid).strip()
        return mid if mid else f"idx:{idx+1}"

    old_by_key: dict[str, dict] = {key_for(b, i): (b or {}) for i, b in enumerate(beats_old)}

    ranges: list[tuple[int, int]] = []
    for i, b in enumerate(beats_new):
        b = b or {}
        k = key_for(b, i)
        old_b = old_by_key.get(k)
        # Fingerprint a beat by the fields that materially affect rendering.
        sig_new = {
            "type": b.get("type") or "",
            "tc_in": b.get("tc_in") or "",
            "duration": b.get("duration") if (b.get("type") or "").lower() == "pause" else None,
            "char": b.get("char") or "",
            "audio": b.get("audio") or "",
            "audio_hash": b.get("audio_hash") or "",
            "transcript_hash": b.get("transcript_hash") or "",
            "visemes": b.get("visemes") or [],
        }
        sig_old = None
        if old_b is not None:
            sig_old = {
                "type": old_b.get("type") or "",
                "tc_in": old_b.get("tc_in") or "",
                "duration": old_b.get("duration") if (old_b.get("type") or "").lower() == "pause" else None,
                "char": old_b.get("char") or "",
                "audio": old_b.get("audio") or "",
                "audio_hash": old_b.get("audio_hash") or "",
                "transcript_hash": old_b.get("transcript_hash") or "",
                "visemes": old_b.get("visemes") or [],
            }

        changed = (sig_old is None) or (_stable_hash(sig_old) != _stable_hash(sig_new))
        if not changed:
            continue

        # Determine beat span in *new* timeline.
        start_s = _parse_tc_seconds(str(b.get("tc_in") or "00:00:00.000"))
        # Prefer next beat tc_in as end bound (prevents runaway on missing audio).
        end_s = None
        if i + 1 < len(beats_new):
            end_s = _parse_tc_seconds(str((beats_new[i + 1] or {}).get("tc_in") or "00:00:00.000"))
        if end_s is None or end_s <= start_s:
            if (b.get("type") or "").lower() == "pause" or (not b.get("audio")):
                try:
                    end_s = start_s + float(b.get("duration") or 0.5)
                except Exception:
                    end_s = start_s + 0.5
            else:
                # Speech beat: use last viseme time as a floor.
                vmax = start_s
                for ev in (b.get("visemes") or []):
                    try:
                        vmax = max(vmax, float(ev.get("t") or vmax))
                    except Exception:
                        pass
                end_s = max(vmax, start_s + 1.0)

        s_f = _sec_to_frame_1based(start_s, fps)
        e_f = _sec_to_frame_1based(end_s, fps)
        s_f = max(int(frame_start), int(s_f) - int(pad_frames))
        e_f = min(int(frame_end), int(e_f) + int(pad_frames))
        if e_f >= s_f:
            ranges.append((s_f, e_f))

    return _merge_ranges(ranges)


def _chunk_ranges(start: int, end: int, shards: int) -> list[tuple[int, int]]:
    total = end - start + 1
    base = total // shards
    rem = total % shards
    out: list[tuple[int, int]] = []
    cur = start
    for i in range(shards):
        size = base + (1 if i < rem else 0)
        if size <= 0:
            continue
        s = cur
        e = cur + size - 1
        out.append((s, e))
        cur = e + 1
    return out


def _blender_env(scratch_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["TMPDIR"] = str(scratch_dir)
    env.setdefault("HOME", str(scratch_dir))
    env.setdefault("XDG_CACHE_HOME", str(scratch_dir / ".cache"))
    env.setdefault("XDG_CONFIG_HOME", str(scratch_dir / ".config"))
    env.setdefault("XDG_DATA_HOME", str(scratch_dir / ".local" / "share"))
    (scratch_dir / ".cache").mkdir(parents=True, exist_ok=True)
    (scratch_dir / ".config").mkdir(parents=True, exist_ok=True)
    (scratch_dir / ".local" / "share").mkdir(parents=True, exist_ok=True)
    return env


def _scan_existing_frames(frames_dir: Path, stem: str) -> set[int]:
    """
    Return frame numbers already present in frames_dir.
    Expected filename format: <stem>_####.png
    """
    out: set[int] = set()
    if not frames_dir.exists():
        return out
    pat = re.compile(rf"^{re.escape(stem)}_(\d{{4}})\.png$")
    for p in frames_dir.glob("*.png"):
        m = pat.match(p.name)
        if not m:
            continue
        try:
            out.add(int(m.group(1)))
        except Exception:
            pass
    return out


def _missing_ranges_in_span(start: int, end: int, existing: set[int]) -> list[tuple[int, int]]:
    """
    Compute missing contiguous frame ranges in [start, end] inclusive.
    """
    missing: list[tuple[int, int]] = []
    cur_s = None
    for f in range(int(start), int(end) + 1):
        if f in existing:
            if cur_s is not None:
                missing.append((cur_s, f - 1))
                cur_s = None
            continue
        if cur_s is None:
            cur_s = f
    if cur_s is not None:
        missing.append((cur_s, int(end)))
    return missing


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "run_full_video_creation_sequence.config.json"),
        help="Path to run_full_video_creation_sequence.config.json",
    )
    ap.add_argument("--shards", type=int, default=2, help="Number of frame shards")
    ap.add_argument("--max_parallel", type=int, default=0, help="Max concurrent shard renders (default=shards)")
    ap.add_argument("--frame_start", type=int, default=1, help="Start frame (default 1)")
    ap.add_argument("--frame_end", type=int, default=0, help="End frame (0 = auto from director)")
    ap.add_argument("--skip_prepare", action="store_true", help="Skip prepare stage and reuse existing prepared scene")
    ap.add_argument("--prepared_scene", default="", help="Explicit prepared scene path to reuse")
    ap.add_argument("--render_only", action="store_true", help="Skip pre-render pipeline (parse/TTS/Whisper)")
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Resume: keep existing frames and only render missing spans (does not delete frames folder).",
    )
    ap.add_argument(
        "--smart_resume",
        action="store_true",
        help="Smart resume: keep existing frames and rerender only frame spans affected by director/script changes (plus any missing frames).",
    )
    ap.add_argument(
        "--force_range",
        action="store_true",
        help="Force rerender the requested frame range even if frames already exist (use with --frame_start/--frame_end).",
    )
    ap.add_argument(
        "--plan_only",
        action="store_true",
        help="Print the computed render plan (frame ranges) and exit without rendering.",
    )
    ap.add_argument(
        "--pad_frames",
        type=int,
        default=16,
        help="Padding frames added around changed-beat spans when using --smart_resume (default 16).",
    )
    ap.add_argument("--skip_mux", action="store_true", help="Skip mux step")
    ap.add_argument("--skip_overlays", action="store_true", help="Skip apply_overlays final pass")
    ap.add_argument("--cleanup", action="store_true", help="Delete prepared scene and temp scratch dirs at end")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    cfg_path = Path(args.config).expanduser().resolve()
    cfg = _load_json(cfg_path)
    project_root = Path(__file__).resolve().parents[1]

    blender_raw = str(cfg.get("blender_binary") or "/Applications/Blender.app/Contents/MacOS/Blender")
    blender_bin = str((project_root / blender_raw).resolve()) if not Path(blender_raw).is_absolute() else str(Path(blender_raw).resolve())
    base_scene = (project_root / cfg.get("base_scene_blend", "scenes/base_scene.blend")).resolve()
    default_char = (project_root / cfg.get("default_character_blend", "assets/DefaultCharacter.blend")).resolve()
    generator_inputs = (project_root / cfg.get("generator_inputs_json", "manifests/generator_inputs.json")).resolve()
    script_txt = (project_root / cfg.get("script_txt", "script.txt")).resolve()
    # We optionally redirect default outputs into out/<project_id>/... so reruns are project-scoped.
    # This still respects custom config paths.
    _manifest_cfg = str(cfg.get("manifest_csv_out", "manifests/lines.csv"))
    _director_cfg = str(cfg.get("director_json_out", "director_visemes.json"))
    _out_video_cfg = str(cfg.get("out_video", "out/blender_render.mp4"))
    project_id = _resolve_project_id(generator_inputs, script_txt) or "Video-01"
    project_out_dir = (project_root / "out" / project_id).resolve()
    wants_project_scoped_outputs = True
    if wants_project_scoped_outputs and (not Path(_manifest_cfg).is_absolute()) and _manifest_cfg.replace("\\", "/") == "manifests/lines.csv":
        manifest_csv_out = (project_out_dir / "lines.csv").resolve()
    else:
        manifest_csv_out = (project_root / _manifest_cfg).resolve() if not Path(_manifest_cfg).is_absolute() else Path(_manifest_cfg).resolve()
    if wants_project_scoped_outputs and (not Path(_director_cfg).is_absolute()) and _director_cfg.replace("\\", "/") == "director_visemes.json":
        director_json = (project_out_dir / "director_visemes.json").resolve()
    else:
        director_json = (project_root / _director_cfg).resolve() if not Path(_director_cfg).is_absolute() else Path(_director_cfg).resolve()
    if wants_project_scoped_outputs and (not Path(_out_video_cfg).is_absolute()) and _out_video_cfg.replace("\\", "/") == "out/blender_render.mp4":
        out_video = (project_out_dir / "blender_render.mp4").resolve()
    else:
        out_video = (project_root / _out_video_cfg).resolve() if not Path(_out_video_cfg).is_absolute() else Path(_out_video_cfg).resolve()
    mux_fg_width_ratio = float(cfg.get("mux_fg_width_ratio", 0.73))
    max_frame_end = int(cfg.get("max_frame_end", 12000))
    force_tts = bool(cfg.get("force_tts", False))
    use_smart_prompt_tts = bool(cfg.get("use_smart_prompt_tts", False))
    smart_prompt_context_char_limit = int(cfg.get("smart_prompt_context_char_limit", 2000))
    smart_prompt_include_speaker_labels = bool(cfg.get("smart_prompt_include_speaker_labels", True))
    force_whisper = bool(cfg.get("force_whisper", False))
    hdri_path_cfg = cfg.get("hdri_path")
    hdri_strength_cfg = cfg.get("hdri_strength", 0.7)
    run_generate_chars = bool(cfg.get("run_generate_characters", True))
    run_export_chars = bool(cfg.get("run_export_characters", True))
    run_apply_overlays = bool(cfg.get("run_apply_overlays", False))
    overlay_config = (project_root / cfg.get("overlay_config", "scripts/apply_overlays.config.json")).resolve()
    overlay_image = (project_root / cfg.get("overlay_image", "scenes/VideoPauseOverlay1.png")).resolve()
    overlay_out = (project_root / cfg.get("overlay_out", str(out_video))).resolve()
    overlay_fps = int(cfg.get("overlay_fps", 24))
    export_chars_out_dir = (project_root / cfg.get("export_characters_output_dir", "out/exports/characters")).resolve()
    export_image_width = int(cfg.get("export_image_width", 1200))
    export_file_prefix = str(cfg.get("export_file_prefix", "Char"))
    export_table_object_name = str(cfg.get("export_table_object_name", "Dining table round for 4 people")).strip()
    export_table_file_prefix = str(cfg.get("export_table_file_prefix", "Table"))
    background_image = (project_root / cfg.get("background_image", "scenes/SceneBackground.png")).resolve()

    # Snapshot prior director for smart_resume change detection (must correspond to existing frames).
    prev_director = director_json.with_suffix(".prev.json")
    if args.smart_resume and director_json.exists():
        try:
            _ensure_parent(prev_director)
            shutil.copyfile(director_json, prev_director)
            print(f"[smart_resume] snapped previous director -> {prev_director}", flush=True)
        except Exception as ex:
            print(f"[smart_resume] warn: failed to snapshot previous director: {ex}", flush=True)

    if not args.render_only:
        parse_script_py = project_root / "scripts" / "parse_screenplay_to_manifest.py"
        _ensure_parent(manifest_csv_out)
        # Copy inputs into project out dir for traceability (best-effort; does not affect pipeline).
        try:
            project_out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(generator_inputs, project_out_dir / "generator_inputs.json")
            shutil.copyfile(script_txt, project_out_dir / "script.txt")
        except Exception:
            pass
        _run(
            [
                sys.executable,
                str(parse_script_py),
                "--in_txt",
                str(script_txt),
                "--out_csv",
                str(manifest_csv_out),
                "--project_id",
                project_id,
            ],
            cwd=project_root,
        )

        generated_audio = False
        if force_tts or _need_tts(manifest_csv_out):
            tts_script_name = (
                "tts_typecast_smartprompt_from_manifest.py"
                if use_smart_prompt_tts
                else "tts_typecast_from_manifest.py"
            )
            tts_script = project_root / "scripts" / tts_script_name
            if not os.environ.get("TYPECAST_API_KEY"):
                raise SystemExit("TYPECAST_API_KEY not set; export it to create audio.")
            tts_cmd = [
                sys.executable,
                str(tts_script),
                "--manifest_csv",
                str(manifest_csv_out),
                "--generator_inputs_json",
                str(generator_inputs),
            ]
            if use_smart_prompt_tts:
                tts_cmd += [
                    "--context_char_limit",
                    str(smart_prompt_context_char_limit),
                    "--include_speaker_labels",
                    "true" if smart_prompt_include_speaker_labels else "false",
                ]
            _run(tts_cmd, cwd=project_root)
            generated_audio = True
        else:
            print("[skip] TTS: all audio files already exist and match manifest.")

        if force_whisper or generated_audio or (not director_json.exists()):
            whisper_py = project_root / "scripts" / "whisperx_to_director_visemes.py"
            _run(
                [
                    sys.executable,
                    str(whisper_py),
                    "--manifest_csv",
                    str(manifest_csv_out),
                    "--generator_inputs_json",
                    str(generator_inputs),
                    "--out",
                    str(director_json),
                    "--script_txt",
                    str(script_txt),
                ],
                cwd=project_root,
            )
        else:
            print("[skip] WhisperX: director_visemes.json present and audio unchanged.")

    if not director_json.exists():
        raise SystemExit(f"director_json_out not found after pre-render pipeline: {director_json}")

    d = _load_json(director_json)
    gi = _load_json(generator_inputs)
    fps = int(((gi.get("run") or {}).get("fps")) or d.get("fps") or 24)
    auto_frame_end = max(1, int(round((_estimate_end_seconds_from_director(d) + 2.0) * fps)))
    frame_start = max(1, int(args.frame_start))
    frame_end = int(args.frame_end) if int(args.frame_end) > 0 else auto_frame_end
    frame_end = min(frame_end, max_frame_end)
    if frame_end < frame_start:
        raise SystemExit(f"Invalid frame range: {frame_start}..{frame_end}")

    shards = max(1, int(args.shards))
    max_parallel = max(1, int(args.max_parallel)) if int(args.max_parallel) > 0 else shards

    single_blender_py = project_root / "scripts" / "blender_pipeline_single_process.py"
    mux_py = project_root / "scripts" / "mux_from_director.py"
    tmp_dir = project_root / "out" / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    prepared_scene = Path(args.prepared_scene).expanduser().resolve() if args.prepared_scene else (tmp_dir / f"prepared_scene_{ts}.blend")
    frames_dir = out_video.parent / f"{out_video.stem}_frames"
    frames_pattern = frames_dir / f"{out_video.stem}_%04d.png"
    _ensure_parent(out_video)

    print(f"[shard] project_id={project_id} out_video={out_video}", flush=True)
    print(f"[shard] frame_range={frame_start}..{frame_end} fps={fps} shards={shards} max_parallel={max_parallel}", flush=True)

    if not args.skip_prepare:
        prep_scratch = tmp_dir / f"shard_prepare_{ts}"
        prep_env = _blender_env(prep_scratch)
        prep_cmd = [
            blender_bin,
            "-b",
            str(base_scene),
            "--python-exit-code",
            "1",
            "--python",
            str(single_blender_py),
            "--",
            "--generator_inputs_json",
            str(generator_inputs),
            "--base_scene_blend",
            str(base_scene),
            "--default_character_blend",
            str(default_char),
            "--work_scene",
            str(tmp_dir / f"work_scene_prepare_{ts}.blend"),
            "--run_generate_characters" if run_generate_chars else "",
            "--run_configure_roles",
            "--run_export_characters" if run_export_chars else "",
            "--export_characters_output_dir",
            str(export_chars_out_dir),
            "--export_image_width",
            str(export_image_width),
            "--export_file_prefix",
            str(export_file_prefix),
            "--export_table_object_name",
            str(export_table_object_name),
            "--export_table_file_prefix",
            str(export_table_file_prefix),
            "--max_frame_end",
            str(max_frame_end),
            "--prepare_only",
            "--scene_out",
            str(prepared_scene),
            "--hdri_from_config",
            str(cfg_path),
        ]
        if hdri_path_cfg:
            prep_cmd += ["--hdri_path", str(hdri_path_cfg)]
        if hdri_strength_cfg is not None:
            prep_cmd += ["--hdri_strength", str(hdri_strength_cfg)]
        prep_cmd = [x for x in prep_cmd if x != ""]
        _run(prep_cmd, env=prep_env, cwd=project_root)
    else:
        if not prepared_scene.exists():
            raise SystemExit(f"--skip_prepare provided but prepared scene not found: {prepared_scene}")

    keep_existing_frames_dir = bool(args.resume or args.smart_resume or args.force_range)
    if frames_dir.exists() and (not keep_existing_frames_dir):
        shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    existing_frames = _scan_existing_frames(frames_dir, out_video.stem) if keep_existing_frames_dir else set()
    if keep_existing_frames_dir:
        have = len([f for f in existing_frames if frame_start <= f <= frame_end])
        need = (frame_end - frame_start + 1) - have
        print(f"[frames] existing_frames_in_range={have} missing_frames={max(0, need)}", flush=True)

    # Plan render spans
    spans_to_render: list[tuple[int, int]] = []
    if args.force_range:
        spans_to_render = [(frame_start, frame_end)]
    elif args.smart_resume:
        missing_spans = _missing_ranges_in_span(frame_start, frame_end, existing_frames)
        changed_spans: list[tuple[int, int]] = []
        old_d = None
        if prev_director.exists():
            try:
                old_d = _load_json(prev_director)
            except Exception:
                old_d = None
        try:
            changed_spans = _beat_change_ranges_frames(
                old_director=old_d,
                new_director=d,
                fps=fps,
                pad_frames=int(args.pad_frames),
                frame_start=frame_start,
                frame_end=frame_end,
            )
        except Exception as ex:
            print(f"[smart_resume] warn: failed to compute changed spans; falling back to missing-only: {ex}", flush=True)
            changed_spans = []
        spans_to_render = _merge_ranges(list(missing_spans) + list(changed_spans))
        print(f"[smart_resume] changed_spans={changed_spans}", flush=True)
        print(f"[smart_resume] missing_spans={missing_spans}", flush=True)
    elif args.resume:
        spans_to_render = _missing_ranges_in_span(frame_start, frame_end, existing_frames)
    else:
        spans_to_render = [(frame_start, frame_end)]

    if not spans_to_render:
        print("[plan] nothing to render (frames already present and no detected changes)", flush=True)
        spans_to_render = []
    else:
        print(f"[plan] spans_to_render={spans_to_render}", flush=True)

    if args.plan_only:
        return

    # Expand to shard ranges only when we have a single large span, to preserve
    # the prior "split full range into N shards" behavior without over-fragmenting.
    tasks: list[tuple[int, int]] = []
    if len(spans_to_render) == 1 and shards > 1:
        s0, e0 = spans_to_render[0]
        tasks = _chunk_ranges(s0, e0, shards)
    else:
        tasks = list(spans_to_render)

    if not tasks:
        # Still proceed to mux/overlays using existing frames.
        print("[plan] no render tasks; proceeding to mux/overlays (if enabled)", flush=True)
    else:
        print(f"[plan] tasks={tasks}", flush=True)

    def _run_task(idx: int, r: tuple[int, int]) -> None:
        s, e = r
        shard_env = _blender_env(tmp_dir / f"shard_run_{ts}_{idx}")

        force_configure_roles = (os.environ.get("VPG_FORCE_CONFIGURE_ROLES") or "").strip() == "1"
        shard_cmd = [
            blender_bin,
            "-b",
            str(prepared_scene),
            "--python-exit-code",
            "1",
            "--python",
            str(single_blender_py),
            "--",
            "--generator_inputs_json",
            str(generator_inputs),
            "--input_scene",
            str(prepared_scene),
            "--director_json",
            str(director_json),
            "--out_video",
            str(out_video),
            "--frame_start",
            str(int(s)),
            "--frame_end",
            str(int(e)),
            "--max_frame_end",
            str(max_frame_end),
            "--no_audio",
            "--no_clean_frames",
            "--hdri_from_config",
            str(cfg_path),
        ]
        if force_configure_roles:
            shard_cmd += ["--run_configure_roles"]
        if hdri_path_cfg:
            shard_cmd += ["--hdri_path", str(hdri_path_cfg)]
        if hdri_strength_cfg is not None:
            shard_cmd += ["--hdri_strength", str(hdri_strength_cfg)]
        print(f"[task:{idx}] render frames={s}..{e}", flush=True)
        _run(shard_cmd, env=shard_env, cwd=project_root)
        print(f"[task:{idx}] done frames={s}..{e}", flush=True)

    if tasks:
        with ThreadPoolExecutor(max_workers=max_parallel) as ex:
            futs = [ex.submit(_run_task, i + 1, r) for i, r in enumerate(tasks)]
            for fut in as_completed(futs):
                fut.result()

    expected = frame_end - frame_start + 1
    final_existing = _scan_existing_frames(frames_dir, out_video.stem)
    missing_final = [f for f in range(frame_start, frame_end + 1) if f not in final_existing]
    produced_in_range = expected - len(missing_final)
    print(f"[shard] frames produced_in_range={produced_in_range} expected={expected}", flush=True)
    if missing_final:
        # Print a small preview; the user can rerun with --resume to fill gaps.
        preview = missing_final[:25]
        raise SystemExit(f"Missing frames (first {len(preview)} of {len(missing_final)}): {preview}")

    if not args.skip_mux:
        cmd_mux = [
            sys.executable,
            str(mux_py),
            "--director",
            str(director_json),
            "--frames",
            str(frames_pattern),
            "--out",
            str(out_video),
            "--generator_inputs_json",
            str(generator_inputs),
            "--fg_width_ratio",
            str(mux_fg_width_ratio),
        ]
        if background_image.exists():
            cmd_mux += ["--background", str(background_image)]
        _run(cmd_mux, cwd=project_root)

    if run_apply_overlays and (not args.skip_overlays):
        apply_py = project_root / "scripts" / "apply_overlays.py"
        desired_overlay_out = overlay_out or out_video
        tmp_overlay_out = out_video.with_name(f"{out_video.stem}.with_overlays.mp4")
        writing_in_place = desired_overlay_out.resolve() == out_video.resolve()
        target_overlay_out = tmp_overlay_out if writing_in_place else desired_overlay_out
        _ensure_parent(target_overlay_out)

        cmd_apply = [sys.executable, str(apply_py)]
        if overlay_config.exists():
            cmd_apply += ["--config", str(overlay_config)]
        cmd_apply += [
            "--script",
            str(script_txt),
            "--director",
            str(director_json),
            "--base",
            str(out_video),
            "--out",
            str(target_overlay_out),
            "--fps",
            str(overlay_fps),
            "--generator_inputs_json",
            str(generator_inputs),
        ]
        if overlay_image.exists():
            cmd_apply += ["--overlay_image", str(overlay_image)]
        _run(cmd_apply, cwd=project_root)
        if writing_in_place:
            os.replace(target_overlay_out, out_video)
            print(f"[info] Applied overlays in-place -> {out_video}", flush=True)
        else:
            print(f"[info] Applied overlays -> {target_overlay_out}", flush=True)

    print(f"[done] local sharded render complete: {out_video}", flush=True)

    if args.cleanup:
        try:
            if prepared_scene.exists():
                prepared_scene.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()

