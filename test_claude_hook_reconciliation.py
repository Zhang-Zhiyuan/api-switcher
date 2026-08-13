from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.auto_continue.claude_provider import (
    AUTO_CONTINUE_EVENTS,
    AUTO_CONTINUE_HOOK_TIMEOUT_SECONDS,
    ERROR_RECOVERY_HOOK_TIMEOUT_SECONDS,
    ClaudeProvider,
    _read_claude_settings_json,
    _restore_local_files,
    _snapshot_local_files,
)
from models.auto_continue import AutoContinueSettings


@pytest.fixture
def provider(tmp_path, monkeypatch) -> ClaudeProvider:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return ClaudeProvider()


def _event_hooks(settings: dict, event_name: str) -> list[dict]:
    result = []
    groups = settings.get("hooks", {}).get(event_name, [])
    if isinstance(groups, dict):
        groups = [groups]
    for group in groups:
        if isinstance(group, dict):
            hooks = group.get("hooks", [])
            if isinstance(hooks, dict):
                hooks = [hooks]
            if isinstance(hooks, list):
                result.extend(hook for hook in hooks if isinstance(hook, dict))
    return result


def _settings(*, enabled: bool = True, recovery: bool = True) -> AutoContinueSettings:
    return AutoContinueSettings(
        enabled=enabled,
        error_recovery_enabled=recovery,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
        git_snapshot_on_recovery=True,
    )


def test_claude_config_dir_honors_isolation_environment(tmp_path, monkeypatch):
    isolated = tmp_path / "isolated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated))

    assert ClaudeProvider().get_config_dir() == isolated


def test_ensure_current_installation_upgrades_scripts_and_definitions(provider):
    settings = _settings()
    provider.save_settings(settings)
    provider.get_hook_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_hook_script_path().write_text("stale main", encoding="utf-8")
    provider.get_error_recovery_script_path().write_text("stale recovery", encoding="utf-8")

    managed_main = provider._powershell_hook_command(provider.get_hook_script_path())
    managed_recovery = provider._powershell_hook_command(provider.get_error_recovery_script_path())
    user_main = r'powershell.exe -File "C:\user-hooks\auto_continue_stop.ps1"'
    relative_user_main = "powershell.exe -File auto_continue_stop.ps1"
    user_recovery = r'powershell.exe -File "C:\user-hooks\error_recovery.ps1"'
    provider.get_claude_settings_path().write_text(
        json.dumps(
            {
                "userSetting": "preserved",
                "hooks": {
                    "Stop": [{"matcher": "user-scope", "hooks": [
                        {"type": "command", "command": user_main, "timeout": 4},
                        {"type": "command", "command": relative_user_main, "timeout": 4},
                        {"type": "command", "command": managed_main, "timeout": 10},
                    ]}],
                    "SubagentStop": [{"hooks": [
                        {"type": "command", "command": managed_main, "timeout": 10},
                    ]}],
                    "ResponseError": [{"hooks": [
                        {"type": "command", "command": user_recovery, "timeout": 5},
                        {"type": "command", "command": managed_recovery, "timeout": 10},
                    ]}],
                },
            }
        ),
        encoding="utf-8",
    )

    assert provider.ensure_current_installation(settings) is True

    installed = json.loads(provider.get_claude_settings_path().read_text(encoding="utf-8"))
    assert installed["userSetting"] == "preserved"
    assert user_main in [hook.get("command") for hook in _event_hooks(installed, "Stop")]
    assert relative_user_main in [
        hook.get("command") for hook in _event_hooks(installed, "Stop")
    ]
    assert any(
        group.get("matcher") == "user-scope"
        and any(hook.get("command") == user_main for hook in group.get("hooks", []))
        for group in installed["hooks"]["Stop"]
    )
    assert user_recovery in [
        hook.get("command") for hook in _event_hooks(installed, "ResponseError")
    ]
    for event_name in ("Stop", "UserPromptSubmit", "SessionStart"):
        owned = [
            hook
            for hook in _event_hooks(installed, event_name)
            if provider._is_owned_auto_continue_command(hook.get("command", ""))
        ]
        assert len(owned) == 1
        assert owned[0]["type"] == "command"
        assert owned[0]["timeout"] == AUTO_CONTINUE_HOOK_TIMEOUT_SECONDS
        assert owned[0]["command"] == managed_main
    assert not any(
        provider._is_owned_auto_continue_command(hook.get("command", ""))
        for hook in _event_hooks(installed, "SubagentStop")
    )
    recovery_hooks = [
        hook
        for hook in _event_hooks(installed, "ResponseError")
        if provider._is_owned_error_recovery_command(hook.get("command", ""))
    ]
    assert len(recovery_hooks) == 1
    assert recovery_hooks[0]["timeout"] == ERROR_RECOVERY_HOOK_TIMEOUT_SECONDS
    assert recovery_hooks[0]["command"] == managed_recovery
    assert provider.get_hook_script_path().read_text(encoding="utf-8-sig") == (
        provider._render_hook_script(settings)
    )
    assert provider.get_error_recovery_script_path().read_text(encoding="utf-8-sig") == (
        provider._render_error_recovery_script(settings)
    )
    assert provider.is_hook_registered()
    assert provider.is_error_recovery_installed()
    assert provider.ensure_current_installation(settings) is False


def test_ensure_current_installation_removes_only_unneeded_managed_hooks(provider):
    active = _settings()
    provider.save_settings(active)
    provider.ensure_current_installation(active)

    config_path = provider.get_claude_settings_path()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    user_command = r'powershell.exe -File "C:\user-hooks\auto_continue_stop.ps1"'
    user_recovery = r'powershell.exe -File "C:\user-hooks\error_recovery.ps1"'
    config["hooks"]["Stop"].append({"hooks": [{"command": user_command}]})
    config["hooks"]["ResponseError"].append({"hooks": [{"command": user_recovery}]})
    config_path.write_text(json.dumps(config), encoding="utf-8")

    disabled = AutoContinueSettings(
        enabled=False,
        training_auto_continue_enabled=False,
        error_recovery_enabled=False,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
        auto_approve_permission_requests=False,
    )
    provider.save_settings(disabled)

    assert provider.ensure_current_installation(disabled) is True

    installed = json.loads(config_path.read_text(encoding="utf-8"))
    for event_name in AUTO_CONTINUE_EVENTS:
        assert not any(
            provider._is_owned_auto_continue_command(hook.get("command", ""))
            for hook in _event_hooks(installed, event_name)
        )
    assert not any(
        provider._is_owned_error_recovery_command(hook.get("command", ""))
        for hook in _event_hooks(installed, "ResponseError")
    )
    assert user_command in [hook.get("command") for hook in _event_hooks(installed, "Stop")]
    assert user_recovery in [
        hook.get("command") for hook in _event_hooks(installed, "ResponseError")
    ]
    assert provider.get_hook_script_path().exists()
    assert not provider.get_error_recovery_script_path().exists()
    assert provider.is_hook_registered()


def test_git_only_reconciliation_uses_prompt_hooks_without_stop(provider):
    settings = AutoContinueSettings(
        enabled=False,
        training_auto_continue_enabled=False,
        error_recovery_enabled=False,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
    )
    provider.save_settings(settings)

    assert provider.ensure_current_installation(settings) is True

    installed = json.loads(
        provider.get_claude_settings_path().read_text(encoding="utf-8")
    )
    assert not any(
        provider._is_owned_auto_continue_command(hook.get("command", ""))
        for hook in _event_hooks(installed, "Stop")
    )
    for event_name in ("UserPromptSubmit", "SessionStart"):
        assert sum(
            provider._is_owned_auto_continue_command(hook.get("command", ""))
            for hook in _event_hooks(installed, event_name)
        ) == 1
    assert provider.is_hook_registered()


def test_manager_reconcile_all_upgrades_claude_installation(provider):
    from core.auto_continue.manager import AutoContinueManager

    settings = _settings()
    provider.save_settings(settings)
    provider.get_hook_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_hook_script_path().write_text("old", encoding="utf-8")
    managed_command = provider._powershell_hook_command(provider.get_hook_script_path())
    provider.get_claude_settings_path().write_text(
        json.dumps({
            "hooks": {
                "Stop": [{"hooks": [{
                    "type": "command",
                    "command": managed_command,
                    "timeout": 10,
                }]}],
            }
        }),
        encoding="utf-8",
    )

    manager = AutoContinueManager()
    manager.claude = provider
    manager.codex = object()

    assert manager.reconcile_all_installations() == {"codex": True, "claude": True}
    assert provider.is_hook_registered()
    assert provider.is_error_recovery_installed()


def test_status_rejects_registration_or_script_drift(provider):
    settings = _settings()
    provider.save_settings(settings)
    provider.ensure_current_installation(settings)
    config_path = provider.get_claude_settings_path()

    config = json.loads(config_path.read_text(encoding="utf-8"))
    managed_stop = next(
        hook
        for hook in _event_hooks(config, "Stop")
        if provider._is_owned_auto_continue_command(hook.get("command", ""))
    )
    managed_stop["timeout"] = 10
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert not provider.is_hook_registered()

    provider.ensure_current_installation(settings)
    provider.get_hook_script_path().write_text("stale", encoding="utf-8")
    assert not provider.is_hook_registered()

    provider.ensure_current_installation(settings)
    provider.get_error_recovery_script_path().write_text("stale", encoding="utf-8")
    assert not provider.is_error_recovery_installed()


def test_reconciliation_relocates_owned_hooks_from_every_custom_event(provider):
    settings = _settings()
    provider.save_settings(settings)
    provider.ensure_current_installation(settings)

    config_path = provider.get_claude_settings_path()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    managed_main = provider._powershell_hook_command(provider.get_hook_script_path())
    managed_recovery = provider._powershell_hook_command(
        provider.get_error_recovery_script_path()
    )
    user_main_hook = {
        "type": "command",
        "command": r'powershell.exe -File "C:\user-hooks\custom-main.ps1"',
        "timeout": 9,
        "userHookField": {"keep": True},
    }
    user_recovery_hook = {
        "type": "command",
        "command": r'powershell.exe -File "C:\user-hooks\custom-recovery.ps1"',
        "timeout": 8,
        "userHookField": "preserved",
    }
    expected_custom_main = {
        "matcher": "custom-main-scope",
        "customGroupField": {"owner": "user"},
        "hooks": [user_main_hook],
    }
    expected_custom_recovery = {
        "matcher": "custom-recovery-scope",
        "customGroupField": ["keep", 1],
        "hooks": [user_recovery_hook],
    }
    config["hooks"]["CustomLifecycle"] = {
        **expected_custom_main,
        "hooks": [
            {
                "type": "command",
                "command": managed_main,
                "timeout": 3,
                "obsoleteManagedField": True,
            },
            user_main_hook,
        ],
    }
    config["hooks"]["CustomRecovery"] = [{
        **expected_custom_recovery,
        "hooks": [
            {
                "type": "command",
                "command": managed_recovery,
                "timeout": 4,
            },
            user_recovery_hook,
        ],
    }]
    # Claude settings historically also accepted singleton event groups and a
    # singleton hook object. Reconciliation must recognize this owned shape.
    config["hooks"]["LegacySingleton"] = {
        "hooks": {"type": "command", "command": managed_main, "timeout": 2}
    }
    config["hooks"]["OwnedOnlyWithUserMetadata"] = {
        "matcher": "retain-even-when-hook-is-removed",
        "customGroupField": {"keep": "metadata"},
        "hooks": {"type": "command", "command": managed_main, "timeout": 2},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    assert not provider.is_hook_registered()
    assert not provider.is_error_recovery_installed()
    assert provider.ensure_current_installation(settings) is True

    installed = json.loads(config_path.read_text(encoding="utf-8"))
    assert installed["hooks"]["CustomLifecycle"] == expected_custom_main
    assert installed["hooks"]["CustomRecovery"] == [expected_custom_recovery]
    assert "LegacySingleton" not in installed["hooks"]
    assert installed["hooks"]["OwnedOnlyWithUserMetadata"] == {
        "matcher": "retain-even-when-hook-is-removed",
        "customGroupField": {"keep": "metadata"},
        "hooks": [],
    }
    assert provider.is_hook_registered()
    assert provider.is_error_recovery_installed()
    assert provider.ensure_current_installation(settings) is False


def test_unregister_removes_auto_hook_from_custom_events_and_preserves_users(provider):
    settings = _settings()
    provider.save_settings(settings)
    provider.ensure_current_installation(settings)

    config_path = provider.get_claude_settings_path()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    managed_main = provider._powershell_hook_command(provider.get_hook_script_path())
    user_hook = {
        "type": "command",
        "command": r'powershell.exe -File "C:\user-hooks\keep-custom.ps1"',
        "timeout": 6,
        "customHookField": 42,
    }
    expected_group = {
        "matcher": "retain-group",
        "customGroupField": "retain-value",
        "hooks": [user_hook],
    }
    config["hooks"]["CustomUnregister"] = {
        **expected_group,
        "hooks": [
            {"type": "command", "command": managed_main, "timeout": 1},
            user_hook,
        ],
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    provider.unregister_hook()

    installed = json.loads(config_path.read_text(encoding="utf-8"))
    assert installed["hooks"]["CustomUnregister"] == expected_group
    assert not any(
        provider._is_owned_auto_continue_command(hook.get("command", ""))
        for event_name in installed["hooks"]
        for hook in _event_hooks(installed, event_name)
    )
    assert provider.is_error_recovery_installed()


def test_recovery_install_and_uninstall_clean_every_custom_event(provider):
    settings = _settings()
    provider.save_settings(settings)
    provider.install_error_recovery(settings=settings)

    config_path = provider.get_claude_settings_path()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    managed_recovery = provider._powershell_hook_command(
        provider.get_error_recovery_script_path()
    )
    user_hook = {
        "type": "command",
        "command": r'powershell.exe -File "C:\user-hooks\keep-recovery.ps1"',
        "timeout": 5,
        "customHookField": "keep",
    }
    expected_group = {
        "matcher": "recovery-user-scope",
        "customGroupField": {"keep": "all"},
        "hooks": [user_hook],
    }
    config["hooks"]["CustomRecoveryInstall"] = {
        **expected_group,
        "hooks": [
            {"type": "command", "command": managed_recovery, "timeout": 1},
            user_hook,
        ],
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    assert not provider.is_error_recovery_installed()
    provider.install_error_recovery(settings=settings)
    installed = json.loads(config_path.read_text(encoding="utf-8"))
    assert installed["hooks"]["CustomRecoveryInstall"] == expected_group
    assert provider.is_error_recovery_installed()

    installed["hooks"]["AnotherCustomRecovery"] = {
        "hooks": {"type": "command", "command": managed_recovery, "timeout": 2}
    }
    config_path.write_text(json.dumps(installed), encoding="utf-8")
    provider.uninstall_error_recovery()

    uninstalled = json.loads(config_path.read_text(encoding="utf-8"))
    assert uninstalled["hooks"]["CustomRecoveryInstall"] == expected_group
    assert "AnotherCustomRecovery" not in uninstalled["hooks"]
    assert not any(
        provider._is_owned_error_recovery_command(hook.get("command", ""))
        for event_name in uninstalled["hooks"]
        for hook in _event_hooks(uninstalled, event_name)
    )
    assert not provider.get_error_recovery_script_path().exists()


def test_reconciliation_rolls_back_all_managed_files_on_late_failure(
    provider,
    monkeypatch,
):
    settings = _settings()
    provider.save_settings(settings)
    provider.get_hook_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_hook_script_path().write_bytes(b"old-main")
    provider.get_error_recovery_script_path().write_bytes(b"old-recovery")
    provider.get_claude_settings_path().write_text(
        json.dumps({"hooks": {}, "userSetting": "keep"}),
        encoding="utf-8",
    )
    provider.get_permission_rules_state_path().write_bytes(b'{"rules":["Bash"]}')
    paths = [
        provider.get_hook_script_path(),
        provider.get_error_recovery_script_path(),
        provider.get_claude_settings_path(),
        provider.get_permission_rules_state_path(),
    ]
    before = {path: path.read_bytes() for path in paths}

    def fail_recovery(**_kwargs):
        raise OSError("simulated late write failure")

    monkeypatch.setattr(provider, "install_error_recovery", fail_recovery)

    with pytest.raises(RuntimeError, match="Failed to reconcile Claude"):
        provider.ensure_current_installation(settings)

    assert {path: path.read_bytes() for path in paths} == before


def test_invalid_claude_settings_is_not_overwritten_and_manager_reports_failure(
    provider,
):
    from core.auto_continue.manager import AutoContinueManager

    settings = _settings()
    provider.save_settings(settings)
    config_path = provider.get_claude_settings_path()
    invalid = b"{not valid json"
    config_path.write_bytes(invalid)
    provider.get_hook_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_hook_script_path().write_bytes(b"old-script")

    with pytest.raises(RuntimeError, match="did not overwrite"):
        provider.ensure_current_installation(settings)
    assert config_path.read_bytes() == invalid
    assert provider.get_hook_script_path().read_bytes() == b"old-script"

    manager = AutoContinueManager()
    manager.claude = provider
    assert manager.reconcile_installation("claude") is False
    assert config_path.read_bytes() == invalid


def test_invalid_auto_settings_are_preserved_while_managed_hooks_are_disabled(
    provider,
):
    from core.auto_continue.manager import AutoContinueManager

    invalid_settings = b"{broken auto settings"
    provider.get_settings_path().write_bytes(invalid_settings)
    managed_command = provider._powershell_hook_command(provider.get_hook_script_path())
    managed_recovery_command = provider._powershell_hook_command(
        provider.get_error_recovery_script_path()
    )
    user_command = r'powershell.exe -File "C:\user-hooks\keep.ps1"'
    provider.get_claude_settings_path().write_text(
        json.dumps({
            "hooks": {
                "Stop": [{"hooks": [
                    {"type": "command", "command": managed_command, "timeout": 10},
                    {"type": "command", "command": user_command, "timeout": 5},
                ]}],
                "CustomInvalidSettings": {
                    "matcher": "keep-custom-group",
                    "customGroupField": True,
                    "hooks": [
                        {"type": "command", "command": managed_command, "timeout": 2},
                        {
                            "type": "command",
                            "command": managed_recovery_command,
                            "timeout": 2,
                        },
                        {"type": "command", "command": user_command, "timeout": 5},
                    ],
                },
            }
        }),
        encoding="utf-8",
    )

    manager = AutoContinueManager()
    manager.claude = provider
    assert manager.reconcile_installation("claude") is True

    assert provider.get_settings_path().read_bytes() == invalid_settings
    installed = json.loads(provider.get_claude_settings_path().read_text(encoding="utf-8"))
    commands = [hook.get("command") for hook in _event_hooks(installed, "Stop")]
    assert managed_command not in commands
    assert user_command in commands
    custom_group = installed["hooks"]["CustomInvalidSettings"]
    assert custom_group["matcher"] == "keep-custom-group"
    assert custom_group["customGroupField"] is True
    custom_commands = [
        hook.get("command")
        for hook in _event_hooks(installed, "CustomInvalidSettings")
    ]
    assert managed_command not in custom_commands
    assert managed_recovery_command not in custom_commands
    assert user_command in custom_commands


def test_explicit_repair_does_not_overwrite_corrupt_settings_without_backup(
    provider,
    monkeypatch,
):
    import core.auto_continue.claude_provider as claude_module

    config_path = provider.get_claude_settings_path()
    invalid = b"{invalid"
    config_path.write_bytes(invalid)
    monkeypatch.setattr(claude_module, "_backup_claude_settings_file", lambda *_args: None)

    with pytest.raises(RuntimeError, match="could not be backed up"):
        provider.register_hook(settings=_settings())

    assert config_path.read_bytes() == invalid
    assert not list(config_path.parent.glob("settings.json.bak-*"))


def test_rollback_skips_unchanged_user_file(provider, monkeypatch):
    import core.auto_continue.claude_provider as claude_module

    config_path = provider.get_claude_settings_path()
    config_path.write_bytes(b"{invalid user settings")
    original_mtime = config_path.stat().st_mtime_ns
    writes = []

    def record_write(*args, **kwargs):
        writes.append((args, kwargs))

    monkeypatch.setattr(claude_module, "atomic_write_bytes", record_write)

    assert _restore_local_files({config_path: config_path.read_bytes()}) == ""
    assert writes == []
    assert config_path.stat().st_mtime_ns == original_mtime


def test_snapshot_and_settings_read_handle_disappeared_file_without_exists_check(
    tmp_path,
    monkeypatch,
):
    missing = tmp_path / "disappeared-settings.json"

    def forbidden_exists(_path):
        raise AssertionError("exists() must not precede the read")

    monkeypatch.setattr(Path, "exists", forbidden_exists)

    assert _snapshot_local_files([missing]) == {missing: None}
    assert _read_claude_settings_json(missing) == {}


def test_snapshot_does_not_treat_permission_error_as_missing(tmp_path, monkeypatch):
    protected = tmp_path / "protected-settings.json"
    protected.write_bytes(b"user config")
    original_read_bytes = Path.read_bytes

    def deny_snapshot_read(path):
        if path == protected:
            raise PermissionError("simulated sharing violation")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", deny_snapshot_read)

    with pytest.raises(PermissionError, match="sharing violation"):
        _snapshot_local_files([protected])


def test_permission_error_never_overwrites_claude_settings(provider, monkeypatch):
    settings = _settings()
    provider.save_settings(settings)
    config_path = provider.get_claude_settings_path()
    original = b'{"hooks": {}, "userSetting": "keep"}'
    config_path.write_bytes(original)
    original_mtime = config_path.stat().st_mtime_ns
    original_read_text = Path.read_text

    def deny_config_read(path, *args, **kwargs):
        if path == config_path:
            raise PermissionError("simulated sharing violation")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_config_read)

    with pytest.raises(RuntimeError, match="did not overwrite"):
        provider.ensure_current_installation(settings)
    with pytest.raises(RuntimeError, match="could not be backed up"):
        provider.register_hook(settings=settings)

    assert config_path.read_bytes() == original
    assert config_path.stat().st_mtime_ns == original_mtime
    assert not list(config_path.parent.glob("settings.json.bak-*"))
