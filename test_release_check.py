from __future__ import annotations

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


def test_check_artifacts_passes_with_only_onedir_exe(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(release_check, "APP_NAME", "ApiSwitcher")
    exe_path = tmp_path / "dist" / "ApiSwitcher" / "ApiSwitcher.exe"
    exe_path.parent.mkdir(parents=True)
    exe_path.write_bytes(b"exe")

    assert release_check.check_artifacts() is True


def test_check_artifacts_fails_when_stale_onefile_exists(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(release_check, "APP_NAME", "ApiSwitcher")
    onedir_exe = tmp_path / "dist" / "ApiSwitcher" / "ApiSwitcher.exe"
    onedir_exe.parent.mkdir(parents=True)
    onedir_exe.write_bytes(b"exe")
    stale_onefile = tmp_path / "dist" / "ApiSwitcher.exe"
    stale_onefile.write_bytes(b"stale")

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
