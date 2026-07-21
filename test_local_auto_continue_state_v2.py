from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from core.auto_continue.script_generator import generate_hook_script
from test_auto_continue_logic_optimization import (
    _local_hook,
    _powershell,
    _run_local,
    _settings,
)


def _state_key(
    provider: str,
    session_id: str,
    event: str = "Stop",
    agent_id: str = "",
) -> str:
    seed = f"{provider}|{session_id}|{event}|{agent_id}"
    return hashlib.sha256(seed.encode()).hexdigest()


def _state_path(tmp_path: Path) -> Path:
    return tmp_path / "tmp" / "auto_continue_stop_state.json"


def _read_state(tmp_path: Path) -> dict:
    return json.loads(_state_path(tmp_path).read_text(encoding="utf-8-sig"))


def _decision_log(tmp_path: Path) -> list[dict]:
    path = tmp_path / "tmp" / "auto_continue_stop_log.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )


def _incomplete_settings(**updates) -> dict:
    values = {
        "max_continuations": -1,
        "incomplete_patterns": ["still need"],
        "blocker_patterns": [],
    }
    values.update(updates)
    return _settings(**values)


@contextmanager
def _dotnet_file_handle(
    path: Path,
    *,
    mode: str = "OpenOrCreate",
    access: str = "ReadWrite",
    share: str = "None",
):
    """Hold a real .NET file handle with explicit Windows sharing semantics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    literal = str(path).replace("'", "''")
    command = f"""
$stream = [System.IO.File]::Open(
    '{literal}',
    [System.IO.FileMode]::{mode},
    [System.IO.FileAccess]::{access},
    [System.IO.FileShare]::{share}
)
try {{
    [Console]::Out.WriteLine('READY')
    [Console]::Out.Flush()
    [void][Console]::In.ReadLine()
}} finally {{
    $stream.Dispose()
}}
"""
    process = subprocess.Popen(
        [_powershell(), "-NoProfile", "-Command", command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    ready = process.stdout.readline().strip()
    if ready != "READY":
        _, stderr = process.communicate(timeout=5)
        raise AssertionError(f"failed to acquire test handle: {ready!r} {stderr}")
    try:
        yield
    finally:
        assert process.stdin is not None
        process.stdin.write("\n")
        process.stdin.flush()
        process.communicate(timeout=5)
        assert process.returncode == 0


def test_local_settings_path_is_a_literal_with_quotes_and_subexpressions(tmp_path):
    special_dir = tmp_path / "O'Brien $([Environment]) [literal]"
    special_dir.mkdir()
    settings_path = special_dir / "auto_continue_settings.json"
    script_path = special_dir / "auto_continue_stop.ps1"
    settings_path.write_text(
        json.dumps(_incomplete_settings(), ensure_ascii=False),
        encoding="utf-8",
    )
    generated = generate_hook_script(str(settings_path), False, "codex")
    script_path.write_text(generated, encoding="utf-8-sig")

    expected_literal = "'" + str(settings_path).replace("'", "''") + "'"
    result = _run_local(
        script_path,
        {
            "hook_event_name": "Stop",
            "session_id": "special-settings-path",
            "last_assistant_message": "I still need to finish verification.",
        },
        cwd=tmp_path,
    )

    assert f"$settingsPath = {expected_literal}" in generated
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"


@pytest.mark.parametrize(
    ("pending_field", "pending_value"),
    [
        (
            "background_tasks",
            [
                {
                    "id": "task-1",
                    "type": "subagent",
                    "status": "running",
                    "description": "review changes",
                }
            ],
        ),
        (
            "session_crons",
            [
                {
                    "id": "cron-1",
                    "schedule": "0 9 * * *",
                    "recurring": True,
                    "prompt": "check the build",
                }
            ],
        ),
    ],
)
def test_local_claude_main_stop_defers_to_pending_work_but_subagent_can_continue(
    tmp_path,
    pending_field,
    pending_value,
):
    script = _local_hook(tmp_path, _incomplete_settings(), "claude")
    session_id = f"pending-{pending_field}"
    main_payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "last_assistant_message": "I still need to integrate the pending result.",
        "background_tasks": [],
        "session_crons": [],
        pending_field: pending_value,
    }

    main_stop = _run_local(script, main_payload, cwd=tmp_path)

    assert main_stop.returncode == 0, main_stop.stderr
    assert main_stop.stdout.strip() == ""
    assert not _state_path(tmp_path).exists()
    assert _decision_log(tmp_path)[-1]["reason"] == "background_work_pending"

    subagent_payload = dict(main_payload)
    subagent_payload.update(
        {
            "hook_event_name": "SubagentStop",
            "agent_id": "agent-1",
            "last_assistant_message": "I still need to finish the subagent work.",
        }
    )
    subagent_stop = _run_local(script, subagent_payload, cwd=tmp_path)

    assert subagent_stop.returncode == 0, subagent_stop.stderr
    assert json.loads(subagent_stop.stdout)["decision"] == "block"
    assert _state_key("claude", session_id, "SubagentStop", "agent-1") in _read_state(tmp_path)


@pytest.mark.parametrize(
    "background_fields",
    [
        {},
        {"background_tasks": [], "session_crons": []},
    ],
)
def test_local_claude_missing_or_empty_background_arrays_continue_normally(
    tmp_path,
    background_fields,
):
    script = _local_hook(tmp_path, _incomplete_settings(), "claude")
    session_id = "no-pending-background-work"
    payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "last_assistant_message": "I still need to finish the foreground work.",
        **background_fields,
    }

    result = _run_local(script, payload, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    assert _state_key("claude", session_id) in _read_state(tmp_path)


def test_local_structured_content_ignores_tool_arguments_after_visible_completion(tmp_path):
    script = _local_hook(tmp_path, _incomplete_settings(), "codex")
    payload = {
        "hook_event_name": "Stop",
        "session_id": "structured-completion",
        "content": [
            {
                "type": "text",
                "text": "Everything is complete. All tests pass.",
            },
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {
                    "command": 'echo "I still need to fix this fixture"',
                },
            },
        ],
    }

    result = _run_local(script, payload, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert not _state_path(tmp_path).exists()
    assert _decision_log(tmp_path)[-1]["reason"] == "terminal_completion_detected"


def test_local_state_v2_stops_on_third_identical_message(tmp_path):
    settings = _incomplete_settings(
        max_continuations=2,
        max_stagnant_continuations=3,
    )
    script = _local_hook(tmp_path, settings, "codex")
    message = "I still need to finish verification."
    payload = {
        "hook_event_name": "Stop",
        "session_id": "stagnant-session",
        "last_assistant_message": message,
    }

    results = [_run_local(script, payload, cwd=tmp_path) for _ in range(3)]

    assert all(result.returncode == 0 for result in results)
    assert json.loads(results[0].stdout)["decision"] == "block"
    assert json.loads(results[1].stdout)["decision"] == "block"
    assert results[2].stdout.strip() == ""
    assert _decision_log(tmp_path)[-1]["reason"] == "no_progress_detected"

    record = _read_state(tmp_path)[_state_key("codex", "stagnant-session")]
    assert record["count"] == 2
    assert record["repeat_count"] == 3
    assert record["message_hash"] == hashlib.sha256(message.encode()).hexdigest()
    assert record["updated_at"] > 0
    assert record["scope_hash"]


def test_local_stagnation_guard_can_be_disabled(tmp_path):
    settings = _incomplete_settings(max_stagnant_continuations=0)
    script = _local_hook(tmp_path, settings, "codex")
    payload = {
        "hook_event_name": "Stop",
        "session_id": "stagnation-disabled",
        "last_assistant_message": "I still need to finish verification.",
    }

    results = [_run_local(script, payload, cwd=tmp_path) for _ in range(4)]

    assert all(result.returncode == 0 for result in results)
    assert all(json.loads(result.stdout)["decision"] == "block" for result in results)
    record = _read_state(tmp_path)[_state_key("codex", "stagnation-disabled")]
    assert record["count"] == 4
    assert record["repeat_count"] == 4


def test_local_different_incomplete_messages_do_not_trip_stagnation_guard(tmp_path):
    settings = _incomplete_settings(max_stagnant_continuations=3)
    script = _local_hook(tmp_path, settings, "codex")

    results = [
        _run_local(
            script,
            {
                "hook_event_name": "Stop",
                "session_id": "progress-session",
                "last_assistant_message": f"I still need to finish verification phase {index}.",
            },
            cwd=tmp_path,
        )
        for index in range(1, 5)
    ]

    assert all(result.returncode == 0 for result in results)
    assert all(json.loads(result.stdout)["decision"] == "block" for result in results)
    assert all(entry["reason"] != "no_progress_detected" for entry in _decision_log(tmp_path))
    record = _read_state(tmp_path)[_state_key("codex", "progress-session")]
    assert record["count"] == 4
    assert record["repeat_count"] == 1


def test_local_state_v2_expires_after_24_hours(tmp_path):
    settings = _incomplete_settings(max_continuations=1)
    script = _local_hook(tmp_path, settings, "codex")
    state_path = _state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    session_id = "expired-session"
    key = _state_key("codex", session_id)
    old = time.time() - 25 * 60 * 60
    state_path.write_text(
        json.dumps(
            {
                key: {
                    "count": 9,
                    "updated_at": old,
                    "message_hash": "old-message",
                    "repeat_count": 9,
                    "scope_hash": "old-scope",
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run_local(
        script,
        {
            "hook_event_name": "Stop",
            "session_id": session_id,
            "last_assistant_message": "I still need to finish verification.",
        },
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    record = _read_state(tmp_path)[key]
    assert record["count"] == 1
    assert record["repeat_count"] == 1
    assert record["updated_at"] > old + 24 * 60 * 60


def test_local_user_prompt_resets_subagent_stop_for_same_session(tmp_path):
    settings = _incomplete_settings(max_continuations=1)
    script = _local_hook(tmp_path, settings, "claude")
    stop_payload = {
        "hook_event_name": "SubagentStop",
        "session_id": "subagent-reset-session",
        "agent_id": "agent-1",
        "last_assistant_message": "I still need to finish the subagent review.",
    }

    first = _run_local(script, stop_payload, cwd=tmp_path)
    exhausted = _run_local(script, stop_payload, cwd=tmp_path)
    prompt = _run_local(
        script,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "subagent-reset-session",
            "prompt": "start new work",
        },
        cwd=tmp_path,
    )
    after_prompt = _run_local(script, stop_payload, cwd=tmp_path)

    assert json.loads(first.stdout)["decision"] == "block"
    assert exhausted.stdout.strip() == ""
    assert prompt.returncode == 0, prompt.stderr
    assert prompt.stdout.strip() == ""
    assert json.loads(after_prompt.stdout)["decision"] == "block"


def test_local_empty_prompt_reset_does_not_leave_metadata_state(tmp_path):
    script = _local_hook(tmp_path, _incomplete_settings(), "codex")

    prompt = _run_local(
        script,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "empty-prompt-reset",
            "prompt": "new work",
        },
        cwd=tmp_path,
    )

    assert prompt.returncode == 0, prompt.stderr
    assert prompt.stdout.strip() == ""
    assert not _state_path(tmp_path).exists()
    assert Path(f"{_state_path(tmp_path)}.lock").exists()


@pytest.mark.parametrize(
    ("source", "should_reset"),
    [
        ("compact", False),
        ("resume", False),
        ("startup", True),
        ("clear", True),
    ],
)
def test_local_session_start_only_resets_for_new_or_cleared_sessions(
    tmp_path,
    source,
    should_reset,
):
    script = _local_hook(
        tmp_path,
        _incomplete_settings(max_continuations=1),
        "codex",
    )
    session_id = f"session-start-{source}"
    stop_payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "last_assistant_message": "I still need to finish verification.",
    }

    first = _run_local(script, stop_payload, cwd=tmp_path)
    session_start = _run_local(
        script,
        {
            "hook_event_name": "SessionStart",
            "session_id": session_id,
            "source": source,
        },
        cwd=tmp_path,
    )
    after_start = _run_local(script, stop_payload, cwd=tmp_path)

    assert json.loads(first.stdout)["decision"] == "block"
    assert session_start.returncode == 0, session_start.stderr
    if should_reset:
        assert json.loads(after_start.stdout)["decision"] == "block"
    else:
        assert after_start.stdout.strip() == ""


def test_local_busy_persistent_state_lock_defers_prompt_reset(tmp_path):
    script = _local_hook(
        tmp_path,
        _incomplete_settings(max_continuations=1),
        "codex",
    )
    session_id = "busy-state-lock"
    payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "last_assistant_message": "I still need to finish verification.",
    }
    first = _run_local(script, payload, cwd=tmp_path)
    state_path = _state_path(tmp_path)
    lock_path = Path(f"{state_path}.lock")
    marker_path = Path(f"{state_path}.reset.{_state_key('codex', session_id, '__scope__')}")

    assert json.loads(first.stdout)["decision"] == "block"
    assert lock_path.exists()
    before = _read_state(tmp_path)
    with _dotnet_file_handle(lock_path):
        prompt = _run_local(
            script,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "prompt": "start fresh",
            },
            cwd=tmp_path,
        )
        assert prompt.returncode == 0, prompt.stderr
        assert marker_path.exists()
        assert _read_state(tmp_path) == before

    after_release = _run_local(script, payload, cwd=tmp_path)

    assert json.loads(after_release.stdout)["decision"] == "block"
    assert _read_state(tmp_path)[_state_key("codex", session_id)]["count"] == 1
    assert not marker_path.exists()
    assert lock_path.exists()


def test_local_failed_marker_replace_is_consumed_only_once(tmp_path):
    script = _local_hook(
        tmp_path,
        _incomplete_settings(max_continuations=1),
        "codex",
    )
    session_id = "marker-delete-failure"
    payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "last_assistant_message": "I still need to finish verification.",
    }
    first = _run_local(script, payload, cwd=tmp_path)
    state_path = _state_path(tmp_path)
    scope_hash = _state_key("codex", session_id, "__scope__")
    marker_path = Path(f"{state_path}.reset.{scope_hash}")
    old_marker_id = f"{int(time.time()) - 3600}:fixed-old-marker"
    marker_path.write_text(old_marker_id, encoding="utf-8")
    legacy_subagent_key = _state_key(
        "codex",
        session_id,
        "SubagentStop",
        "legacy-agent",
    )
    state_with_legacy = _read_state(tmp_path)
    state_with_legacy[legacy_subagent_key] = 9
    state_path.write_text(json.dumps(state_with_legacy), encoding="utf-8")

    assert json.loads(first.stdout)["decision"] == "block"
    with _dotnet_file_handle(
        marker_path,
        mode="Open",
        access="Read",
        share="ReadWrite",
    ):
        prompt = _run_local(
            script,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "prompt": "start fresh",
            },
            cwd=tmp_path,
        )
        after_prompt = _run_local(script, payload, cwd=tmp_path)

        assert prompt.returncode == 0, prompt.stderr
        assert "Failed to persist state reset marker" in prompt.stderr
        assert json.loads(after_prompt.stdout)["decision"] == "block"
        assert marker_path.exists()

    final = _run_local(script, payload, cwd=tmp_path)

    assert final.returncode == 0, final.stderr
    assert final.stdout.strip() == ""
    assert _decision_log(tmp_path)[-1]["reason"] == "max_continuations_reached"
    assert _read_state(tmp_path)[_state_key("codex", session_id)]["count"] == 1
    assert not marker_path.exists()

    legacy_subagent = _run_local(
        script,
        {
            "hook_event_name": "SubagentStop",
            "session_id": session_id,
            "agent_id": "legacy-agent",
            "last_assistant_message": "I still need to finish the subagent work.",
        },
        cwd=tmp_path,
    )
    assert json.loads(legacy_subagent.stdout)["decision"] == "block"
    assert _read_state(tmp_path)[legacy_subagent_key]["count"] == 1


def test_local_unreadable_old_marker_expires_by_marker_mtime(tmp_path):
    script = _local_hook(
        tmp_path,
        _incomplete_settings(max_continuations=1),
        "codex",
    )
    session_id = "old-unreadable-marker"
    payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "last_assistant_message": "I still need to finish verification.",
    }
    first = _run_local(script, payload, cwd=tmp_path)
    state_path = _state_path(tmp_path)
    marker_path = Path(
        f"{state_path}.reset.{_state_key('codex', session_id, '__scope__')}"
    )
    marker_path.mkdir()
    old = time.time() - 25 * 60 * 60
    os.utime(marker_path, (old, old))

    second = _run_local(script, payload, cwd=tmp_path)

    assert json.loads(first.stdout)["decision"] == "block"
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == ""
    assert _read_state(tmp_path)[_state_key("codex", session_id)]["count"] == 1


def test_local_legacy_integer_state_is_preserved_and_upgraded(tmp_path):
    settings = _incomplete_settings(max_continuations=3)
    script = _local_hook(tmp_path, settings, "codex")
    state_path = _state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    session_id = "legacy-state-session"
    key = _state_key("codex", session_id)
    state_path.write_text(json.dumps({key: 1}), encoding="utf-8")

    result = _run_local(
        script,
        {
            "hook_event_name": "Stop",
            "session_id": session_id,
            "last_assistant_message": "I still need to finish verification.",
        },
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    record = _read_state(tmp_path)[key]
    assert isinstance(record, dict)
    assert record["count"] == 2
    assert record["repeat_count"] == 1
    assert record["message_hash"]
    assert record["updated_at"] > 0


def test_local_prompt_removes_only_current_legacy_main_key(tmp_path):
    script = _local_hook(tmp_path, _incomplete_settings(), "codex")
    state_path = _state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    current_key = _state_key("codex", "legacy-current")
    other_key = _state_key("codex", "legacy-other")
    state_path.write_text(
        json.dumps({current_key: 1, other_key: 7}),
        encoding="utf-8",
    )

    prompt = _run_local(
        script,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "legacy-current",
            "prompt": "new work",
        },
        cwd=tmp_path,
    )

    assert prompt.returncode == 0, prompt.stderr
    state = _read_state(tmp_path)
    assert current_key not in state
    assert state[other_key]["count"] == 7
    assert state[other_key]["scope_hash"] == ""


def test_local_legacy_scalars_migrate_once_using_state_file_mtime(tmp_path):
    script = _local_hook(tmp_path, _incomplete_settings(), "codex")
    state_path = _state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    current_key = _state_key("codex", "legacy-migration-current")
    other_key = _state_key("codex", "legacy-migration-other")
    state_path.write_text(
        json.dumps({current_key: 1, other_key: 7}),
        encoding="utf-8",
    )
    legacy_mtime = time.time() - 60 * 60
    os.utime(state_path, (legacy_mtime, legacy_mtime))

    first = _run_local(
        script,
        {
            "hook_event_name": "Stop",
            "session_id": "legacy-migration-current",
            "last_assistant_message": "I still need to finish phase one.",
        },
        cwd=tmp_path,
    )
    first_state = _read_state(tmp_path)
    migrated_updated_at = first_state[other_key]["updated_at"]
    second = _run_local(
        script,
        {
            "hook_event_name": "Stop",
            "session_id": "legacy-migration-current",
            "last_assistant_message": "I still need to finish phase two.",
        },
        cwd=tmp_path,
    )
    second_state = _read_state(tmp_path)

    assert json.loads(first.stdout)["decision"] == "block"
    assert json.loads(second.stdout)["decision"] == "block"
    assert first_state[current_key]["count"] == 2
    assert first_state[other_key]["count"] == 7
    assert abs(migrated_updated_at - legacy_mtime) < 3
    assert second_state[other_key]["updated_at"] == migrated_updated_at


def test_local_expired_legacy_scalars_use_file_mtime_for_ttl(tmp_path):
    script = _local_hook(
        tmp_path,
        _incomplete_settings(max_continuations=1),
        "codex",
    )
    state_path = _state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    current_key = _state_key("codex", "expired-legacy-current")
    other_key = _state_key("codex", "expired-legacy-other")
    state_path.write_text(
        json.dumps({current_key: 9, other_key: 7}),
        encoding="utf-8",
    )
    old = time.time() - 25 * 60 * 60
    os.utime(state_path, (old, old))

    result = _run_local(
        script,
        {
            "hook_event_name": "Stop",
            "session_id": "expired-legacy-current",
            "last_assistant_message": "I still need to finish verification.",
        },
        cwd=tmp_path,
    )

    assert json.loads(result.stdout)["decision"] == "block"
    state = _read_state(tmp_path)
    assert state[current_key]["count"] == 1
    assert other_key not in state


def test_local_prompt_reset_prevents_legacy_subagent_count_inheritance(tmp_path):
    script = _local_hook(
        tmp_path,
        _incomplete_settings(max_continuations=1),
        "claude",
    )
    state_path = _state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    session_id = "legacy-subagent-reset"
    subagent_key = _state_key("claude", session_id, "SubagentStop", "agent-legacy")
    state_path.write_text(json.dumps({subagent_key: 9}), encoding="utf-8")
    old = time.time() - 60
    os.utime(state_path, (old, old))

    prompt = _run_local(
        script,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id,
            "prompt": "start fresh",
        },
        cwd=tmp_path,
    )
    subagent_stop = _run_local(
        script,
        {
            "hook_event_name": "SubagentStop",
            "session_id": session_id,
            "agent_id": "agent-legacy",
            "last_assistant_message": "I still need to finish the review.",
        },
        cwd=tmp_path,
    )

    assert prompt.returncode == 0, prompt.stderr
    assert json.loads(subagent_stop.stdout)["decision"] == "block"
    assert _read_state(tmp_path)[subagent_key]["count"] == 1


def test_local_stop_git_snapshot_runs_only_for_block_after_lock_release(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git is not available")

    cases = [
        (
            "completed",
            _incomplete_settings(
                git_auto_snapshot=True,
                git_snapshot_on_start=True,
            ),
            "Everything is complete. All tests pass.",
            False,
        ),
        (
            "max",
            _incomplete_settings(
                max_continuations=0,
                git_auto_snapshot=True,
                git_snapshot_on_start=True,
            ),
            "I still need to finish verification.",
            False,
        ),
        (
            "no-progress",
            _incomplete_settings(
                max_stagnant_continuations=1,
                git_auto_snapshot=True,
                git_snapshot_on_start=True,
            ),
            "I still need to finish verification.",
            False,
        ),
        (
            "block",
            _incomplete_settings(
                git_auto_snapshot=True,
                git_snapshot_on_start=True,
            ),
            "I still need to finish verification.",
            True,
        ),
    ]
    generated = ""
    for name, settings, message, expect_snapshot in cases:
        config_dir = tmp_path / f"config-{name}"
        project_dir = tmp_path / f"project-{name}"
        config_dir.mkdir()
        project_dir.mkdir()
        script = _local_hook(config_dir, settings, "codex")
        result = _run_local(
            script,
            {
                "hook_event_name": "Stop",
                "session_id": f"snapshot-{name}",
                "last_assistant_message": message,
            },
            cwd=project_dir,
        )

        assert result.returncode == 0, result.stderr
        assert (project_dir / ".git").exists() is expect_snapshot
        if expect_snapshot:
            assert json.loads(result.stdout)["decision"] == "block"
            generated = script.read_text(encoding="utf-8-sig")
        else:
            assert result.stdout.strip() == ""

    durable_index = generated.index("# The decision state is durable")
    release_index = generated.index("Release-StateLock", durable_index)
    snapshot_index = generated.index(
        '$gitSnapshotHash = Create-GitSnapshot -Message "git-snapshot"',
        durable_index,
    )
    assert release_index < snapshot_index
    assert "Creating git snapshot on stop hook" not in generated
    for allow_reason in (
        "terminal_completion_detected",
        "max_continuations_reached",
        "no_progress_detected",
    ):
        assert generated.index(allow_reason) < snapshot_index

    # Permission and continuation locks use durable lock files as well; file
    # existence is never treated as ownership.
    assert "[System.IO.FileMode]::CreateNew" not in generated
    assert "Remove-Item -Path $permissionLockPath" not in generated


def test_local_git_auto_push_has_hard_five_second_timeout(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git is not available")

    project_dir = tmp_path / "slow-push-project"
    bare_dir = tmp_path / "slow-push-origin.git"
    config_dir = tmp_path / "slow-push-config"
    project_dir.mkdir()
    config_dir.mkdir()

    commands = [
        (project_dir, ("init",)),
        (project_dir, ("checkout", "-b", "main")),
        (project_dir, ("config", "user.name", "Auto Continue Test")),
        (project_dir, ("config", "user.email", "auto-continue@example.invalid")),
    ]
    for cwd, args in commands:
        result = _git(*args, cwd=cwd)
        assert result.returncode == 0, result.stderr
    (project_dir / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (("add", "seed.txt"), ("commit", "-m", "seed")):
        result = _git(*args, cwd=project_dir)
        assert result.returncode == 0, result.stderr
    init_bare = _git("init", "--bare", str(bare_dir), cwd=tmp_path)
    assert init_bare.returncode == 0, init_bare.stderr
    for args in (
        ("remote", "add", "origin", str(bare_dir)),
        ("push", "-u", "origin", "main"),
    ):
        result = _git(*args, cwd=project_dir)
        assert result.returncode == 0, result.stderr

    remote_before = _git(
        "--git-dir",
        str(bare_dir),
        "rev-parse",
        "refs/heads/main",
        cwd=tmp_path,
    ).stdout.strip()
    pre_push = project_dir / ".git" / "hooks" / "pre-push"
    pre_push.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8", newline="\n")
    os.chmod(pre_push, 0o755)
    (project_dir / "seed.txt").write_text("seed\nchanged\n", encoding="utf-8")

    script = _local_hook(
        config_dir,
        _incomplete_settings(
            git_auto_snapshot=True,
            git_snapshot_on_start=True,
            git_auto_push=True,
        ),
        "codex",
    )
    started = time.monotonic()
    hook = _run_local(
        script,
        {
            "hook_event_name": "Stop",
            "session_id": "slow-git-push",
            "last_assistant_message": "I still need to finish verification.",
        },
        cwd=project_dir,
    )
    elapsed = time.monotonic() - started

    local_after = _git("rev-parse", "HEAD", cwd=project_dir).stdout.strip()
    remote_after = _git(
        "--git-dir",
        str(bare_dir),
        "rev-parse",
        "refs/heads/main",
        cwd=tmp_path,
    ).stdout.strip()
    generated = script.read_text(encoding="utf-8-sig")
    timeout_helper = generated.split("function Invoke-GitCommandWithTimeout", 1)[1].split(
        "function Create-GitSnapshot",
        1,
    )[0]
    push_function = generated.split("function Push-GitSnapshot", 1)[1].split(
        "try {",
        1,
    )[0]

    assert hook.returncode == 0, hook.stderr
    assert json.loads(hook.stdout)["decision"] == "block"
    assert elapsed < 12
    assert "timed out after 5 seconds" in hook.stderr
    assert local_after != remote_before, hook.stderr
    assert remote_after == remote_before
    assert "[int]$TimeoutMilliseconds = 5000" in timeout_helper
    assert "$process.WaitForExit($TimeoutMilliseconds)" in timeout_helper
    assert "taskkill.exe" in timeout_helper
    assert "function Invoke-GitCommandWithinBudget" in timeout_helper
    assert "$gitBudgetMilliseconds = 5000" in generated
    assert '"commit.gpgSign=false"' in generated
    assert "$pushOutput = git push" not in push_function


@pytest.mark.skipif(os.name != "nt", reason="Windows command shim exercises taskkill /T")
def test_local_entire_git_snapshot_has_five_second_budget(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    config_dir = tmp_path / "config"
    project_dir = tmp_path / "project"
    fake_bin.mkdir()
    config_dir.mkdir()
    project_dir.mkdir()
    (project_dir / "work.txt").write_text("changed\n", encoding="utf-8")
    (fake_bin / "git.cmd").write_text(
        "@echo off\n"
        "if /I \"%~1\"==\"rev-parse\" (\n"
        "  echo .git\n"
        "  exit /b 0\n"
        ")\n"
        "if /I \"%~1\"==\"status\" (\n"
        "  powershell.exe -NoProfile -Command \"Start-Sleep -Seconds 30\"\n"
        "  exit /b 0\n"
        ")\n"
        "exit /b 0\n",
        encoding="utf-8",
    )
    script = _local_hook(
        config_dir,
        _incomplete_settings(
            git_auto_snapshot=True,
            git_snapshot_on_start=True,
            git_auto_push=False,
        ),
        "codex",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    started = time.monotonic()
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        input=json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "slow-git-status",
                "last_assistant_message": "I still need to finish verification.",
            }
        ),
        cwd=project_dir,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=12,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    assert elapsed < 9
    assert "Git snapshot timed out after 5 seconds" in result.stderr
    assert _read_state(config_dir)[_state_key("codex", "slow-git-status")]["count"] == 1
