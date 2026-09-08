"""Keep the test suite away from a developer's real API切换器 data."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest


_ORIGINAL_DATA_DIR = os.environ.get("API_SWITCHER_DATA_DIR")
_PYTEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="api-switcher-pytest-data-"))
os.environ["API_SWITCHER_DATA_DIR"] = str(_PYTEST_DATA_DIR)

# A crashed Git-snapshot test must not turn the shared Windows temp root into
# an ancestor repository for every later ``tmp_path`` case.  Keep repository
# discovery inside each test directory while preserving any caller-supplied
# ceilings (for example release_check's own isolated root).
_SYSTEM_TEMP_ROOT = str(Path(tempfile.gettempdir()).resolve())
_git_ceilings = [
    value
    for value in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep)
    if value
]
if os.path.normcase(_SYSTEM_TEMP_ROOT) not in {
    os.path.normcase(os.path.abspath(value)) for value in _git_ceilings
}:
    _git_ceilings.append(_SYSTEM_TEMP_ROOT)
os.environ["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(_git_ceilings)


def _cleanup_pytest_data_dir() -> None:
    if _ORIGINAL_DATA_DIR is None:
        os.environ.pop("API_SWITCHER_DATA_DIR", None)
    else:
        os.environ["API_SWITCHER_DATA_DIR"] = _ORIGINAL_DATA_DIR
    shutil.rmtree(_PYTEST_DATA_DIR, ignore_errors=True)


atexit.register(_cleanup_pytest_data_dir)


@pytest.fixture(scope="session")
def tk_root():
    """One Tk interpreter for routing UI tests; use Toplevels per test.

    Repeatedly destroying/recreating CTk roots while global font/scaling caches
    remain alive can leave Tcl initialization unusable on Windows.
    """
    import customtkinter as ctk
    import tkinter
    import sys

    appearance = ctk.get_appearance_mode()
    ctk.set_appearance_mode("dark")
    try:
        root = ctk.CTk()
    except tkinter.TclError as exc:
        ctk.set_appearance_mode(appearance)
        if sys.platform == "win32":
            raise  # A broken Tcl interpreter must not silently skip release coverage.
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()
    ctk.set_appearance_mode(appearance)
