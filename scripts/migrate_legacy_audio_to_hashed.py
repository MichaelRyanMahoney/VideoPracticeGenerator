#!/usr/bin/env python3
"""
Migrate legacy local audio filenames (e.g. audio/MEDIATORB_057.wav) into the new
hash-based cache layout (e.g. audio/VIDEO-01/<audio_hash>.wav).

This script is intentionally conservative:
- It matches rows by *content identity* (speaker + transcript + delivery settings),
  not by row id/order (since scripts change and ids shift).
- It only copies when a matching source WAV exists.
- It never modifies manifests; it only copies files on disk.

Typical usage:
  python3 scripts/migrate_legacy_audio_to_hashed.py \
    --old_manifest manifests/lines.csv.old \
    --new_manifest manifests/lines.csv \
    --project_root .

Then run TTS normally; any missing hashed WAVs will be generated.
"""

from __future__ import annotations

import argparse
import csv
import shutil
from dataclasses import dataclass
from pathlib import Path


def _is_s3_uri(s: str) -> bool:
    return isinstance(s, str) and s.startswith("s3://")


def _norm(s: object) -> str:
    return ("" if s is None else str(s)).strip()


@dataclass(frozen=True)
class LineKey:
    speaker: str
    transcript: str
    typecast_mode: str
    hesitant: str
    emotion_preset: str
    emotion_intensity: str
    tempo: str
    pitch: str
    volume: str


def _row_key(row: dict) -> LineKey:
    # Keep this as string-based matching to avoid float formatting differences.
    # These values come from the manifest CSV and represent the effective synthesis inputs.
    return LineKey(
        speaker=_norm(row.get("speaker")),
        transcript=_norm(row.get("transcript")),
        typecast_mode=_norm(row.get("typecast_mode")).lower() or "smart",
        hesitant=_norm(row.get("hesitant")).lower(),
        emotion_preset=_norm(row.get("emotion_preset")).lower(),
        emotion_intensity=_norm(row.get("emotion_intensity")),
        tempo=_norm(row.get("tempo")),
        pitch=_norm(row.get("pitch")),
        volume=_norm(row.get("volume")),
    )


def _resolve_audio_path(project_root: Path, audio_ref: str) -> Path:
    p = Path(audio_ref)
    if p.is_absolute():
        return p
    return (project_root / p).resolve()


def _read_manifest(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        rdr = csv.DictReader(f)
        return [dict(r) for r in rdr]


def _is_pause_row(row: dict) -> bool:
    speaker = _norm(row.get("speaker")).upper()
    audio = _norm(row.get("audio"))
    transcript = _norm(row.get("transcript"))
    return speaker in {"PAUSE", "BREAK"} or (not audio and not transcript)


def main() -> None:
    ap = argparse.ArgumentParser(description="Copy legacy audio into new hash-based cache layout.")
    ap.add_argument("--old_manifest", required=True, help="Old manifest CSV (legacy audio paths).")
    ap.add_argument("--new_manifest", required=True, help="New manifest CSV (hashed audio paths).")
    ap.add_argument(
        "--project_root",
        default=".",
        help="Project root used to resolve relative audio paths (default: current directory).",
    )
    ap.add_argument("--dry_run", action="store_true", help="Print actions without copying.")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite destination audio if it already exists.")
    args = ap.parse_args()

    project_root = Path(args.project_root).expanduser().resolve()
    old_manifest = Path(args.old_manifest).expanduser().resolve()
    new_manifest = Path(args.new_manifest).expanduser().resolve()

    if not old_manifest.exists():
        raise SystemExit(f"Old manifest not found: {old_manifest}")
    if not new_manifest.exists():
        raise SystemExit(f"New manifest not found: {new_manifest}")

    old_rows = _read_manifest(old_manifest)
    new_rows = _read_manifest(new_manifest)

    # Build lookup of legacy source WAVs by synthesis identity.
    sources_by_key: dict[LineKey, list[Path]] = {}
    for r in old_rows:
        if _is_pause_row(r):
            continue
        audio_ref = _norm(r.get("audio"))
        if not audio_ref or _is_s3_uri(audio_ref):
            continue
        src = _resolve_audio_path(project_root, audio_ref)
        if not src.exists():
            continue
        k = _row_key(r)
        sources_by_key.setdefault(k, []).append(src)

    scanned = 0
    copied = 0
    already = 0
    missing = 0
    skipped_s3 = 0
    no_match = 0

    for r in new_rows:
        if _is_pause_row(r):
            continue
        scanned += 1
        audio_ref = _norm(r.get("audio"))
        if not audio_ref:
            continue
        if _is_s3_uri(audio_ref):
            skipped_s3 += 1
            continue

        dst = _resolve_audio_path(project_root, audio_ref)
        if dst.exists() and not args.overwrite:
            already += 1
            continue

        k = _row_key(r)
        candidates = sources_by_key.get(k) or []
        src = next((p for p in candidates if p.exists()), None)
        if not src:
            # We couldn't migrate this; TTS will regenerate it later.
            if candidates:
                missing += 1
            else:
                no_match += 1
            continue

        if args.dry_run:
            print(f"[dry-run] copy {src} -> {dst}")
            copied += 1
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        print(f"[copy] {src} -> {dst}")
        copied += 1

    print("")
    print("[summary]")
    print(f"- project_root: {project_root}")
    print(f"- old_manifest: {old_manifest}")
    print(f"- new_manifest: {new_manifest}")
    print(f"- scanned new speech rows: {scanned}")
    print(f"- copied: {copied}")
    print(f"- already existed: {already}")
    print(f"- skipped (s3 audio): {skipped_s3}")
    print(f"- no matching legacy row: {no_match}")
    print(f"- matched row but source missing: {missing}")


if __name__ == "__main__":
    main()

