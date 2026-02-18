from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def _work_parent_dir() -> Path:
    """
    Prefer putting temp work under a persistent volume (typically /data) to avoid filling
    container root. This directory may persist between runs; callers should cleanup their
    per-run subdirectories.
    """
    work_root = (os.environ.get("VPG_WORK_DIR") or "").strip()
    if work_root:
        return Path(work_root).expanduser().resolve()
    data_dir = (os.environ.get("VPG_DATA_DIR") or "/data").strip()
    return (Path(data_dir).expanduser().resolve() / "tmp")


def make_work_dir(prefix: str) -> Path:
    parent = _work_parent_dir()
    try:
        parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
    except Exception:
        # Fall back to system temp if the preferred location isn't available.
        return Path(tempfile.mkdtemp(prefix=prefix))


def should_keep_workdir() -> bool:
    return (os.environ.get("VPG_KEEP_WORKDIR") or "0").strip() == "1"


def cleanup_work_dir(path: Path) -> None:
    if should_keep_workdir():
        print(f"[workdir] keep: {path}")
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass

