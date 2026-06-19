from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import build_exe


def _lazy_project_targets(*roots: Path) -> set[str]:
    targets: set[str] = set()
    prefixes = ("core.", "models.", "ui.")
    for root in roots:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name not in {"LazyModule", "LazyAttribute"}:
                    continue
                target = node.args[0]
                if (
                    isinstance(target, ast.Constant)
                    and isinstance(target.value, str)
                    and target.value.startswith(prefixes)
                ):
                    targets.add(target.value)
    return targets


def test_tab_hidden_imports_cover_app_tab_specs():
    from ui import app

    tab_modules = {module_name for _label, _attr, module_name, _class_name, _eager in app.TAB_SPECS}

    assert tab_modules <= set(build_exe.UI_TAB_HIDDEN_IMPORTS)


def test_project_hidden_imports_include_core_sync_manager():
    assert "core.sync_manager" in build_exe._project_hidden_imports()
    assert "models.auto_continue" in build_exe._project_hidden_imports()
    assert "ui.dialogs.confirm_dialog" in build_exe._project_hidden_imports()


def test_project_hidden_imports_cover_all_project_modules():
    expected: set[str] = set()
    for package_name in ("core", "models", "ui"):
        package_dir = Path(package_name)
        for path in package_dir.rglob("*.py"):
            if path.name == "__init__.py" or "__pycache__" in path.parts:
                continue
            expected.add(".".join((package_name, *path.relative_to(package_dir).with_suffix("").parts)))

    assert expected
    assert expected <= set(build_exe._project_hidden_imports())


def test_project_hidden_imports_cover_lazy_project_targets():
    hidden_imports = set(build_exe._project_hidden_imports())
    lazy_targets = _lazy_project_targets(Path("core"), Path("ui"), Path("models"))

    assert lazy_targets
    assert lazy_targets <= hidden_imports


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


def test_create_spec_file_includes_project_core_hidden_imports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "core").mkdir()
    (tmp_path / "models").mkdir()
    (tmp_path / "ui" / "dialogs").mkdir(parents=True)
    (tmp_path / "core" / "sync_manager.py").write_text("", encoding="utf-8")
    (tmp_path / "models" / "auto_continue.py").write_text("", encoding="utf-8")
    (tmp_path / "ui" / "dialogs" / "confirm_dialog.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(build_exe, "SPEC_PATH", tmp_path / "ApiSwitcher.spec")

    build_exe.create_spec_file()

    spec_text = build_exe.SPEC_PATH.read_text(encoding="utf-8")
    assert "core.sync_manager" in spec_text
    assert "models.auto_continue" in spec_text
    assert "ui.dialogs.confirm_dialog" in spec_text


def test_create_spec_file_includes_current_project_hidden_imports(monkeypatch, tmp_path):
    monkeypatch.setattr(build_exe, "SPEC_PATH", tmp_path / "ApiSwitcher.spec")

    build_exe.create_spec_file()

    spec_text = build_exe.SPEC_PATH.read_text(encoding="utf-8")
    missing = [module for module in build_exe._project_hidden_imports() if module not in spec_text]
    assert missing == []


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
