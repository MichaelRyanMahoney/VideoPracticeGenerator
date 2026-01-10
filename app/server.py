from flask import Flask, request, jsonify
from pathlib import Path
import os, json, uuid

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("VPG_DATA_DIR", "./data")).resolve()
(DATA_DIR / "jobs").mkdir(parents=True, exist_ok=True)

def job_dir(job_id: str) -> Path:
    return DATA_DIR / "jobs" / job_id

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
    (jdir / "status.json").write_text(json.dumps({"jobId": job_id, "status": "queued"}))
    return jsonify(jobId=job_id, status="queued")

@app.get("/jobs/<job_id>")
def get_job(job_id: string := str):
    jdir = job_dir(job_id)
    sp = jdir / "status.json"
    if not sp.exists():
        return jsonify(error="not found"), 404
    return jsonify(json.loads(sp.read_text()))

if __name__ == "__main__":
    # Dev server; in container we'll run gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)