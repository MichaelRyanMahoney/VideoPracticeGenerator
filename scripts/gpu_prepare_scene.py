#!/usr/bin/env python3
"""
GPU-side step: prepare a render-ready .blend scene with role characters appended and configured.

It downloads generator_inputs.json from S3, then:
  1) Copies the base scene from the container (scenes/base_scene.blend) into a temp work_scene.blend
  2) Runs blender_generate_character_files.py to generate per-role blends and append them into the work scene
  3) Runs blender_configure_roles_for_render.py to apply selectors/colors and save
  4) Uploads the prepared scene .blend to S3 for render shards to use
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import boto3
import uuid

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


def s3_upload(s3, src: Path, uri: str) -> None:
    b, k = s3_parse(uri)
    print(f"[s3] upload {src} -> {uri}")
    s3.upload_file(str(src), b, k)


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("[run]", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
    if res.returncode != 0:
        raise SystemExit(res.returncode)


def blender_supports_flag(blender_bin: str, flag: str) -> bool:
    """
    Blender CLI flags can vary across builds. Some environments ship Blender builds
    where certain flags aren't recognized (and Blender interprets them as file paths).
    Detect support via `--help` once at runtime.
    """
    try:
        res = subprocess.run([blender_bin, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = res.stdout or ""
        return flag in out
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generator_inputs_s3", required=True)
    ap.add_argument("--prepared_scene_out_s3", required=True)
    ap.add_argument("--prepared_scene_cache_s3", default="", help="Optional cache destination (s3://...). If provided, upload prepared scene here too.")
    args = ap.parse_args()

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    s3 = boto3.client("s3", region_name=region)
    project_root = Path(__file__).resolve().parents[1]

    cfg_path = project_root / "run_full_video_creation_sequence.config.json"
    cfg = json.loads(cfg_path.read_text())
    blender_bin = (
        os.environ.get("VPG_BLENDER_BIN")
        or os.environ.get("VPG_BLENDER_BIN_PATH")
        or cfg.get("blender_binary")
        or "/usr/local/bin/blender"
    )
    base_scene_rel = cfg.get("base_scene_blend") or "scenes/base_scene.blend"
    base_scene_path = (project_root / base_scene_rel).resolve()
    if not base_scene_path.exists():
        raise SystemExit(f"Base scene not found in image: {base_scene_path}")

    # IMPORTANT: Blender saving can fail on some container/Batch-mounted filesystems even when
    # Python can write there (often due to the way Blender writes temp files + atomic renames).
    # To avoid that class of issues, do the heavy Blender IO on a local scratch filesystem.
    # You can override with VPG_PREPARE_SCENE_WORKDIR=/app/out/... if you explicitly want it.
    forced = (os.environ.get("VPG_PREPARE_SCENE_WORKDIR") or "").strip()
    work = None
    if forced:
        work = Path(forced).expanduser().resolve() / str(uuid.uuid4())
        work.mkdir(parents=True, exist_ok=True)
        print(f"[gpu_prepare_scene] workdir (forced) = {work}")
    else:
        work = make_work_dir("vpg_prepare_scene_")
        print(f"[gpu_prepare_scene] workdir (tmp) = {work}")

    # Force Blender to use a clean config + a tempdir that exists in this container.
    # This prevents "No such file or directory" failures caused by user prefs pointing
    # temp/backup paths at non-existent locations in Batch/ECS.
    blender_env = os.environ.copy()
    blender_env["TMPDIR"] = str(work)
    blender_env.setdefault("HOME", str(work))
    blender_env.setdefault("XDG_CACHE_HOME", str(work / ".cache"))
    blender_env.setdefault("XDG_CONFIG_HOME", str(work / ".config"))
    blender_env.setdefault("XDG_DATA_HOME", str(work / ".local" / "share"))
    (work / ".cache").mkdir(parents=True, exist_ok=True)
    (work / ".config").mkdir(parents=True, exist_ok=True)
    (work / ".local" / "share").mkdir(parents=True, exist_ok=True)
    blender_flags: list[str] = []
    # Keep factory-startup (helps avoid broken user prefs), but allow disabling if desired.
    if (os.environ.get("VPG_BLENDER_FACTORY_STARTUP") or "1").strip() == "1":
        blender_flags.append("--factory-startup")
    # Only pass --tempdir if the current Blender build actually supports it.
    if blender_supports_flag(str(blender_bin), "--tempdir"):
        blender_flags += ["--tempdir", str(work)]
    else:
        print("[gpu_prepare_scene] note: blender does not advertise --tempdir; relying on TMPDIR instead.")
    try:
        gen_inputs = work / "generator_inputs.json"
        s3_download(s3, args.generator_inputs_s3, gen_inputs)

        # Create a working scene
        work_scene = work / "work_scene.blend"
        work_scene.write_bytes(base_scene_path.read_bytes())

        # 1) Generate role blends + append into the work scene (positions included)
        default_char_blend = (project_root / (cfg.get("default_character_blend") or "assets/DefaultCharacter.blend")).resolve()
        if not default_char_blend.exists():
            raise SystemExit(f"Default character blend not found in image: {default_char_blend}")
        gen_script = project_root / "scripts" / "blender_generate_character_files.py"
        prepared_scene = work / "prepared_scene.blend"
        run([
            str(blender_bin),
            *blender_flags,
            "-b",
            str(default_char_blend),
            "--python",
            str(gen_script),
            "--",
            "--config",
            str(gen_inputs),
            "--source",
            str(default_char_blend),
            "--append-scene",
            str(work_scene),
            "--scene-save-as",
            str(prepared_scene),
            "--outdir",
            str(work / "role_blends"),
        ], cwd=project_root, env=blender_env)

        # 2) Configure roles/colors/selectors in the prepared scene
        cfg_script = project_root / "scripts" / "blender_configure_roles_for_render.py"
        run([
            str(blender_bin),
            *blender_flags,
            "-b",
            str(prepared_scene),
            "--python",
            str(cfg_script),
            "--",
            "--config",
            str(gen_inputs),
            # Ensure World/HDRI is applied even when generator_inputs.json does not specify it.
            # blender_configure_roles_for_render.py supports reading HDRI settings from an external
            # config file (our repo-level run_full_video_creation_sequence.config.json).
            "--hdri_from_config",
            str(cfg_path),
            "--save",
        ], cwd=project_root, env=blender_env)

        # 3) Verify role collections exist (fail fast if not)
        verify_py = project_root / "scripts" / "blender_verify_roles_in_scene.py"
        run([
            str(blender_bin),
            *blender_flags,
            "-b",
            str(prepared_scene),
            "--python",
            str(verify_py),
            "--",
            "--roles",
            "Disputant1",
            "MediatorA",
            "MediatorB",
            "Disputant2",
        ], cwd=project_root, env=blender_env)

        s3_upload(s3, prepared_scene, args.prepared_scene_out_s3)
        if args.prepared_scene_cache_s3:
            s3_upload(s3, prepared_scene, args.prepared_scene_cache_s3)
        print("[gpu_prepare_scene] done.")
    finally:
        # If you forced the workdir, assume you might want to inspect it unless you explicitly unset KEEP.
        if work and (not should_keep_workdir()) and (not forced):
            cleanup_work_dir(work)


if __name__ == "__main__":
    main()

