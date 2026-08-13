"""Small helpers for robust atomic file writes."""
from __future__ import annotations

import time
import uuid
import os
import shutil
from pathlib import Path


TRANSIENT_WINERRORS = {5, 32}


def replace_with_retry(source: Path, target: Path, attempts: int = 5) -> None:
    """Replace a file, tolerating short-lived Windows locks."""
    source = Path(source)
    target = Path(target)

    for attempt in range(attempts):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt >= attempts - 1:
                raise
        except OSError as exc:
            if getattr(exc, "winerror", None) not in TRANSIENT_WINERRORS or attempt >= attempts - 1:
                raise
        time.sleep(0.05 * (attempt + 1))


def temp_path_for(path: Path) -> Path:
    path = Path(path)
    return path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write text to a unique temp file and atomically replace the target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path_for(path)
    try:
        with tmp.open("w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write bytes to a unique temp file and atomically replace the target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path_for(path)
    try:
        with tmp.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def atomic_copy_file(source: Path, target: Path) -> None:
    """Copy a file through a bounded-memory temporary and atomically publish it."""
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path_for(target)
    try:
        with source.open("rb") as source_handle, tmp.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        shutil.copymode(source, tmp, follow_symlinks=False)
        replace_with_retry(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
