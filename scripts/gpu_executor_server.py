#!/usr/bin/env python3
"""
GPU Executor Service (Option A)

Runs on the GPU EC2 instance (inside the GPU image). Exposes a small HTTP API that:
  - runs scene prep (gpu_prepare_scene.py)
  - runs director build (gpu_build_director.py)
  - runs render (worker_render.py)

All inputs/outputs are S3 URIs so the CPU instance can trigger work remotely.

Security model (recommended):
  - Restrict inbound to this service via Security Group (CPU SG -> GPU SG on port).
  - Optionally require a shared token via VPG_GPU_EXECUTOR_TOKEN.
"""

import os
import subprocess
from pathlib import Path

from flask import Flask, jsonify, request


app = Flask(__name__)


def _require_token() -> None:
    token = (os.environ.get("VPG_GPU_EXECUTOR_TOKEN") or "").strip()
    if not token:
        return
    got = (request.headers.get("X-VPG-Token") or "").strip()
    if got != token:
        # Don't leak details
        raise PermissionError("unauthorized")


def _run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    print("[gpu-exec][run]", " ".join(cmd), flush=True)
    res = subprocess.run(cmd, env=env)
    if res.returncode != 0:
        raise RuntimeError(f"command failed rc={res.returncode}: {' '.join(cmd)}")


def _env_with_region(body: dict) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("AWS_REGION", body.get("aws_region") or env.get("AWS_REGION") or "us-east-1")
    return env


def _scene(body: dict) -> None:
    env = _env_with_region(body)
    env.setdefault("VPG_BLENDER_BIN", "/usr/local/bin/blender")
    cmd = [
        "python",
        "scripts/gpu_prepare_scene.py",
        "--generator_inputs_s3",
        body["generator_inputs_s3"],
        "--prepared_scene_out_s3",
        body["prepared_scene_out_s3"],
    ]
    prepared_scene_cache_s3 = (body.get("prepared_scene_cache_s3") or "").strip()
    if prepared_scene_cache_s3:
        cmd += ["--prepared_scene_cache_s3", prepared_scene_cache_s3]
    _run(cmd, env=env)


def _director(body: dict) -> None:
    env = _env_with_region(body)
    cmd = [
        "python",
        "scripts/gpu_build_director.py",
        "--manifest_s3",
        body["manifest_s3"],
        "--generator_inputs_s3",
        body["generator_inputs_s3"],
        "--script_s3",
        body["script_s3"],
        "--director_out_s3",
        body["director_out_s3"],
    ]
    _run(cmd, env=env)


def _render(body: dict) -> None:
    env = _env_with_region(body)
    env.setdefault("VPG_BLENDER_BIN", "/usr/local/bin/blender")
    env.setdefault("VPG_XVFB", str(int(body.get("xvfb", 1))))

    cmd = [
        "python",
        "scripts/worker_render.py",
        "--director_s3",
        body["director_s3"],
        "--generator_inputs_s3",
        body["generator_inputs_s3"],
        "--frames_out_s3_prefix",
        body["frames_out_s3_prefix"],
        "--frame_start",
        str(int(body["frame_start"])),
        "--frame_end",
        str(int(body["frame_end"])),
    ]
    scene_s3 = (body.get("scene_s3") or "").strip()
    if scene_s3:
        cmd += ["--scene_s3", scene_s3]
    if bool(body.get("transparent") or False):
        cmd.append("--transparent")
    _run(cmd, env=env)


def _render_auto(body: dict) -> None:
    """
    Render with automatic frame range computation (uses batch_render_array_entrypoint.py logic)
    but runs as a single shard on this single GPU machine.
    """
    env = _env_with_region(body)
    env.setdefault("VPG_BLENDER_BIN", "/usr/local/bin/blender")
    env.setdefault("VPG_XVFB", str(int(body.get("xvfb", 1))))
    # Pretend to be shard 0 of 1
    env["AWS_BATCH_JOB_ARRAY_INDEX"] = "0"
    env["VPG_RENDER_SHARDS"] = "1"

    cmd = [
        "python",
        "scripts/batch_render_array_entrypoint.py",
        "--director_s3",
        body["director_s3"],
        "--generator_inputs_s3",
        body["generator_inputs_s3"],
        "--frames_out_s3_prefix",
        body["frames_out_s3_prefix"],
        "--shards",
        "1",
    ]
    scene_s3 = (body.get("scene_s3") or "").strip()
    if scene_s3:
        cmd += ["--scene_s3", scene_s3]
    # Optional fps override
    if int(body.get("fps") or 0) > 0:
        cmd += ["--fps", str(int(body["fps"]))]
    _run(cmd, env=env)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/run/scene")
def run_scene():
    _require_token()
    body = request.get_json(force=True, silent=False) or {}
    _scene(body)
    return jsonify(status="ok")


@app.post("/run/director")
def run_director():
    _require_token()
    body = request.get_json(force=True, silent=False) or {}
    _director(body)
    return jsonify(status="ok")


@app.post("/run/render")
def run_render():
    _require_token()
    body = request.get_json(force=True, silent=False) or {}
    _render(body)
    return jsonify(status="ok")


@app.post("/run/render_auto")
def run_render_auto():
    _require_token()
    body = request.get_json(force=True, silent=False) or {}
    _render_auto(body)
    return jsonify(status="ok")


@app.post("/run/all")
def run_all():
    """
    Convenience endpoint: run scene + director + render sequentially.
    """
    _require_token()
    body = request.get_json(force=True, silent=False) or {}
    # Caller may omit director_s3/scene_s3 and rely on the out paths.
    body.setdefault("director_s3", body.get("director_out_s3"))
    body.setdefault("scene_s3", body.get("prepared_scene_out_s3"))
    _scene(body)
    _director(body)
    _render(body)
    return jsonify(status="ok")


@app.post("/run/all_auto")
def run_all_auto():
    """
    Convenience endpoint: run scene + director + render (auto frame range) sequentially.
    """
    _require_token()
    body = request.get_json(force=True, silent=False) or {}
    body.setdefault("director_s3", body.get("director_out_s3"))
    body.setdefault("scene_s3", body.get("prepared_scene_out_s3"))
    _scene(body)
    _director(body)
    _render_auto(body)
    return jsonify(status="ok")


def main():
    host = os.environ.get("VPG_GPU_EXECUTOR_HOST") or "0.0.0.0"
    port = int(os.environ.get("VPG_GPU_EXECUTOR_PORT") or "9000")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()

