import json

import pytest

from core.auto_continue.codex_provider import (
    AUTO_CONTINUE_HOOK_TIMEOUT_SECONDS,
    ERROR_RECOVERY_HOOK_TIMEOUT_SECONDS,
    CodexProvider,
)
from core.auto_continue.manager import AutoContinueManager
from models.auto_continue import AutoContinueSettings


def _event_hooks(data: dict, event_name: str) -> list[dict]:
    result = []
    for item in data.get("hooks", {}).get(event_name, []):
        if isinstance(item, dict) and item.get("command"):
            result.append(item)
        elif isinstance(item, dict):
            result.extend(
                hook
                for hook in item.get("hooks", [])
                if isinstance(hook, dict) and hook.get("command")
            )
    legacy = data.get(event_name)
    if isinstance(legacy, dict) and legacy.get("command"):
        result.append(legacy)
    elif isinstance(legacy, dict):
        result.extend(
            hook
            for hook in legacy.get("hooks", [])
            if isinstance(hook, dict) and hook.get("command")
        )
    elif isinstance(legacy, list):
        for item in legacy:
            if isinstance(item, dict) and item.get("command"):
                result.append(item)
            elif isinstance(item, dict):
                result.extend(
                    hook
                    for hook in item.get("hooks", [])
                    if isinstance(hook, dict) and hook.get("command")
                )
    return result


def _assert_exact_hook(hook: dict, expected: dict) -> None:
    assert hook["type"] == "command"
    assert hook["command"] == expected["command"]
    assert hook["timeout"] == expected["timeout"]


def test_git_only_reconciliation_upgrades_owned_hooks_and_preserves_user_hooks(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    provider = CodexProvider()
    settings = AutoContinueSettings(
        enabled=False,
        training_auto_continue_enabled=False,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
        error_recovery_enabled=True,
    )
    provider.save_settings(settings)

    provider.get_hook_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_hook_script_path().write_text("old main script", encoding="utf-8")
    provider.get_error_recovery_script_path().write_text("old recovery script", encoding="utf-8")
    provider.get_config_toml_path().write_text(
        "[features]\nhooks = true\n",
        encoding="utf-8",
    )

    # Older builds escaped path separators before json.dumps and registered a
    # Git-only Stop hook. Both recovery events also used the unsafe 10s timeout.
    old_auto_path = str(provider.get_hook_script_path()).replace("\\", "\\\\")
    old_recovery_path = str(provider.get_error_recovery_script_path()).replace("\\", "\\\\")
    old_auto_command = (
        'powershell.exe -NoProfile -ExecutionPolicy Bypass '
        f'-File "{old_auto_path}"'
    )
    old_recovery_command = (
        'powershell.exe -NoProfile -ExecutionPolicy Bypass '
        f'-File "{old_recovery_path}"'
    )
    user_stop = {
        "matcher": "user-owned",
        "customField": {"keep": True},
        "hooks": [{
            "type": "command",
            "command": 'powershell.exe -File "D:\\user\\auto_continue_stop.ps1"',
            "timeout": 7,
        }],
    }
    relative_user_stop = {
        "hooks": [{
            "type": "command",
            "command": "powershell.exe -File auto_continue_stop.ps1",
            "timeout": 8,
        }],
    }
    provider.get_hooks_json_path().write_text(
        json.dumps({
            "hooks": {
                "Stop": [
                    user_stop,
                    relative_user_stop,
                    {"hooks": [{
                        "type": "command",
                        "command": old_auto_command,
                        "timeout": 10,
                    }]},
                ],
                "UserPromptSubmit": [{"hooks": [{
                    "type": "command",
                    "command": old_auto_command,
                    "timeout": 10,
                }]}],
                "SessionStart": [{"hooks": [{
                    "type": "prompt",
                    "command": old_auto_command,
                    "timeout": AUTO_CONTINUE_HOOK_TIMEOUT_SECONDS,
                }]}],
                "Error": [{"hooks": [{
                    "type": "command",
                    "command": old_recovery_command,
                    "timeout": 10,
                }]}],
                "ResponseError": [{"hooks": [{
                    "type": "command",
                    "command": old_recovery_command,
                    "timeout": 10,
                }]}],
            },
        }),
        encoding="utf-8",
    )

    assert not provider.is_hook_registered()
    assert not provider.is_error_recovery_installed()
    assert provider.ensure_current_installation(settings) is True

    installed = json.loads(provider.get_hooks_json_path().read_text(encoding="utf-8"))
    auto_expected = provider._auto_continue_hook_definition()
    recovery_expected = provider._error_recovery_hook_definition()

    # A Git-only installation has no Stop hook owned by API Switcher. The
    # unrelated absolute-path user hook and its group metadata survive exactly.
    assert installed["hooks"]["Stop"] == [user_stop, relative_user_stop]
    for event_name in ("UserPromptSubmit", "SessionStart"):
        hooks = _event_hooks(installed, event_name)
        assert len(hooks) == 1
        _assert_exact_hook(hooks[0], auto_expected)
        assert hooks[0]["timeout"] == AUTO_CONTINUE_HOOK_TIMEOUT_SECONDS
    for event_name in provider.ERROR_RECOVERY_EVENTS:
        hooks = _event_hooks(installed, event_name)
        assert len(hooks) == 1
        _assert_exact_hook(hooks[0], recovery_expected)
        assert hooks[0]["timeout"] == ERROR_RECOVERY_HOOK_TIMEOUT_SECONDS

    assert provider.is_hook_registered()
    assert provider.is_error_recovery_installed()
    assert provider.get_hook_script_path().read_text(encoding="utf-8-sig") == provider._render_hook_script(settings)

    tracked_paths = [
        provider.get_hook_script_path(),
        provider.get_error_recovery_script_path(),
        provider.get_hooks_json_path(),
        provider.get_config_toml_path(),
        provider.get_hooks_feature_state_path(),
    ]
    before = {path: path.read_bytes() if path.exists() else None for path in tracked_paths}
    assert provider.ensure_current_installation(settings) is False
    after = {path: path.read_bytes() if path.exists() else None for path in tracked_paths}
    assert after == before


def test_reconciliation_rolls_back_both_hook_families_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    provider = CodexProvider()
    settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
        error_recovery_enabled=True,
    )
    provider.save_settings(settings)
    provider.get_hooks_json_path().write_text(
        json.dumps({
            "hooks": {
                "Stop": [{
                    "matcher": "user-owned",
                    "hooks": [{"command": "user-hook.exe", "timeout": 9}],
                }],
            },
        }),
        encoding="utf-8",
    )
    provider.get_config_toml_path().write_text(
        "[features]\nhooks = false\n",
        encoding="utf-8",
    )

    tracked_paths = [
        provider.get_hook_script_path(),
        provider.get_error_recovery_script_path(),
        provider.get_hooks_json_path(),
        provider.get_config_toml_path(),
        provider.get_hooks_feature_state_path(),
    ]
    before = {path: path.read_bytes() if path.exists() else None for path in tracked_paths}

    def fail_recovery_install():
        provider.get_error_recovery_script_path().write_text("partial", encoding="utf-8")
        provider.get_hooks_json_path().write_text("{}", encoding="utf-8")
        raise OSError("simulated recovery write failure")

    monkeypatch.setattr(provider, "install_error_recovery", fail_recovery_install)
    with pytest.raises(RuntimeError, match="simulated recovery write failure"):
        provider.ensure_current_installation(settings)

    after = {path: path.read_bytes() if path.exists() else None for path in tracked_paths}
    assert after == before


def test_manager_repair_unregisters_when_no_main_hook_feature_is_needed(monkeypatch):
    calls = []
    settings = AutoContinueSettings(
        enabled=False,
        training_auto_continue_enabled=False,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
        error_recovery_enabled=False,
    )

    class FakeProvider:
        def load_settings(self):
            return settings

        def _settings_require_hook(self, _settings):
            return False

        def unregister_hook(self):
            calls.append("unregister")

        def install_error_recovery(self):
            calls.append("install_recovery")

        def uninstall_error_recovery(self):
            calls.append("uninstall_recovery")

        def uninstall_guidance(self):
            calls.append("uninstall_guidance")

    manager = AutoContinueManager()
    monkeypatch.setattr(manager, "get_provider", lambda _name: FakeProvider())
    manager.repair("codex")

    assert calls == ["unregister", "uninstall_recovery", "uninstall_guidance"]


def test_startup_reconciliation_reports_success_and_contains_failures(monkeypatch, caplog):
    settings = AutoContinueSettings()
    manager = AutoContinueManager()

    class CurrentProvider:
        def load_settings(self):
            return settings

        def ensure_current_installation(self, value):
            assert value is settings
            return False  # changed=False is still a successful reconciliation.

    monkeypatch.setattr(manager, "get_provider", lambda _name: CurrentProvider())
    assert manager.reconcile_installation("codex") is True

    class MissingProvider:
        def load_settings(self):
            return None

    monkeypatch.setattr(manager, "get_provider", lambda _name: MissingProvider())
    assert manager.reconcile_installation("codex") is True

    class BrokenProvider:
        def load_settings(self):
            return settings

        def ensure_current_installation(self, _settings):
            raise OSError("locked hooks.json")

    monkeypatch.setattr(manager, "get_provider", lambda _name: BrokenProvider())
    assert manager.reconcile_installation("codex") is False
    assert "locked hooks.json" in caplog.text


def test_invalid_settings_disable_existing_managed_hooks(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    provider = CodexProvider()
    provider.get_hook_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_hook_script_path().write_text("old unbounded hook", encoding="utf-8")
    provider.get_error_recovery_script_path().write_text(
        "old unbounded recovery hook",
        encoding="utf-8",
    )
    provider.get_settings_path().write_text(
        json.dumps({"enabled": True, "max_continuations": 101}),
        encoding="utf-8",
    )
    provider.get_config_toml_path().write_text(
        "[features]\nhooks = true\n",
        encoding="utf-8",
    )
    auto_hook = provider._auto_continue_hook_definition()
    recovery_hook = provider._error_recovery_hook_definition()
    user_hook = {"type": "command", "command": "user-hook.exe", "timeout": 9}
    provider.get_hooks_json_path().write_text(
        json.dumps({
            "hooks": {
                "Stop": [{"hooks": [user_hook, auto_hook]}],
                "Error": [{"hooks": [recovery_hook]}],
                "ResponseError": [{"hooks": [recovery_hook]}],
            },
        }),
        encoding="utf-8",
    )

    manager = AutoContinueManager()
    monkeypatch.setattr(manager, "get_provider", lambda _name: provider)

    assert manager.reconcile_installation("codex") is True

    installed = json.loads(provider.get_hooks_json_path().read_text(encoding="utf-8"))
    assert _event_hooks(installed, "Stop") == [user_hook]
    assert _event_hooks(installed, "Error") == []
    assert _event_hooks(installed, "ResponseError") == []
    assert provider.get_hook_script_path().exists()
    assert not provider.get_error_recovery_script_path().exists()
    assert "Disabled codex managed hooks" in caplog.text
