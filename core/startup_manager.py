"""Windows startup integration for API Switcher."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows platforms
    winreg = None


REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VALUE_NAME = "APISwitcher"
START_MINIMIZED_ARGS = ("--minimized",)


@dataclass(frozen=True)
class StartupStatus:
    supported: bool
    enabled: bool
    registered_command: str | None
    expected_command: str
    matches_expected: bool
    error: str = ""


def is_supported() -> bool:
    """Return True when per-user startup registration is supported."""
    return sys.platform == "win32" and winreg is not None


def _pythonw_path() -> Path:
    executable = Path(sys.executable).resolve()
    if executable.name.lower() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
    return executable


def _main_script_path() -> Path:
    return Path(__file__).resolve().parent.parent / "main.py"


def build_startup_command(start_minimized: bool = True) -> str:
    """Build the command written to HKCU Run."""
    if getattr(sys, "frozen", False):
        args = [str(Path(sys.executable).resolve())]
    else:
        args = [str(_pythonw_path()), str(_main_script_path())]
    if start_minimized:
        args.extend(START_MINIMIZED_ARGS)
    return subprocess.list2cmdline(args)


def _normalize_command(command: str | None) -> str:
    return " ".join(str(command or "").strip().split()).casefold()


def commands_match(left: str | None, right: str | None) -> bool:
    return bool(left and right and _normalize_command(left) == _normalize_command(right))


def get_registered_command() -> str | None:
    """Return the current HKCU Run command, or None when it is not registered."""
    if not is_supported():
        return None
    assert winreg is not None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _value_type = winreg.QueryValueEx(key, REG_VALUE_NAME)
    except FileNotFoundError:
        return None
    return str(value)


def enable_startup(command: str | None = None) -> str:
    """Enable per-user startup and return the registered command."""
    if not is_supported():
        raise RuntimeError("当前系统不支持开机自启动设置")
    assert winreg is not None
    command = command or build_startup_command(start_minimized=True)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, REG_VALUE_NAME, 0, winreg.REG_SZ, command)
    return command


def disable_startup() -> bool:
    """Disable per-user startup. Returns True when a value was removed."""
    if not is_supported():
        return False
    assert winreg is not None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, REG_VALUE_NAME)
        return True
    except FileNotFoundError:
        return False


def set_startup_enabled(enabled: bool) -> StartupStatus:
    if enabled:
        enable_startup()
    else:
        disable_startup()
    return get_startup_status()


def get_startup_status() -> StartupStatus:
    expected_command = build_startup_command(start_minimized=True)
    if not is_supported():
        return StartupStatus(
            supported=False,
            enabled=False,
            registered_command=None,
            expected_command=expected_command,
            matches_expected=False,
            error="当前系统不支持开机自启动设置",
        )

    try:
        registered_command = get_registered_command()
    except OSError as e:
        return StartupStatus(
            supported=True,
            enabled=False,
            registered_command=None,
            expected_command=expected_command,
            matches_expected=False,
            error=str(e),
        )

    return StartupStatus(
        supported=True,
        enabled=bool(registered_command),
        registered_command=registered_command,
        expected_command=expected_command,
        matches_expected=commands_match(registered_command, expected_command),
    )
