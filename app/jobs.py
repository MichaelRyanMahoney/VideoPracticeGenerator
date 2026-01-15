import os
import json
import sys
import threading
import subprocess
from pathlib import Path
from datetime import datetime


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _data_dir() -> Path:
    return Path(os.environ.get("VPG_DATA_DIR", "./data")).resolve()


def job_dir(job_id: str) -> Path:
    return _data_dir() / "jobs" / job_id


def _status_path(job_id: str) -> Path:
    return job_dir(job_id) / "status.json"


def _write_status(job_id: str, status: str, extra: dict | None = None) -> None:
    payload = {"jobId": job_id, "status": status, "updatedAt": _now_iso()}
    if extra:
        payload.update(extra)
    sp = _status_path(job_id)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(payload))


def _build_job_config(job_id: str) -> Path:
    """
    Create a per-job config overriding input/output paths into the job folder.
    """
    project_root = Path(__file__).resolve().parents[1]
    default_cfg_path = project_root / "run_full_video_creation_sequence.config.json"
    if not default_cfg_path.exists():
        raise RuntimeError(f"Missing default config: {default_cfg_path}")
    cfg = json.loads(default_cfg_path.read_text())

    jdir = job_dir(job_id)
    inputs = jdir / "inputs"
    outputs = jdir / "out"
    manifests = jdir / "manifests"
    outputs.mkdir(parents=True, exist_ok=True)
    manifests.mkdir(parents=True, exist_ok=True)

    # Override key paths to be job-scoped
    cfg["generator_inputs_json"] = str(inputs / "generator_inputs.json")
    cfg["script_txt"] = str(inputs / "script.txt")
    cfg["manifest_csv_out"] = str(manifests / "lines.csv")
    cfg["director_json_out"] = str(jdir / "director_visemes.json")
    cfg["out_video"] = str(outputs / "blender_render.mp4")
    # Optional: background image can be user-provided in inputs later
    # Leave other paths (blender_binary, assets/scenes) as repo defaults

    # Environment overrides (useful in Docker/local)
    blender_bin_env = os.environ.get("VPG_BLENDER_BIN")
    if blender_bin_env:
        cfg["blender_binary"] = blender_bin_env
    if os.environ.get("VPG_SKIP_RENDER") == "1":
        cfg["skip_render"] = True
    if os.environ.get("VPG_SKIP_MUX") == "1":
        cfg["skip_mux"] = True
    if os.environ.get("VPG_SKIP_BLENDER") == "1":
        # Disable all Blender-dependent steps
        cfg["run_generate_characters"] = False
        cfg["run_export_characters"] = False
        cfg["skip_configure_roles"] = True

    job_cfg_path = jdir / "run.config.json"
    job_cfg_path.write_text(json.dumps(cfg))
    return job_cfg_path


def _run_orchestrator(job_id: str) -> None:
    project_root = Path(__file__).resolve().parents[1]
    jdir = job_dir(job_id)
    log_path = jdir / "job.log"
    try:
        cfg_path = _build_job_config(job_id)
    except Exception as ex:
        _write_status(job_id, "failed", {"error": f"config: {ex}"})
        return

    cmd = [
        sys.executable,
        str(project_root / "scripts" / "run_full_video_creation_sequence.py"),
        "--config",
        str(cfg_path),
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    _write_status(job_id, "running", {"cmd": cmd})
    with open(log_path, "ab", buffering=0) as logf:
        logf.write(f"[start] {' '.join(cmd)}\n".encode())
        try:
            proc = subprocess.Popen(cmd, cwd=str(project_root), env=env, stdout=logf, stderr=subprocess.STDOUT)
            rc = proc.wait()
            if rc == 0:
                out_mp4 = jdir / "out" / "blender_render.mp4"
                _write_status(job_id, "completed", {"output": str(out_mp4)})
            else:
                _write_status(job_id, "failed", {"returncode": rc})
        except Exception as ex:
            _write_status(job_id, "failed", {"error": str(ex)})


def start_job(job_id: str) -> None:
    """
    Spawn a background thread to run the orchestrator for this job.
    """
    t = threading.Thread(target=_run_orchestrator, args=(job_id,), daemon=True)
    t.start()


def read_status(job_id: str) -> dict:
    sp = _status_path(job_id)
    if not sp.exists():
        return {"jobId": job_id, "status": "unknown"}
    try:
        return json.loads(sp.read_text())
    except Exception:
        return {"jobId": job_id, "status": "unknown"}


