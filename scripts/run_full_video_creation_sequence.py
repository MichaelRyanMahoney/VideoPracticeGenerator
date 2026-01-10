#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def run_cmd(cmd: list[str], cwd: Path | None = None) -> None:
    print("[run]", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
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
    background_image = Path(cfg.get("background_image")) if cfg.get("background_image") else None
    cleanup_temp = bool(cfg.get("cleanup_temp", True))
    force_tts = bool(cfg.get("force_tts", False))
    force_whisper = bool(cfg.get("force_whisper", False))
    skip_render = bool(cfg.get("skip_render", False))
    skip_mux = bool(cfg.get("skip_mux", False))
    # HDRI settings (kept in this orchestrator config)
    hdri_path_cfg = cfg.get("hdri_path")
    hdri_strength_cfg = cfg.get("hdri_strength", 0.7)
    # Character generation/export steps
    run_generate_chars = bool(cfg.get("run_generate_characters", True))
    run_export_chars = bool(cfg.get("run_export_characters", True))
    export_chars_out_dir = Path(cfg.get("export_characters_output_dir") or (project_root / "out" / "exports" / "characters"))
    export_image_width = int(cfg.get("export_image_width", 1200))
    export_file_prefix = str(cfg.get("export_file_prefix", "Char"))

    # Derived paths
    tmp_dir = project_root / "out" / "_tmp"
    ensure_parent(tmp_dir / "x")
    ts = time.strftime("%Y%m%d_%H%M%S")
    tmp_scene = tmp_dir / f"work_scene_{ts}.blend"

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
        cmd_gen = [
            str(blender_bin),
            "-b",
            str(default_character_blend),
            "--python",
            str(gen_script),
            "--",
            "--config",
            str(generator_inputs_json),
            "--source",
            str(default_character_blend),
            "--append-scene",
            str(tmp_scene),
            "--scene-save",
        ]
        run_cmd(cmd_gen)
    elif run_generate_chars:
        print("[skip] Character generation: blender_generate_character_files.py not found.")

    # 1c) (Optional) Export character PNGs from the combined temp scene (with characters appended and positioned)
    export_script = project_root / "scripts" / "blender_export_characters.py"
    if run_export_chars and export_script.exists():
        ensure_parent(export_chars_out_dir / "x")
        cmd_export = [
            str(blender_bin),
            "-b",
            str(tmp_scene),
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
        run_cmd(cmd_export)
    elif run_export_chars:
        print("[skip] Character export: blender_export_characters.py not found.")

    # 2) Configure roles in scene per generator_inputs.json
    cfg_script = project_root / "scripts" / "blender_configure_roles_for_render.py"
    if not cfg_script.exists():
        raise SystemExit(f"Missing script: {cfg_script}")
    cmd_cfg = [
        str(blender_bin),
        "-b",
        str(tmp_scene),
        "--python",
        str(cfg_script),
        "--",
        "--config",
        str(generator_inputs_json),
        "--trace",
        "--save",
    ]
    # Pass HDRI overrides from orchestrator config
    if hdri_path_cfg:
        cmd_cfg += ["--hdri_path", str(hdri_path_cfg)]
    if hdri_strength_cfg is not None:
        cmd_cfg += ["--hdri_strength", str(hdri_strength_cfg)]
    run_cmd(cmd_cfg)

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
        tts_script = project_root / "scripts" / "tts_typecast_from_manifest.py"
        if not tts_script.exists():
            raise SystemExit(f"Missing script: {tts_script}")
        if not os.environ.get("TYPECAST_API_KEY"):
            raise SystemExit("TYPECAST_API_KEY not set; export it to create audio.")
        run_cmd(
            [
                sys.executable,
                str(tts_script),
                "--manifest_csv",
                str(manifest_csv_out),
                "--generator_inputs_json",
                str(generator_inputs_json),
            ]
        )
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
        cmd_render = [
            str(blender_bin),
            "-b",
            str(tmp_scene),
            "--python",
            str(run_director_py),
            "--",
            "--director",
            str(director_json_out),
            "--out",
            str(out_video),
        ]
        run_cmd(cmd_render)
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
        ]
        if background_image and background_image.exists():
            cmd_mux += ["--background", str(background_image)]
        run_cmd(cmd_mux)
    else:
        print("[skip] Mux step per config.")

    # 8) Cleanup
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


