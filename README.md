# VideoPracticeGenerator

Minimal Flask API to submit video generation jobs.

## Dev
- python3 -m venv .venv && source .venv/bin/activate
- pip install -r requirements.txt
- python app/server.py

## New architecture (CPU API + CPU Batch/Workers)

This repo supports a **CPU-first Flask API** that queues jobs and runs **director + render + finalize** on CPU workers (EC2+SQS or AWS Batch CPU), with S3 as the shared artifact store.

### What runs where
- **CPU (Flask / worker)**
  - Receives requests in the shape of `generator_inputs.json` + `script.txt`
  - Uploads job inputs to S3 and enqueues SQS payloads with S3 URIs (portable across worker hosts)
  - Builds a manifest CSV with **stable audio hashing** and **S3 audio URIs**
  - Generates missing audio via Typecast and uploads to S3
  - Submits/renders director + frame shards on CPU execution targets
- **CPU render/align workers (EC2 ASG or AWS Batch CPU)**
  - `scripts/gpu_build_director.py`: downloads manifest + audio from S3 and runs WhisperX (CPU mode) → uploads `director_visemes.json`
  - `scripts/batch_render_array_entrypoint.py`: renders **frame ranges** in parallel shards → uploads PNG frames to S3

### Audio caching (stable hashing)
`scripts/parse_screenplay_to_manifest.py` now writes:
- `audio_hash`: sha256 derived from role + transcript + delivery attrs + (if available) voice_id
- `audio`: either a local path (legacy) or an `s3://...` URI if you pass `--audio_s3_prefix`

When `audio` is an S3 URI, `scripts/tts_typecast_from_manifest.py`:
- **skips** synthesis if the object already exists in S3
- otherwise generates WAV and uploads to that URI

### Typecast model + delivery controls
The TTS pipeline uses Typecast `ssfm-v30` (`POST /v1/text-to-speech`) with preset emotion prompts.

- Supported `emotion_preset`: `normal`, `happy`, `sad`, `angry`, `whisper`, `toneup`, `tonedown`
- `emotion_intensity`: `0.0` to `2.0`
- `tempo`: `0.5` to `2.0`
- `pitch`: semitone shift (`0` neutral, range `-12..12`)
- `volume`: `0` to `200`

### Parallel rendering (Batch array jobs)
Distributed render shards render with `--no_audio` (timeline end estimated from visemes), so render workers do not need to download audio.

## Running locally with Docker

### CPU API container

```bash
docker compose -f docker-compose.cpu.yml up --build
```

### CPU render worker container (local/EC2 CPU host)

Build a CPU-only render image (Blender + WhisperX CPU toolchain):

```bash
docker build -f docker/Dockerfile.render-cpu -t vpg-render-cpu .
```

### Prevent re-downloading WhisperX/PyTorch models + avoid filling container `/tmp`
Two common sources of wasted time and disk on a single EC2 GPU box are:
- **Torch/torchaudio model checkpoints** downloading into container root (default: `/root/.cache/...`)
- **Per-line audio WAV downloads** caching under `/tmp` (container overlay)

This repo now defaults the GPU docker-compose files to keep caches on `/data`:
- `TORCH_HOME=/data/.cache/torch`
- `VPG_ALIGN_MODEL_DIR=/data/.cache/torchaudio`
- `VPG_WORK_DIR=/data/tmp`
- `VPG_AUDIO_CACHE_DIR=/data/cache/audio`

Make sure `/data` is backed by a sufficiently large EBS volume.

### Idempotency: skip director generation when already done
`scripts/gpu_build_director.py` will **skip WhisperX** if the target `director_out_s3` already exists in S3.

- **Default**: skip if exists (`VPG_SKIP_DIRECTOR_IF_EXISTS=1`)
- **Force rebuild**: run `gpu_build_director.py --force` (or set `VPG_SKIP_DIRECTOR_IF_EXISTS=0`)

### Render cache (reuse frames across jobs)
If **director + scene + render settings** are identical, you can reuse already-rendered frames instead of re-rendering.

When enabled (`VPG_ENABLE_RENDER_CACHE=1`, default), single-node GPU mode and the GPU executor mode will:
- compute a deterministic `render_cache_key`
- check for the “last expected frame” under:
  - `projects/<project>/render_cache/<render_cache_key>/frames`
- on a cache hit, **skip rendering** and finalize using that frames prefix

To force a new cached render even if inputs are the same:
- bump `VPG_RENDER_CACHE_VERSION` (default: `1`)

### Quick disk cleanup (host)
If you do run out of disk on the EC2 host, Docker artifacts are often the culprit:

```bash
docker system df
docker system prune -af
docker volume prune -f
```

## AWS wiring (what you create in AWS)

You will create:
- **S3 bucket** for job inputs/audio/frames/output
- **SQS queue** for job requests (CPU worker pulls from this)
- Optional **AWS Batch Compute Environment (CPU)** (Spot-friendly) + job queue
- **AWS Batch Job Definitions** (CPU image from `docker/Dockerfile.render-cpu`):
  - director job definition running `python scripts/gpu_build_director.py ...`
  - render array job definition running `python scripts/batch_render_array_entrypoint.py ...`

### Required environment variables (CPU API / worker)
- `AWS_REGION`
- `VPG_S3_BUCKET`
- `VPG_S3_PREFIX` (default: `vpg`)
- `VPG_SQS_QUEUE_URL`
- `VPG_BATCH_JOB_QUEUE_RENDER` (or legacy `VPG_BATCH_JOB_QUEUE_GPU`)
- `VPG_BATCH_JOB_DEF_DIRECTOR` (or legacy `VPG_BATCH_JOB_DEF_GPU_DIRECTOR`)
- `VPG_BATCH_JOB_DEF_RENDER` (or legacy `VPG_BATCH_JOB_DEF_GPU_RENDER`)
- `VPG_RENDER_SHARDS` (default: `8`)
- `VPG_WORKER_CONCURRENCY` (default: `1`)
- `VPG_SQS_VISIBILITY_TIMEOUT` (default: `3600`)

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

Queue payloads are S3-based (`scriptS3`, `generatorInputsS3`) so workers can run on independent instances without shared local filesystems.

Operational hardening guidance (DLQ, autoscaling, CloudWatch, S3 lifecycle): `docs/aws_cpu_scaling_ops.md`.

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