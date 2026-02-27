#!/usr/bin/env python3
import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> None:
    print("[run]", " ".join(cmd))
    res = subprocess.run(cmd)
    if res.returncode != 0:
        raise SystemExit(res.returncode)


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]

    ap = argparse.ArgumentParser(
        description="Re-run mux + apply_overlays, optionally applying a patch PNG on a frame range."
    )
    ap.add_argument("--director", default=str(project_root / "director_visemes.json"))
    ap.add_argument(
        "--frames",
        default=str(project_root / "out" / "blender_render_frames" / "blender_render_%04d.png"),
        help="PNG sequence pattern (default: out/blender_render_frames/blender_render_%04d.png)",
    )
    ap.add_argument("--generator_inputs_json", default=str(project_root / "manifests" / "generator_inputs.json"))
    ap.add_argument("--background", default=str(project_root / "scenes" / "SceneBackground.png"))
    ap.add_argument("--fg_width_ratio", type=float, default=0.73)
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--audio_bitrate", default="192k")
    ap.add_argument("--mux_out", default=str(project_root / "out" / "blender_render.mp4"))

    ap.add_argument("--patch_image", default=str(project_root / "scenes" / "PatchOverlay.png"))
    ap.add_argument("--patch_frame_start", type=int, default=261)
    ap.add_argument("--patch_frame_end", type=int, default=1288)

    ap.add_argument("--skip_overlays", action="store_true", help="Only run mux (skip apply_overlays).")
    ap.add_argument("--overlay_config", default=str(project_root / "scripts" / "apply_overlays.config.json"))
    ap.add_argument("--overlay_out", default=str(project_root / "out" / "blender_render_with_overlays.mp4"))
    ap.add_argument("--overlay_fps", type=int, default=24)

    args = ap.parse_args()

    mux_py = project_root / "scripts" / "mux_from_director.py"
    apply_py = project_root / "scripts" / "apply_overlays.py"

    mux_cmd = [
        sys.executable,
        str(mux_py),
        "--director",
        str(Path(args.director)),
        "--frames",
        str(Path(args.frames)),
        "--out",
        str(Path(args.mux_out)),
        "--generator_inputs_json",
        str(Path(args.generator_inputs_json)),
        "--fg_width_ratio",
        str(float(args.fg_width_ratio)),
        "--crf",
        str(int(args.crf)),
        "--audio_bitrate",
        str(args.audio_bitrate),
    ]

    bg = str(args.background or "").strip()
    if bg:
        mux_cmd += ["--background", str(Path(bg))]

    patch_img = str(args.patch_image or "").strip()
    if patch_img and int(args.patch_frame_start) > 0 and int(args.patch_frame_end) > 0:
        mux_cmd += [
            "--patch_image",
            str(Path(patch_img)),
            "--patch_frame_start",
            str(int(args.patch_frame_start)),
            "--patch_frame_end",
            str(int(args.patch_frame_end)),
        ]

    run(mux_cmd)

    if args.skip_overlays:
        return

    apply_cmd = [
        sys.executable,
        str(apply_py),
        "--config",
        str(Path(args.overlay_config)),
        "--generator_inputs_json",
        str(Path(args.generator_inputs_json)),
        "--script",
        str(project_root / "script.txt"),
        "--director",
        str(Path(args.director)),
        "--base",
        str(Path(args.mux_out)),
        "--out",
        str(Path(args.overlay_out)),
        "--fps",
        str(int(args.overlay_fps)),
    ]
    run(apply_cmd)


if __name__ == "__main__":
    main()

