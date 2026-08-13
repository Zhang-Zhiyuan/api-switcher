"""Small helpers for deferring optional imports until first use."""

from __future__ import annotations

import importlib
import threading
from types import ModuleType
from typing import Any


class LazyModule:
    """Thread-safe proxy that imports a module only when an attribute is used."""

    def __init__(self, module_name: str):
        self._module_name = module_name
        self._module: ModuleType | None = None
        self._lock = threading.RLock()

    def _load(self) -> ModuleType:
        module = self._module
        if module is not None:
            return module
        with self._lock:
            if self._module is None:
                self._module = importlib.import_module(self._module_name)
            return self._module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


class LazyAttribute:
    """Thread-safe proxy for one attribute inside a lazily imported module."""

    def __init__(self, module_name: str, attr_name: str):
        self._module = LazyModule(module_name)
        self._attr_name = attr_name
        self._attr: Any | None = None
        self._lock = threading.RLock()

    def _load(self) -> Any:
        attr = self._attr
        if attr is not None:
            return attr
        with self._lock:
            if self._attr is None:
                self._attr = getattr(self._module, self._attr_name)
            return self._attr

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._load()(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)
