from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any


CACHE_MISS = object()


class FileValueCache:
    """Small mtime/size based cache for parsed local files."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, tuple[tuple[str, int | None, int | None], Any]] = {}

    def get(self, path: Path) -> Any:
        key, signature = self._signature(path)
        with self._lock:
            cached = self._values.get(key)
            if cached and cached[0] == signature:
                return copy.deepcopy(cached[1])
        return CACHE_MISS

    def set(self, path: Path, value: Any) -> None:
        key, signature = self._signature(path)
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
    def _signature(path: Path) -> tuple[str, tuple[str, int | None, int | None]]:
        path_key = str(path.resolve(strict=False))
        try:
            stat = path.stat()
            return path_key, (path_key, int(stat.st_mtime_ns), int(stat.st_size))
        except FileNotFoundError:
            return path_key, (path_key, None, None)
        except OSError:
            return path_key, (path_key, None, None)
