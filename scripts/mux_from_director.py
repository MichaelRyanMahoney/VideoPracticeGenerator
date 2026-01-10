#!/usr/bin/env python3
"""
Mux PNG frames (from --transparent renders) with a mixed audio track derived
from director_visemes.json (or director.json). Uses ffmpeg for mixing/encoding.

Usage example:
  python3 scripts/mux_from_director.py \
    --director "/abs/path/to/director_visemes.json" \
    --frames "/abs/path/to/out/four_heads_demo_frames/four_heads_demo_%04d.png" \
    --out "/abs/path/to/out/four_heads_demo.mp4"
"""
import argparse
import json
import shlex
import subprocess
from pathlib import Path


def parse_timecode_to_seconds(tc: str) -> float:
    """
    Parse 'HH:MM:SS.sss' into seconds (float).
    """
    tc = (tc or "").strip()
    if not tc:
        return 0.0
    try:
        hh, mm, ss = tc.split(":")
        return float(hh) * 3600.0 + float(mm) * 60.0 + float(ss)
    except Exception:
        return 0.0


def build_ffmpeg_cmd(frames_pattern: str, fps: int, audio_offsets_ms: list[tuple[str, int]], out_mp4: str, crf: int = 18, audio_bitrate: str = "192k") -> list[str]:
    """
    frames_pattern: e.g., "/.../out/four_heads_demo_frames/four_heads_demo_%04d.png"
    audio_offsets_ms: list of tuples (audio_path, delay_ms)
    """
    cmd: list[str] = []
    cmd += ["ffmpeg", "-y"]
    # Video input (image sequence)
    cmd += ["-framerate", str(int(fps)), "-i", frames_pattern]

    # Audio inputs
    for audio_path, _ms in audio_offsets_ms:
        cmd += ["-i", audio_path]

    # Build filter_complex for audio delays and mix
    # Inputs:
    #   [0:v] = frames
    #   [1:a], [2:a], ... = audio streams
    # We will create [a1], [a2], ... then amix into [amix]
    filter_parts: list[str] = []
    labels: list[str] = []
    for idx, (_path, delay_ms) in enumerate(audio_offsets_ms, start=1):
        a_in = f"{idx}:a"
        a_out = f"a{idx}"
        # adelay needs per-channel delay: "ms|ms" for stereo
        delay_expr = f"{int(delay_ms)}|{int(delay_ms)}"
        filter_parts.append(f"[{a_in}]adelay={delay_expr},apad[{a_out}]")
        labels.append(f"[{a_out}]")

    if labels:
        # Normalize=0 to preserve original gain; adjust in post if needed
        # Ensure consistent format for broad compatibility
        amix = "".join(labels) + f"amix=inputs={len(labels)}:normalize=0, aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[amix]"
        filter_str = "; ".join(filter_parts + [amix])
        cmd += ["-filter_complex", filter_str]
        # Map video and mixed audio
        cmd += ["-map", "0:v:0", "-map", "[amix]"]
    else:
        # No audio inputs; map only video (silent MP4)
        cmd += ["-map", "0:v:0"]

    # Encoding params
    cmd += [
        "-c:v", "libx264",
        "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p",
    ]
    if labels:
        cmd += ["-c:a", "aac", "-b:a", audio_bitrate]
    # Shortest to stop when video or audio ends (whichever is shorter)
    cmd += ["-shortest", out_mp4]
    return cmd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--director", required=True, help="Path to director_visemes.json or director.json")
    ap.add_argument("--frames", required=True, help="Image sequence pattern (e.g., /path/.../%04d.png)")
    ap.add_argument("--out", required=True, help="Output MP4 path")
    ap.add_argument("--fps", type=int, help="Override FPS (defaults to generator_inputs.json run.fps, else director fps, else 24)")
    ap.add_argument("--background", help="Optional background image to place behind RGBA frames (e.g., SceneBackground1.png)")
    ap.add_argument("--fg_width_ratio", type=float, default=0.98, help="Foreground width as a fraction of background width (preserve aspect). Default 0.98")
    ap.add_argument("--fg_contrast", type=float, default=1.0, help="Foreground contrast multiplier. Default 1.0 (no change)")
    ap.add_argument("--fg_sharpen", type=float, default=0.0, help="Foreground unsharp luma amount (0-5). Default 0.0 (off)")
    ap.add_argument("--crf", type=int, default=18, help="Video quality (lower=better; default 18)")
    ap.add_argument("--audio_bitrate", default="192k", help="AAC bitrate (default 192k)")
    ap.add_argument("--dry_run", action="store_true", help="Print ffmpeg command and exit")
    args = ap.parse_args()

    director_path = Path(args.director)
    data = json.loads(director_path.read_text())
    # Resolve FPS priority:
    # 1) CLI --fps
    # 2) manifests/generator_inputs.json run.fps (project default)
    # 3) fps from director JSON
    # 4) fallback 24
    fps = None
    if args.fps:
        fps = int(args.fps)
    else:
        try:
            gen_inputs_path = Path(__file__).resolve().parents[1] / "manifests" / "generator_inputs.json"
            if gen_inputs_path.exists():
                gen_inputs = json.loads(gen_inputs_path.read_text())
                run_cfg = gen_inputs.get("run") or {}
                if "fps" in run_cfg:
                    fps = int(run_cfg["fps"])
        except Exception:
            fps = None
        if fps is None:
            fps = int(data.get("fps", 24))
    # Resolution (for background scaling/cropping if needed)
    try:
        render_res = data.get("render", {}).get("resolution", [1920, 1080])
        width, height = int(render_res[0]), int(render_res[1])
    except Exception:
        width, height = 1920, 1080

    beats = data.get("beats", [])
    audio_offsets_ms: list[tuple[str, int]] = []
    for b in beats:
        audio = (b.get("audio") or "").strip()
        if not audio:
            continue
        tc_in = b.get("tc_in") or "00:00:00.000"
        t_sec = parse_timecode_to_seconds(tc_in)
        delay_ms = int(round(t_sec * 1000.0))
        p = Path(audio)
        if not p.exists():
            # Skip missing audio with a notice; keep going
            print(f"[mux] Warning: missing audio file, skipping: {p}")
            continue
        audio_offsets_ms.append((str(p), delay_ms))

    frames_pattern = str(Path(args.frames))
    out_mp4 = str(Path(args.out))

    # Build ffmpeg command; handle optional background overlay
    if args.background:
        bg_path = str(Path(args.background))
        cmd: list[str] = []
        cmd += ["ffmpeg", "-y"]
        # Background image (looped)
        cmd += ["-loop", "1", "-framerate", str(int(fps)), "-i", bg_path]
        # Foreground frames (RGBA PNG sequence)
        cmd += ["-framerate", str(int(fps)), "-i", frames_pattern]
        # Audio inputs
        for audio_path, _ms in audio_offsets_ms:
            cmd += ["-i", audio_path]

        # Build filter_complex: keep background native size, scale frames to bg width,
        # bottom-align overlay, then audio mix
        filter_parts: list[str] = []
        # [0:v] = bg, [1:v] = frames
        # 1) Ensure background is even-sized for H.264
        filter_parts.append(f"[0:v]scale=ceil(iw/2)*2:ceil(ih/2)*2[bg]")
        # 2) Keep foreground at its original render size; optional minimal processing
        #    Keep alpha intact by processing in yuva444p domain only if needed
        c = float(args.fg_contrast)
        s = float(args.fg_sharpen)
        if c != 1.0 or s > 0.0:
            filter_parts.append(
                f"[1:v]format=rgba,format=yuva444p,scale=1400:-1:flags=bicubic,eq=contrast={c}" + (f",unsharp=7:7:{s}:7:7:0.0" if s > 0.0 else "") + ",format=rgba[fg]"
            )
        else:
            filter_parts.append(
                "[1:v]format=rgba,scale=1400:-1:flags=bicubic[fg]"
            )
        # 3) Ensure alpha on foreground for proper compositing
        filter_parts.append(f"[fg]format=rgba[fg]")
        # 4) Center horizontally and bottom-align the foreground over the background
        filter_parts.append(f"[bg][fg]overlay=x=(main_w-overlay_w)/2:y=main_h-overlay_h:shortest=1[outv]")

        # Audio delays/mix: inputs start at index 2 when bg is provided
        labels: list[str] = []
        for i, (_path, delay_ms) in enumerate(audio_offsets_ms, start=2):
            a_in = f"{i}:a"
            a_out = f"a{i}"
            delay_expr = f"{int(delay_ms)}|{int(delay_ms)}"
            filter_parts.append(f"[{a_in}]adelay={delay_expr},apad[{a_out}]")
            labels.append(f"[{a_out}]")
        if labels:
            amix = "".join(labels) + f"amix=inputs={len(labels)}:normalize=0, aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[amix]"
            filter_parts.append(amix)
        cmd += ["-filter_complex", "; ".join(filter_parts)]
        # Map the composed video and audio
        cmd += ["-map", "[outv]"]
        if labels:
            cmd += ["-map", "[amix]"]
        # Encoding
        cmd += ["-c:v", "libx264", "-crf", str(int(args.crf)), "-pix_fmt", "yuv420p"]
        if labels:
            cmd += ["-c:a", "aac", "-b:a", args.audio_bitrate]
        cmd += ["-shortest", out_mp4]
    else:
        cmd = build_ffmpeg_cmd(
            frames_pattern=frames_pattern,
            fps=fps,
            audio_offsets_ms=audio_offsets_ms,
            out_mp4=out_mp4,
            crf=int(args.crf),
            audio_bitrate=args.audio_bitrate
        )

    print("[mux] ffmpeg command:")
    print(" ", shlex.join(cmd))
    if args.dry_run:
        return

    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    print(f"[mux] Wrote: {out_mp4}")


if __name__ == "__main__":
    main()


