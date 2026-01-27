#!/usr/bin/env python3
"""
GPU-side step: build director_visemes.json from a manifest CSV + audio in S3.

This downloads:
  - manifest CSV (with audio column as s3://... URIs)
  - generator_inputs.json
  - script.txt (optional; used for [PAUSE]/[SHOW MEDIATOR] parsing)

Then runs whisperx_to_director_visemes.py to produce director_visemes.json and uploads it back to S3.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import boto3


def is_s3(uri: str) -> bool:
    return isinstance(uri, str) and uri.startswith("s3://")


def s3_parse(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://")
    no = uri[5:]
    return no.split("/", 1)[0], no.split("/", 1)[1]


def s3_download(s3, uri: str, dest: Path) -> None:
    b, k = s3_parse(uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[s3] download {uri} -> {dest}")
    s3.download_file(b, k, str(dest))


def s3_upload(s3, src: Path, uri: str) -> None:
    b, k = s3_parse(uri)
    print(f"[s3] upload {src} -> {uri}")
    s3.upload_file(str(src), b, k)


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("[run]", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if res.returncode != 0:
        raise SystemExit(res.returncode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest_s3", required=True)
    ap.add_argument("--generator_inputs_s3", required=True)
    ap.add_argument("--script_s3", default="")
    ap.add_argument("--director_out_s3", required=True)
    ap.add_argument("--fps", type=int, default=24)
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    work = Path(tempfile.mkdtemp(prefix="vpg_gpu_director_"))

    manifest = work / "manifest.csv"
    gen_inputs = work / "generator_inputs.json"
    script_txt = work / "script.txt"
    out_director = work / "director_visemes.json"

    s3_download(s3, args.manifest_s3, manifest)
    s3_download(s3, args.generator_inputs_s3, gen_inputs)
    if args.script_s3:
        s3_download(s3, args.script_s3, script_txt)

    # Run whisperx builder
    project_root = Path(__file__).resolve().parents[1]
    whisper_py = project_root / "scripts" / "whisperx_to_director_visemes.py"
    cmd = [
        sys.executable,
        str(whisper_py),
        "--manifest_csv",
        str(manifest),
        "--generator_inputs_json",
        str(gen_inputs),
        "--fps",
        str(int(args.fps)),
        "--out",
        str(out_director),
        "--audio_cache_dir",
        str(work / "audio_cache"),
    ]
    if args.script_s3:
        cmd += ["--script_txt", str(script_txt)]
    run(cmd, cwd=project_root)

    s3_upload(s3, out_director, args.director_out_s3)
    print("[gpu_build_director] done.")


if __name__ == "__main__":
    main()

