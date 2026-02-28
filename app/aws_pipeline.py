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

def _json_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()

def _render_cache_key(
    *,
    project_id: str,
    scene_cache_key: str,
    director_obj: dict[str, Any],
    generator_inputs_json: Path,
) -> str:
    """
    Cache key for rendered frames. Should change whenever output frames would change.

    Inputs included:
      - scene_cache_key (captures prepared scene inputs + version)
      - director content hash (captures timing/visemes/pauses/etc)
      - render-relevant settings from generator_inputs.json run.*
      - blender version (if provided via env)
      - an explicit cache version bump knob (VPG_RENDER_CACHE_VERSION)
    """
    try:
        gi = json.loads(generator_inputs_json.read_text(encoding="utf-8"))
    except Exception:
        gi = {}
    run_cfg = (gi.get("run") or {}) if isinstance(gi, dict) else {}
    payload = {
        "v": int(os.environ.get("VPG_RENDER_CACHE_VERSION") or "1"),
        "project_id": str(project_id),
        "scene_cache_key": str(scene_cache_key),
        "director_sha256": _json_hash(director_obj),
        "render_engine": str(run_cfg.get("render_engine") or ""),
        "quality": str(run_cfg.get("quality") or run_cfg.get("render_quality") or ""),
        "fps": int(run_cfg.get("fps") or 0) or int(director_obj.get("fps", 24) or 24),
        # The pipeline always renders PNG RGBA frames at the moment.
        "transparent": True,
        "blender_version": str(os.environ.get("BLENDER_VERSION") or ""),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def _tc_to_seconds(tc: str) -> float:
    try:
        h, m, s = (tc or "00:00:00.000").split(":")
        return float(h) * 3600 + float(m) * 60 + float(s)
    except Exception:
        return 0.0


def _estimate_end_seconds_from_director(director: dict[str, Any]) -> float:
    """
    Match scripts/batch_render_array_entrypoint.py logic: estimate end time from beats/visemes
    without needing audio.
    """
    beats = director.get("beats") or []
    end_s = 0.0
    for b in beats:
        t0 = _tc_to_seconds(b.get("tc_in") or "00:00:00.000")
        if (b.get("type") or "").lower() == "pause":
            try:
                dur = float(b.get("duration", 1.0))
            except Exception:
                dur = 1.0
            end_s = max(end_s, t0 + max(0.0, dur))
            continue
        vmax = 0.0
        for ev in (b.get("visemes") or []):
            try:
                vmax = max(vmax, float(ev.get("t", 0.0)))
            except Exception:
                pass
        end_s = max(end_s, max(vmax, t0) + 0.5)
    return float(end_s)


def _s3_head(s3, s3_uri: str) -> bool:
    b, k = _s3_parse(s3_uri)
    try:
        s3.head_object(Bucket=b, Key=k)
        return True
    except Exception:
        return False


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
    batch_job_def_director: str
    batch_job_def_render: str
    batch_job_def_prepare_scene: str
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
    # If we're using a dedicated executor service OR running compute steps locally,
    # AWS Batch is not required.
    gpu_exec_url = (os.environ.get("VPG_GPU_EXECUTOR_URL") or "").strip()
    run_compute_locally = (os.environ.get("VPG_RUN_COMPUTE_LOCALLY") or os.environ.get("VPG_RUN_GPU_LOCALLY") or "").strip() == "1"
    queue = (os.environ.get("VPG_BATCH_JOB_QUEUE_RENDER") or os.environ.get("VPG_BATCH_JOB_QUEUE_GPU") or "").strip()
    jd_director = (os.environ.get("VPG_BATCH_JOB_DEF_DIRECTOR") or os.environ.get("VPG_BATCH_JOB_DEF_GPU_DIRECTOR") or "").strip()
    jd_render = (os.environ.get("VPG_BATCH_JOB_DEF_RENDER") or os.environ.get("VPG_BATCH_JOB_DEF_GPU_RENDER") or "").strip()
    jd_prepare = (os.environ.get("VPG_BATCH_JOB_DEF_PREPARE_SCENE") or os.environ.get("VPG_BATCH_JOB_DEF_GPU_PREPARE_SCENE") or "").strip() or jd_render
    if not gpu_exec_url and not run_compute_locally:
        if not queue:
            raise RuntimeError("Missing VPG_BATCH_JOB_QUEUE_RENDER (or legacy VPG_BATCH_JOB_QUEUE_GPU)")
        if not jd_director or not jd_render:
            raise RuntimeError("Missing VPG_BATCH_JOB_DEF_DIRECTOR/VPG_BATCH_JOB_DEF_RENDER (or legacy GPU names)")
    shards = int(os.environ.get("VPG_RENDER_SHARDS") or "8")
    return AwsJobConfig(
        region=region,
        s3_bucket=bucket,
        s3_prefix=prefix,
        batch_job_queue=queue,
        batch_job_def_director=jd_director,
        batch_job_def_render=jd_render,
        batch_job_def_prepare_scene=jd_prepare,
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


def _load_local_run_config(project_root: Path) -> dict[str, Any]:
    """
    Load local run configuration so AWS uses the same knobs as local runs.

    We intentionally mirror `scripts/run_full_video_creation_sequence.py`'s config keys so
    TTS (smartprompt vs preset) behaves identically between local and AWS runs.
    """
    cfg_path = project_root / "run_full_video_creation_sequence.config.json"
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def submit_full_job(project_root: Path, project_id: str, job_id: str, script_txt: Path, generator_inputs_json: Path) -> dict[str, Any]:
    """
    CPU-side orchestrator (runs on the CPU EC2 instance):
      - uploads inputs to S3
      - builds manifest with audio hashes and S3 audio URIs
      - generates missing audio via Typecast and uploads to S3
      - submits director job + render array job
    """
    cfg = load_aws_config()
    s3 = boto3.client("s3", region_name=cfg.region)
    gpu_exec_url = (os.environ.get("VPG_GPU_EXECUTOR_URL") or "").strip().rstrip("/")
    gpu_exec_token = (os.environ.get("VPG_GPU_EXECUTOR_TOKEN") or "").strip()
    run_compute_locally = (os.environ.get("VPG_RUN_COMPUTE_LOCALLY") or os.environ.get("VPG_RUN_GPU_LOCALLY") or "").strip() == "1"
    batch = boto3.client("batch", region_name=cfg.region) if (not gpu_exec_url and not run_compute_locally) else None

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
    local_cfg = _load_local_run_config(project_root)
    use_smart_prompt_tts = bool(local_cfg.get("use_smart_prompt_tts", False))
    smart_prompt_context_char_limit = int(local_cfg.get("smart_prompt_context_char_limit", 2000))
    smart_prompt_include_speaker_labels = bool(local_cfg.get("smart_prompt_include_speaker_labels", True))

    tts_script_name = (
        "tts_typecast_smartprompt_from_manifest.py"
        if use_smart_prompt_tts
        else "tts_typecast_from_manifest.py"
    )
    tts_py = project_root / "scripts" / tts_script_name
    tts_cmd = [
        sys.executable,
        str(tts_py),
        "--manifest_csv",
        str(manifest_local),
        "--generator_inputs_json",
        str(generator_inputs_json.resolve()),
    ]
    if use_smart_prompt_tts:
        tts_cmd += [
            "--context_char_limit",
            str(smart_prompt_context_char_limit),
            "--include_speaker_labels",
            "true" if smart_prompt_include_speaker_labels else "false",
        ]
    _run(
        tts_cmd,
        cwd=project_root,
        env=os.environ.copy(),
    )

    # Scene cache: reuse prepared scenes across jobs when character inputs haven't changed.
    scene_cache_key = _scene_cache_key(project_root, generator_inputs_json.resolve())
    scene_cache_uri = s3_uri(cfg, f"projects/{project_id}/scene_cache/{scene_cache_key}/prepared_scene.blend")
    scene_cache_hit = _s3_exists(s3, scene_cache_uri)
    # Always render against a concrete prepared scene URI. Prefer the per-job path for isolation,
    # but fall back to the shared cache object if copy fails for any reason.
    scene_s3_for_render = paths["prepared_scene"]
    scene_copy_ok = False
    scene_copy_error = ""
    if scene_cache_hit:
        try:
            # Keep job outputs isolated: copy cached scene into this job's scene location.
            _s3_copy(s3, scene_cache_uri, paths["prepared_scene"])
            scene_copy_ok = _s3_exists(s3, paths["prepared_scene"])
        except Exception as ex:
            scene_copy_ok = False
            scene_copy_error = str(ex)
        if not scene_copy_ok:
            scene_s3_for_render = scene_cache_uri
        write_status(
            s3,
            paths["status"],
            job_id,
            "scene_cache_hit",
            {
                "sceneCacheKey": scene_cache_key,
                "sceneCacheUri": scene_cache_uri,
                "sceneCacheHit": True,
                "sceneCopyOk": bool(scene_copy_ok),
                "sceneCopyError": scene_copy_error,
                "sceneS3ForRender": scene_s3_for_render,
            },
        )

    # ---- Single-node mode: run GPU steps locally on this machine (no Batch, no GPU executor HTTP) ----
    # IMPORTANT: this must happen BEFORE any Batch submission logic below.
    if run_compute_locally:
        env_gpu = os.environ.copy()
        env_gpu["AWS_REGION"] = cfg.region
        env_gpu.setdefault("AWS_DEFAULT_REGION", cfg.region)
        env_gpu.setdefault("VPG_BLENDER_BIN", "/usr/local/bin/blender")
        env_gpu.setdefault("VPG_XVFB", "1")

        # Ensure scene exists (cache hit means we can render from cache or per-job copy)
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
        # Skip render if frames already exist for this job (prevents expensive accidental rerenders).
        # We consider a render complete if the last expected frame PNG exists in S3.
        enable_render_cache = (os.environ.get("VPG_ENABLE_RENDER_CACHE") or "1").strip() == "1"
        if (os.environ.get("VPG_SKIP_RENDER_IF_FRAMES_EXIST") or "1").strip() == "1":
            try:
                # Compute expected total_frames using the same logic as batch_render_array_entrypoint.
                director_obj = _s3_get_json(s3, paths["director"])
                try:
                    gi = json.loads(generator_inputs_json.read_text(encoding="utf-8"))
                    fps = int(((gi.get("run") or {}).get("fps")) or 0) or int(director_obj.get("fps", 24))
                except Exception:
                    fps = int(director_obj.get("fps", 24) or 24)
                end_s = _estimate_end_seconds_from_director(director_obj)
                total_frames = max(1, int(round(end_s * int(fps))) + 2)

                # Render cache: reuse frames across jobs when inputs/settings match.
                frames_prefix_used = paths["frames_prefix"]
                render_cache_key = ""
                render_cache_prefix = ""
                if enable_render_cache:
                    render_cache_key = _render_cache_key(
                        project_id=project_id,
                        scene_cache_key=scene_cache_key,
                        director_obj=director_obj,
                        generator_inputs_json=generator_inputs_json.resolve(),
                    )
                    render_cache_prefix = s3_uri(cfg, f"projects/{project_id}/render_cache/{render_cache_key}/frames")
                    cache_stem = f"batch_render_{render_cache_key[:8]}"
                    cache_last = f"{render_cache_prefix.rstrip('/')}/{cache_stem}_{int(total_frames):04d}.png"
                    if _s3_head(s3, cache_last):
                        frames_prefix_used = render_cache_prefix
                        write_status(
                            s3,
                            paths["status"],
                            job_id,
                            "render_cache_hit",
                            {"renderCacheKey": render_cache_key, "framesPrefix": frames_prefix_used, "total_frames": int(total_frames)},
                        )
                        if os.environ.get("VPG_RUN_FINALIZE", "0") == "1":
                            write_status(s3, paths["status"], job_id, "finalizing")
                            _run_finalize(project_root, cfg, paths, frames_prefix_s3=frames_prefix_used)
                            write_status(s3, paths["status"], job_id, "completed", {"output": paths["out_mp4"], "paths": paths, "framesPrefix": frames_prefix_used})
                            return {"jobId": job_id, "projectId": project_id, "paths": paths, "mode": "single_node_gpu", "output": paths["out_mp4"], "renderSkipped": True, "renderCacheHit": True, "framesPrefix": frames_prefix_used}
                        write_status(
                            s3,
                            paths["status"],
                            job_id,
                            "completed_gpu",
                            {"paths": paths, "sceneCacheKey": scene_cache_key, "sceneCacheUri": scene_cache_uri, "sceneCacheHit": bool(scene_cache_hit), "renderSkipped": True, "renderCacheHit": True, "framesPrefix": frames_prefix_used},
                        )
                        return {"jobId": job_id, "projectId": project_id, "paths": paths, "mode": "single_node_gpu", "renderSkipped": True, "renderCacheHit": True, "framesPrefix": frames_prefix_used}

                stem = f"batch_render_{job_id[:8]}"
                last_frame_key = f"{paths['frames_prefix'].rstrip('/')}/{stem}_{int(total_frames):04d}.png"
                if _s3_head(s3, last_frame_key):
                    write_status(s3, paths["status"], job_id, "render_skipped", {"reason": "frames_exist", "total_frames": int(total_frames)})
                    # Proceed to finalize if enabled
                    if os.environ.get("VPG_RUN_FINALIZE", "0") == "1":
                        write_status(s3, paths["status"], job_id, "finalizing")
                        _run_finalize(project_root, cfg, paths, frames_prefix_s3=paths["frames_prefix"])
                        write_status(s3, paths["status"], job_id, "completed", {"output": paths["out_mp4"], "paths": paths, "framesPrefix": paths["frames_prefix"]})
                        return {"jobId": job_id, "projectId": project_id, "paths": paths, "mode": "single_node_gpu", "output": paths["out_mp4"], "renderSkipped": True}
                    write_status(
                        s3,
                        paths["status"],
                        job_id,
                        "completed_gpu",
                        {"paths": paths, "sceneCacheKey": scene_cache_key, "sceneCacheUri": scene_cache_uri, "sceneCacheHit": bool(scene_cache_hit), "renderSkipped": True},
                    )
                    return {"jobId": job_id, "projectId": project_id, "paths": paths, "mode": "single_node_gpu", "renderSkipped": True}
            except Exception:
                # If detection fails, fall through to rendering.
                pass
        # Use the existing "compute frame range from director timeline" logic, but as a single shard.
        env_gpu["AWS_BATCH_JOB_ARRAY_INDEX"] = "0"
        env_gpu["VPG_RENDER_SHARDS"] = "1"
        frames_prefix_for_render = paths["frames_prefix"]
        if enable_render_cache:
            try:
                director_obj = _s3_get_json(s3, paths["director"])
                render_cache_key = _render_cache_key(
                    project_id=project_id,
                    scene_cache_key=scene_cache_key,
                    director_obj=director_obj,
                    generator_inputs_json=generator_inputs_json.resolve(),
                )
                frames_prefix_for_render = s3_uri(cfg, f"projects/{project_id}/render_cache/{render_cache_key}/frames")
            except Exception:
                frames_prefix_for_render = paths["frames_prefix"]
        cmd = [
            sys.executable,
            str(project_root / "scripts" / "batch_render_array_entrypoint.py"),
            "--director_s3",
            paths["director"],
            "--generator_inputs_s3",
            paths["generator_inputs"],
            "--scene_s3",
            scene_s3_for_render,
            "--frames_out_s3_prefix",
            frames_prefix_for_render,
            "--shards",
            "1",
        ]
        _run(cmd, cwd=project_root, env=env_gpu)

        # Optional: run finalize (mux/overlays/upload/email) locally after frames are uploaded.
        if os.environ.get("VPG_RUN_FINALIZE", "0") == "1":
            write_status(s3, paths["status"], job_id, "finalizing")
            _run_finalize(project_root, cfg, paths, frames_prefix_s3=frames_prefix_for_render)
            write_status(s3, paths["status"], job_id, "completed", {"output": paths["out_mp4"], "paths": paths, "framesPrefix": frames_prefix_for_render})
            return {"jobId": job_id, "projectId": project_id, "paths": paths, "mode": "single_node_gpu", "output": paths["out_mp4"], "framesPrefix": frames_prefix_for_render}

        write_status(
            s3,
            paths["status"],
            job_id,
            "completed_gpu",
            {"paths": paths, "sceneCacheKey": scene_cache_key, "sceneCacheUri": scene_cache_uri, "sceneCacheHit": bool(scene_cache_hit), "framesPrefix": frames_prefix_for_render},
        )
        return {"jobId": job_id, "projectId": project_id, "paths": paths, "mode": "single_node_gpu", "framesPrefix": frames_prefix_for_render}

    # ---- EC2 GPU Executor mode (Option A) ----
    if gpu_exec_url:
        write_status(s3, paths["status"], job_id, "submitting_gpu")
        hdrs = {"Content-Type": "application/json"}
        if gpu_exec_token:
            hdrs["X-VPG-Token"] = gpu_exec_token

        # Ensure scene exists (cache hit means we can render from cache or per-job copy)
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
        enable_render_cache = (os.environ.get("VPG_ENABLE_RENDER_CACHE") or "1").strip() == "1"
        frames_prefix_for_render = paths["frames_prefix"]
        if enable_render_cache:
            try:
                director_obj = _s3_get_json(s3, paths["director"])
                try:
                    gi = json.loads(generator_inputs_json.read_text(encoding="utf-8"))
                    fps = int(((gi.get("run") or {}).get("fps")) or 0) or int(director_obj.get("fps", 24))
                except Exception:
                    fps = int(director_obj.get("fps", 24) or 24)
                end_s = _estimate_end_seconds_from_director(director_obj)
                total_frames = max(1, int(round(end_s * int(fps))) + 2)
                render_cache_key = _render_cache_key(
                    project_id=project_id,
                    scene_cache_key=scene_cache_key,
                    director_obj=director_obj,
                    generator_inputs_json=generator_inputs_json.resolve(),
                )
                frames_prefix_for_render = s3_uri(cfg, f"projects/{project_id}/render_cache/{render_cache_key}/frames")
                cache_stem = f"batch_render_{render_cache_key[:8]}"
                cache_last = f"{frames_prefix_for_render.rstrip('/')}/{cache_stem}_{int(total_frames):04d}.png"
                if _s3_head(s3, cache_last):
                    write_status(
                        s3,
                        paths["status"],
                        job_id,
                        "render_cache_hit",
                        {"renderCacheKey": render_cache_key, "framesPrefix": frames_prefix_for_render, "total_frames": int(total_frames)},
                    )
                    write_status(
                        s3,
                        paths["status"],
                        job_id,
                        "completed_gpu",
                        {"paths": paths, "sceneCacheKey": scene_cache_key, "sceneCacheUri": scene_cache_uri, "sceneCacheHit": bool(scene_cache_hit), "renderCacheHit": True, "framesPrefix": frames_prefix_for_render},
                    )
                    return {"jobId": job_id, "projectId": project_id, "paths": paths, "mode": "gpu_executor", "renderCacheHit": True, "framesPrefix": frames_prefix_for_render}
            except Exception:
                frames_prefix_for_render = paths["frames_prefix"]
        payload = {
            "aws_region": cfg.region,
            "director_s3": paths["director"],
            "generator_inputs_s3": paths["generator_inputs"],
            "scene_s3": scene_s3_for_render,
            "frames_out_s3_prefix": frames_prefix_for_render,
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
            {"paths": paths, "sceneCacheKey": scene_cache_key, "sceneCacheUri": scene_cache_uri, "sceneCacheHit": bool(scene_cache_hit), "framesPrefix": frames_prefix_for_render},
        )
        return {"jobId": job_id, "projectId": project_id, "paths": paths, "mode": "gpu_executor", "framesPrefix": frames_prefix_for_render}

    # ---- AWS Batch mode ----
    # Submit GPU director job (whisperx)
    write_status(s3, paths["status"], job_id, "submitting_gpu")
    director_job = batch.submit_job(
        jobName=f"vpg-director-{project_id}-{job_id[:8]}",
        jobQueue=cfg.batch_job_queue,
        jobDefinition=cfg.batch_job_def_director,
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
    if not scene_cache_hit:
        prepare_job = batch.submit_job(
            jobName=f"vpg-scene-{project_id}-{job_id[:8]}",
            jobQueue=cfg.batch_job_queue,
            jobDefinition=cfg.batch_job_def_prepare_scene,
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
    # Submit render array job that depends on BOTH director and prepared scene.
    depends = [{"jobId": director_job_id}]
    if prepare_job_id:
        depends.append({"jobId": prepare_job_id})
    render_submit = {
        "jobName": f"vpg-render-{project_id}-{job_id[:8]}",
        "jobQueue": cfg.batch_job_queue,
        "jobDefinition": cfg.batch_job_def_render,
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
                scene_s3_for_render,
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
        _run_finalize(project_root, cfg, paths, frames_prefix_s3=paths["frames_prefix"])
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


def _run_finalize(project_root: Path, cfg: AwsJobConfig, paths: dict[str, str], frames_prefix_s3: str | None = None) -> None:
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
        (frames_prefix_s3 or paths["frames_prefix"]),
        "--out_mp4_s3",
        paths["out_mp4"],
    ]
    if paths.get("generator_inputs"):
        cmd += ["--generator_inputs_s3", paths["generator_inputs"]]
    # Pass script for overlays alignment (optional)
    if paths.get("script"):
        cmd += ["--script_s3", paths["script"]]
    if overlay_cfg_s3:
        cmd += ["--overlay_config_s3", overlay_cfg_s3]
    if email_to and email_from:
        cmd += ["--email_to", email_to, "--email_from", email_from]
    _run(cmd, cwd=project_root, env=os.environ.copy())

