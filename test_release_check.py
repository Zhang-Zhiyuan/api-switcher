from __future__ import annotations

import stat
from pathlib import Path

import release_check


def test_release_check_uses_project_local_pytest_tempdir():
    pytest_command = dict(release_check.CHECKS)["pytest"]

    assert "-p" in pytest_command
    assert "no:cacheprovider" in pytest_command
    assert "--basetemp" in pytest_command
    assert release_check.PYTEST_BASETEMP.as_posix() in pytest_command


def test_runtime_dependencies_do_not_require_pywin32_for_free_threaded_python():
    assert "win32api" not in release_check.RUNTIME_DEPENDENCY_IMPORTS


def test_runtime_dependencies_use_the_interpreter_toml_reader():
    expected = "tomllib" if release_check.sys.version_info >= (3, 11) else "tomli"
    obsolete = "tomli" if expected == "tomllib" else "tomllib"

    assert expected in release_check.RUNTIME_DEPENDENCY_IMPORTS
    assert obsolete not in release_check.RUNTIME_DEPENDENCY_IMPORTS


def test_requirements_do_not_pin_pywin32():
    requirements = Path("requirements.txt").read_text(encoding="utf-8").lower()

    assert "pywin32" not in requirements


def test_release_check_pytest_env_stops_git_parent_discovery(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    env = release_check._command_env("pytest")

    expected = str((tmp_path / release_check.PYTEST_BASETEMP).resolve())
    assert env["TMP"] == expected
    assert env["TEMP"] == expected
    assert env["GIT_CEILING_DIRECTORIES"] == expected


def test_cleanup_intermediate_files_keeps_dist_and_storage(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for path in [
        tmp_path / "build" / "work.txt",
        tmp_path / ".pytest_cache" / "cache.txt",
        tmp_path / ".ruff_cache" / "cache.txt",
        tmp_path / "pkg" / "__pycache__" / "module.pyc",
        tmp_path / ".venv" / "Lib" / "site-packages" / "pkg" / "__pycache__" / "module.pyc",
        tmp_path / "data" / "embedded" / "__pycache__" / "module.pyc",
        tmp_path / "ApiSwitcher.spec",
        tmp_path / "dist" / "ApiSwitcher.exe",
        tmp_path / "storage" / "profiles.json",
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    readonly = tmp_path / "build" / "readonly.txt"
    readonly.write_text("x", encoding="utf-8")
    readonly.chmod(stat.S_IREAD)
    monkeypatch.setattr(release_check, "APP_NAME", "ApiSwitcher")

    assert release_check.cleanup_intermediate_files() is True

    assert not (tmp_path / "build").exists()
    assert not (tmp_path / ".pytest_cache").exists()
    assert not (tmp_path / ".ruff_cache").exists()
    assert not (tmp_path / "pkg" / "__pycache__").exists()
    assert (tmp_path / ".venv" / "Lib" / "site-packages" / "pkg" / "__pycache__").exists()
    assert (tmp_path / "data" / "embedded" / "__pycache__").exists()
    assert not (tmp_path / "ApiSwitcher.spec").exists()
    assert (tmp_path / "dist" / "ApiSwitcher.exe").exists()
    assert (tmp_path / "storage" / "profiles.json").exists()


def test_check_artifacts_passes_with_only_onefile_exe(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(release_check, "APP_NAME", "ApiSwitcher")
    exe_path = tmp_path / "dist" / "ApiSwitcher.exe"
    exe_path.parent.mkdir(parents=True)
    exe_path.write_bytes(b"exe")

    assert release_check.check_artifacts() is True


def test_check_artifacts_fails_when_stale_onedir_exists(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(release_check, "APP_NAME", "ApiSwitcher")
    onefile = tmp_path / "dist" / "ApiSwitcher.exe"
    onefile.parent.mkdir(parents=True)
    onefile.write_bytes(b"exe")
    stale_onedir = tmp_path / "dist" / "ApiSwitcher"
    stale_onedir.mkdir()
    (stale_onedir / "ApiSwitcher.exe").write_bytes(b"stale")

    assert release_check.check_artifacts() is False


def test_check_artifacts_fails_when_stale_zip_exists(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(release_check, "APP_NAME", "ApiSwitcher")
    onefile = tmp_path / "dist" / "ApiSwitcher.exe"
    onefile.parent.mkdir(parents=True)
    onefile.write_bytes(b"exe")
    (tmp_path / "dist" / "ApiSwitcher.zip").write_bytes(b"stale")

    assert release_check.check_artifacts() is False


def test_check_source_mojibake_passes_for_utf8_chinese(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ok.py").write_text('TEXT = "正在加载配置"\n', encoding="utf-8")

    assert release_check.check_source_mojibake() is True


def test_check_source_mojibake_fails_for_common_gbk_mojibake(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    bad_text = release_check._to_common_mojibake("正在加载配置")
    (tmp_path / "bad.py").write_text(f'TEXT = "{bad_text}"\n', encoding="utf-8")

    assert release_check.check_source_mojibake() is False


def test_source_checks_skip_generated_and_user_data_trees(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    bad_text = release_check._to_common_mojibake("正在加载配置")
    for relative in [
        "build/generated.py",
        "dist/bundled.py",
        "storage/profiles.json",
        "data/settings.json",
        ".venv/package.py",
    ]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'TEXT = "{bad_text}"\nthis is invalid python\n', encoding="utf-8")

    (tmp_path / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert release_check.check_source_mojibake() is True
    assert release_check.check_python_syntax() is True


def test_check_python_syntax_detects_untracked_source_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    assert release_check.check_python_syntax() is False


def test_git_diff_check_is_optional_outside_a_git_checkout(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(release_check.shutil, "which", lambda _name: None)

    assert release_check.check_git_diff() is True


def test_run_command_reports_missing_executable(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("missing command")

    monkeypatch.setattr(release_check.subprocess, "run", missing)

    assert release_check.run_command("missing", ["missing-tool"]) is False
