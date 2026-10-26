from __future__ import annotations

import copy
import hashlib
import threading
from pathlib import Path
from typing import Any


CACHE_MISS = object()
_FileSignature = tuple[str, int | None, int | None] | tuple[str, bytes]


class FileValueCache:
    """Cache parsed local files by metadata or the supplied source content."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, tuple[_FileSignature, Any]] = {}

    def get(self, path: Path, *, content: str | None = None) -> Any:
        key, signature = self._signature(path, content=content)
        with self._lock:
            cached = self._values.get(key)
            if cached and cached[0] == signature:
                return copy.deepcopy(cached[1])
        return CACHE_MISS

    def set(self, path: Path, value: Any, *, content: str | None = None) -> None:
        key, signature = self._signature(path, content=content)
        with self._lock:
            self._values[key] = (signature, copy.deepcopy(value))

    def clear(self, path: Path | None = None) -> None:
        with self._lock:
            if path is None:
                self._values.clear()
                return
            key, _signature = self._signature(path)
            self._values.pop(key, None)

    @staticmethod
    def _signature(path: Path, *, content: str | None = None) -> tuple[str, _FileSignature]:
        path_key = str(path.resolve(strict=False))
        if content is not None:
            # Tie parsed credentials to the exact text read, even when rapid
            # external writes share a timestamp or happen during parsing.
            return path_key, (path_key, hashlib.sha256(content.encode("utf-8")).digest())
        try:
            stat = path.stat()
            return path_key, (path_key, int(stat.st_mtime_ns), int(stat.st_size))
        except FileNotFoundError:
            return path_key, (path_key, None, None)
        except OSError:
            return path_key, (path_key, None, None)
