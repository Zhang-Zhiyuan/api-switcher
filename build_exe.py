"""Build API Switcher as a single Windows EXE with PyInstaller."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


APP_NAME = "API切换器"
SPEC_PATH = Path(f"{APP_NAME}.spec")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def check_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401

        print("PyInstaller is installed.", flush=True)
        return True
    except ImportError:
        print("PyInstaller is not installed. Installing...", flush=True)
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "pyinstaller"],
                stderr=subprocess.STDOUT,
            )
            return True
        except Exception as exc:
            print(f"Failed to install PyInstaller: {exc}", flush=True)
            return False


def create_spec_file() -> None:
    datas = []
    for source, target in [
        ("config", "config"),
        ("assets", "assets"),
        ("icon.ico", "."),
        ("icon.png", "."),
        ("PENDING_WORK.md", "."),
    ]:
        if Path(source).exists():
            datas.append((source, target))

    icon_line = "icon='icon.ico'," if Path("icon.ico").exists() else "icon=None,"

    spec_content = f"""# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas={datas!r},
    hiddenimports=[
        'customtkinter',
        'PIL',
        'PIL._tkinter_finder',
        'keyring.backends.Windows',
        'tomli_w',
        'tomli',
        'tomllib',
        'paramiko',
        'cryptography',
        'pystray',
        'ui.dialogs.close_choice_dialog',
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
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

    SPEC_PATH.write_text(spec_content, encoding="utf-8")
    print(f"Spec file written: {SPEC_PATH}", flush=True)


def build_exe() -> bool:
    print("\nStarting PyInstaller build...\n", flush=True)
    try:
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--clean",
                "--noconfirm",
                str(SPEC_PATH),
            ],
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Build failed: {exc}", flush=True)
        return False

    exe_path = Path("dist") / f"{APP_NAME}.exe"
    print(f"\nBuild complete: {exe_path.resolve()}", flush=True)
    return True


def main() -> int:
    print("=" * 80, flush=True)
    print("API Switcher build tool", flush=True)
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

    create_spec_file()
    return 0 if build_exe() else 1


if __name__ == "__main__":
    raise SystemExit(main())
