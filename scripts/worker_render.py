#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
import os

import boto3


def run_cmd(cmd: list[str], cwd: Path | None = None) -> None:
    print("[run]", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if res.returncode != 0:
        raise SystemExit(res.returncode)


def s3_parse_uri(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://"), f"Not an s3 uri: {uri}"
    no = uri[5:]
    bucket, key = no.split("/", 1)
    return bucket, key


def s3_download(s3, uri: str, dest: Path) -> None:
    b, k = s3_parse_uri(uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[s3] download {uri} -> {dest}")
    s3.download_file(b, k, str(dest))


def s3_upload_dir(s3, src_dir: Path, s3_prefix: str, include_suffix: str = ".png") -> None:
    b, k_prefix = s3_parse_uri(s3_prefix)
    for f in sorted(src_dir.rglob(f"*{include_suffix}")):
        rel = f.relative_to(src_dir)
        key = f"{k_prefix.rstrip('/')}/{rel.as_posix()}"
        print(f"[s3] upload {f} -> s3://{b}/{key}")
        s3.upload_file(str(f), b, key)


def main():
    ap = argparse.ArgumentParser(description="Render worker: downloads inputs from S3, configures scene, renders a frame range, uploads frames to S3.")
    ap.add_argument("--director_s3", required=True, help="s3://.../director_visemes.json")
    ap.add_argument("--generator_inputs_s3", required=True, help="s3://.../manifests/generator_inputs.json")
    ap.add_argument("--frames_out_s3_prefix", required=True, help="s3://bucket/jobs/<id>/frames")
    ap.add_argument("--frame_start", type=int, required=True)
    ap.add_argument("--frame_end", type=int, required=True)
    ap.add_argument("--transparent", action="store_true", help="Render PNG RGBA frames")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))

    project_root = Path(__file__).resolve().parents[1]
    work = project_root / "out" / "_batch_work"
    work.mkdir(parents=True, exist_ok=True)
    local_director = work / "director_visemes.json"
    local_gen_inputs = work / "generator_inputs.json"
    s3_download(s3, args.director_s3, local_director)
    s3_download(s3, args.generator_inputs_s3, local_gen_inputs)

    # Prepare temp scene by copying base scene and configuring roles
    cfg_path = project_root / "run_full_video_creation_sequence.config.json"
    cfg = json.loads(cfg_path.read_text())
    blender_bin = cfg.get("blender_binary") or "/usr/local/blender/blender"
    base_scene = cfg.get("base_scene_blend") or "scenes/base_scene.blend"
    base_scene_path = (project_root / base_scene).resolve()
    if not base_scene_path.exists():
        raise SystemExit(f"Base scene not found: {base_scene_path}")

    ts_scene = work / "work_scene.blend"
    ts_scene.write_bytes(base_scene_path.read_bytes())

    cfg_script = project_root / "scripts" / "blender_configure_roles_for_render.py"
    pre = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24"] if os.environ.get("VPG_XVFB") == "1" else []
    run_cmd(pre + [
        str(blender_bin), "-b", str(ts_scene),
        "--python", str(cfg_script),
        "--", "--config", str(local_gen_inputs), "--save"
    ])

    # Render the requested range with transparent frames
    out_video = project_root / "out" / "batch_render.mp4"
    run_director_py = project_root / "scripts" / "run_director_visemes.py"
    render_cmd = pre + [
        str(blender_bin), "-b", str(ts_scene),
        "--python", str(run_director_py),
        "--", "--director", str(local_director),
        "--out", str(out_video),
        "--frame_start", str(int(args.frame_start)),
        "--frame_end", str(int(args.frame_end)),
        "--no_audio",
        "--no_clean_frames",
    ]
    if args.transparent:
        render_cmd.append("--transparent")
    run_cmd(render_cmd)

    # Upload frames back to S3
    frames_dir = out_video.parent / f"{out_video.stem}_frames"
    if not frames_dir.exists():
        print("[warn] frames directory missing; nothing to upload.")
    else:
        s3_upload_dir(s3, frames_dir, args.frames_out_s3_prefix)

    print("[worker_render] done.")


if __name__ == "__main__":
    main()


