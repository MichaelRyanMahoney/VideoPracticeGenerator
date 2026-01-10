MediatorSPARK Four Heads: End-to-End Guide (from script to video)

This README takes you from an input script to a finished video, including generating audio, building director.json, and rendering in Blender.

0) Requirements
- Python 3.10+ and pip
- ffmpeg in PATH (for audio conversion)
- Blender 4.1+ installed
- Optional TTS: Typecast API key if you use cloud voices
Default runner (recommended)
- Use the viseme-based runner: scripts/run_director_visemes.py

Suggested environment variables (set once per shell):
POSIX shells (macOS/Linux):
  export PROJ="/absolute/path/to/Testing_FourHeads"
  export BLENDER="/Applications/Blender.app/Contents/MacOS/Blender"   # adjust per system
Windows PowerShell:
  $env:PROJ = "C:\\path\\to\\Testing_FourHeads"
  $env:BLENDER = "C:\\Program Files\\Blender Foundation\\Blender 4.1\\blender.exe"

Activate the Python virtual environment (venv in sibling "Testing"):
POSIX shells (macOS/Linux):
  source "$(dirname "$PROJ")/Testing/venv/bin/activate"
Windows PowerShell:
  $parent = Split-Path $env:PROJ -Parent
  & "$parent\\Testing\\venv\\Scripts\\Activate.ps1"
To deactivate later:
  deactivate

1) Prepare your Blender scene (one-time per model set)
- In your .blend scene (recommended: scenes/base_scene.blend), ensure per-character part objects exist:
  <Name>_body, <Name>_teeth, <Name>_eyes, <Name>_hair
  Example: Emily_body, Emily_teeth, Emily_eyes, Emily_hair
- Delete or disable all unnecessary objects to speed up per-frame syncing (keep only head parts + camera(s) + lights).
- Save the scene.

2) Provide your script
- Place your screenplay-like text in PROJ/script.txt.
- Format: lines with SPEAKER in caps followed by their lines. Example:
  EMILY (MEDIATOR)
  I think we can find common ground here.
  MICHAEL (DISPUTANT 1)
  I'm open to suggestions.

3) Parse the script into a manifest CSV
This creates manifests/scene1.csv with rows: id,speaker,audio,transcript
POSIX:
  python3 "scripts/parse_screenplay_to_manifest.py" \
    --in_txt "script.txt" \
    --out_csv "manifests/scene1.csv"
PowerShell:
  python "$env:PROJ/scripts/parse_screenplay_to_manifest.py" --in_txt "$env:PROJ/script.txt" --out_csv "$env:PROJ/manifests/scene1.csv"

4) Generate audio files (Typecast)
- Prepare manifests/typecast_voices.json with speaker -> voice_id mappings.
POSIX:
  export TYPECAST_API_KEY="<your_key>"
  python3 "scripts/tts_typecast_from_manifest.py" \
    --manifest_csv "manifests/scene1.csv" \
    --voice_map "manifests/typecast_voices.json"
PowerShell:
  $env:TYPECAST_API_KEY = "<your_key>"
  python "$env:PROJ/scripts/tts_typecast_from_manifest.py" --manifest_csv "$env:PROJ/manifests/scene1.csv" --voice_map "$env:PROJ/manifests/typecast_voices.json"

5) Build director_visemes.json (forced alignment with WhisperX)
Install dependencies once:
POSIX:
  python3 -m pip install --upgrade pip
  python3 -m pip install whisperx g2p_en torch
PowerShell:
  python -m pip install --upgrade pip
  python -m pip install whisperx g2p_en torch

Create director_visemes.json using your CSV and character map (manifests/characters.json maps speaker -> mesh_name):
POSIX:
  python3 "scripts/whisperx_to_director_visemes.py" \
    --manifest_csv "manifests/scene1.csv" \
    --charmap_json "manifests/characters.json" \
    --out "director_visemes.json"
PowerShell:
  python "$env:PROJ/scripts/whisperx_to_director_visemes.py" --manifest_csv "$env:PROJ/manifests/scene1.csv" --charmap_json "$env:PROJ/manifests/characters.json" --fps 24 --out "$env:PROJ/director_visemes.json"

Notes:
- You can edit manifests/characters.json to point to your scene’s mesh names (legacy *HeadMesh names are ok; the runner auto-detects part objects).

6) Render the video from Blender
Fast preset (lower resolution/samples, disable heavy Eevee features; fps unchanged):
POSIX:
  /Applications/Blender.app/Contents/MacOS/Blender \
    -b "scenes/base_scene.blend" \
    -P "scripts/run_director_visemes.py" -- \
    --director "director_visemes.json" \
    --out "out/four_heads_demo.mp4" \
    --quality fast
PowerShell:
  & $env:BLENDER -b "$env:PROJ/scenes/base_scene.blend" -P "$env:PROJ/scripts/run_director_visemes.py" -- --director "$env:PROJ/director_visemes.json" --out "$env:PROJ/out/four_heads_demo.mp4" --quality fast

For highest quality (slower): use --quality full instead of fast.
To reduce frame count, set "fps" in "$PROJ/director.json" (e.g., 18) before rendering.
Material Preview-style render (ignores scene lights)
- Add --engine workbench on the command to render with Workbench (Material Preview-like).
- Or set in director.json:
  "render": { "engine": "workbench" }
Note: Workbench uses each material’s “Viewport Display” color. If characters look flat/grey, set per-material Viewport Display colors in Blender, otherwise it may resemble Eevee defaults.

Transparent background (Film > Transparent)
- Pass --transparent to enable Film Transparent and write PNG RGBA frames (MP4 does not support alpha). Example:
POSIX:
  /Applications/Blender.app/Contents/MacOS/Blender \
    -b "$PROJ/scenes/base_scene.blend" \
    -P "$PROJ/scripts/run_director_visemes.py" -- \
    --director "$PROJ/director_visemes.json" \
    --out "$PROJ/out/four_heads_demo.mp4" \
    --transparent
This creates frames at: out/four_heads_demo_frames/four_heads_demo_####.png
You can composite these over any background in your NLE, or encode later (see section 7).

7) Optional: render to image sequence and encode later
Render PNG frames (set in Blender UI or customize the script), then encode:
POSIX:
  ffmpeg -framerate 18 -i "$PROJ/out/frames/%05d.png" -c:v libx264 -pix_fmt yuv420p "$PROJ/out/final.mp4"
PowerShell:
  ffmpeg -framerate 18 -i "$env:PROJ/out/frames/%05d.png" -c:v libx264 -pix_fmt yuv420p "$env:PROJ/out/final.mp4"

Automated mux from director (frames + timeline audio → MP4)
- Use the helper script to combine your transparent PNG frames with the mixed audio derived from director_visemes.json:
POSIX:
  python3 "$PROJ/scripts/mux_from_director.py" \
    --director "$PROJ/director_visemes.json" \
    --frames "$PROJ/out/four_heads_demo_frames/four_heads_demo_%04d.png" \
    --out "$PROJ/out/four_heads_demo.mp4"
Notes:
- FPS is read from the director (defaults to 24). Override with --fps if needed.
- The script aligns each beat’s audio by tc_in and mixes them using ffmpeg.
- For a dry run, add --dry_run to print the ffmpeg command without executing.
Background image compositing (during mux)
- To place a static background behind the transparent frames during mux, pass --background:
POSIX:
  python3 "$PROJ/scripts/mux_from_director.py" \
    --director "$PROJ/director_visemes.json" \
    --frames "$PROJ/out/four_heads_demo_frames/four_heads_demo_%04d.png" \
    --background "$PROJ/scenes/SceneBackground.png" \
    --out "$PROJ/out/four_heads_demo.mp4"
Notes:
- The output video resolution matches the background image. Use a 1920x1080 background (SceneBackground.png) for standard landscape output.
- Foreground PNG frames are scaled to a fixed width of 1400px (aspect preserved), centered horizontally, and bottom-aligned over the background.
- If the scaled foreground exceeds the background height, it will crop at the top; otherwise it sits flush to the bottom.

7.1) Insert pause slates from [OVERLAY] markers (freeze + fade)
- If your script.txt contains standalone [OVERLAY] lines, you can insert pause slates that fade in over a frozen frame, sit for N seconds, then fade out, while silence is inserted to keep A/V in sync for the rest of the video.
POSIX:
  python3 "$PROJ/scripts/apply_overlays.py" \
    --script "$PROJ/script.txt" \
    --director "$PROJ/director_visemes.json" \
    --base "$PROJ/out/four_heads_demo.mp4" \
    --overlay_image "$PROJ/scenes/VideoPauseOverlay1.png" \
    --duration 12 \
    --fade 0.5 --overlay_alpha 0.9 --anchor prev_end --pre_roll_frames 4 \
    --out "$PROJ/out/four_heads_demo_with_overlays.mp4"
Notes:
- The [OVERLAY] markers are aligned to the end of the previous beat (start of next), then shifted earlier by --pre_roll_frames (default 2) to avoid catching the first mouth-open frame.
- The pause clip uses the exact video frame at the insertion time as the background under your overlay image.
- You can adjust --duration and --fade as needed.

7.2) Add moving ProcessForm icon tied to [ProcessFormSwap]
- To overlay a small icon that starts on the right and swaps horizontally by ~300px on every [ProcessFormSwap] tag, add these flags:
POSIX:
  python3 "$PROJ/scripts/apply_overlays.py" \
    --script "$PROJ/script.txt" \
    --director "$PROJ/director_visemes.json" \
    --base "$PROJ/out/four_heads_demo.mp4" \
    --overlay_image "$PROJ/scenes/VideoPauseOverlay1.png" \
    --duration 12 \
    --fade 0.5 --overlay_alpha 0.9 --anchor prev_end --pre_roll_frames 4 \
    --pf_icon "$PROJ/scenes/ProcessFormIcon.png" --pf_width 200 --pf_dx 300 --pf_margin 60 --pf_y 60 --pf_anim_sec 0.5 \
    --out "$PROJ/out/four_heads_demo_with_overlays.mp4"
Notes:
- The icon is visible for the entire video, starts on the right, and moves smoothly (0.5s) between right/left at each [ProcessFormSwap].
- Adjust --pf_dx for the horizontal distance, --pf_y for vertical position, and --pf_width for icon size.

8) Realtime preview workflow (screen record)
If you want a very fast result without full rendering, prepare a playback-ready .blend and then screen-record:
Step A: Prepare a playback .blend (no rendering)
POSIX:
  /Applications/Blender.app/Contents/MacOS/Blender \
    -b "$PROJ/scenes/base_scene.blend" \
    -P "$PROJ/scripts/run_director_visemes.py" -- \
    --director "$PROJ/director_visemes.json" \
    --prepare_viewport_blend "$PROJ/out/preview_playback.blend" \
    --no_render
PowerShell:
  & $env:BLENDER -b "$env:PROJ/scenes/base_scene.blend" -P "$env:PROJ/scripts/run_director_visemes.py" -- --director "$env:PROJ/director_visemes.json" --prepare_viewport_blend "$env:PROJ/out/preview_playback.blend" --no_render
Step B: Open preview_playback.blend in Blender (UI), set viewport shading to Material Preview or Rendered, ensure Playback Sync = AV Sync, press Spacebar to play, and screen-record (system audio if configured).
Optional: If you prefer Blender to output viewport frames quickly, run Blender with UI (no -b) and add --viewport_render to the command in Step A. This uses Viewport Render Animation and your current output settings; note it may not embed audio.

8) Tips for speed and stability
- Keep the scene minimal (only head parts + camera/lights). Remove modifiers/drivers from static meshes.
- Persistent Data is enabled in the fast preset to reduce per-frame syncing.
- You can chunk renders by frame range to preview sooner.
 - For faster final renders:
   - Use --engine workbench with --quality fast.
   - Lower resolution in director JSON: "render": { "resolution": [1280, 720] }.
   - Lower frame rate in director JSON: top-level "fps": 18 (or 15/12).
   - These reduce total pixels/frames and speed up linearly.

You now have a reproducible pipeline from script to final video.
MediatorSPARK Four Heads: Quick Start to Final Video

0) Requirements
- Blender 4.1+ installed (macOS path typically: /Applications/Blender.app/Contents/MacOS/Blender)
- This folder contains:
  - scenes/base_scene.blend (recommended scene)
  - director.json (timeline, audio, visemes)
  - scripts/run_director_visemes.py (default runner; OVR viseme shapekeys)
  - audio/*.wav (per-beat audio files)

1) Prepare your scene (only once per model update)
- Ensure each character has these mesh objects in the .blend:
  <Name>_body, <Name>_teeth, <Name>_eyes, <Name>_hair
  Example: Emily_body, Emily_teeth, Emily_eyes, Emily_hair
- Optional: delete unneeded objects (rigs, props, clothes) to speed up sync.
- Save the scene (e.g., scenes/base_scene.blend).

2) Configure the timeline (optional)
- director_visemes.json already contains beats and audio. You usually do not need to regenerate it.

3) Recommended: Fast render settings
- The script supports a fast preset. When you pass --quality fast it will:
  - Reduce resolution scale
  - Lower Eevee render samples
  - Disable heavy Eevee features
  - Enable persistent data caching
  - It does not change fps; set it explicitly in director.json if desired

4) Render to a video (MP4)
Example command (macOS):
/Applications/Blender.app/Contents/MacOS/Blender \
  -b /Users/michaelmahoney/Desktop/MediatorSPARK/MockMediationGenerator/Testing_FourHeads/scenes/base_scene.blend \
  -P /Users/michaelmahoney/Desktop/MediatorSPARK/MockMediationGenerator/Testing_FourHeads/scripts/run_director_visemes.py -- \
  --director /Users/michaelmahoney/Desktop/MediatorSPARK/MockMediationGenerator/Testing_FourHeads/director_visemes.json \
  --out /Users/michaelmahoney/Desktop/MediatorSPARK/MockMediationGenerator/Testing_FourHeads/out/four_heads_demo.mp4 \
  --quality fast --engine workbench

Notes:
- Use --quality full for highest quality (slower).
- To stop mid-render and still get reliable output, prefer image sequences (see section 5).

5) (Optional) Render to an image sequence, then encode
- Set output in Blender to PNG and use the same script; or run Blender GUI.
- After frames render (e.g., to out/frames/%05d.png), encode with ffmpeg:
ffmpeg -framerate 18 -i /path/to/out/frames/%05d.png -c:v libx264 -pix_fmt yuv420p /path/to/out/final.mp4
- You can render in chunks (frame ranges) and re-encode anytime.

6) Tuning and overrides (CLI)
- Lead frames:         --lead_frames 2
- Time offset seconds: --time_offset_sec -0.09
- Smooth factor:       --smooth_factor 1.0
- Quality preset:      --quality fast | full

7) Troubleshooting
- Error: No mesh objects found for character 'X':
  Ensure <Name>_body/teeth/eyes/hair exist in the scene, or set the per-character "profile"/global "weights_profile" as needed. Add "force_parts_only": true if you want to ignore legacy *HeadMesh.
- File won’t play after interrupt (Ctrl+C):
  MP4 may be incomplete. Prefer rendering to PNG frames and assemble later.
- Performance is slow:
  Delete unneeded objects from the scene, keep only head parts + camera/lights. Use --quality fast. Reduce resolution scale. Persistent Data is enabled automatically by the script.

8) Advanced (optional)
- Blinks: enable/disable by toggling ENABLE_BLINKS in scripts/run_director_visemes.py.
- Viseme mapping: adjust scripts/ovr_viseme_map.py if your shapekey names differ.

That’s it. With the scene prepared and director_visemes.json present, a single Blender command will generate the video in out/.

