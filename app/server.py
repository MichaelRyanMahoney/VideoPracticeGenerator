from flask import Flask, request, jsonify
from pathlib import Path
import os, json, uuid

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("VPG_DATA_DIR", "./data")).resolve()
(DATA_DIR / "jobs").mkdir(parents=True, exist_ok=True)

def job_dir(job_id: str) -> Path:
    return DATA_DIR / "jobs" / job_id

def _load_project_id_from_generator_inputs(path: Path) -> str:
    try:
        data = json.loads(path.read_text())
        run = data.get("run") or {}
        pid = (run.get("project_name") or run.get("projectId") or run.get("project") or "").strip()
        return pid
    except Exception:
        return ""

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.post("/jobs")
def create_job():
    job_id = str(uuid.uuid4())
    jdir = job_dir(job_id)
    (jdir / "inputs").mkdir(parents=True, exist_ok=True)
    script = request.files.get("script")
    gen = request.files.get("generator_inputs")
    if script:
        script.save(jdir / "inputs" / "script.txt")
    if gen:
        gen.save(jdir / "inputs" / "generator_inputs.json")
    project_id = _load_project_id_from_generator_inputs(jdir / "inputs" / "generator_inputs.json") if (jdir / "inputs" / "generator_inputs.json").exists() else ""
    if not project_id:
        project_id = request.form.get("project_id", "") or request.args.get("project_id", "") or "Video-01"
    # Initialize status and start background job
    (jdir / "status.json").write_text(json.dumps({"jobId": job_id, "status": "queued", "projectId": project_id}))

    # If SQS is configured, enqueue for the AWS worker; otherwise fall back to local orchestrator.
    sqs_url = (os.environ.get("VPG_SQS_QUEUE_URL") or "").strip()
    if sqs_url:
        import boto3
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        sqs = boto3.client("sqs", region_name=region)
        body = {
            "jobId": job_id,
            "projectId": project_id,
            "localScriptPath": str((jdir / "inputs" / "script.txt").resolve()),
            "localGeneratorInputsPath": str((jdir / "inputs" / "generator_inputs.json").resolve()),
        }
        sqs.send_message(QueueUrl=sqs_url, MessageBody=json.dumps(body))
        return jsonify(jobId=job_id, projectId=project_id, status="queued", mode="aws_sqs", dataDir=str(jdir))

    # Local legacy mode
    from .jobs import start_job  # local import to avoid circulars in WSGI reload
    start_job(job_id)
    return jsonify(jobId=job_id, projectId=project_id, status="queued", mode="local", dataDir=str(jdir))

@app.get("/jobs/<job_id>")
def get_job(job_id: str):
    # If AWS is configured, prefer S3-backed status (it updates as the pipeline progresses).
    local_status = None
    sp = job_dir(job_id) / "status.json"
    if sp.exists():
        try:
            local_status = json.loads(sp.read_text())
        except Exception:
            local_status = None

    if os.environ.get("VPG_S3_BUCKET") and os.environ.get("VPG_S3_PREFIX"):
        try:
            import boto3
            region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
            s3 = boto3.client("s3", region_name=region)
            # Prefer projectId from local status; fallback to query param.
            project_id = ""
            if isinstance(local_status, dict):
                project_id = (local_status.get("projectId") or "").strip()
            project_id = project_id or request.args.get("project_id", "").strip()
            if project_id:
                prefix = os.environ.get("VPG_S3_PREFIX").strip().strip("/")
                bucket = os.environ.get("VPG_S3_BUCKET").strip()
                key = f"{prefix}/projects/{project_id}/jobs/{job_id}/status.json"
                obj = s3.get_object(Bucket=bucket, Key=key)
                payload = json.loads(obj["Body"].read().decode("utf-8"))
                return jsonify(payload)
        except Exception:
            # If S3 fetch fails, fall back to local (if any)
            pass

    if isinstance(local_status, dict):
        return jsonify(local_status)
    return jsonify(error="not found"), 404


@app.post("/admin/gpu/warm")
def gpu_warm():
    """
    Optional convenience endpoint: keep one GPU instance warm by setting Batch compute env minvCpus.
    Requires VPG_BATCH_COMPUTE_ENV and AWS credentials.
    """
    ce = (os.environ.get("VPG_BATCH_COMPUTE_ENV") or "").strip()
    if not ce:
        return jsonify(error="VPG_BATCH_COMPUTE_ENV not configured"), 400
    min_vcpus = int(request.args.get("min_vcpus", "8"))
    import boto3
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    batch = boto3.client("batch", region_name=region)
    batch.update_compute_environment(computeEnvironment=ce, computeResources={"minvCpus": int(min_vcpus)})
    return jsonify(status="ok", computeEnvironment=ce, minvCpus=int(min_vcpus))


@app.post("/admin/gpu/off")
def gpu_off():
    ce = (os.environ.get("VPG_BATCH_COMPUTE_ENV") or "").strip()
    if not ce:
        return jsonify(error="VPG_BATCH_COMPUTE_ENV not configured"), 400
    import boto3
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    batch = boto3.client("batch", region_name=region)
    batch.update_compute_environment(computeEnvironment=ce, computeResources={"minvCpus": 0})
    return jsonify(status="ok", computeEnvironment=ce, minvCpus=0)

if __name__ == "__main__":
    # Dev server; in container we'll run gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)