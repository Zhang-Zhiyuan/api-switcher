"""Build API Switcher with PyInstaller."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


APP_NAME = "API切换器"
SPEC_PATH = Path(f"{APP_NAME}.spec")
DEFAULT_BUNDLE_MODE = "onefile"
SUPPORTED_BUNDLE_MODES = {"onefile", "onedir"}
UI_TAB_HIDDEN_IMPORTS = [
    "ui.tabs.claude_tab",
    "ui.tabs.codex_tab",
    "ui.tabs.env_tab",
    "ui.tabs.browser_tab",
    "ui.tabs.session_migration_tab",
    "ui.tabs.ssh_tab",
    "ui.tabs.local_proxy_tab",
    "ui.tabs.common_tab",
    "ui.tabs.usage_stats_tab",
    "ui.tabs.backup_tab",
    "ui.tabs.log_viewer_tab",
]
EXCLUDED_MODULES = [
    "IPython",
    "jupyter",
    "matplotlib",
    "notebook",
    "numpy",
    "pandas",
    "pygame",
    "scipy",
]


def _collect_project_modules(package_dir: Path, package_name: str) -> list[str]:
    """Return import names for project modules that PyInstaller cannot infer from lazy imports."""
    if not package_dir.exists():
        return []

    modules: list[str] = []
    for path in package_dir.rglob("*.py"):
        if path.name == "__init__.py" or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_dir).with_suffix("")
        modules.append(".".join((package_name, *relative.parts)))
    return sorted(set(modules))


def _project_hidden_imports() -> list[str]:
    return _collect_project_modules(Path("core"), "core")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _utf8_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    dll_dirs = [str(path) for path in _pyinstaller_dll_search_dirs()]
    if dll_dirs:
        current_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(dll_dirs + ([current_path] if current_path else []))
    return env


def _pyinstaller_dll_search_dirs() -> list[Path]:
    """Return Python/Conda DLL directories PyInstaller needs on PATH."""
    candidates: list[Path] = []
    prefixes = [
        os.environ.get("CONDA_PREFIX"),
        sys.prefix,
        sys.base_prefix,
    ]
    for raw_prefix in prefixes:
        if not raw_prefix:
            continue
        prefix = Path(raw_prefix)
        for relative in ("Library/bin", "DLLs", "bin"):
            path = prefix / relative
            if path.is_dir() and path not in candidates:
                candidates.append(path)
    return candidates


def check_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401

        print("PyInstaller is installed.", flush=True)
        return True
    except ImportError:
        print("PyInstaller is not installed. Installing...", flush=True)
        try:
            subprocess.check_call(
                [sys.executable, "-X", "utf8", "-m", "pip", "install", "pyinstaller"],
                stderr=subprocess.STDOUT,
                env=_utf8_subprocess_env(),
            )
            return True
        except Exception as exc:
            print(f"Failed to install PyInstaller: {exc}", flush=True)
            return False


def create_spec_file(bundle_mode: str = DEFAULT_BUNDLE_MODE) -> None:
    if bundle_mode not in SUPPORTED_BUNDLE_MODES:
        raise ValueError(f"Unsupported bundle mode: {bundle_mode}")

    datas = []
    for source, target in [
        ("config", "config"),
        ("assets", "assets"),
        ("icon.ico", "."),
        ("icon.png", "."),
    ]:
        if Path(source).exists():
            datas.append((source, target))

    icon_line = "icon='icon.ico'," if Path("icon.ico").exists() else "icon=None,"

    if bundle_mode == "onefile":
        output_block = f"""exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name={APP_NAME!r},
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    {icon_line}
)
"""
    else:
        output_block = f"""exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name={APP_NAME!r},
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    {icon_line}
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name={APP_NAME!r},
)
"""

    hiddenimports = [
        "customtkinter",
        "PIL",
        "PIL._tkinter_finder",
        "keyring.backends.Windows",
        "tomli_w",
        "tomli",
        "tomllib",
        "paramiko",
        "cryptography",
        "pystray",
        "ui.dialogs.close_choice_dialog",
        "ui.dialogs.proxy_quality_dialog",
        "ui.widgets.proxy_quality_panel",
        *UI_TAB_HIDDEN_IMPORTS,
        *_project_hidden_imports(),
    ]

    spec_content = f"""# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas={datas!r},
    hiddenimports={hiddenimports!r},
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes={EXCLUDED_MODULES!r},
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

{output_block}
"""

    SPEC_PATH.write_text(spec_content, encoding="utf-8")
    print(f"Spec file written: {SPEC_PATH} ({bundle_mode})", flush=True)


def _artifact_path(bundle_mode: str) -> Path:
    if bundle_mode == "onedir":
        return Path("dist") / APP_NAME / f"{APP_NAME}.exe"
    if bundle_mode == "onefile":
        return Path("dist") / f"{APP_NAME}.exe"
    raise ValueError(f"Unsupported bundle mode: {bundle_mode}")


def _stale_artifact_path(bundle_mode: str) -> Path:
    if bundle_mode == "onedir":
        return Path("dist") / f"{APP_NAME}.exe"
    if bundle_mode == "onefile":
        return Path("dist") / APP_NAME
    raise ValueError(f"Unsupported bundle mode: {bundle_mode}")


def _remove_stale_artifact(bundle_mode: str) -> bool:
    stale_path = _stale_artifact_path(bundle_mode)
    if not stale_path.exists():
        return True

    dist_dir = Path("dist").resolve()
    resolved = stale_path.resolve()
    if resolved == dist_dir or dist_dir not in resolved.parents:
        print(f"Refusing to remove unexpected artifact outside dist: {resolved}", flush=True)
        return False

    try:
        if stale_path.is_dir():
            _rmtree_with_retry(stale_path)
        else:
            _unlink_with_retry(stale_path)
    except Exception as exc:
        print(f"Build cleanup failed: could not remove stale artifact {resolved}: {exc}", flush=True)
        return False

    print(f"Removed stale artifact: {resolved}", flush=True)
    return True


def _is_safe_workspace_path(path: Path) -> bool:
    workspace = Path.cwd().resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved != workspace and workspace in resolved.parents


def clean_intermediate_files() -> bool:
    """Remove generated build files that are not release artifacts."""
    paths = [Path("build"), SPEC_PATH]
    ok = True
    for path in paths:
        if not path.exists():
            continue
        if not _is_safe_workspace_path(path):
            print(f"Refusing to remove unexpected intermediate path: {path}", flush=True)
            ok = False
            continue
        try:
            if path.is_dir():
                _rmtree_with_retry(path)
            else:
                _unlink_with_retry(path)
            print(f"Removed intermediate: {path}", flush=True)
        except Exception as exc:
            print(f"Failed to remove intermediate {path}: {exc}", flush=True)
            ok = False
    return ok


def _rmtree_with_retry(path: Path) -> None:
    shutil.rmtree(path, onexc=_make_writable_and_retry)


def _unlink_with_retry(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def _make_writable_and_retry(function, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def build_exe(bundle_mode: str = DEFAULT_BUNDLE_MODE, clean_intermediates: bool = True) -> bool:
    print("\nStarting PyInstaller build...\n", flush=True)
    try:
        subprocess.check_call(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "PyInstaller",
                "--clean",
                "--noconfirm",
                str(SPEC_PATH),
            ],
            stderr=subprocess.STDOUT,
            env=_utf8_subprocess_env(),
        )
    except subprocess.CalledProcessError as exc:
        print(f"Build failed: {exc}", flush=True)
        return False

    exe_path = _artifact_path(bundle_mode)
    if not exe_path.is_file() or exe_path.stat().st_size <= 0:
        print(f"Build failed: expected EXE was not created: {exe_path.resolve()}", flush=True)
        return False

    if not _remove_stale_artifact(bundle_mode):
        return False

    if not smoke_test_exe(exe_path):
        return False

    if clean_intermediates and not clean_intermediate_files():
        return False

    print(f"\nBuild complete: {exe_path.resolve()}", flush=True)
    return True


def smoke_test_exe(exe_path: Path, timeout_seconds: float = 8.0) -> bool:
    """Launch the packaged app briefly and fail fast if it exits with an error."""
    if os.name != "nt":
        return True
    startupinfo = None
    creationflags = 0
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            [str(exe_path.resolve())],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except Exception as exc:
        print(f"Smoke test failed: could not launch {exe_path}: {exc}", flush=True)
        return False

    try:
        output, _ = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        print("Smoke test passed: packaged app stayed running.", flush=True)
        return True

    if proc.returncode == 0:
        print("Smoke test passed: packaged app exited cleanly.", flush=True)
        return True

    print(f"Smoke test failed: packaged app exited with code {proc.returncode}.", flush=True)
    if output:
        print(output[-4000:], flush=True)
    return False


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a smoke-test process and any PyInstaller child process."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.terminate()
    for _ in range(10):
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        proc.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build API Switcher with PyInstaller.")
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Keep PyInstaller build/ and the generated spec file for debugging.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--onedir",
        action="store_true",
        help="Build a dist/API切换器 folder instead of the default single EXE.",
    )
    mode_group.add_argument(
        "--onefile",
        action="store_true",
        help="Build the default single-file dist/API切换器.exe.",
    )
    args = parser.parse_args(argv)
    bundle_mode = "onedir" if args.onedir else DEFAULT_BUNDLE_MODE

    print("=" * 80, flush=True)
    print("API Switcher build tool", flush=True)
    print(f"Bundle mode: {bundle_mode}", flush=True)
    print("=" * 80, flush=True)

    if not Path("main.py").exists():
        print("main.py was not found. Run this script from the project root.", flush=True)
        return 2

    if not Path("icon.ico").exists():
        print("icon.ico not found. Creating icon...", flush=True)
        try:
            import create_icon

            create_icon.create_icon()
        except Exception as exc:
            print(f"Icon creation failed, continuing without it: {exc}", flush=True)

    if not check_pyinstaller():
        return 1

    create_spec_file(bundle_mode)
    return 0 if build_exe(bundle_mode, clean_intermediates=not args.keep_intermediates) else 1


if __name__ == "__main__":
    raise SystemExit(main())
