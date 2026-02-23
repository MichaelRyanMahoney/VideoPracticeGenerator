#!/usr/bin/env python3
"""
Minimal debug helper: download director + generator_inputs + prepared_scene from S3 and
render a single frame (or small range) to a local PNG sequence.

This is meant for quick sanity checks on the GPU box/container without running the whole workflow.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import boto3

from workdir_utils import cleanup_work_dir, make_work_dir, should_keep_workdir


def s3_parse(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://")
    no = uri[5:]
    return no.split("/", 1)[0], no.split("/", 1)[1]


def s3_download(s3, uri: str, dest: Path) -> None:
    b, k = s3_parse(uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[s3] download {uri} -> {dest}")
    s3.download_file(b, k, str(dest))

def s3_upload_file(s3, src: Path, uri: str) -> None:
    b, k = s3_parse(uri)
    print(f"[s3] upload {src} -> {uri}")
    s3.upload_file(str(src), b, k)


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("[run]", " ".join(cmd), flush=True)
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
    if res.returncode != 0:
        raise SystemExit(res.returncode)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--director_s3", required=True, help="s3://.../director_visemes.json")
    ap.add_argument("--generator_inputs_s3", required=True, help="s3://.../generator_inputs.json")
    ap.add_argument("--scene_s3", required=True, help="s3://.../prepared_scene.blend")
    ap.add_argument("--frame", type=int, default=1, help="Single frame to render (default: 1)")
    ap.add_argument("--frame_end", type=int, default=0, help="Optional end frame (if 0, uses --frame)")
    ap.add_argument("--engine", choices=["eevee", "workbench", "cycles"], default="cycles")
    ap.add_argument("--no_audio", action="store_true", default=True, help="Render without audio/VSE (default: true).")
    ap.add_argument("--transparent", action="store_true", default=False, help="Force Film Transparent + RGBA PNG.")
    ap.add_argument("--opaque", action="store_true", default=False, help="Force opaque background (debugging).")
    ap.add_argument("--out_dir", default="", help="Local output dir. Default: a temp work dir.")
    ap.add_argument(
        "--out_s3_prefix",
        default="",
        help="Optional S3 prefix to upload rendered PNG(s), e.g. s3://bucket/vpg/projects/Video-01/debug/renders",
    )
    args = ap.parse_args()

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    s3 = boto3.client("s3", region_name=region)

    work = make_work_dir("vpg_debug_render_") if not args.out_dir else Path(args.out_dir).expanduser().resolve()
    if not args.out_dir:
        print(f"[debug] workdir={work}")
    work.mkdir(parents=True, exist_ok=True)

    try:
        local_director = work / "director_visemes.json"
        local_gen = work / "generator_inputs.json"
        local_scene = work / "prepared_scene.blend"
        s3_download(s3, args.director_s3, local_director)
        s3_download(s3, args.generator_inputs_s3, local_gen)
        s3_download(s3, args.scene_s3, local_scene)

        # Ensure run_director_visemes reads the job-specific generator_inputs
        env = os.environ.copy()
        env["VPG_GENERATOR_INPUTS_JSON"] = str(local_gen)
        # Important: sequencer rendering can cause black frames if no VSE video strips exist.
        env["VPG_USE_SEQUENCER"] = "0"

        project_root = Path(__file__).resolve().parents[1]
        blender_bin = env.get("VPG_BLENDER_BIN") or "/usr/local/bin/blender"

        frame_start = int(args.frame)
        frame_end = int(args.frame_end or 0) or frame_start

        out_mp4 = work / "debug.mp4"
        cmd = [
            str(blender_bin),
            "-b",
            str(local_scene),
            "--python-exit-code",
            "1",
            "--python",
            str(project_root / "scripts" / "run_director_visemes.py"),
            "--",
            "--director",
            str(local_director),
            "--out",
            str(out_mp4),
            "--frame_start",
            str(frame_start),
            "--frame_end",
            str(frame_end),
            "--no_clean_frames",
            "--frames",
            "--engine",
            str(args.engine),
        ]
        if args.no_audio:
            cmd.append("--no_audio")
        if args.opaque:
            cmd.append("--opaque")
        elif args.transparent:
            cmd.append("--transparent")

        run(cmd, cwd=project_root, env=env)

        frames_dir = work / f"{out_mp4.stem}_frames"
        print(f"[out] frames_dir={frames_dir}")
        # Show a small listing hint
        try:
            any_png = next(frames_dir.glob("*.png"), None)
            if any_png:
                print(f"[out] sample_png={any_png}")
        except Exception:
            pass

        if args.out_s3_prefix:
            prefix = args.out_s3_prefix.rstrip("/")
            pngs = sorted(frames_dir.glob("*.png"))
            if not pngs:
                raise SystemExit(f"[error] no PNGs found to upload in {frames_dir}")
            for p in pngs:
                dst = f"{prefix}/{p.name}"
                s3_upload_file(s3, p, dst)
            print(f"[out] uploaded_png_count={len(pngs)} prefix={prefix}")
        print("[debug_render_one_frame] done.")
    finally:
        if (not args.out_dir) and (not should_keep_workdir()):
            cleanup_work_dir(work)


if __name__ == "__main__":
    main()

