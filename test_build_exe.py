from __future__ import annotations

import subprocess

import build_exe


def test_build_exe_reports_pyinstaller_failure(monkeypatch):
    def fail_build(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["PyInstaller"])

    monkeypatch.setattr(build_exe.subprocess, "check_call", fail_build)

    assert build_exe.build_exe() is False


def test_build_exe_reports_missing_artifact(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build_exe, "APP_NAME", "ApiSwitcher")
    monkeypatch.setattr(build_exe.subprocess, "check_call", lambda *args, **kwargs: 0)

    assert build_exe.build_exe() is False


def test_build_exe_main_propagates_build_failure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "main.py").write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr(build_exe, "check_pyinstaller", lambda: True)
    monkeypatch.setattr(build_exe, "create_spec_file", lambda: None)
    monkeypatch.setattr(build_exe, "build_exe", lambda: False)

    assert build_exe.main() == 1
