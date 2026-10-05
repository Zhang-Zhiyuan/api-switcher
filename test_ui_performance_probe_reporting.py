"""Probe failure reports must survive without constructing a native window."""
import json
from types import SimpleNamespace

import pytest

from tools.ui_performance_probe import _write_probe_report


@pytest.fixture
def args():
    return SimpleNamespace(scenario="nodes", count=1000, width=740, long_names=True)


def test_completed_report_validates_and_records_success(tmp_path, args):
    validated, errors = [], []
    output = tmp_path / "nested" / "probe"
    report = _write_probe_report(output, args, [{"phase": "open"}], errors, completed=True,
                                 validate=lambda: validated.append(True))
    assert validated == [True] and not errors
    assert report["success"] and report["completed"]
    assert json.loads(output.with_suffix(".json").read_text(encoding="utf-8")) == report


@pytest.mark.parametrize("reason", ["open timed out", "watchdog timeout", "ValueError: synthetic callback error", ""])
def test_incomplete_report_never_requires_unexecuted_final_state(tmp_path, args, reason):
    errors = [reason] if reason else []
    output = tmp_path / "partial"

    def unavailable():
        pytest.fail("check_all was never run; final-state validation must not execute")

    report = _write_probe_report(output, args, [], errors, completed=False, current_phase="open",
                                 progress={"rendered_rows": 700, "checked_nodes": 0}, validate=unavailable)
    assert not report["success"] and not report["completed"]
    assert report["errors"] == errors and errors
    assert report["current_phase"] == "open"
    assert report["progress"]["rendered_rows"] == 700
    assert json.loads(output.with_suffix(".json").read_text(encoding="utf-8")) == report


def test_failed_final_validation_is_reported_with_type_and_traceback(tmp_path, args, capsys):
    errors = []

    def invalid():
        raise AssertionError("synthetic count mismatch")

    report = _write_probe_report(tmp_path / "failed", args, [], errors, completed=True, validate=invalid)
    assert report["completed"] and not report["success"]
    assert errors == ["final validation failed: AssertionError: synthetic count mismatch"]
    assert "Traceback" in capsys.readouterr().err
    assert json.loads((tmp_path / "failed.json").read_text(encoding="utf-8"))["errors"] == errors


def test_existing_callback_failure_cannot_be_overwritten_by_success(tmp_path, args):
    errors = ["RuntimeError: synthetic earlier callback failure"]
    validated = []
    report = _write_probe_report(tmp_path / "failed", args, [], errors, completed=True,
                                 validate=lambda: validated.append(True))
    assert not validated and not report["success"]
    assert report["errors"] == errors


def test_report_write_failure_is_not_silently_accepted(tmp_path, args):
    parent_file = tmp_path / "file-not-directory"
    parent_file.write_text("synthetic", encoding="utf-8")
    with pytest.raises(OSError):
        _write_probe_report(parent_file / "failed", args, [], ["open timed out"], completed=False)
