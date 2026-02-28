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
import glob
import json
import re
import shlex
import subprocess
import os
import tempfile
from pathlib import Path
from typing import Optional

from workdir_utils import cleanup_work_dir, make_work_dir, should_keep_workdir

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


def _load_project_id_from_generator_inputs(path: Path) -> str:
    try:
        data = json.loads(path.read_text() or "{}")
        run_cfg = data.get("run") or {}
        pid = (
            (run_cfg.get("project_name") or "")
            or (run_cfg.get("projectId") or "")
            or (run_cfg.get("project_id") or "")
            or (run_cfg.get("project") or "")
        )
        return str(pid).strip()
    except Exception:
        return ""


def _find_child_case_insensitive(parent: Path, desired_name: str) -> Optional[Path]:
    try:
        if not parent.exists() or not parent.is_dir():
            return None
        want = (desired_name or "").lower()
        if not want:
            return None
        for ch in parent.iterdir():
            try:
                if ch.name.lower() == want:
                    return ch
            except Exception:
                continue
    except Exception:
        return None
    return None


def _prefer_project_prefixed_path(path_str: str, project_id: str) -> str:
    """
    If a sibling file exists with '<project_id>-<basename>' (case-insensitive),
    prefer it. Otherwise return the original string.
    """
    pid = (project_id or "").strip()
    if (not pid) or (not path_str):
        return path_str
    p0 = Path(path_str)
    prefix = f"{pid}-".lower()
    try:
        if p0.name.lower().startswith(prefix):
            return path_str
    except Exception:
        pass
    hit = _find_child_case_insensitive(p0.parent, f"{pid}-{p0.name}")
    return str(hit) if hit else path_str


def build_ffmpeg_cmd(frames_pattern: str, fps: int, audio_offsets_ms: list[tuple[str, int]], out_mp4: str, crf: int = 18, audio_bitrate: str = "192k", max_duration: Optional[float] = None) -> list[str]:
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
    if max_duration:
        cmd += ["-t", f"{max_duration:.3f}"]
    else:
        cmd += ["-shortest"]
    cmd += [out_mp4]
    return cmd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--director", required=True, help="Path to director_visemes.json or director.json")
    ap.add_argument("--frames", required=True, help="Image sequence pattern (e.g., /path/.../%04d.png)")
    ap.add_argument("--out", required=True, help="Output MP4 path")
    ap.add_argument("--fps", type=int, help="Override FPS (defaults to generator_inputs.json run.fps, else director fps, else 24)")
    ap.add_argument(
        "--generator_inputs_json",
        default="",
        help="Optional path to generator_inputs.json (preferred over repo default for FPS lookup).",
    )
    ap.add_argument("--background", help="Optional background image to place behind RGBA frames (e.g., SceneBackground1.png)")
    ap.add_argument("--fg_width_ratio", type=float, default=0.73, help="Foreground width as a fraction of output width (preserve aspect). Default 0.73")
    ap.add_argument("--fg_contrast", type=float, default=1.0, help="Foreground contrast multiplier. Default 1.0 (no change)")
    ap.add_argument("--fg_sharpen", type=float, default=0.0, help="Foreground unsharp luma amount (0-5). Default 0.0 (off)")
    ap.add_argument("--patch_image", help="Optional full-frame PNG to overlay on top of video (e.g., to patch a render glitch).")
    ap.add_argument("--patch_frame_start", type=int, default=0, help="Start frame number in filename space (e.g., 261 for *_0261.png). Inclusive. Default 0 (disabled).")
    ap.add_argument("--patch_frame_end", type=int, default=0, help="End frame number in filename space (e.g., 1288 for *_1288.png). Inclusive. Default 0 (disabled).")
    ap.add_argument("--crf", type=int, default=18, help="Video quality (lower=better; default 18)")
    ap.add_argument("--audio_bitrate", default="192k", help="AAC bitrate (default 192k)")
    ap.add_argument("--dry_run", action="store_true", help="Print ffmpeg command and exit")
    args = ap.parse_args()

    director_path = Path(args.director)
    data = json.loads(director_path.read_text())
    # Resolve FPS priority:
    # 1) CLI --fps
    # 2) job-scoped generator_inputs.json (CLI/env/inferred), else repo default
    # 3) fps from director JSON
    # 4) fallback 24
    fps = None
    project_id = ""
    if args.fps:
        fps = int(args.fps)
    else:
        try:
            gen_inputs_path = None
            if args.generator_inputs_json:
                gen_inputs_path = Path(args.generator_inputs_json).expanduser()
            if (not gen_inputs_path) or (not gen_inputs_path.exists()):
                env_path = (os.environ.get("VPG_GENERATOR_INPUTS_JSON") or "").strip()
                if env_path:
                    gen_inputs_path = Path(env_path).expanduser()
            if (not gen_inputs_path) or (not gen_inputs_path.exists()):
                # Common job layout: <job>/director_visemes.json with sibling <job>/inputs/generator_inputs.json
                inferred = director_path.parent / "inputs" / "generator_inputs.json"
                if inferred.exists():
                    gen_inputs_path = inferred
            if (not gen_inputs_path) or (not gen_inputs_path.exists()):
                gen_inputs_path = Path(__file__).resolve().parents[1] / "manifests" / "generator_inputs.json"
            if gen_inputs_path.exists():
                gen_inputs = json.loads(gen_inputs_path.read_text())
                run_cfg = gen_inputs.get("run") or {}
                if "fps" in run_cfg:
                    fps = int(run_cfg["fps"])
                project_id = _load_project_id_from_generator_inputs(gen_inputs_path)
                print(f"[mux] FPS source: generator_inputs_json={gen_inputs_path} fps={fps}")
        except Exception:
            fps = None
        if fps is None:
            fps = int(data.get("fps", 24))
            print(f"[mux] FPS source: director fps={fps}")
    if fps is None:
        fps = 24
        print(f"[mux] FPS source: fallback fps={fps}")

    # Prefer project-prefixed background/patch assets when present.
    if project_id:
        if args.background:
            args.background = _prefer_project_prefixed_path(str(args.background), project_id)
        if args.patch_image:
            args.patch_image = _prefer_project_prefixed_path(str(args.patch_image), project_id)

    # Resolve background/patch paths robustly across local runs vs AWS/Docker.
    repo_root = Path(os.environ.get("VPG_REPO_ROOT") or Path(__file__).resolve().parents[1]).resolve()

    def _resolve_runtime_asset(p: str | None) -> str | None:
        s = (p or "").strip()
        if not s:
            return None
        pp = Path(s)
        if pp.is_absolute():
            return str(pp)
        # First try relative to the director/job folder (common in AWS finalizer workdirs).
        cand = (director_path.parent / pp).resolve()
        if cand.exists():
            return str(cand)
        # Then try relative to the repo root (in-image assets: scenes/, assets/).
        cand2 = (repo_root / pp).resolve()
        if cand2.exists():
            return str(cand2)
        # Finally, try "as if" the path was written relative to scripts/ (e.g. ../scenes/...).
        cand3 = (repo_root / "scripts" / pp).resolve()
        if cand3.exists():
            return str(cand3)
        return s

    args.background = _resolve_runtime_asset(args.background)
    args.patch_image = _resolve_runtime_asset(args.patch_image)
    # Resolution (for background scaling/cropping if needed)
    try:
        render_res = data.get("render", {}).get("resolution", [1920, 1080])
        width, height = int(render_res[0]), int(render_res[1])
    except Exception:
        width, height = 1920, 1080

    beats = data.get("beats", [])
    audio_offsets_ms: list[tuple[str, int]] = []

    def is_s3(uri: str) -> bool:
        return isinstance(uri, str) and uri.startswith("s3://")

    def s3_parse(uri: str) -> tuple[str, str]:
        assert uri.startswith("s3://")
        no = uri[5:]
        return no.split("/", 1)[0], no.split("/", 1)[1]

    s3 = None  # lazily initialized only if we see s3:// audio URIs
    dl_dir = make_work_dir("vpg_mux_audio_")

    def ensure_local_audio(audio_ref: str) -> Path:
        if not is_s3(audio_ref):
            p = Path(audio_ref)
            if not p.is_absolute():
                p = (director_path.parent / p).resolve()
            return p
        try:
            import boto3  # type: ignore
        except Exception as e:
            raise SystemExit(
                "mux_from_director: audio URI is s3://... but boto3 is not installed.\n"
                "Install it with: pip install boto3\n"
                f"Details: {e}"
            )
        nonlocal s3
        if s3 is None:
            s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
        b, k = s3_parse(audio_ref)
        safe = k.replace("/", "__")
        dst = dl_dir / safe
        if not dst.exists():
            print(f"[s3] download {audio_ref} -> {dst}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(b, k, str(dst))
        return dst

    try:
        # Only initialize S3 client if we actually need it.
        s3 = None
        for b in beats:
            audio = (b.get("audio") or "").strip()
            if not audio:
                continue
            tc_in = b.get("tc_in") or "00:00:00.000"
            t_sec = parse_timecode_to_seconds(tc_in)
            delay_ms = int(round(t_sec * 1000.0))
            p = ensure_local_audio(audio)
            if not p.exists():
                # Skip missing audio with a notice; keep going
                print(f"[mux] Warning: missing audio file, skipping: {p}")
                continue
            audio_offsets_ms.append((str(p.resolve()), delay_ms))

        frames_pattern = str(Path(args.frames))
        out_mp4 = str(Path(args.out))

        # Compute video duration from frame count for a hard output cap
        # (apad produces infinite audio; -shortest is unreliable with filter_complex)
        _frame_glob = re.sub(r'%\d*d', '*', frames_pattern)
        _num_frames = len(glob.glob(_frame_glob))
        _video_dur = (_num_frames / float(fps) + 2.0) if (_num_frames and fps) else None
        if _video_dur:
            print(f"[mux] Frame count: {_num_frames}, fps: {fps}, hard duration cap: {_video_dur:.3f}s")

        # Build ffmpeg command; handle optional background overlay
        fg_ratio = max(0.1, min(1.0, float(args.fg_width_ratio)))
        fg_target_w = max(2, int(round(width * fg_ratio)))
        if fg_target_w % 2:
            fg_target_w += 1

        patch_enabled = bool(args.patch_image and int(args.patch_frame_start) > 0 and int(args.patch_frame_end) > 0 and int(args.patch_frame_end) >= int(args.patch_frame_start))
        patch_start_idx = max(0, int(args.patch_frame_start) - 1)  # frames are typically numbered starting at 0001; ffmpeg n starts at 0
        patch_end_idx = max(0, int(args.patch_frame_end) - 1)

        if args.background:
            bg_path = str(Path(args.background))
            cmd: list[str] = []
            cmd += ["ffmpeg", "-y"]
            # Background image (looped)
            cmd += ["-loop", "1", "-framerate", str(int(fps)), "-i", bg_path]
            # Foreground frames (RGBA PNG sequence)
            cmd += ["-framerate", str(int(fps)), "-i", frames_pattern]
            # Optional patch image (looped)
            if patch_enabled:
                cmd += ["-loop", "1", "-framerate", str(int(fps)), "-i", str(Path(args.patch_image))]
            # Audio inputs
            for audio_path, _ms in audio_offsets_ms:
                cmd += ["-i", audio_path]

            # Build filter_complex: keep background native size, scale frames to bg width,
            # bottom-align overlay, then audio mix
            filter_parts: list[str] = []
            # [0:v] = bg, [1:v] = frames
            # 1) Ensure background is even-sized for H.264
            filter_parts.append(f"[0:v]scale=ceil(iw/2)*2:ceil(ih/2)*2[bg]")
            # 2) Optional patch overlay applied in raw PNG space (before scaling/compositing)
            if patch_enabled:
                enable_expr = f"between(n\\,{patch_start_idx}\\,{patch_end_idx})"
                filter_parts.append(f"[1:v]format=rgba[fgsrc]")
                filter_parts.append(f"[2:v]format=rgba[patch0]")
                filter_parts.append(f"[patch0][fgsrc]scale2ref=w=main_w:h=main_h[patch][fgref]")
                filter_parts.append(f"[fgref][patch]overlay=x=0:y=0:format=auto:enable='{enable_expr}'[fgsrcp]")
                fg_in = "fgsrcp"
            else:
                fg_in = "1:v"
            # 2) Keep foreground at its original render size; optional minimal processing
            #    Keep alpha intact by processing in yuva444p domain only if needed
            c = float(args.fg_contrast)
            s = float(args.fg_sharpen)
            if c != 1.0 or s > 0.0:
                filter_parts.append(
                    f"[{fg_in}]format=rgba,format=yuva444p,scale={fg_target_w}:-1:flags=bicubic,eq=contrast={c}" + (f",unsharp=7:7:{s}:7:7:0.0" if s > 0.0 else "") + ",format=rgba[fg]"
                )
            else:
                filter_parts.append(
                    f"[{fg_in}]format=rgba,scale={fg_target_w}:-1:flags=bicubic[fg]"
                )
            # 3) Ensure alpha on foreground for proper compositing
            filter_parts.append(f"[fg]format=rgba[fg]")
            # 4) Center horizontally and bottom-align the foreground over the background
            filter_parts.append(f"[bg][fg]overlay=x=(main_w-overlay_w)/2:y=main_h-overlay_h:shortest=1[outv0]")
            outv_tag = "outv0"

            # Audio delays/mix: inputs start after (bg, frames, optional patch)
            labels: list[str] = []
            audio_start_idx = 3 if patch_enabled else 2
            for i, (_path, delay_ms) in enumerate(audio_offsets_ms, start=audio_start_idx):
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
            cmd += ["-map", f"[{outv_tag}]"]
            if labels:
                cmd += ["-map", "[amix]"]
            # Encoding
            cmd += ["-c:v", "libx264", "-crf", str(int(args.crf)), "-pix_fmt", "yuv420p"]
            if labels:
                cmd += ["-c:a", "aac", "-b:a", args.audio_bitrate]
            if _video_dur:
                cmd += ["-t", f"{_video_dur:.3f}"]
            else:
                cmd += ["-shortest"]
            cmd += [out_mp4]
        else:
            # No explicit background image:
            # still keep legacy composition behavior by scaling foreground and bottom-aligning
            # into a fixed canvas (director render resolution).
            cmd = ["ffmpeg", "-y", "-framerate", str(int(fps)), "-i", frames_pattern]
            if patch_enabled:
                cmd += ["-loop", "1", "-framerate", str(int(fps)), "-i", str(Path(args.patch_image))]
            for audio_path, _ms in audio_offsets_ms:
                cmd += ["-i", audio_path]

            filter_parts: list[str] = []
            # Optional patch overlay applied in raw PNG space (before scaling/padding)
            if patch_enabled:
                enable_expr = f"between(n\\,{patch_start_idx}\\,{patch_end_idx})"
                filter_parts.append(f"[0:v]format=rgba[fgsrc]")
                filter_parts.append(f"[1:v]format=rgba[patch0]")
                filter_parts.append(f"[patch0][fgsrc]scale2ref=w=main_w:h=main_h[patch][fgref]")
                filter_parts.append(f"[fgref][patch]overlay=x=0:y=0:format=auto:enable='{enable_expr}'[fgsrcp]")
                fg_in = "fgsrcp"
            else:
                fg_in = "0:v"
            c = float(args.fg_contrast)
            s = float(args.fg_sharpen)
            if c != 1.0 or s > 0.0:
                filter_parts.append(
                    f"[{fg_in}]format=rgba,format=yuva444p,scale={fg_target_w}:-1:flags=bicubic,eq=contrast={c}" + (f",unsharp=7:7:{s}:7:7:0.0" if s > 0.0 else "") + ",format=rgba[fg]"
                )
            else:
                filter_parts.append(f"[{fg_in}]format=rgba,scale={fg_target_w}:-1:flags=bicubic[fg]")
            filter_parts.append(f"[fg]pad={width}:{height}:(ow-iw)/2:(oh-ih):black[outv0]")
            outv_tag = "outv0"

            labels: list[str] = []
            audio_start_idx = 2 if patch_enabled else 1
            for i, (_path, delay_ms) in enumerate(audio_offsets_ms, start=audio_start_idx):
                a_in = f"{i}:a"
                a_out = f"a{i}"
                delay_expr = f"{int(delay_ms)}|{int(delay_ms)}"
                filter_parts.append(f"[{a_in}]adelay={delay_expr},apad[{a_out}]")
                labels.append(f"[{a_out}]")
            if labels:
                amix = "".join(labels) + f"amix=inputs={len(labels)}:normalize=0, aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[amix]"
                filter_parts.append(amix)

            cmd += ["-filter_complex", "; ".join(filter_parts)]
            cmd += ["-map", f"[{outv_tag}]"]
            if labels:
                cmd += ["-map", "[amix]"]
            cmd += ["-c:v", "libx264", "-crf", str(int(args.crf)), "-pix_fmt", "yuv420p"]
            if labels:
                cmd += ["-c:a", "aac", "-b:a", args.audio_bitrate]
            if _video_dur:
                cmd += ["-t", f"{_video_dur:.3f}"]
            else:
                cmd += ["-shortest"]
            cmd += [out_mp4]

        print("[mux] ffmpeg command:")
        print(" ", shlex.join(cmd))
        if args.dry_run:
            return

        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)
        print(f"[mux] Wrote: {out_mp4}")
    finally:
        if not should_keep_workdir():
            cleanup_work_dir(dl_dir)


if __name__ == "__main__":
    main()


