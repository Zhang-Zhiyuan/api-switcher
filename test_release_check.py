from __future__ import annotations

import stat

import release_check


def test_release_check_uses_project_local_pytest_tempdir():
    pytest_command = dict(release_check.CHECKS)["pytest"]

    assert "-p" in pytest_command
    assert "no:cacheprovider" in pytest_command
    assert "--basetemp" in pytest_command
    assert release_check.PYTEST_BASETEMP.as_posix() in pytest_command


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
