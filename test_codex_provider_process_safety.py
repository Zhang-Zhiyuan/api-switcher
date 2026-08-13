import json
import os
import subprocess
from types import SimpleNamespace

import pytest

import core.auto_continue.codex_provider as provider_module
from core.auto_continue.codex_provider import (
    MANAGED_HOOK_PROCESS_QUERY_TIMEOUT_SECONDS,
    MANAGED_HOOK_TASKKILL_TIMEOUT_SECONDS,
    CodexProvider,
    _read_codex_hooks_json,
    _snapshot_local_files,
)
from core.auto_continue.manager import AutoContinueManager
from models.auto_continue import AutoContinueSettings


def _isolated_provider(tmp_path, monkeypatch, **kwargs) -> CodexProvider:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    return CodexProvider(**kwargs)


def _all_hook_commands(data: dict) -> list[str]:
    def commands(value) -> list[str]:
        if isinstance(value, list):
            return [command for item in value for command in commands(item)]
        if not isinstance(value, dict):
            return []
        found = [str(value["command"])] if value.get("command") else []
        nested = value.get("hooks")
        if isinstance(nested, list):
            found.extend(command for item in nested for command in commands(item))
        return found

    event_values = []
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        event_values.extend(hooks.values())
    event_values.extend(value for key, value in data.items() if key != "hooks")
    return [command for value in event_values for command in commands(value)]


def test_unregister_rejects_invalid_hooks_json_without_replacing_it(
    tmp_path,
    monkeypatch,
):
    provider = _isolated_provider(tmp_path, monkeypatch)
    hooks_path = provider.get_hooks_json_path()
    config_path = provider.get_config_toml_path()
    state_path = provider.get_hooks_feature_state_path()
    invalid_hooks = b'\xef\xbb\xbf{"hooks": { definitely-not-json'
    original_config = b"[features]\nhooks = true\n"
    original_state = b'{"previous_enabled": false}'
    hooks_path.write_bytes(invalid_hooks)
    config_path.write_bytes(original_config)
    state_path.write_bytes(original_state)

    with pytest.raises(RuntimeError, match="hooks.json is invalid"):
        provider.unregister_hook()

    assert hooks_path.read_bytes() == invalid_hooks
    assert config_path.read_bytes() == original_config
    assert state_path.read_bytes() == original_state
    assert list(tmp_path.glob("hooks.json.bak-*")) == []


def test_snapshot_treats_only_file_not_found_as_missing(tmp_path, monkeypatch):
    target = tmp_path / "hooks.json"
    target.write_text("{}", encoding="utf-8")
    path_type = type(target)

    def vanished(_self):
        raise FileNotFoundError(str(target))

    monkeypatch.setattr(path_type, "read_bytes", vanished)
    assert _snapshot_local_files([target]) == {target: None}

    def denied(_self):
        raise PermissionError(str(target))

    monkeypatch.setattr(path_type, "read_bytes", denied)
    with pytest.raises(PermissionError):
        _snapshot_local_files([target])


def test_hooks_reader_distinguishes_disappearance_from_permission_error(
    tmp_path,
    monkeypatch,
):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text("{}", encoding="utf-8")
    path_type = type(hooks_path)

    def vanished(_self, *args, **kwargs):
        raise FileNotFoundError(str(hooks_path))

    monkeypatch.setattr(path_type, "read_text", vanished)
    assert _read_codex_hooks_json(hooks_path) == {}

    def denied(_self, *args, **kwargs):
        raise PermissionError(str(hooks_path))

    monkeypatch.setattr(path_type, "read_text", denied)
    assert _read_codex_hooks_json(hooks_path) is None
    assert _read_codex_hooks_json(hooks_path, recover=True) is None


def test_permission_error_cannot_be_downgraded_to_empty_hooks_config(
    tmp_path,
    monkeypatch,
):
    provider = _isolated_provider(tmp_path, monkeypatch)
    hooks_path = provider.get_hooks_json_path()
    original_hooks = b'{"hooks":{"Stop":[{"hooks":[{"command":"user.exe"}]}]}}'
    hooks_path.write_bytes(original_hooks)
    path_type = type(hooks_path)
    original_read_text = path_type.read_text

    def deny_hooks_read(self, *args, **kwargs):
        if self == hooks_path:
            raise PermissionError("simulated ACL denial")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(path_type, "read_text", deny_hooks_read)
    with pytest.raises(RuntimeError, match="hooks.json could not be read safely"):
        provider.register_hook(settings=AutoContinueSettings(enabled=True))
    with pytest.raises(RuntimeError, match="managed hooks were not inspected or removed"):
        provider._has_owned_hook_entries(provider._is_owned_auto_continue_command)

    assert hooks_path.read_bytes() == original_hooks
    assert list(tmp_path.glob("hooks.json.bak-*")) == []


def test_invalid_settings_disable_fails_without_overwriting_invalid_hooks(
    tmp_path,
    monkeypatch,
):
    provider = _isolated_provider(
        tmp_path,
        monkeypatch,
        process_inventory=lambda: [],
        process_tree_terminator=lambda _pid: True,
    )
    hooks_path = provider.get_hooks_json_path()
    invalid_hooks = b"{this file belongs to the user and is damaged"
    hooks_path.write_bytes(invalid_hooks)
    provider.get_hook_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_hook_script_path().write_text("old main", encoding="utf-8")
    provider.get_error_recovery_script_path().write_text("old recovery", encoding="utf-8")

    original_atomic_write_bytes = provider_module.atomic_write_bytes

    def reject_hooks_replacement(path, content, *args, **kwargs):
        if path == hooks_path:
            raise AssertionError("invalid hooks.json must not be replaced during disable")
        return original_atomic_write_bytes(path, content, *args, **kwargs)

    monkeypatch.setattr(provider_module, "atomic_write_bytes", reject_hooks_replacement)
    with pytest.raises(RuntimeError, match="managed hooks were not inspected or removed"):
        provider.disable_managed_hooks_for_invalid_settings()

    assert hooks_path.read_bytes() == invalid_hooks
    assert provider.get_hook_script_path().read_text(encoding="utf-8") == "old main"
    assert provider.get_error_recovery_script_path().read_text(encoding="utf-8") == "old recovery"
    assert list(tmp_path.glob("hooks.json.bak-*")) == []


def test_startup_reconciliation_reports_invalid_hooks_disable_failure(
    tmp_path,
    monkeypatch,
    caplog,
):
    provider = _isolated_provider(
        tmp_path,
        monkeypatch,
        process_inventory=lambda: [],
        process_tree_terminator=lambda _pid: True,
    )
    provider.get_settings_path().write_text(
        '{"enabled": true, "max_continuations": 101}',
        encoding="utf-8",
    )
    hooks_path = provider.get_hooks_json_path()
    invalid_hooks = b"{invalid managed hook configuration"
    hooks_path.write_bytes(invalid_hooks)

    manager = AutoContinueManager()
    monkeypatch.setattr(manager, "get_provider", lambda _name: provider)

    assert manager.reconcile_installation("codex") is False
    assert hooks_path.read_bytes() == invalid_hooks
    assert "hooks.json is invalid" in caplog.text
    assert "Disabled codex managed hooks" not in caplog.text


def test_get_status_rejects_stale_main_and_recovery_scripts(tmp_path, monkeypatch):
    provider = _isolated_provider(tmp_path, monkeypatch)
    settings = AutoContinueSettings(
        enabled=True,
        git_auto_snapshot=False,
        error_recovery_enabled=True,
    )
    provider.save_settings(settings)
    provider.install_hook_script(settings=settings)
    provider.register_hook(settings=settings)
    provider.install_error_recovery()

    healthy = provider.get_status()
    assert healthy.hook_registered is True
    assert healthy.error_recovery_installed is True
    assert healthy.last_error is None

    provider.get_hook_script_path().write_text("stale main", encoding="utf-8")
    assert provider.is_hook_registered() is False
    stale_main = provider.get_status()
    assert stale_main.hook_script_exists is True
    assert stale_main.hook_registered is False
    assert stale_main.error_recovery_installed is True
    assert "auto-continue hook script is missing or stale" in stale_main.last_error

    provider.install_hook_script(settings=settings)
    provider.get_error_recovery_script_path().write_text(
        "stale recovery",
        encoding="utf-8",
    )
    assert provider.is_error_recovery_installed() is False
    stale_recovery = provider.get_status()
    assert stale_recovery.hook_registered is True
    assert stale_recovery.error_recovery_installed is False
    assert "error-recovery hook script is missing or stale" in stale_recovery.last_error


def test_current_installation_performs_zero_process_inventory_calls(
    tmp_path,
    monkeypatch,
):
    inventory_calls = []
    provider = _isolated_provider(
        tmp_path,
        monkeypatch,
        process_inventory=lambda: inventory_calls.append("inventory") or [],
        process_verifier=lambda _pid: pytest.fail("no PID should be verified"),
        process_tree_terminator=lambda _pid: pytest.fail("no PID should be terminated"),
    )
    settings = AutoContinueSettings(
        enabled=True,
        git_auto_snapshot=False,
        error_recovery_enabled=True,
    )
    provider.save_settings(settings)
    provider.install_hook_script(settings=settings)
    provider.register_hook(settings=settings)
    provider.install_error_recovery()

    assert provider.ensure_current_installation(settings) is False
    assert inventory_calls == []


def test_stale_script_performs_exactly_one_process_inventory_call(
    tmp_path,
    monkeypatch,
):
    inventory_calls = []
    provider = _isolated_provider(
        tmp_path,
        monkeypatch,
        process_inventory=lambda: inventory_calls.append("inventory") or [],
        process_verifier=lambda _pid: pytest.fail("empty inventory has no PID"),
        process_tree_terminator=lambda _pid: pytest.fail("empty inventory has no PID"),
    )
    settings = AutoContinueSettings(enabled=True, git_auto_snapshot=False)
    provider.save_settings(settings)
    provider.install_hook_script(settings=settings)
    provider.register_hook(settings=settings)
    provider.get_hook_script_path().write_text("stale script", encoding="utf-8")

    assert provider.ensure_current_installation(settings) is True
    assert inventory_calls == ["inventory"]
    assert provider.get_hook_script_path().read_text(
        encoding="utf-8-sig"
    ) == provider._render_hook_script(settings)


def test_stale_registration_performs_exactly_one_process_inventory_call(
    tmp_path,
    monkeypatch,
):
    inventory_calls = []
    provider = _isolated_provider(
        tmp_path,
        monkeypatch,
        process_inventory=lambda: inventory_calls.append("inventory") or [],
        process_verifier=lambda _pid: pytest.fail("empty inventory has no PID"),
        process_tree_terminator=lambda _pid: pytest.fail("empty inventory has no PID"),
    )
    settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
    )
    provider.save_settings(settings)
    provider.install_hook_script(settings=settings)
    provider.register_hook(settings=settings)
    hooks = json.loads(provider.get_hooks_json_path().read_text(encoding="utf-8"))
    hooks["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 10
    provider.get_hooks_json_path().write_text(json.dumps(hooks), encoding="utf-8")

    assert provider.ensure_current_installation(settings) is True
    assert inventory_calls == ["inventory"]
    assert provider.is_hook_registered() is True


def test_reconcile_removes_owned_hooks_from_unexpected_container_and_legacy_events(
    tmp_path,
    monkeypatch,
):
    inventory_calls = []
    provider = _isolated_provider(
        tmp_path,
        monkeypatch,
        process_inventory=lambda: inventory_calls.append("inventory") or [],
        process_verifier=lambda _pid: pytest.fail("empty inventory has no PID"),
        process_tree_terminator=lambda _pid: pytest.fail("empty inventory has no PID"),
    )
    settings = AutoContinueSettings(
        enabled=True,
        apply_to_subagents=False,
        git_auto_snapshot=False,
        error_recovery_enabled=True,
    )
    provider.save_settings(settings)
    provider.install_hook_script(settings=settings)
    provider.register_hook(settings=settings)
    provider.install_error_recovery()

    user_subagent_hook = {
        "type": "command",
        "command": "user-subagent-hook.exe --keep",
        "timeout": 47,
        "userHookField": "container-user-value",
    }
    subagent_group = {
        "matcher": "worker-*",
        "customGroupField": {"owner": "user", "location": "container"},
        "hooks": [
            user_subagent_hook,
            {
                **provider._auto_continue_hook_definition(),
                "statusMessage": "obsolete subagent auto-continue hook",
            },
        ],
    }
    user_legacy_hook = {
        "type": "command",
        "command": provider._powershell_hook_command(
            tmp_path / "user-hooks" / "error_recovery.ps1"
        ),
        "timeout": 53,
        "userHookField": "legacy-user-value",
    }
    legacy_group = {
        "matcher": "tool-*",
        "customGroupField": {"owner": "user", "location": "legacy"},
        "hooks": [
            user_legacy_hook,
            {
                **provider._error_recovery_hook_definition(),
                "statusMessage": "obsolete legacy recovery hook",
            },
        ],
    }

    hooks_path = provider.get_hooks_json_path()
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    data["hooks"]["SubagentStop"] = [subagent_group]
    data["PostToolUse"] = legacy_group
    hooks_path.write_text(json.dumps(data), encoding="utf-8")

    assert provider.is_hook_registered() is False
    assert provider.is_error_recovery_installed() is False
    assert provider.ensure_current_installation(settings) is True
    assert inventory_calls == ["inventory"]
    assert provider.is_hook_registered() is True
    assert provider.is_error_recovery_installed() is True

    reconciled = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert reconciled["hooks"]["SubagentStop"] == [
        {
            **subagent_group,
            "hooks": [user_subagent_hook],
        }
    ]
    assert reconciled["PostToolUse"] == {
        **legacy_group,
        "hooks": [user_legacy_hook],
    }
    commands = _all_hook_commands(reconciled)
    unexpected_commands = [
        command
        for command in commands
        if provider._is_owned_auto_continue_command(command)
        or provider._is_owned_error_recovery_command(command)
    ]
    assert len(unexpected_commands) == 5


def test_invalid_settings_disable_removes_owned_hooks_from_every_event(
    tmp_path,
    monkeypatch,
):
    provider = _isolated_provider(
        tmp_path,
        monkeypatch,
        process_inventory=lambda: [],
        process_verifier=lambda _pid: pytest.fail("empty inventory has no PID"),
        process_tree_terminator=lambda _pid: pytest.fail("empty inventory has no PID"),
    )
    settings = AutoContinueSettings(
        enabled=True,
        git_auto_snapshot=False,
        error_recovery_enabled=True,
    )
    provider.save_settings(settings)
    provider.install_hook_script(settings=settings)
    provider.register_hook(settings=settings)
    provider.install_error_recovery()

    user_container_hook = {
        "type": "command",
        "command": provider._powershell_hook_command(
            tmp_path / "user-hooks" / "error_recovery.ps1"
        ),
        "timeout": 61,
        "userHookField": "same-name container script",
    }
    container_group = {
        "matcher": "notification-*",
        "customGroupField": ["keep", "container", "metadata"],
        "hooks": [
            user_container_hook,
            {
                **provider._error_recovery_hook_definition(),
                "statusMessage": "unexpected recovery hook",
            },
        ],
    }
    user_legacy_hook = {
        "type": "command",
        "command": provider._powershell_hook_command(
            tmp_path / "user-hooks" / "auto_continue_stop.ps1"
        ),
        "timeout": 67,
        "userHookField": "same-name legacy script",
    }
    legacy_group = {
        "matcher": "subagent-*",
        "customGroupField": ["keep", "legacy", "metadata"],
        "hooks": [
            user_legacy_hook,
            {
                **provider._auto_continue_hook_definition(),
                "statusMessage": "unexpected auto-continue hook",
            },
        ],
    }

    hooks_path = provider.get_hooks_json_path()
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    data["hooks"]["Notification"] = [container_group]
    data["SubagentStop"] = legacy_group
    hooks_path.write_text(json.dumps(data), encoding="utf-8")

    assert provider.disable_managed_hooks_for_invalid_settings() is True

    disabled = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert disabled["hooks"]["Notification"] == [
        {
            **container_group,
            "hooks": [user_container_hook],
        }
    ]
    assert disabled["SubagentStop"] == {
        **legacy_group,
        "hooks": [user_legacy_hook],
    }
    assert not any(
        provider._is_owned_auto_continue_command(command)
        or provider._is_owned_error_recovery_command(command)
        for command in _all_hook_commands(disabled)
    )
    assert provider.get_hook_script_path().exists()
    assert not provider.get_error_recovery_script_path().exists()


def test_inventory_failure_is_logged_and_does_not_block_script_migration(
    tmp_path,
    monkeypatch,
    caplog,
):
    inventory_calls = []

    def unavailable_inventory():
        inventory_calls.append("inventory")
        raise OSError("simulated WMI failure")

    provider = _isolated_provider(
        tmp_path,
        monkeypatch,
        process_inventory=unavailable_inventory,
        process_verifier=lambda _pid: pytest.fail("inventory failed before verification"),
        process_tree_terminator=lambda _pid: pytest.fail("inventory failed before termination"),
    )
    settings = AutoContinueSettings(enabled=True, git_auto_snapshot=False)
    provider.save_settings(settings)
    provider.install_hook_script(settings=settings)
    provider.register_hook(settings=settings)
    provider.get_hook_script_path().write_text("stale script", encoding="utf-8")

    assert provider.ensure_current_installation(settings) is True
    assert inventory_calls == ["inventory"]
    assert provider.is_hook_registered() is True
    assert "continuing hook migration without orphan cleanup" in caplog.text


def test_registration_health_requires_persisted_settings(tmp_path, monkeypatch):
    provider = _isolated_provider(tmp_path, monkeypatch)
    provider.install_hook_script()
    provider.register_hook()
    provider.install_error_recovery()

    assert not provider.get_settings_path().exists()
    assert provider.is_hook_registered() is False
    assert provider.is_error_recovery_installed() is False


def test_registration_health_requires_both_generated_scripts(tmp_path, monkeypatch):
    provider = _isolated_provider(tmp_path, monkeypatch)
    settings = AutoContinueSettings(
        enabled=True,
        git_auto_snapshot=False,
        error_recovery_enabled=True,
    )
    provider.save_settings(settings)
    provider.register_hook(settings=settings)
    provider._register_error_recovery_hook()

    assert not provider.get_hook_script_path().exists()
    assert not provider.get_error_recovery_script_path().exists()
    assert provider.is_hook_registered() is False
    assert provider.is_error_recovery_installed() is False


def test_registration_health_rejects_empty_scripts(tmp_path, monkeypatch):
    provider = _isolated_provider(tmp_path, monkeypatch)
    settings = AutoContinueSettings(
        enabled=True,
        git_auto_snapshot=False,
        error_recovery_enabled=True,
    )
    provider.save_settings(settings)
    provider.register_hook(settings=settings)
    provider._register_error_recovery_hook()
    provider.get_hook_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_hook_script_path().write_text("", encoding="utf-8")
    provider.get_error_recovery_script_path().write_text("", encoding="utf-8")

    assert provider.is_hook_registered() is False
    assert provider.is_error_recovery_installed() is False


def test_startup_cleanup_terminates_only_exact_path_orphaned_managed_hooks(
    tmp_path,
    monkeypatch,
):
    terminated = []
    provider = _isolated_provider(
        tmp_path,
        monkeypatch,
        process_inventory=lambda: process_records,
        process_verifier=lambda pid: next(
            (record for record in process_records if record.get("pid") == pid),
            None,
        ),
        process_tree_terminator=lambda pid: terminated.append(pid) or True,
    )
    main_command = provider._powershell_hook_command(provider.get_hook_script_path())
    recovery_command = provider._powershell_hook_command(
        provider.get_error_recovery_script_path()
    ).replace("\\", "\\\\")
    same_name_elsewhere = (
        'powershell.exe -NoProfile -File "D:\\UserHooks\\auto_continue_stop.ps1"'
    )
    command_payload_only = (
        'powershell.exe -Command "Write-Output -File '
        f"'{provider.get_hook_script_path()}'\""
    )
    process_records = [
        {
            "pid": 101,
            "parent_pid": 9001,
            "name": "powershell.exe",
            "command_line": main_command,
            "creation_time": "2026-07-22T08:00:00.0000000Z",
        },
        {
            "pid": 102,
            "parent_pid": 9002,
            "name": "pwsh.exe",
            "command_line": recovery_command.replace("powershell.exe", "pwsh.exe", 1),
            "creation_time": "2026-07-22T08:00:01.0000000Z",
        },
        {
            "pid": 103,
            "parent_pid": 9003,
            "name": "powershell.exe",
            "command_line": same_name_elsewhere,
        },
        {
            "pid": 104,
            "parent_pid": 9004,
            "name": "powershell.exe",
            "command_line": command_payload_only,
        },
        {
            "pid": 105,
            "parent_pid": 200,
            "name": "powershell.exe",
            "command_line": main_command,
        },
        {
            "pid": 106,
            "parent_pid": 9006,
            "name": "powershell.exe",
            "command_line": "powershell.exe -File auto_continue_stop.ps1",
        },
        {
            "pid": 107,
            "parent_pid": 9007,
            "name": "cmd.exe",
            "command_line": f'cmd.exe /C "{main_command}"',
        },
        {
            "pid": 200,
            "parent_pid": 300,
            "name": "cmd.exe",
            "command_line": "cmd.exe /D /S /C active-hook",
        },
        {
            "pid": 300,
            "parent_pid": 0,
            "name": "codex.exe",
            "command_line": "codex.exe app-server",
        },
        {
            "pid": os.getpid(),
            "parent_pid": 9999,
            "name": "powershell.exe",
            "command_line": main_command,
        },
    ]
    settings = AutoContinueSettings(
        enabled=False,
        training_auto_continue_enabled=False,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
        error_recovery_enabled=False,
    )
    provider.save_settings(settings)
    provider.get_hooks_json_path().write_text("{}", encoding="utf-8")
    provider.get_hook_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_hook_script_path().write_text("old managed script", encoding="utf-8")

    assert provider.ensure_current_installation(settings) is True
    assert terminated == [101, 102]


def _run_identity_change_cleanup(tmp_path, monkeypatch, verified_record):
    terminated = []
    holder = {}
    provider = _isolated_provider(
        tmp_path,
        monkeypatch,
        process_inventory=lambda: [holder["initial"]],
        process_verifier=lambda _pid: verified_record(holder["initial"]),
        process_tree_terminator=lambda pid: terminated.append(pid) or True,
    )
    holder["initial"] = {
        "pid": 5151,
        "parent_pid": 9191,
        "name": "powershell.exe",
        "command_line": provider._powershell_hook_command(
            provider.get_hook_script_path()
        ),
        "creation_time": "2026-07-22T08:15:00.0000000Z",
    }

    assert provider.cleanup_orphaned_managed_hook_processes() == []
    assert terminated == []


def test_startup_cleanup_skips_reused_pid_with_new_creation_time(
    tmp_path,
    monkeypatch,
):
    def reused_pid(initial):
        return {
            **initial,
            "creation_time": "2026-07-22T08:16:00.0000000Z",
        }

    _run_identity_change_cleanup(tmp_path, monkeypatch, reused_pid)


def test_startup_cleanup_skips_pid_whose_script_command_changed(
    tmp_path,
    monkeypatch,
):
    def changed_command(initial):
        return {
            **initial,
            "command_line": initial["command_line"].replace(
                "auto_continue_stop.ps1",
                "error_recovery.ps1",
            ),
        }

    _run_identity_change_cleanup(tmp_path, monkeypatch, changed_command)


def test_startup_cleanup_skips_pid_that_exited_before_reverification(
    tmp_path,
    monkeypatch,
):
    _run_identity_change_cleanup(tmp_path, monkeypatch, lambda _initial: None)


@pytest.mark.skipif(os.name != "nt", reason="Windows process query implementation")
def test_inventory_and_pid_reverification_queries_have_hard_timeouts(monkeypatch):
    calls = []

    def successful_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        output = "null" if "ProcessId = 6060" in arguments[-1] else "[]"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(provider_module.subprocess, "run", successful_run)

    assert CodexProvider._query_windows_process_inventory() == []
    assert CodexProvider._query_windows_process_by_id(6060) is None
    assert len(calls) == 2
    assert all(
        kwargs["timeout"] == MANAGED_HOOK_PROCESS_QUERY_TIMEOUT_SECONDS
        for _arguments, kwargs in calls
    )


def test_taskkill_tree_uses_hard_timeout_and_contains_timeout(monkeypatch):
    calls = []

    def successful_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

    monkeypatch.setattr(provider_module.subprocess, "run", successful_run)
    assert CodexProvider._terminate_windows_process_tree(4242) is True
    arguments, kwargs = calls[0]
    assert arguments == ["taskkill.exe", "/PID", "4242", "/T", "/F"]
    assert kwargs["timeout"] == MANAGED_HOOK_TASKKILL_TIMEOUT_SECONDS

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("taskkill.exe", 2)

    monkeypatch.setattr(provider_module.subprocess, "run", timed_out)
    assert CodexProvider._terminate_windows_process_tree(4343) is False
