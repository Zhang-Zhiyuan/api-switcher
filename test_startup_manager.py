from __future__ import annotations

from pathlib import Path

from core import startup_manager
from main import parse_args


class _FakeKey:
    def __init__(self, registry):
        self.registry = registry

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict[str, str] = {}

    def OpenKey(self, root, path, reserved=0, access=0):  # noqa: N802
        if path != startup_manager.REG_RUN_KEY:
            raise FileNotFoundError(path)
        return _FakeKey(self)

    def CreateKeyEx(self, root, path, reserved=0, access=0):  # noqa: N802
        if path != startup_manager.REG_RUN_KEY:
            raise FileNotFoundError(path)
        return _FakeKey(self)

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, value_type, value):  # noqa: N802
        self.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_startup_enable_disable_uses_current_user_run_key(monkeypatch):
    fake_winreg = _FakeWinreg()
    command = r'"C:\Tools\API切换器\API切换器.exe" --minimized'

    monkeypatch.setattr(startup_manager, "winreg", fake_winreg)
    monkeypatch.setattr(startup_manager.sys, "platform", "win32")
    monkeypatch.setattr(startup_manager, "build_startup_command", lambda start_minimized=True: command)

    status = startup_manager.get_startup_status()
    assert status.supported is True
    assert status.enabled is False

    enabled = startup_manager.set_startup_enabled(True)
    assert enabled.enabled is True
    assert enabled.matches_expected is True
    assert fake_winreg.values[startup_manager.REG_VALUE_NAME] == command

    disabled = startup_manager.set_startup_enabled(False)
    assert disabled.enabled is False
    assert startup_manager.REG_VALUE_NAME not in fake_winreg.values


def test_build_startup_command_for_source_run_includes_minimized(monkeypatch, tmp_path):
    python_exe = tmp_path / "python.exe"
    python_exe.write_text("", encoding="utf-8")
    pythonw_exe = tmp_path / "pythonw.exe"
    pythonw_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(startup_manager.sys, "executable", str(python_exe))
    monkeypatch.setattr(startup_manager.sys, "frozen", False, raising=False)

    command = startup_manager.build_startup_command()

    assert str(pythonw_exe) in command
    assert str(Path("main.py")) in command
    assert "--minimized" in command


def test_main_start_minimized_aliases():
    assert parse_args(["--minimized"]).start_minimized is True
    assert parse_args(["--start-minimized"]).start_minimized is True
    assert parse_args(["--tray"]).start_minimized is True
    assert parse_args([]).start_minimized is False
