# VideoPracticeGenerator

Minimal Flask API to submit video generation jobs.

## Dev
- python3 -m venv .venv && source .venv/bin/activate
- pip install -r requirements.txt
- python app/server.py

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