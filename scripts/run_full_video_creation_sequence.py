#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import re
from pathlib import Path
import os


def run_cmd(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("[run]", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
    if res.returncode != 0:
        raise SystemExit(res.returncode)


def file_exists(p: str | Path) -> bool:
    try:
        return Path(p).exists()
    except Exception:
        return False


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())

def _project_id_from_generator_inputs(generator_inputs_json: Path) -> str:
    try:
        gi = load_json(generator_inputs_json)
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


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _path_from_cfg(project_root: Path, value: str | Path | None) -> Path | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        return p
    return project_root / p


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
        # keep minimum per-line floor so super-short viseme rows still advance
        end_s = max(end_s, max(vmax, t0 + 1.0))
    return max(0.0, float(end_s))


def _count_script_pauses(script_path: Path) -> int:
    try:
        text = script_path.read_text()
    except Exception:
        return 0
    return len(re.findall(r"\[PAUSE\]", text, flags=re.IGNORECASE))


def _count_director_pause_beats(director: dict) -> int:
    total = 0
    for b in director.get("beats", []) or []:
        if str(b.get("type") or "").lower() == "pause":
            total += 1
    return total


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "run_full_video_creation_sequence.config.json"),
        help="Path to run_full_video_creation_sequence.config.json",
    )
    ap.add_argument("--frame_start", type=int, default=0, help="Optional render start frame override (0 = auto)")
    ap.add_argument("--frame_end", type=int, default=0, help="Optional render end frame override (0 = auto/guarded)")
    ap.add_argument(
        "--no_clean_frames",
        action="store_true",
        help="Do not delete existing PNG frames in the output frames folder (useful for partial rerenders).",
    )
    return ap.parse_args()


def need_tts(manifest_csv: Path) -> bool:
    import csv
    if not manifest_csv.exists():
        return True
    with open(manifest_csv, newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            audio = (row.get("audio") or "").strip()
            if not audio:
                # pause or empty row
                continue
            if not Path(audio).exists():
                return True
    return False


def main():
    args = parse_args()
    cfg_path = Path(args.config).expanduser().resolve()
    if not cfg_path.exists():
        raise SystemExit(f"Config not found: {cfg_path}")
    cfg = load_json(cfg_path)

    project_root = Path(__file__).resolve().parents[1]

    blender_bin = Path(cfg.get("blender_binary") or "/Applications/Blender.app/Contents/MacOS/Blender")
    default_character_blend = Path(cfg.get("default_character_blend") or "")
    base_scene_blend = Path(cfg.get("base_scene_blend") or (project_root / "scenes" / "base_scene7_with_chars.blend"))
    generator_inputs_json = Path(cfg.get("generator_inputs_json") or (project_root / "manifests" / "generator_inputs.json"))
    script_txt = Path(cfg.get("script_txt") or (project_root / "script.txt"))
    manifest_csv_out = Path(cfg.get("manifest_csv_out") or (project_root / "manifests" / "lines.csv"))
    # Legacy voice_map_json no longer required; use generator_inputs_json for Typecast voices
    director_json_out = Path(cfg.get("director_json_out") or (project_root / "director_visemes.json"))
    out_video = Path(cfg.get("out_video") or (project_root / "out" / "visemes.mp4"))
    background_image = _path_from_cfg(project_root, cfg.get("background_image"))
    cleanup_temp = bool(cfg.get("cleanup_temp", True))
    force_tts = bool(cfg.get("force_tts", False))
    use_smart_prompt_tts = bool(cfg.get("use_smart_prompt_tts", False))
    smart_prompt_context_char_limit = int(cfg.get("smart_prompt_context_char_limit", 2000))
    smart_prompt_include_speaker_labels = bool(cfg.get("smart_prompt_include_speaker_labels", True))
    force_whisper = bool(cfg.get("force_whisper", False))
    skip_render = bool(cfg.get("skip_render", False))
    skip_mux = bool(cfg.get("skip_mux", False))
    run_apply_overlays = bool(cfg.get("run_apply_overlays", False))
    overlay_config = _path_from_cfg(project_root, cfg.get("overlay_config"))
    overlay_image = _path_from_cfg(project_root, cfg.get("overlay_image"))
    overlay_out = _path_from_cfg(project_root, cfg.get("overlay_out"))
    overlay_fps = int(cfg.get("overlay_fps", 24))
    mux_fg_width_ratio = float(cfg.get("mux_fg_width_ratio", 0.73))
    enforce_render_frame_guard = bool(cfg.get("enforce_render_frame_guard", True))
    render_frame_guard_pad_sec = float(cfg.get("render_frame_guard_pad_sec", 2.0))
    max_frame_end = int(cfg.get("max_frame_end", 12000))
    # HDRI settings (kept in this orchestrator config)
    hdri_path_cfg = cfg.get("hdri_path")
    hdri_strength_cfg = cfg.get("hdri_strength", 0.7)
    # Character generation/export steps
    run_generate_chars = bool(cfg.get("run_generate_characters", True))
    run_export_chars = bool(cfg.get("run_export_characters", True))
    export_chars_out_dir = Path(cfg.get("export_characters_output_dir") or (project_root / "out" / "exports" / "characters"))
    export_image_width = int(cfg.get("export_image_width", 1200))
    export_file_prefix = str(cfg.get("export_file_prefix", "Char"))
    export_table_object_name = str(cfg.get("export_table_object_name", "Dining table round for 4 people")).strip()
    export_table_file_prefix = str(cfg.get("export_table_file_prefix", "Table"))

    # Derived paths
    tmp_dir = project_root / "out" / "_tmp"
    ensure_parent(tmp_dir / "x")
    ts = time.strftime("%Y%m%d_%H%M%S")
    tmp_scene = tmp_dir / f"work_scene_{ts}.blend"
    blender_scratch = tmp_dir / f"blender_scratch_{ts}"
    blender_scratch.mkdir(parents=True, exist_ok=True)

    # Ensure subprocesses that support env-based input discovery prefer this run's config.
    os.environ["VPG_GENERATOR_INPUTS_JSON"] = str(generator_inputs_json)
    # Force Blender to use writable temp/config homes in containerized runs.
    blender_env = os.environ.copy()
    blender_env["TMPDIR"] = str(blender_scratch)
    blender_env.setdefault("HOME", str(blender_scratch))
    blender_env.setdefault("XDG_CACHE_HOME", str(blender_scratch / ".cache"))
    blender_env.setdefault("XDG_CONFIG_HOME", str(blender_scratch / ".config"))
    blender_env.setdefault("XDG_DATA_HOME", str(blender_scratch / ".local" / "share"))
    (blender_scratch / ".cache").mkdir(parents=True, exist_ok=True)
    (blender_scratch / ".config").mkdir(parents=True, exist_ok=True)
    (blender_scratch / ".local" / "share").mkdir(parents=True, exist_ok=True)

    # 1) Prepare scene blend: copy base scene → tmp scene
    if not base_scene_blend.exists():
        raise SystemExit(f"Base scene not found: {base_scene_blend}")
    shutil.copyfile(base_scene_blend, tmp_scene)
    print(f"[info] Copied base scene to: {tmp_scene}")

    # 1b/2/2b) Blender scene prep + optional exports are now executed in a single
    # Blender process later in this script (after director JSON is ready), to avoid
    # save/reopen boundaries across Blender subprocesses.
    gen_script = project_root / "scripts" / "blender_generate_character_files.py"
    if run_generate_chars and (not gen_script.exists()):
        print("[skip] Character generation: blender_generate_character_files.py not found.")

    # Validate Blender helper scripts early (actual execution happens in single-process runner).
    cfg_script = project_root / "scripts" / "blender_configure_roles_for_render.py"
    if not bool(cfg.get("skip_configure_roles", False)) and (not cfg_script.exists()):
        raise SystemExit(f"Missing script: {cfg_script}")

    export_script = project_root / "scripts" / "blender_export_characters.py"
    if run_export_chars and (not export_script.exists()):
        print("[skip] Character export: blender_export_characters.py not found.")

    # 3) Build manifest CSV from script.txt
    parse_script_py = project_root / "scripts" / "parse_screenplay_to_manifest.py"
    if not parse_script_py.exists():
        raise SystemExit(f"Missing script: {parse_script_py}")
    project_id = _resolve_project_id(generator_inputs_json, script_txt)
    ensure_parent(manifest_csv_out)
    run_cmd(
        [
            sys.executable,
            str(parse_script_py),
            "--in_txt",
            str(script_txt),
            "--out_csv",
            str(manifest_csv_out),
            "--project_id",
            project_id,
        ]
    )

    # 4) TTS (Typecast) if needed
    generated_audio = False
    if force_tts or need_tts(manifest_csv_out):
        tts_script_name = (
            "tts_typecast_smartprompt_from_manifest.py"
            if use_smart_prompt_tts
            else "tts_typecast_from_manifest.py"
        )
        tts_script = project_root / "scripts" / tts_script_name
        if not tts_script.exists():
            raise SystemExit(f"Missing script: {tts_script}")
        if not os.environ.get("TYPECAST_API_KEY"):
            raise SystemExit("TYPECAST_API_KEY not set; export it to create audio.")
        tts_cmd = [
            sys.executable,
            str(tts_script),
            "--manifest_csv",
            str(manifest_csv_out),
            "--generator_inputs_json",
            str(generator_inputs_json),
        ]
        if use_smart_prompt_tts:
            tts_cmd += [
                "--context_char_limit",
                str(smart_prompt_context_char_limit),
                "--include_speaker_labels",
                "true" if smart_prompt_include_speaker_labels else "false",
            ]
        run_cmd(tts_cmd)
        generated_audio = True
    else:
        print("[skip] TTS: all audio files already exist and match manifest.")

    # 5) WhisperX to director (only if audio generated or director missing, unless forced)
    if force_whisper or generated_audio or (not director_json_out.exists()):
        whisper_py = project_root / "scripts" / "whisperx_to_director_visemes.py"
        if not whisper_py.exists():
            raise SystemExit(f"Missing script: {whisper_py}")
        run_cmd(
            [
                sys.executable,
                str(whisper_py),
                "--manifest_csv",
                str(manifest_csv_out),
                "--generator_inputs_json",
                str(generator_inputs_json),
                "--out",
                str(director_json_out),
                "--script_txt",
                str(script_txt),
            ]
        )
    else:
        print("[skip] WhisperX: director_visemes.json present and audio unchanged.")

    # Validate pause coverage so overlay timing cannot silently drift.
    if director_json_out.exists():
        try:
            director_data = load_json(director_json_out)
            script_pause_count = _count_script_pauses(script_txt)
            director_pause_count = _count_director_pause_beats(director_data)
            if script_pause_count > 0 and director_pause_count == 0:
                raise SystemExit(
                    f"Pause coverage check failed: script has {script_pause_count} [PAUSE] marker(s) "
                    f"but director has 0 pause beats ({director_json_out})."
                )
            if script_pause_count >= 6 and director_pause_count < max(3, int(script_pause_count * 0.5)):
                raise SystemExit(
                    f"Pause coverage check failed: script has {script_pause_count} [PAUSE] marker(s) "
                    f"but director has only {director_pause_count} pause beats ({director_json_out})."
                )
        except SystemExit:
            raise
        except Exception as ex:
            print(f"[warn] pause coverage validation skipped due to error: {ex}")

    # 6) Run single-process Blender pipeline (prep/config/export/render).
    frames_dir = out_video.parent / f"{out_video.stem}_frames"
    frames_pattern = frames_dir / f"{out_video.stem}_%04d.png"
    single_blender_py = project_root / "scripts" / "blender_pipeline_single_process.py"
    if not single_blender_py.exists():
        raise SystemExit(f"Missing script: {single_blender_py}")
    if run_generate_chars:
        if not default_character_blend:
            raise SystemExit("default_character_blend not set in config.")
        if not Path(default_character_blend).exists():
            raise SystemExit(f"default_character_blend not found: {default_character_blend}")
    ensure_parent(out_video)
    pre = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24"] if os.environ.get("VPG_XVFB") == "1" else []
    cmd_blender_pipeline = pre + [
        str(blender_bin),
        "-b",
        str(base_scene_blend),
        "--python-exit-code",
        "1",
        "--python",
        str(single_blender_py),
        "--",
        "--generator_inputs_json",
        str(generator_inputs_json),
        "--base_scene_blend",
        str(base_scene_blend),
        "--work_scene",
        str(tmp_scene),
        "--run_generate_characters" if run_generate_chars else "",
        "--default_character_blend" if run_generate_chars else "",
        str(default_character_blend) if run_generate_chars else "",
        "--run_configure_roles" if (not bool(cfg.get("skip_configure_roles", False))) else "",
        "--run_export_characters" if run_export_chars else "",
        "--export_characters_output_dir",
        str(export_chars_out_dir),
        "--export_image_width",
        str(export_image_width),
        "--export_file_prefix",
        str(export_file_prefix),
        "--export_table_object_name",
        str(export_table_object_name or ""),
        "--export_table_file_prefix",
        str(export_table_file_prefix),
        "--max_frame_end",
        str(max_frame_end),
        "--hdri_from_config",
        str(cfg_path),
    ]
    if hdri_path_cfg:
        cmd_blender_pipeline += ["--hdri_path", str(hdri_path_cfg)]
    if hdri_strength_cfg is not None:
        cmd_blender_pipeline += ["--hdri_strength", str(hdri_strength_cfg)]
    if skip_render:
        cmd_blender_pipeline += ["--prepare_only"]
    else:
        cmd_blender_pipeline += [
            "--director_json",
            str(director_json_out),
            "--out_video",
            str(out_video),
        ]
        if int(args.frame_start) > 0:
            cmd_blender_pipeline += ["--frame_start", str(int(args.frame_start))]
        if int(args.frame_end) > 0:
            cmd_blender_pipeline += ["--frame_end", str(int(args.frame_end))]
        if bool(args.no_clean_frames):
            cmd_blender_pipeline += ["--no_clean_frames"]
        if enforce_render_frame_guard and director_json_out.exists() and generator_inputs_json.exists():
            try:
                d = load_json(director_json_out)
                gi = load_json(generator_inputs_json)
                fps_guard = int(((gi.get("run") or {}).get("fps")) or d.get("fps") or 24)
                est_end_s = _estimate_end_seconds_from_director(d)
                guard_frame_end = max(1, int(round((est_end_s + render_frame_guard_pad_sec) * fps_guard)))
                # Only apply guard when the caller did not specify an explicit end frame.
                if int(args.frame_end) <= 0:
                    cmd_blender_pipeline += ["--frame_end", str(guard_frame_end)]
                print(f"[info] render frame guard enabled: frame_end={guard_frame_end} (fps={fps_guard}, est_end_s={est_end_s:.3f})")
            except Exception as ex:
                print(f"[warn] failed to compute render frame guard; continuing without it: {ex}")
    # Remove empty placeholders added by conditional args above.
    cmd_blender_pipeline = [x for x in cmd_blender_pipeline if x != ""]
    run_cmd(cmd_blender_pipeline, env=blender_env)

    # 7) Mux to MP4
    if not skip_mux:
        mux_py = project_root / "scripts" / "mux_from_director.py"
        if not mux_py.exists():
            raise SystemExit(f"Missing script: {mux_py}")
        cmd_mux = [
            sys.executable,
            str(mux_py),
            "--director",
            str(director_json_out),
            "--frames",
            str(frames_pattern),
            "--out",
            str(out_video),
            "--generator_inputs_json",
            str(generator_inputs_json),
            "--fg_width_ratio",
            str(mux_fg_width_ratio),
        ]
        if background_image and background_image.exists():
            cmd_mux += ["--background", str(background_image)]
        run_cmd(cmd_mux)
    else:
        print("[skip] Mux step per config.")

    # 8) Optional overlays/final polish pass
    if run_apply_overlays:
        apply_py = project_root / "scripts" / "apply_overlays.py"
        if not apply_py.exists():
            raise SystemExit(f"Missing script: {apply_py}")
        if overlay_config and not overlay_config.exists():
            raise SystemExit(f"Overlay config not found: {overlay_config}")
        if not overlay_config and not overlay_image:
            raise SystemExit(
                "run_apply_overlays=True requires either overlay_config or overlay_image in config."
            )

        # Avoid read/write to same file path for apply_overlays.
        desired_overlay_out = overlay_out or out_video
        tmp_overlay_out = out_video.with_name(f"{out_video.stem}.with_overlays.mp4")
        writing_in_place = desired_overlay_out.resolve() == out_video.resolve()
        target_overlay_out = tmp_overlay_out if writing_in_place else desired_overlay_out
        ensure_parent(target_overlay_out)

        cmd_apply = [sys.executable, str(apply_py)]
        if overlay_config:
            cmd_apply += ["--config", str(overlay_config)]
        # Explicitly override timeline I/O so apply_overlays always uses this run's artifacts.
        cmd_apply += [
            "--script", str(script_txt),
            "--director", str(director_json_out),
            "--base", str(out_video),
            "--out", str(target_overlay_out),
            "--fps", str(overlay_fps),
            "--generator_inputs_json", str(generator_inputs_json),
        ]
        if overlay_image:
            cmd_apply += ["--overlay_image", str(overlay_image)]
        run_cmd(cmd_apply)

        if writing_in_place:
            os.replace(target_overlay_out, out_video)
            print(f"[info] Applied overlays in-place -> {out_video}")
        else:
            print(f"[info] Applied overlays -> {target_overlay_out}")
    else:
        print("[skip] Overlays step per config.")

    # 9) Cleanup
    if cleanup_temp:
        try:
            if tmp_scene.exists():
                tmp_scene.unlink()
                print(f"[cleanup] Removed temp scene: {tmp_scene}")
        except Exception as ex:
            print(f"[cleanup] Warning: failed to remove {tmp_scene}: {ex}")

    print("[done] Full video creation sequence completed.")


if __name__ == "__main__":
    main()


