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
    calls = []
    monkeypatch.setattr(build_exe, "create_spec_file", lambda bundle_mode="onefile": calls.append(("spec", bundle_mode)))
    monkeypatch.setattr(
        build_exe,
        "build_exe",
        lambda bundle_mode="onefile", clean_intermediates=True: calls.append(
            ("build", bundle_mode, clean_intermediates)
        )
        or False,
    )

    assert build_exe.main([]) == 1
    assert calls == [("spec", "onefile"), ("build", "onefile", True)]


def test_build_exe_main_accepts_onedir_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "main.py").write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr(build_exe, "check_pyinstaller", lambda: True)
    calls = []
    monkeypatch.setattr(build_exe, "create_spec_file", lambda bundle_mode="onefile": calls.append(("spec", bundle_mode)))
    monkeypatch.setattr(
        build_exe,
        "build_exe",
        lambda bundle_mode="onefile", clean_intermediates=True: calls.append(
            ("build", bundle_mode, clean_intermediates)
        )
        or True,
    )

    assert build_exe.main(["--onedir"]) == 0
    assert calls == [("spec", "onedir"), ("build", "onedir", True)]


def test_build_exe_main_can_keep_intermediates(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "main.py").write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr(build_exe, "check_pyinstaller", lambda: True)
    calls = []
    monkeypatch.setattr(build_exe, "create_spec_file", lambda bundle_mode="onefile": calls.append(("spec", bundle_mode)))
    monkeypatch.setattr(
        build_exe,
        "build_exe",
        lambda bundle_mode="onefile", clean_intermediates=True: calls.append(
            ("build", bundle_mode, clean_intermediates)
        )
        or True,
    )

    assert build_exe.main(["--keep-intermediates"]) == 0
    assert calls == [("spec", "onefile"), ("build", "onefile", False)]


def test_create_spec_file_includes_lazy_tab_hidden_imports(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build_exe, "SPEC_PATH", tmp_path / "ApiSwitcher.spec")

    build_exe.create_spec_file()

    spec_text = build_exe.SPEC_PATH.read_text(encoding="utf-8")
    for module_name in build_exe.UI_TAB_HIDDEN_IMPORTS:
        assert module_name in spec_text


def test_create_spec_file_excludes_heavy_optional_modules(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build_exe, "SPEC_PATH", tmp_path / "ApiSwitcher.spec")

    build_exe.create_spec_file()

    spec_text = build_exe.SPEC_PATH.read_text(encoding="utf-8")
    for module_name in build_exe.EXCLUDED_MODULES:
        assert module_name in spec_text


def test_build_exe_onedir_checks_folder_artifact(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build_exe, "APP_NAME", "ApiSwitcher")
    monkeypatch.setattr(build_exe.subprocess, "check_call", lambda *args, **kwargs: 0)
    monkeypatch.setattr(build_exe, "smoke_test_exe", lambda *args, **kwargs: True)
    exe_path = tmp_path / "dist" / "ApiSwitcher" / "ApiSwitcher.exe"
    exe_path.parent.mkdir(parents=True)
    exe_path.write_bytes(b"exe")

    assert build_exe.build_exe("onedir") is True


def test_build_exe_onedir_removes_stale_onefile_artifact(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build_exe, "APP_NAME", "ApiSwitcher")
    monkeypatch.setattr(build_exe.subprocess, "check_call", lambda *args, **kwargs: 0)
    monkeypatch.setattr(build_exe, "smoke_test_exe", lambda *args, **kwargs: True)
    folder_exe = tmp_path / "dist" / "ApiSwitcher" / "ApiSwitcher.exe"
    folder_exe.parent.mkdir(parents=True)
    folder_exe.write_bytes(b"exe")
    stale_onefile = tmp_path / "dist" / "ApiSwitcher.exe"
    stale_onefile.write_bytes(b"stale")

    assert build_exe.build_exe("onedir") is True
    assert not stale_onefile.exists()


def test_build_exe_onefile_removes_stale_onedir_artifact(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build_exe, "APP_NAME", "ApiSwitcher")
    monkeypatch.setattr(build_exe.subprocess, "check_call", lambda *args, **kwargs: 0)
    monkeypatch.setattr(build_exe, "smoke_test_exe", lambda *args, **kwargs: True)
    onefile_exe = tmp_path / "dist" / "ApiSwitcher.exe"
    onefile_exe.parent.mkdir(parents=True)
    onefile_exe.write_bytes(b"exe")
    stale_onedir = tmp_path / "dist" / "ApiSwitcher"
    stale_onedir.mkdir()
    (stale_onedir / "ApiSwitcher.exe").write_bytes(b"stale")

    assert build_exe.build_exe("onefile") is True
    assert not stale_onedir.exists()


def test_build_exe_cleans_intermediate_files(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build_exe, "APP_NAME", "ApiSwitcher")
    monkeypatch.setattr(build_exe, "SPEC_PATH", tmp_path / "ApiSwitcher.spec")
    monkeypatch.setattr(build_exe.subprocess, "check_call", lambda *args, **kwargs: 0)
    monkeypatch.setattr(build_exe, "smoke_test_exe", lambda *args, **kwargs: True)
    onefile_exe = tmp_path / "dist" / "ApiSwitcher.exe"
    onefile_exe.parent.mkdir(parents=True)
    onefile_exe.write_bytes(b"exe")
    (tmp_path / "build" / "ApiSwitcher").mkdir(parents=True)
    build_exe.SPEC_PATH.write_text("# generated", encoding="utf-8")

    assert build_exe.build_exe("onefile") is True
    assert not (tmp_path / "build").exists()
    assert not build_exe.SPEC_PATH.exists()


def test_pyinstaller_env_prepends_conda_dll_dirs(monkeypatch, tmp_path):
    prefix = tmp_path / "conda"
    for relative in ("Library/bin", "DLLs", "bin"):
        (prefix / relative).mkdir(parents=True)
    monkeypatch.setenv("CONDA_PREFIX", str(prefix))
    monkeypatch.setenv("PATH", "ORIGINAL_PATH")
    monkeypatch.setattr(build_exe.sys, "prefix", str(prefix))
    monkeypatch.setattr(build_exe.sys, "base_prefix", str(prefix))

    env = build_exe._utf8_subprocess_env()

    path_parts = env["PATH"].split(build_exe.os.pathsep)
    assert path_parts[:3] == [
        str(prefix / "Library/bin"),
        str(prefix / "DLLs"),
        str(prefix / "bin"),
    ]
    assert path_parts[3] == "ORIGINAL_PATH"
