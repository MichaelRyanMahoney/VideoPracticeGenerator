import json
import os
import subprocess
import sys
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import requests


def _now_iso() -> str:
    import datetime

    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _is_s3(uri: str) -> bool:
    return isinstance(uri, str) and uri.startswith("s3://")


def _s3_parse(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://")
    no = uri[5:]
    return no.split("/", 1)[0], no.split("/", 1)[1]


def _s3_upload_file(s3, local_path: Path, s3_uri: str) -> None:
    b, k = _s3_parse(s3_uri)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    s3.upload_file(str(local_path), b, k)


def _s3_put_json(s3, s3_uri: str, payload: dict[str, Any]) -> None:
    b, k = _s3_parse(s3_uri)
    s3.put_object(Bucket=b, Key=k, Body=json.dumps(payload).encode("utf-8"), ContentType="application/json")


def _s3_get_json(s3, s3_uri: str) -> dict[str, Any]:
    b, k = _s3_parse(s3_uri)
    obj = s3.get_object(Bucket=b, Key=k)
    return json.loads(obj["Body"].read().decode("utf-8"))


def _s3_exists(s3, s3_uri: str) -> bool:
    b, k = _s3_parse(s3_uri)
    try:
        s3.head_object(Bucket=b, Key=k)
        return True
    except Exception:
        return False


def _s3_copy(s3, src_s3_uri: str, dst_s3_uri: str) -> None:
    sb, sk = _s3_parse(src_s3_uri)
    db, dk = _s3_parse(dst_s3_uri)
    if sb != db:
        raise RuntimeError(f"S3 copy across buckets not supported: {sb} -> {db}")
    s3.copy_object(Bucket=db, Key=dk, CopySource={"Bucket": sb, "Key": sk})


# Bump this whenever Blender scene prep logic/assets change in a way that should invalidate cached scenes.
SCENE_PREP_CACHE_VERSION = 2


def _scene_cache_key(project_root: Path, generator_inputs_json: Path) -> str:
    """
    Cache key for prepared scene. Intentionally only depends on character-related inputs + a version
    so we can reuse prepared scenes across jobs unless characters (or scene-prep version) change.
    """
    gi = json.loads(generator_inputs_json.read_text(encoding="utf-8"))
    cfg_path = project_root / "run_full_video_creation_sequence.config.json"
    repo_cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    payload = {
        "v": int(SCENE_PREP_CACHE_VERSION),
        "base_scene_blend": repo_cfg.get("base_scene_blend") or "scenes/base_scene.blend",
        "default_character_blend": repo_cfg.get("default_character_blend") or "assets/DefaultCharacter.blend",
        "characters": gi.get("characters") or {},
        "blender_mapping": gi.get("blender_mapping") or {},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
    if res.returncode != 0:
        raise RuntimeError(f"Command failed rc={res.returncode}: {' '.join(cmd)}")


@dataclass
class AwsJobConfig:
    region: str
    s3_bucket: str
    s3_prefix: str
    batch_job_queue: str
    batch_job_def_gpu_director: str
    batch_job_def_gpu_render: str
    batch_job_def_gpu_prepare_scene: str
    render_shards: int = 8
    batch_compute_env: str = ""  # optional (for warm/off)
    email_to: str = ""
    email_from: str = ""


def load_aws_config() -> AwsJobConfig:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    bucket = (os.environ.get("VPG_S3_BUCKET") or "").strip()
    if not bucket:
        raise RuntimeError("Missing VPG_S3_BUCKET")
    prefix = (os.environ.get("VPG_S3_PREFIX") or "vpg").strip().strip("/")
    # If we're using a dedicated GPU executor EC2 service (Option A) OR running GPU steps locally,
    # AWS Batch is not required.
    gpu_exec_url = (os.environ.get("VPG_GPU_EXECUTOR_URL") or "").strip()
    run_gpu_locally = (os.environ.get("VPG_RUN_GPU_LOCALLY") or "").strip() == "1"
    queue = (os.environ.get("VPG_BATCH_JOB_QUEUE_GPU") or "").strip()
    jd_director = (os.environ.get("VPG_BATCH_JOB_DEF_GPU_DIRECTOR") or "").strip()
    jd_render = (os.environ.get("VPG_BATCH_JOB_DEF_GPU_RENDER") or "").strip()
    jd_prepare = (os.environ.get("VPG_BATCH_JOB_DEF_GPU_PREPARE_SCENE") or "").strip() or jd_render
    if not gpu_exec_url and not run_gpu_locally:
        if not queue:
            raise RuntimeError("Missing VPG_BATCH_JOB_QUEUE_GPU")
    if not jd_director or not jd_render:
        raise RuntimeError("Missing VPG_BATCH_JOB_DEF_GPU_DIRECTOR or VPG_BATCH_JOB_DEF_GPU_RENDER")
    shards = int(os.environ.get("VPG_RENDER_SHARDS") or "8")
    return AwsJobConfig(
        region=region,
        s3_bucket=bucket,
        s3_prefix=prefix,
        batch_job_queue=queue,
        batch_job_def_gpu_director=jd_director,
        batch_job_def_gpu_render=jd_render,
        batch_job_def_gpu_prepare_scene=jd_prepare,
        render_shards=max(1, shards),
        batch_compute_env=(os.environ.get("VPG_BATCH_COMPUTE_ENV") or "").strip(),
        email_to=(os.environ.get("VPG_EMAIL_TO") or "").strip(),
        email_from=(os.environ.get("VPG_EMAIL_FROM") or "").strip(),
    )


def s3_uri(cfg: AwsJobConfig, key: str) -> str:
    return f"s3://{cfg.s3_bucket}/{cfg.s3_prefix.strip('/')}/{key.lstrip('/')}"


def job_paths(cfg: AwsJobConfig, project_id: str, job_id: str) -> dict[str, str]:
    base = f"projects/{project_id}/jobs/{job_id}"
    return {
        "status": s3_uri(cfg, f"{base}/status.json"),
        "script": s3_uri(cfg, f"{base}/inputs/script.txt"),
        "generator_inputs": s3_uri(cfg, f"{base}/inputs/generator_inputs.json"),
        "manifest": s3_uri(cfg, f"{base}/manifests/lines.csv"),
        "director": s3_uri(cfg, f"{base}/director/director_visemes.json"),
        "prepared_scene": s3_uri(cfg, f"{base}/scene/prepared_scene.blend"),
        "frames_prefix": s3_uri(cfg, f"{base}/frames"),
        "out_mp4": s3_uri(cfg, f"{base}/out/video.mp4"),
        "audio_prefix": s3_uri(cfg, f"projects/{project_id}/audio"),
    }


def write_status(s3, status_uri: str, job_id: str, state: str, extra: dict[str, Any] | None = None) -> None:
    """
    Write job status JSON to S3, merging with any existing status payload so we don't
    lose fields like batch job IDs when status transitions.
    """
    payload: dict[str, Any] = {}
    try:
        payload = _s3_get_json(s3, status_uri)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    payload.setdefault("jobId", job_id)
    payload["status"] = state
    payload["updatedAt"] = _now_iso()
    if extra:
        payload.update(extra)
    _s3_put_json(s3, status_uri, payload)


def submit_full_job(project_root: Path, project_id: str, job_id: str, script_txt: Path, generator_inputs_json: Path) -> dict[str, Any]:
    """
    CPU-side orchestrator (runs on the CPU EC2 instance):
      - uploads inputs to S3
      - builds manifest with audio hashes and S3 audio URIs
      - generates missing audio via Typecast and uploads to S3
      - submits GPU director job + GPU render array job
    """
    cfg = load_aws_config()
    s3 = boto3.client("s3", region_name=cfg.region)
    gpu_exec_url = (os.environ.get("VPG_GPU_EXECUTOR_URL") or "").strip().rstrip("/")
    gpu_exec_token = (os.environ.get("VPG_GPU_EXECUTOR_TOKEN") or "").strip()
    run_gpu_locally = (os.environ.get("VPG_RUN_GPU_LOCALLY") or "").strip() == "1"
    batch = boto3.client("batch", region_name=cfg.region) if (not gpu_exec_url and not run_gpu_locally) else None

    paths = job_paths(cfg, project_id, job_id)
    write_status(s3, paths["status"], job_id, "queued", {"projectId": project_id})

    # Upload inputs
    write_status(s3, paths["status"], job_id, "uploading_inputs")
    _s3_upload_file(s3, script_txt, paths["script"])
    _s3_upload_file(s3, generator_inputs_json, paths["generator_inputs"])

    # Build manifest locally (audio points at S3, includes audio_hash)
    write_status(s3, paths["status"], job_id, "building_manifest")
    tmp = Path(os.environ.get("VPG_DATA_DIR", "./data")).resolve() / "jobs" / job_id / "_aws_work"
    tmp.mkdir(parents=True, exist_ok=True)
    manifest_local = tmp / "lines.csv"
    env = os.environ.copy()
    env["VPG_GENERATOR_INPUTS_JSON"] = str(generator_inputs_json.resolve())
    parse_py = project_root / "scripts" / "parse_screenplay_to_manifest.py"
    _run(
        [
            sys.executable,
            str(parse_py),
            "--in_txt",
            str(script_txt.resolve()),
            "--out_csv",
            str(manifest_local),
            "--project_id",
            project_id,
            "--audio_s3_prefix",
            paths["audio_prefix"],
        ],
        cwd=project_root,
        env=env,
    )
    _s3_upload_file(s3, manifest_local, paths["manifest"])

    # Generate any missing audio (uploads to S3 as needed)
    write_status(s3, paths["status"], job_id, "tts")
    tts_py = project_root / "scripts" / "tts_typecast_from_manifest.py"
    _run(
        [
            sys.executable,
            str(tts_py),
            "--manifest_csv",
            str(manifest_local),
            "--generator_inputs_json",
            str(generator_inputs_json.resolve()),
        ],
        cwd=project_root,
        env=os.environ.copy(),
    )

    # Scene cache: reuse prepared scenes across jobs when character inputs haven't changed.
    scene_cache_key = _scene_cache_key(project_root, generator_inputs_json.resolve())
    scene_cache_uri = s3_uri(cfg, f"projects/{project_id}/scene_cache/{scene_cache_key}/prepared_scene.blend")
    scene_cache_hit = _s3_exists(s3, scene_cache_uri)
    if scene_cache_hit:
        # Keep job outputs isolated: copy cached scene into this job's scene location.
        _s3_copy(s3, scene_cache_uri, paths["prepared_scene"])
        write_status(
            s3,
            paths["status"],
            job_id,
            "scene_cache_hit",
            {"sceneCacheKey": scene_cache_key, "sceneCacheUri": scene_cache_uri, "sceneCacheHit": True},
        )

    # ---- Single-node mode: run GPU steps locally on this machine (no Batch, no GPU executor HTTP) ----
    # IMPORTANT: this must happen BEFORE any Batch submission logic below.
    if run_gpu_locally:
        env_gpu = os.environ.copy()
        env_gpu["AWS_REGION"] = cfg.region
        env_gpu.setdefault("AWS_DEFAULT_REGION", cfg.region)
        env_gpu.setdefault("VPG_BLENDER_BIN", "/usr/local/bin/blender")
        env_gpu.setdefault("VPG_XVFB", "1")

        # Ensure scene exists (cache hit means it was copied above)
        if not scene_cache_hit:
            write_status(s3, paths["status"], job_id, "gpu_scene_local")
            cmd = [
                sys.executable,
                str(project_root / "scripts" / "gpu_prepare_scene.py"),
                "--generator_inputs_s3",
                paths["generator_inputs"],
                "--prepared_scene_out_s3",
                paths["prepared_scene"],
                "--prepared_scene_cache_s3",
                scene_cache_uri,
            ]
            _run(cmd, cwd=project_root, env=env_gpu)

        write_status(s3, paths["status"], job_id, "gpu_director_local")
        cmd = [
            sys.executable,
            str(project_root / "scripts" / "gpu_build_director.py"),
            "--manifest_s3",
            paths["manifest"],
            "--generator_inputs_s3",
            paths["generator_inputs"],
            "--script_s3",
            paths["script"],
            "--director_out_s3",
            paths["director"],
        ]
        _run(cmd, cwd=project_root, env=env_gpu)

        write_status(s3, paths["status"], job_id, "gpu_render_local")
        # Use the existing "compute frame range from director timeline" logic, but as a single shard.
        env_gpu["AWS_BATCH_JOB_ARRAY_INDEX"] = "0"
        env_gpu["VPG_RENDER_SHARDS"] = "1"
        cmd = [
            sys.executable,
            str(project_root / "scripts" / "batch_render_array_entrypoint.py"),
            "--director_s3",
            paths["director"],
            "--generator_inputs_s3",
            paths["generator_inputs"],
            "--scene_s3",
            paths["prepared_scene"],
            "--frames_out_s3_prefix",
            paths["frames_prefix"],
            "--shards",
            "1",
        ]
        _run(cmd, cwd=project_root, env=env_gpu)

        write_status(
            s3,
            paths["status"],
            job_id,
            "completed_gpu",
            {"paths": paths, "sceneCacheKey": scene_cache_key, "sceneCacheUri": scene_cache_uri, "sceneCacheHit": bool(scene_cache_hit)},
        )
        return {"jobId": job_id, "projectId": project_id, "paths": paths, "mode": "single_node_gpu"}

    # Submit GPU director job (whisperx)
    write_status(s3, paths["status"], job_id, "submitting_gpu")
    director_job_id = ""
    if not gpu_exec_url:
    director_job = batch.submit_job(
        jobName=f"vpg-director-{project_id}-{job_id[:8]}",
        jobQueue=cfg.batch_job_queue,
        jobDefinition=cfg.batch_job_def_gpu_director,
        containerOverrides={
            "command": [
                "python",
                "scripts/gpu_build_director.py",
                "--manifest_s3",
                paths["manifest"],
                "--generator_inputs_s3",
                paths["generator_inputs"],
                "--script_s3",
                paths["script"],
                "--director_out_s3",
                paths["director"],
            ]
        },
    )
    director_job_id = director_job["jobId"]

    # Submit GPU scene preparation job (append characters + configure roles) in parallel with director.
    prepare_job_id: str | None = None
    if not scene_cache_hit and not gpu_exec_url:
    prepare_job = batch.submit_job(
        jobName=f"vpg-scene-{project_id}-{job_id[:8]}",
        jobQueue=cfg.batch_job_queue,
        jobDefinition=cfg.batch_job_def_gpu_prepare_scene,
        containerOverrides={
            "command": [
                "python",
                "scripts/gpu_prepare_scene.py",
                "--generator_inputs_s3",
                paths["generator_inputs"],
                "--prepared_scene_out_s3",
                paths["prepared_scene"],
                    "--prepared_scene_cache_s3",
                    scene_cache_uri,
            ]
        },
    )
    prepare_job_id = prepare_job["jobId"]

    if gpu_exec_url:
        # ---- EC2 GPU Executor mode (Option A) ----
        hdrs = {"Content-Type": "application/json"}
        if gpu_exec_token:
            hdrs["X-VPG-Token"] = gpu_exec_token

        # Ensure scene exists (cache hit means it was copied above)
        if not scene_cache_hit:
            write_status(s3, paths["status"], job_id, "gpu_scene")
            payload = {
                "aws_region": cfg.region,
                "generator_inputs_s3": paths["generator_inputs"],
                "prepared_scene_out_s3": paths["prepared_scene"],
                "prepared_scene_cache_s3": scene_cache_uri,
            }
            r = requests.post(f"{gpu_exec_url}/run/scene", json=payload, headers=hdrs, timeout=None)
            r.raise_for_status()

        write_status(s3, paths["status"], job_id, "gpu_director")
        payload = {
            "aws_region": cfg.region,
            "manifest_s3": paths["manifest"],
            "generator_inputs_s3": paths["generator_inputs"],
            "script_s3": paths["script"],
            "director_out_s3": paths["director"],
        }
        r = requests.post(f"{gpu_exec_url}/run/director", json=payload, headers=hdrs, timeout=None)
        r.raise_for_status()

        write_status(s3, paths["status"], job_id, "gpu_render")
        payload = {
            "aws_region": cfg.region,
            "director_s3": paths["director"],
            "generator_inputs_s3": paths["generator_inputs"],
            "scene_s3": paths["prepared_scene"],
            "frames_out_s3_prefix": paths["frames_prefix"],
            # Let the GPU side compute frame ranges
            "xvfb": 1,
        }
        r = requests.post(f"{gpu_exec_url}/run/render_auto", json=payload, headers=hdrs, timeout=None)
        r.raise_for_status()

        write_status(
            s3,
            paths["status"],
            job_id,
            "completed_gpu",
            {"paths": paths, "sceneCacheKey": scene_cache_key, "sceneCacheUri": scene_cache_uri, "sceneCacheHit": bool(scene_cache_hit)},
        )
        return {"jobId": job_id, "projectId": project_id, "paths": paths, "mode": "gpu_executor"}

    # ---- AWS Batch mode ----
    # Submit render array job that depends on BOTH director and prepared scene.
    depends = [{"jobId": director_job_id}]
    if prepare_job_id:
        depends.append({"jobId": prepare_job_id})
    render_submit = {
        "jobName": f"vpg-render-{project_id}-{job_id[:8]}",
        "jobQueue": cfg.batch_job_queue,
        "jobDefinition": cfg.batch_job_def_gpu_render,
        "dependsOn": depends,
        "containerOverrides": {
            "environment": [{"name": "VPG_RENDER_SHARDS", "value": str(int(cfg.render_shards))}],
            "command": [
                "python",
                "scripts/batch_render_array_entrypoint.py",
                "--director_s3",
                paths["director"],
                "--generator_inputs_s3",
                paths["generator_inputs"],
                "--scene_s3",
                paths["prepared_scene"],
                "--frames_out_s3_prefix",
                paths["frames_prefix"],
                "--shards",
                str(int(cfg.render_shards)),
            ],
        },
    }
    # AWS Batch does not allow array jobs of size 1.
    if int(cfg.render_shards) > 1:
        render_submit["arrayProperties"] = {"size": int(cfg.render_shards)}
    render_job = batch.submit_job(**render_submit)

    write_status(
        s3,
        paths["status"],
        job_id,
        "running_gpu",
        {
            "batchDirectorJobId": director_job_id,
            "batchPrepareSceneJobId": prepare_job_id or "",
            "batchRenderJobId": render_job["jobId"],
            "paths": paths,
            "sceneCacheKey": scene_cache_key,
            "sceneCacheUri": scene_cache_uri,
            "sceneCacheHit": bool(scene_cache_hit),
        },
    )

    result = {"jobId": job_id, "projectId": project_id, "paths": paths, "batch": {"director": director_job_id, "render": render_job["jobId"]}}

    # Optional: wait for completion and run CPU finalizer on this instance.
    if os.environ.get("VPG_RUN_FINALIZE", "0") == "1":
        write_status(s3, paths["status"], job_id, "waiting_for_render")
        _wait_for_batch_job(batch, render_job["jobId"], is_array=True)
        write_status(s3, paths["status"], job_id, "finalizing")
        _run_finalize(project_root, cfg, paths)
        write_status(s3, paths["status"], job_id, "completed", {"output": paths["out_mp4"]})

    return result


def _wait_for_batch_job(batch, job_id: str, is_array: bool = False, poll_sec: int = 15, timeout_sec: int = 6 * 3600) -> None:
    """
    Wait for a Batch job to complete. For array jobs, attempts to use statusSummary on the parent job.
    """
    t0 = time.time()
    while True:
        if time.time() - t0 > timeout_sec:
            raise RuntimeError(f"Timeout waiting for Batch job {job_id}")
        resp = batch.describe_jobs(jobs=[job_id])
        jobs = resp.get("jobs") or []
        if not jobs:
            raise RuntimeError(f"Batch job not found: {job_id}")
        j = jobs[0]
        st = j.get("status")
        if st in ("SUCCEEDED",):
            if is_array:
                ss = ((j.get("arrayProperties") or {}).get("statusSummary") or {})
                failed = int(ss.get("FAILED") or 0)
                runnable = int(ss.get("RUNNABLE") or 0)
                running = int(ss.get("RUNNING") or 0)
                starting = int(ss.get("STARTING") or 0)
                pending = runnable + running + starting
                if failed > 0:
                    raise RuntimeError(f"Array job {job_id} finished with failures: {ss}")
                # If parent says succeeded and no failures, consider done.
            return
        if st in ("FAILED",):
            reason = j.get("statusReason") or ""
            raise RuntimeError(f"Batch job failed: {job_id} reason={reason}")
        time.sleep(poll_sec)


def _run_finalize(project_root: Path, cfg: AwsJobConfig, paths: dict[str, str]) -> None:
    """
    Run CPU finalizer locally (mux/overlays/upload/email).
    """
    overlay_cfg_s3 = (os.environ.get("VPG_OVERLAY_CONFIG_S3") or "").strip()
    email_to = cfg.email_to
    email_from = cfg.email_from
    finalize_py = project_root / "scripts" / "worker_finalize.py"
    cmd = [
        sys.executable,
        str(finalize_py),
        "--director_s3",
        paths["director"],
        "--frames_prefix_s3",
        paths["frames_prefix"],
        "--out_mp4_s3",
        paths["out_mp4"],
    ]
    # Pass script for overlays alignment (optional)
    if paths.get("script"):
        cmd += ["--script_s3", paths["script"]]
    if overlay_cfg_s3:
        cmd += ["--overlay_config_s3", overlay_cfg_s3]
    if email_to and email_from:
        cmd += ["--email_to", email_to, "--email_from", email_from]
    _run(cmd, cwd=project_root, env=os.environ.copy())

