from __future__ import annotations

import json
import subprocess
import sys

import performance_probe


def test_interpreter_info_reports_free_threading_fields():
    info = performance_probe.interpreter_info()

    assert isinstance(info["executable"], str)
    assert "free_threaded_build" in info
    assert "py_gil_disabled" in info
    assert "gil_enabled" in info


def test_measure_imports_reports_success_and_failure():
    results = performance_probe.measure_imports(("json", "missing_api_switcher_probe_module"))

    assert results[0]["module"] == "json"
    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "error"
    assert results[1]["error"]


def test_thread_benchmarks_return_speedup_metrics():
    report = performance_probe.measure_thread_benchmarks(
        workers=2,
        io_tasks=2,
        io_delay=0.001,
        cpu_tasks=2,
        cpu_size=1000,
    )

    assert report["workers"] == 2
    assert report["io"]["tasks"] == 2
    assert report["cpu"]["tasks"] == 2
    assert report["io"]["threaded_ms"] >= 0
    assert report["cpu"]["sequential_ms"] >= 0


def test_cli_json_output_without_ui():
    result = subprocess.run(
        [
            sys.executable,
            "performance_probe.py",
            "--json",
            "--skip-imports",
            "--workers",
            "1",
            "--io-tasks",
            "1",
            "--io-delay",
            "0.001",
            "--cpu-tasks",
            "1",
            "--cpu-size",
            "1000",
        ],
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["ui"] == {"enabled": False}
    assert payload["benchmarks"]["workers"] == 1
