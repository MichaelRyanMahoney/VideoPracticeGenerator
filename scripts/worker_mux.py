#!/usr/bin/env python3
import argparse
import os
import subprocess
from pathlib import Path

import boto3


def run_cmd(cmd: list[str]) -> None:
    print("[run]", " ".join(cmd))
    res = subprocess.run(cmd)
    if res.returncode != 0:
        raise SystemExit(res.returncode)


def s3_parse(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://")
    no = uri[5:]
    return no.split("/", 1)[0], no.split("/", 1)[1]


def s3_download_file(s3, uri: str, dst: Path):
    b, k = s3_parse(uri)
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"[s3] get {uri} -> {dst}")
    s3.download_file(b, k, str(dst))


def s3_sync_down(prefix_uri: str, local_dir: Path):
    # Use boto3 list_objects and download; simple alternative to aws s3 sync
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    b, k_prefix = s3_parse(prefix_uri)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=b, Prefix=k_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(k_prefix):].lstrip("/")
            dst = local_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            print(f"[s3] get s3://{b}/{key} -> {dst}")
            s3.download_file(b, key, str(dst))


def s3_upload_file(src: Path, uri: str):
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    b, k = s3_parse(uri)
    print(f"[s3] put {src} -> {uri}")
    s3.upload_file(str(src), b, k)


def main():
    ap = argparse.ArgumentParser(description="Mux worker: downloads frames and director from S3, muxes to MP4, uploads to S3.")
    ap.add_argument("--director_s3", required=True, help="s3://.../director_visemes.json")
    ap.add_argument("--frames_prefix_s3", required=True, help="s3://.../frames")  # contains %04d pngs
    ap.add_argument("--out_mp4_s3", required=True, help="s3://.../out/video.mp4")
    ap.add_argument("--background_s3", help="optional s3://.../background.png")
    args = ap.parse_args()

    work = Path("/tmp/mux_work")
    frames_dir = work / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    director_local = work / "director_visemes.json"
    if args.background_s3:
        background_local = work / "bg.png"
    else:
        background_local = None

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    s3_download_file(s3, args.director_s3, director_local)
    if background_local:
        s3_download_file(s3, args.background_s3, background_local)
    s3_sync_down(args.frames_prefix_s3, frames_dir)

    # Build frames pattern
    # Expect frames like <stem>_####.png; accept any %04d sequence by patterning on the first file's stem
    first = next(frames_dir.rglob("*.png"), None)
    if not first:
        raise SystemExit("No frames found to mux.")
    stem = first.stem.rsplit("_", 1)[0]
    pattern = frames_dir / f"{stem}_%04d.png"

    project_root = Path(__file__).resolve().parents[1]
    mux_py = project_root / "scripts" / "mux_from_director.py"
    cmd = [
        sys.executable, str(mux_py),
        "--director", str(director_local),
        "--frames", str(pattern),
        "--out", str(work / "out.mp4"),
    ]
    if background_local:
        cmd += ["--background", str(background_local)]
    run_cmd(cmd)

    s3_upload_file(work / "out.mp4", args.out_mp4_s3)
    print("[worker_mux] done.")


if __name__ == "__main__":
    import sys
    main()


