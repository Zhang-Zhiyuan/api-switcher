"""Keep the test suite away from a developer's real API切换器 data."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path


_ORIGINAL_DATA_DIR = os.environ.get("API_SWITCHER_DATA_DIR")
_PYTEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="api-switcher-pytest-data-"))
os.environ["API_SWITCHER_DATA_DIR"] = str(_PYTEST_DATA_DIR)


def _cleanup_pytest_data_dir() -> None:
    if _ORIGINAL_DATA_DIR is None:
        os.environ.pop("API_SWITCHER_DATA_DIR", None)
    else:
        os.environ["API_SWITCHER_DATA_DIR"] = _ORIGINAL_DATA_DIR
    shutil.rmtree(_PYTEST_DATA_DIR, ignore_errors=True)


atexit.register(_cleanup_pytest_data_dir)
