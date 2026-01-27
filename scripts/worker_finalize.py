#!/usr/bin/env python3
"""
CPU-side finalizer:
  - downloads director.json and frames from S3
  - muxes to MP4 (mux_from_director.py)
  - optionally applies overlays (apply_overlays.py) if overlay config provided
  - uploads final MP4 to S3
  - optionally emails via SES a presigned download URL
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import boto3


def s3_parse(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://")
    no = uri[5:]
    return no.split("/", 1)[0], no.split("/", 1)[1]


def s3_download_file(s3, uri: str, dst: Path) -> None:
    b, k = s3_parse(uri)
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"[s3] get {uri} -> {dst}")
    s3.download_file(b, k, str(dst))


def s3_sync_down_prefix(s3, prefix_uri: str, local_dir: Path) -> None:
    b, k_prefix = s3_parse(prefix_uri)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=b, Prefix=k_prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(k_prefix.rstrip("/") + "/") :]
            dst = local_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            print(f"[s3] get s3://{b}/{key} -> {dst}")
            s3.download_file(b, key, str(dst))


def s3_upload_file(s3, src: Path, uri: str) -> None:
    b, k = s3_parse(uri)
    print(f"[s3] put {src} -> {uri}")
    s3.upload_file(str(src), b, k)


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("[run]", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if res.returncode != 0:
        raise SystemExit(res.returncode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--director_s3", required=True)
    ap.add_argument("--frames_prefix_s3", required=True, help="s3://.../frames (contains PNGs)")
    ap.add_argument("--out_mp4_s3", required=True)
    ap.add_argument("--script_s3", default="", help="Optional script.txt for overlays timing")
    ap.add_argument("--overlay_config_s3", default="", help="Optional overlays config (json/yaml) for apply_overlays.py")
    ap.add_argument("--email_to", default="", help="Optional email recipient (SES).")
    ap.add_argument("--email_from", default="", help="Optional SES verified sender.")
    ap.add_argument("--presign_seconds", type=int, default=7 * 24 * 3600)
    args = ap.parse_args()

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    s3 = boto3.client("s3", region_name=region)
    ses = boto3.client("ses", region_name=region) if (args.email_to and args.email_from) else None

    work = Path(tempfile.mkdtemp(prefix="vpg_finalize_"))
    director_local = work / "director_visemes.json"
    frames_dir = work / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    script_local = work / "script.txt"
    overlay_cfg_local = work / "overlays.config"

    s3_download_file(s3, args.director_s3, director_local)
    s3_sync_down_prefix(s3, args.frames_prefix_s3, frames_dir)
    if args.script_s3:
        s3_download_file(s3, args.script_s3, script_local)
    if args.overlay_config_s3:
        s3_download_file(s3, args.overlay_config_s3, overlay_cfg_local)

    # Determine frame pattern stem (expects something_%04d.png)
    first = next(frames_dir.rglob("*.png"), None)
    if not first:
        raise SystemExit("No frames found to mux.")
    stem = first.stem.rsplit("_", 1)[0]
    pattern = frames_dir / f"{stem}_%04d.png"

    project_root = Path(__file__).resolve().parents[1]
    base_out = work / "base.mp4"

    # Mux (downloads S3 audio on demand inside mux_from_director.py)
    mux_py = project_root / "scripts" / "mux_from_director.py"
    run([
        sys.executable,
        str(mux_py),
        "--director",
        str(director_local),
        "--frames",
        str(pattern),
        "--out",
        str(base_out),
    ], cwd=project_root)

    final_out = base_out
    if args.overlay_config_s3 and args.script_s3:
        # Apply overlays using config (expects script+director+base+overlay_image+out inside config)
        apply_py = project_root / "scripts" / "apply_overlays.py"
        out2 = work / "final.mp4"
        run([
            sys.executable,
            str(apply_py),
            "--config",
            str(overlay_cfg_local),
        ], cwd=project_root)
        # apply_overlays writes to cfg.out; if you want strict behavior, define cfg.out to be out2.
        # For now, if cfg.out exists, use it; else fallback.
        if out2.exists():
            final_out = out2

    # Upload final MP4
    s3_upload_file(s3, final_out, args.out_mp4_s3)

    # Presign
    b, k = s3_parse(args.out_mp4_s3)
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": b, "Key": k},
        ExpiresIn=int(args.presign_seconds),
    )
    print("[out] presigned_url:", url)

    # Email (optional)
    if ses:
        subj = "MediatorSPARK Video Ready"
        body = f"Your video is ready.\n\nDownload link (expires in {int(args.presign_seconds)}s):\n{url}\n"
        print(f"[ses] send {args.email_from} -> {args.email_to}")
        ses.send_email(
            Source=args.email_from,
            Destination={"ToAddresses": [args.email_to]},
            Message={
                "Subject": {"Data": subj},
                "Body": {"Text": {"Data": body}},
            },
        )

    print("[worker_finalize] done.")


if __name__ == "__main__":
    main()

