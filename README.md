# VideoPracticeGenerator

Minimal Flask API to submit video generation jobs.

## Dev
- python3 -m venv .venv && source .venv/bin/activate
- pip install -r requirements.txt
- python app/server.py

## New architecture (CPU API + GPU Batch)

This repo now supports a **CPU-first Flask API** that queues jobs and offloads **GPU work (WhisperX + Blender render)** to **AWS Batch**, while keeping **mux/overlays/final delivery** on CPU.

### What runs where
- **CPU (Flask / worker)**
  - Receives requests in the shape of `generator_inputs.json` + `script.txt`
  - Builds a manifest CSV with **stable audio hashing** and **S3 audio URIs**
  - Generates missing audio via Typecast and uploads to S3
  - Submits AWS Batch GPU jobs (director + render array)
- **GPU (AWS Batch)**
  - `scripts/gpu_build_director.py`: downloads manifest + audio from S3 and runs WhisperX → uploads `director_visemes.json`
  - `scripts/batch_render_array_entrypoint.py`: Batch array job that renders **frame ranges** in parallel → uploads PNG frames to S3

### Audio caching (stable hashing)
`scripts/parse_screenplay_to_manifest.py` now writes:
- `audio_hash`: sha256 derived from role + transcript + delivery attrs + (if available) voice_id
- `audio`: either a local path (legacy) or an `s3://...` URI if you pass `--audio_s3_prefix`

When `audio` is an S3 URI, `scripts/tts_typecast_from_manifest.py`:
- **skips** synthesis if the object already exists in S3
- otherwise generates WAV and uploads to that URI

### Parallel rendering (Batch array jobs)
Distributed render shards render with `--no_audio` (timeline end estimated from visemes), so render workers do not need to download audio.

## Running locally with Docker

### CPU API container

```bash
docker compose -f docker-compose.cpu.yml up --build
```

### GPU worker container (local GPU host)

```bash
docker compose -f docker-compose.gpu.worker.yml up --build
```

## AWS wiring (what you create in AWS)

You will create:
- **S3 bucket** for job inputs/audio/frames/output
- **SQS queue** for job requests (CPU worker pulls from this)
- **AWS Batch Compute Environment (GPU)** using `g5.*` (optionally Spot) + max vCPU cap
- **AWS Batch Job Queue (GPU)** pointing to that compute environment
- **AWS Batch Job Definitions**:
  - GPU director job definition (image built from `docker/Dockerfile.gpu`) running:
    - `python scripts/gpu_build_director.py ...`
  - GPU render array job definition (same GPU image) running:
    - `python scripts/batch_render_array_entrypoint.py ...`

### Required environment variables (CPU API / worker)
- `AWS_REGION`
- `VPG_S3_BUCKET`
- `VPG_S3_PREFIX` (default: `vpg`)
- `VPG_SQS_QUEUE_URL`
- `VPG_BATCH_JOB_QUEUE_GPU`
- `VPG_BATCH_JOB_DEF_GPU_DIRECTOR`
- `VPG_BATCH_JOB_DEF_GPU_RENDER`
- `VPG_RENDER_SHARDS` (default: `8`)

Optional (keep one GPU warm via endpoints):
- `VPG_BATCH_COMPUTE_ENV`

Optional (CPU finalize on the CPU instance after GPU render completes):
- `VPG_RUN_FINALIZE=1`
- `VPG_OVERLAY_CONFIG_S3` (optional; config file for `apply_overlays.py`)
- `VPG_EMAIL_FROM` and `VPG_EMAIL_TO` (optional; SES)

## API usage

### Submit a job
`POST /jobs` as multipart form-data:
- `script`: file `script.txt`
- `generator_inputs`: file `generator_inputs.json`
- optional `project_id` (otherwise we use `run.project_name` inside generator_inputs.json, else `Video-01`)

If `VPG_SQS_QUEUE_URL` is set, jobs are queued to SQS and processed by:

```bash
python -m app.worker
```

See `config.example.env` for a copy/paste starter.

### Get job status
`GET /jobs/<jobId>`

If local status is missing but S3 is configured, you can pass `?project_id=Video-01` to fetch `status.json` from S3.

### GPU warm/off (optional)
- `POST /admin/gpu/warm?min_vcpus=8`
- `POST /admin/gpu/off`

## EC2 GPU notes (Blender)
- The Blender scripts in `scripts/` already attempt to use **Cycles GPU** (OPTIX → CUDA) for:
  - Character export: `scripts/blender_export_characters.py`
  - Viseme render: `scripts/run_director_visemes.py` (when `manifests/generator_inputs.json` has `run.render_engine` set to Cycles / `BLENDER_CYCLES`)
- If EC2 is still rendering on CPU, it’s usually:
  - **Docker isn’t getting the GPU** (missing NVIDIA Container Toolkit / `--gpus all` / Compose `gpus: all`)
  - **Host driver isn’t working** (`nvidia-smi` fails on the EC2 host)
  - **Eevee + headless/Xvfb** can fall back to software OpenGL; prefer Cycles GPU for headless.

### Quick diagnose
- Print detected Cycles devices from inside Blender:
  - `blender -b scenes/base_scene.blend --python scripts/blender_print_cycles_devices.py`
- Run the API container with GPU (Docker Compose v2+):
  - `docker compose -f docker-compose.gpu.yml up --build`