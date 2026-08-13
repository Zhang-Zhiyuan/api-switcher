import json
from pathlib import Path

import pytest

from core.auto_continue.base import (
    AutoContinueProvider,
    SettingsLoadStatus,
)
from core.auto_continue.manager import AutoContinueManager


class ReconciliationProvider(AutoContinueProvider):
    def __init__(self, root: Path):
        super().__init__("codex")
        self.root = root
        self.disable_calls = 0
        self.ensure_calls = []

    def get_config_dir(self):
        return self.root

    def get_hook_script_path(self):
        return self.root / "hook.ps1"

    def get_settings_path(self):
        return self.root / "auto_continue_settings.json"

    def is_hook_registered(self):
        return False

    def register_hook(self):
        pass

    def unregister_hook(self):
        pass

    def install_hook_script(self):
        pass

    def uninstall_hook_script(self):
        pass

    def ensure_current_installation(self, settings):
        self.ensure_calls.append(settings)

    def disable_managed_hooks_for_invalid_settings(self):
        self.disable_calls += 1


def _manager_for(provider, monkeypatch):
    manager = AutoContinueManager()
    monkeypatch.setattr(manager, "get_provider", lambda _name: provider)
    return manager


@pytest.mark.parametrize(
    "content",
    [
        "{not valid json",
        json.dumps({"enabled": True, "max_continuations": 101}),
        json.dumps(["not", "an", "object"]),
    ],
)
def test_startup_disables_only_confirmed_invalid_settings(
    tmp_path,
    monkeypatch,
    content,
):
    provider = ReconciliationProvider(tmp_path)
    provider.get_settings_path().write_text(content, encoding="utf-8")

    result = provider.load_settings_result()
    assert result.status is SettingsLoadStatus.INVALID
    assert result.settings is None

    manager = _manager_for(provider, monkeypatch)
    assert manager.reconcile_installation("codex") is True
    assert provider.disable_calls == 1
    assert provider.ensure_calls == []


def test_startup_preserves_hooks_when_settings_read_is_denied(
    tmp_path,
    monkeypatch,
    caplog,
):
    provider = ReconciliationProvider(tmp_path)
    settings_path = provider.get_settings_path()
    settings_path.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def deny_settings_read(path, *args, **kwargs):
        if path == settings_path:
            raise PermissionError("settings temporarily locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_settings_read)

    result = provider.load_settings_result()
    assert result.status is SettingsLoadStatus.IO_ERROR
    assert isinstance(result.error, PermissionError)
    # Backward-compatible callers still see None.
    assert provider.load_settings() is None

    manager = _manager_for(provider, monkeypatch)
    assert manager.reconcile_installation("codex") is False
    assert provider.disable_calls == 0
    assert provider.ensure_calls == []
    assert "preserving existing hooks" in caplog.text


def test_file_disappearing_during_read_is_treated_as_missing(
    tmp_path,
    monkeypatch,
):
    provider = ReconciliationProvider(tmp_path)
    settings_path = provider.get_settings_path()
    settings_path.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def disappear_before_read(path, *args, **kwargs):
        if path == settings_path:
            raise FileNotFoundError(str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", disappear_before_read)

    result = provider.load_settings_result()
    assert result.status is SettingsLoadStatus.MISSING

    manager = _manager_for(provider, monkeypatch)
    assert manager.reconcile_installation("codex") is True
    assert provider.disable_calls == 0
    assert provider.ensure_calls == []


def test_loaded_settings_are_reconciled_normally(tmp_path, monkeypatch):
    provider = ReconciliationProvider(tmp_path)
    provider.get_settings_path().write_text(
        json.dumps({"enabled": False, "git_auto_snapshot": False}),
        encoding="utf-8",
    )

    result = provider.load_settings_result()
    assert result.status is SettingsLoadStatus.LOADED
    assert result.settings is not None

    manager = _manager_for(provider, monkeypatch)
    assert manager.reconcile_installation("codex") is True
    assert provider.disable_calls == 0
    assert len(provider.ensure_calls) == 1
