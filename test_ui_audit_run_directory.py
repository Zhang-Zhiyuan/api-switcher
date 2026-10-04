import argparse
from pathlib import Path

import pytest

from tools.ui_visual_audit import audit_run_directory, audit_run_id


@pytest.mark.parametrize("value", ["round1", "CON", "LPT1", "a" * 64, "review-20261004_2"])
def test_run_ids_create_a_single_safe_child(value):
    assert audit_run_id(value) == value
    result = audit_run_directory(Path("dist"), value)
    assert result.parent == Path("dist")
    assert result.name == "run-" + value


@pytest.mark.parametrize("value", ["", ".", "..", "../other", "a/b", "a\\b", "C:other", "a.", "a ", "a" * 65])
def test_run_ids_reject_paths_and_windows_trailing_dots(value):
    with pytest.raises(argparse.ArgumentTypeError):
        audit_run_id(value)


def test_default_keeps_original_output_location():
    assert audit_run_directory(Path("build"), None) == Path("build")
