from __future__ import annotations

import stat
from pathlib import Path

import release_check


def test_release_check_uses_external_system_pytest_tempdir():
    pytest_command = dict(release_check.CHECKS)["pytest"]
    workspace = Path(release_check.__file__).resolve().parent
    basetemp = release_check.PYTEST_BASETEMP.resolve()

    assert "-p" in pytest_command
    assert "no:cacheprovider" in pytest_command
    assert "--basetemp" in pytest_command
    assert release_check.PYTEST_BASETEMP.as_posix() in pytest_command
    assert basetemp.is_absolute()
    assert basetemp != workspace
    assert workspace not in basetemp.parents
    assert basetemp != workspace / "build" / "pytest-tmp"


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


def test_release_requirements_pin_every_direct_dependency_exactly():
    pins = release_check._release_dependency_pins()

    assert pins
    assert {"customtkinter", "PyInstaller", "pytest", "ruff", "PyYAML"} <= set(pins)
    assert all(version and not any(marker in version for marker in (">", "<", "~")) for version in pins.values())


def test_release_dependency_check_rejects_version_drift(monkeypatch, tmp_path):
    requirements = tmp_path / "requirements-release.txt"
    requirements.write_text("ExamplePackage==1.2.3\n", encoding="utf-8")
    monkeypatch.setattr(release_check, "RELEASE_REQUIREMENTS_PATH", requirements)
    monkeypatch.setattr(release_check.importlib.metadata, "version", lambda _name: "2.0.0")

    assert release_check.check_release_dependency_versions() is False


def test_release_dependency_check_accepts_pinned_versions(monkeypatch, tmp_path):
    requirements = tmp_path / "requirements-release.txt"
    requirements.write_text("ExamplePackage==1.2.3\n", encoding="utf-8")
    monkeypatch.setattr(release_check, "RELEASE_REQUIREMENTS_PATH", requirements)
    monkeypatch.setattr(release_check.importlib.metadata, "version", lambda _name: "1.2.3")

    assert release_check.check_release_dependency_versions() is True


def test_release_check_pytest_env_stops_git_parent_discovery(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        release_check,
        "PYTEST_BASETEMP",
        tmp_path / "system-temp" / "pytest",
    )
    monkeypatch.delenv(release_check.PYTEST_ISOLATION_ROOT_ENV, raising=False)
    inherited_names = {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "DEEPSEEK_API_KEY",
        "KIMI_API_KEY",
        "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY",
        "DASHSCOPE_API_KEY",
        "GEMINI_API_KEY",
        "ZHIPUAI_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }
    for name in inherited_names:
        monkeypatch.setenv(name, "must-not-reach-tests")

    env = release_check._command_env("pytest")

    expected = str(release_check.PYTEST_BASETEMP.resolve())
    assert env["TMP"] == expected
    assert env["TEMP"] == expected
    assert env["GIT_CEILING_DIRECTORIES"] == expected
    isolation_root = release_check.PYTEST_BASETEMP.resolve().parent / f"environment-{release_check.os.getpid()}"
    assert env["CODEX_HOME"] == str(isolation_root / "codex")
    assert env["CLAUDE_CONFIG_DIR"] == str(isolation_root / "claude")
    assert env["API_SWITCHER_DATA_DIR"] == str(isolation_root / "api-switcher-data")
    assert env["HOME"] == env["USERPROFILE"] == str(isolation_root / "home")
    assert env["APPDATA"] == str(isolation_root / "appdata" / "roaming")
    assert env["LOCALAPPDATA"] == str(isolation_root / "appdata" / "local")
    assert env["XDG_CONFIG_HOME"] == str(isolation_root / "xdg" / "config")
    assert env["XDG_DATA_HOME"] == str(isolation_root / "xdg" / "data")
    assert env["XDG_CACHE_HOME"] == str(isolation_root / "xdg" / "cache")
    assert env["API_SWITCHER_PORTABLE"] == "0"
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"
    assert env[release_check.PYTEST_ISOLATION_ROOT_ENV] == str(isolation_root)
    assert not ({name.casefold() for name in env} & {name.casefold() for name in inherited_names})
    assert "HOMEDRIVE" not in env
    assert "HOMEPATH" not in env


def test_run_command_cleans_pytest_environment_after_success(monkeypatch, tmp_path):
    basetemp = tmp_path / "release-temp" / "pytest"
    monkeypatch.setattr(release_check, "PYTEST_BASETEMP", basetemp)
    monkeypatch.delenv(release_check.PYTEST_ISOLATION_ROOT_ENV, raising=False)
    captured = {}

    class Result:
        returncode = 0

    def fake_run(_command, **kwargs):
        root = Path(kwargs["env"][release_check.PYTEST_ISOLATION_ROOT_ENV])
        captured["root"] = root
        assert root.is_dir()
        (root / "created-by-test.txt").write_text("isolated", encoding="utf-8")
        return Result()

    monkeypatch.setattr(release_check.subprocess, "run", fake_run)

    assert release_check.run_command("pytest", ["pytest"]) is True
    assert not captured["root"].exists()


def test_run_command_cleans_pytest_environment_after_failure(monkeypatch, tmp_path):
    basetemp = tmp_path / "release-temp" / "pytest"
    monkeypatch.setattr(release_check, "PYTEST_BASETEMP", basetemp)
    monkeypatch.delenv(release_check.PYTEST_ISOLATION_ROOT_ENV, raising=False)
    captured = {}

    class Result:
        returncode = 3

    def fake_run(_command, **kwargs):
        root = Path(kwargs["env"][release_check.PYTEST_ISOLATION_ROOT_ENV])
        captured["root"] = root
        assert root.is_dir()
        (root / "created-by-failed-test.txt").write_text("isolated", encoding="utf-8")
        return Result()

    monkeypatch.setattr(release_check.subprocess, "run", fake_run)

    assert release_check.run_command("pytest", ["pytest"]) is False
    assert not captured["root"].exists()


def test_run_command_cleans_pytest_environment_when_launch_raises(monkeypatch, tmp_path):
    basetemp = tmp_path / "release-temp" / "pytest"
    monkeypatch.setattr(release_check, "PYTEST_BASETEMP", basetemp)
    monkeypatch.delenv(release_check.PYTEST_ISOLATION_ROOT_ENV, raising=False)
    captured = {}

    def fail_run(_command, **kwargs):
        root = Path(kwargs["env"][release_check.PYTEST_ISOLATION_ROOT_ENV])
        captured["root"] = root
        assert root.is_dir()
        (root / "created-before-launch-error.txt").write_text("isolated", encoding="utf-8")
        raise OSError("pytest executable unavailable")

    monkeypatch.setattr(release_check.subprocess, "run", fail_run)

    assert release_check.run_command("pytest", ["pytest"]) is False
    assert not captured["root"].exists()


def test_cleanup_removes_only_safe_inactive_pytest_environment_dirs(monkeypatch, tmp_path):
    basetemp = tmp_path / "release-temp" / "pytest"
    parent = basetemp.parent
    stale = parent / "environment-12345"
    unexpected = parent / "environment-not-a-pid"
    active = parent / "environment-54321"
    for path in (stale, unexpected, active):
        path.mkdir(parents=True)
        (path / "marker.txt").write_text("keep-or-remove", encoding="utf-8")
    monkeypatch.setattr(release_check, "PYTEST_BASETEMP", basetemp)
    monkeypatch.setattr(release_check, "_process_id_is_running", lambda pid: pid == 54321)

    assert release_check.cleanup_pytest_isolation_directories() is True

    assert not stale.exists()
    assert unexpected.exists()
    assert active.exists()


def test_cleanup_intermediate_files_does_not_remove_external_pytest_basetemp(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    external_basetemp = tmp_path / "system-temp" / "pytest"
    marker = external_basetemp / "keep.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("active pytest temp", encoding="utf-8")
    (workspace / "build").mkdir(parents=True)
    (workspace / "build" / "work.txt").write_text("x", encoding="utf-8")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(release_check, "PYTEST_BASETEMP", external_basetemp)

    assert release_check.cleanup_intermediate_files() is True

    assert not (workspace / "build").exists()
    assert marker.read_text(encoding="utf-8") == "active pytest temp"


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


def test_check_artifacts_fails_without_release_exe(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(release_check, "APP_NAME", "ApiSwitcher")

    assert release_check.check_artifacts() is False


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
