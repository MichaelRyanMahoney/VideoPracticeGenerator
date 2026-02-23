#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
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


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "run_full_video_creation_sequence.config.json"),
        help="Path to run_full_video_creation_sequence.config.json",
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
    tmp_scene_with_roles = tmp_dir / f"work_scene_{ts}_with_roles.blend"
    tmp_scene_configured = tmp_dir / f"work_scene_{ts}_configured.blend"
    tmp_scene_pre_render = tmp_dir / f"work_scene_{ts}_pre_render.blend"
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

    # 1b) (Optional) Generate per-role character files and append them into the temp scene
    gen_script = project_root / "scripts" / "blender_generate_character_files.py"
    if run_generate_chars and gen_script.exists():
        if not default_character_blend:
            raise SystemExit("default_character_blend not set in config.")
        if not Path(default_character_blend).exists():
            raise SystemExit(f"default_character_blend not found: {default_character_blend}")
        pre = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24"] if os.environ.get("VPG_XVFB") == "1" else []
        cmd_gen = pre + [
            str(blender_bin),
            "-b",
            str(default_character_blend),
            "--python-exit-code",
            "1",
            "--python",
            str(gen_script),
            "--",
            "--config",
            str(generator_inputs_json),
            "--source",
            str(default_character_blend),
            "--append-scene",
            str(tmp_scene),
            "--scene-save-as",
            str(tmp_scene_with_roles),
        ]
        run_cmd(cmd_gen, env=blender_env)
        # Use a freshly-saved filename for downstream steps to avoid in-place overwrite
        # save issues observed in some Blender/container environments.
        tmp_scene = tmp_scene_with_roles
    elif run_generate_chars:
        print("[skip] Character generation: blender_generate_character_files.py not found.")

    # 2) Configure roles in scene per generator_inputs.json (HDRI, visibility, colors)
    #    Must run BEFORE character export so stills get correct HDRI and character setup.
    cfg_script = project_root / "scripts" / "blender_configure_roles_for_render.py"
    if bool(cfg.get("skip_configure_roles", False)):
        print("[skip] Configure roles step per config (skip_configure_roles=True).")
    else:
        if not cfg_script.exists():
            raise SystemExit(f"Missing script: {cfg_script}")
        pre = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24"] if os.environ.get("VPG_XVFB") == "1" else []
        cmd_cfg = pre + [
            str(blender_bin),
            "-b",
            str(tmp_scene),
            "--python-exit-code",
            "1",
            "--python",
            str(cfg_script),
            "--",
            "--config",
            str(generator_inputs_json),
            "--trace",
            "--save-as",
            str(tmp_scene_configured),
        ]
        if hdri_path_cfg:
            cmd_cfg += ["--hdri_path", str(hdri_path_cfg)]
        if hdri_strength_cfg is not None:
            cmd_cfg += ["--hdri_strength", str(hdri_strength_cfg)]
        run_cmd(cmd_cfg, env=blender_env)
        tmp_scene = tmp_scene_configured

    # 2b) Export character PNGs from the configured scene (after roles/HDRI are set)
    export_script = project_root / "scripts" / "blender_export_characters.py"
    if run_export_chars and export_script.exists():
        ensure_parent(export_chars_out_dir / "x")
        pre = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24"] if os.environ.get("VPG_XVFB") == "1" else []
        cmd_export = pre + [
            str(blender_bin),
            "-b",
            str(tmp_scene),
            "--python-exit-code",
            "1",
            "--python",
            str(export_script),
            "--",
            "--output-dir",
            str(export_chars_out_dir),
            "--roles",
            "Disputant1",
            "MediatorA",
            "MediatorB",
            "Disputant2",
            "--file-prefix",
            export_file_prefix,
            "--image-width",
            str(export_image_width),
            "--generator_inputs_json",
            str(generator_inputs_json),
        ]
        if hdri_path_cfg:
            cmd_export += ["--hdri_path", str(hdri_path_cfg)]
        if hdri_strength_cfg is not None:
            cmd_export += ["--hdri_strength", str(hdri_strength_cfg)]
        run_cmd(cmd_export, env=blender_env)

        if export_table_object_name:
            cmd_export_table = pre + [
                str(blender_bin),
                "-b",
                str(tmp_scene),
                "--python-exit-code",
                "1",
                "--python",
                str(export_script),
                "--",
                "--output-dir",
                str(export_chars_out_dir),
                "--objects",
                export_table_object_name,
                "--file-prefix",
                export_table_file_prefix,
                "--image-width",
                str(export_image_width),
                "--generator_inputs_json",
                str(generator_inputs_json),
            ]
            if hdri_path_cfg:
                cmd_export_table += ["--hdri_path", str(hdri_path_cfg)]
            if hdri_strength_cfg is not None:
                cmd_export_table += ["--hdri_strength", str(hdri_strength_cfg)]
            run_cmd(cmd_export_table, env=blender_env)
    elif run_export_chars:
        print("[skip] Character export: blender_export_characters.py not found.")

    # 3) Build manifest CSV from script.txt
    parse_script_py = project_root / "scripts" / "parse_screenplay_to_manifest.py"
    if not parse_script_py.exists():
        raise SystemExit(f"Missing script: {parse_script_py}")
    ensure_parent(manifest_csv_out)
    run_cmd(
        [
            sys.executable,
            str(parse_script_py),
            "--in_txt",
            str(script_txt),
            "--out_csv",
            str(manifest_csv_out),
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
            ]
        )
    else:
        print("[skip] WhisperX: director_visemes.json present and audio unchanged.")

    # 6) Render visemes in Blender (PNG RGBA frames by default)
    frames_dir = out_video.parent / f"{out_video.stem}_frames"
    frames_pattern = frames_dir / f"{out_video.stem}_%04d.png"
    if not skip_render:
        run_director_py = project_root / "scripts" / "run_director_visemes.py"
        if not run_director_py.exists():
            raise SystemExit(f"Missing script: {run_director_py}")
        ensure_parent(out_video)
        pre = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24"] if os.environ.get("VPG_XVFB") == "1" else []
        # Safety: re-assert HDRI/world setup immediately before rendering.
        # This avoids rendering with stale world state if earlier steps or scene mutations
        # altered node bindings.
        cmd_reassert_hdri = pre + [
            str(blender_bin),
            "-b",
            str(tmp_scene),
            "--python-exit-code",
            "1",
            "--python",
            str(cfg_script),
            "--",
            "--config",
            str(generator_inputs_json),
            "--hdri_from_config",
            str(cfg_path),
            "--save-as",
            str(tmp_scene_pre_render),
        ]
        run_cmd(cmd_reassert_hdri, env=blender_env)
        tmp_scene = tmp_scene_pre_render
        cmd_render = pre + [
            str(blender_bin),
            "-b",
            str(tmp_scene),
            "--python-exit-code",
            "1",
            "--python",
            str(run_director_py),
            "--",
            "--director",
            str(director_json_out),
            "--out",
            str(out_video),
            "--max_frame_end",
            str(max_frame_end),
        ]
        if enforce_render_frame_guard and director_json_out.exists() and generator_inputs_json.exists():
            try:
                d = load_json(director_json_out)
                gi = load_json(generator_inputs_json)
                fps_guard = int(((gi.get("run") or {}).get("fps")) or d.get("fps") or 24)
                est_end_s = _estimate_end_seconds_from_director(d)
                guard_frame_end = max(1, int(round((est_end_s + render_frame_guard_pad_sec) * fps_guard)))
                cmd_render += ["--frame_end", str(guard_frame_end)]
                print(f"[info] render frame guard enabled: frame_end={guard_frame_end} (fps={fps_guard}, est_end_s={est_end_s:.3f})")
            except Exception as ex:
                print(f"[warn] failed to compute render frame guard; continuing without it: {ex}")
        render_env = blender_env.copy()
        # Ensure run_director_visemes uses the job-scoped generator_inputs, not repo default.
        render_env["VPG_GENERATOR_INPUTS_JSON"] = str(generator_inputs_json)
        # Keep these explicit in orchestrated renders to avoid scene-level black outputs.
        render_env.setdefault("VPG_USE_SEQUENCER", "0")
        render_env.setdefault("VPG_USE_COMPOSITING", "0")
        run_cmd(cmd_render, env=render_env)
    else:
        print("[skip] Render step per config.")

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


