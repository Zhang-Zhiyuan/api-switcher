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

        print("PyInstaller is installed.")
        return True
    except ImportError:
        print("PyInstaller is not installed. Installing...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
            return True
        except Exception as exc:
            print(f"Failed to install PyInstaller: {exc}")
            return False


def create_spec_file() -> None:
    datas = []
    for source, target in [
        ("config", "config"),
        ("assets", "assets"),
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
    print(f"Spec file written: {SPEC_PATH}")


def build_exe() -> bool:
    print("\nStarting PyInstaller build...\n")
    try:
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--clean",
                "--noconfirm",
                str(SPEC_PATH),
            ]
        )
    except subprocess.CalledProcessError as exc:
        print(f"Build failed: {exc}")
        return False

    exe_path = Path("dist") / f"{APP_NAME}.exe"
    print(f"\nBuild complete: {exe_path.resolve()}")
    return True


def main() -> None:
    print("=" * 80)
    print("API Switcher build tool")
    print("=" * 80)

    if not Path("main.py").exists():
        print("main.py was not found. Run this script from the project root.")
        return

    if not Path("icon.ico").exists():
        print("icon.ico not found. Creating icon...")
        try:
            import create_icon

            create_icon.create_icon()
        except Exception as exc:
            print(f"Icon creation failed, continuing without it: {exc}")

    if not check_pyinstaller():
        return

    create_spec_file()
    build_exe()


if __name__ == "__main__":
    main()
