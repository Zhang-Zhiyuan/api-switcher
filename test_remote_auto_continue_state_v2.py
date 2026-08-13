import hashlib
import json
import os
from pathlib import Path
import posixpath
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from core import remote_auto_continue
from models.auto_continue import AutoContinueSettings


STATE_META_KEY = "__auto_continue_state_meta_v2__"


def _settings(**updates) -> dict:
    settings = AutoContinueSettings(
        enabled=True,
        max_continuations=10,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    ).to_dict()
    settings.update(updates)
    return settings


def _remote_body(tmp_path: Path, provider: str) -> Path:
    script = remote_auto_continue._generate_remote_hook_script(
        f"/home/test/.{provider}/auto_continue_settings.json",
        f"/home/test/.{provider}/tmp",
        provider,
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    path = tmp_path / f"remote_{provider}_state_v2.py"
    path.write_text(body, encoding="utf-8")
    return path


def _run_remote(
    tmp_path: Path,
    body_path: Path,
    settings: dict,
    payload: dict,
    state_dir: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 15,
) -> subprocess.CompletedProcess:
    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    settings_path.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=timeout,
        check=False,
    )


def _remote_namespace(body_path: Path) -> dict:
    source = body_path.read_text(encoding="utf-8")
    prefix, separator, _tail = source.rpartition("\ntry:\n    main()")
    assert separator
    namespace: dict = {}
    exec(compile(prefix, str(body_path), "exec"), namespace)
    return namespace


def _state_records(state: dict) -> dict:
    return {key: value for key, value in state.items() if key != STATE_META_KEY}


def _instrument_git_snapshot_probe(body_path: Path) -> None:
    source = body_path.read_text(encoding="utf-8")
    start = source.index("def run_git_snapshot(auto_push=False):")
    end = source.index("\n\ndef main():", start)
    replacement = '''def run_git_snapshot(auto_push=False):
    state_path = os.path.join(sys.argv[2], "auto_continue_stop_state.json")
    lock_path = state_path + ".lock"
    probe_fd = acquire_state_lock(lock_path, attempts=1)
    probe = {
        "auto_push": bool(auto_push),
        "lock_acquired": probe_fd is not None,
    }
    if probe_fd is not None:
        release_state_lock(probe_fd, lock_path)
    with open(os.environ["SNAPSHOT_PROBE_PATH"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(probe, sort_keys=True) + "\\n")
    return "probe-commit"
'''
    body_path.write_text(source[:start] + replacement + source[end:], encoding="utf-8")


def _snapshot_probes(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _state_key(provider: str, session_id: str, event: str = "Stop", agent_id: str = "") -> str:
    seed = f"{provider}|{session_id}|{event}|{agent_id}"
    return hashlib.sha256(seed.encode()).hexdigest()


def _scope_hash(provider: str, session_id: str) -> str:
    return hashlib.sha256(f"{provider}|{session_id}".encode()).hexdigest()


def _decision_log(state_dir: Path) -> list[dict]:
    path = state_dir / "auto_continue_stop_log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_remote_state_lock_is_persistent_and_exclusive(tmp_path):
    body = _remote_body(tmp_path, "codex")
    namespace = _remote_namespace(body)
    lock_path = tmp_path / "legacy-existing.lock"
    lock_path.write_text("legacy lock file", encoding="utf-8")

    first_fd = namespace["acquire_state_lock"](str(lock_path), attempts=1)
    assert first_fd is not None
    assert namespace["acquire_state_lock"](str(lock_path), attempts=1) is None
    namespace["release_state_lock"](first_fd, str(lock_path))

    assert lock_path.exists()
    next_fd = namespace["acquire_state_lock"](str(lock_path), attempts=1)
    assert next_fd is not None
    namespace["release_state_lock"](next_fd, str(lock_path))
    assert lock_path.exists()
    assert "os.O_EXCL" not in body.read_text(encoding="utf-8")


def test_remote_state_v2_stops_on_third_identical_message(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    settings = _settings(
        max_continuations=-1,
        max_stagnant_continuations=3,
        incomplete_patterns=["still need"],
        blocker_patterns=[],
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "stagnant-session",
        "last_assistant_message": "I still need to finish verification.",
    }

    results = [
        _run_remote(tmp_path, body, settings, payload, state_dir)
        for _ in range(3)
    ]

    assert all(result.returncode == 0 for result in results)
    assert json.loads(results[0].stdout)["decision"] == "block"
    assert json.loads(results[1].stdout)["decision"] == "block"
    assert results[2].stdout.strip() == ""

    state = json.loads((state_dir / "auto_continue_stop_state.json").read_text(encoding="utf-8"))
    record = state[_state_key("codex", "stagnant-session")]
    assert set(record) == {"count", "updated_at", "message_hash", "repeat_count", "scope_hash"}
    assert record["count"] == 2
    assert record["repeat_count"] == 3
    assert record["message_hash"] == hashlib.sha256(payload["last_assistant_message"].encode()).hexdigest()
    assert record["scope_hash"] == _scope_hash("codex", "stagnant-session")
    assert _decision_log(state_dir)[-1]["reason"] == "no_progress_detected"


def test_remote_stagnation_guard_can_be_disabled(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    settings = _settings(
        max_continuations=-1,
        max_stagnant_continuations=0,
        incomplete_patterns=["still need"],
        blocker_patterns=[],
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "stagnation-disabled",
        "last_assistant_message": "I still need to finish verification.",
    }

    results = [_run_remote(tmp_path, body, settings, payload, state_dir) for _ in range(4)]

    assert all(json.loads(result.stdout)["decision"] == "block" for result in results)
    state = json.loads((state_dir / "auto_continue_stop_state.json").read_text(encoding="utf-8"))
    record = state[_state_key("codex", "stagnation-disabled")]
    assert record["count"] == 4
    assert record["repeat_count"] == 4


@pytest.mark.parametrize("invalid_value", [-1, 21, "invalid"])
def test_remote_invalid_stagnation_limit_uses_default_three(tmp_path, invalid_value):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    settings = _settings(
        max_continuations=-1,
        max_stagnant_continuations=invalid_value,
        incomplete_patterns=["still need"],
        blocker_patterns=[],
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": f"invalid-stagnation-{invalid_value}",
        "last_assistant_message": "I still need to finish verification.",
    }

    results = [_run_remote(tmp_path, body, settings, payload, state_dir) for _ in range(3)]

    assert json.loads(results[0].stdout)["decision"] == "block"
    assert json.loads(results[1].stdout)["decision"] == "block"
    assert results[2].stdout.strip() == ""
    assert _decision_log(state_dir)[-1]["reason"] == "no_progress_detected"


def test_remote_zero_continuation_budget_classifies_and_logs_without_blocking(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    result = _run_remote(
        tmp_path,
        body,
        _settings(
            max_continuations=0,
            max_stagnant_continuations=0,
            incomplete_patterns=["still need"],
            blocker_patterns=[],
        ),
        {
            "hook_event_name": "Stop",
            "session_id": "zero-budget",
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    state = json.loads(
        (state_dir / "auto_continue_stop_state.json").read_text(encoding="utf-8")
    )
    assert state[_state_key("codex", "zero-budget")]["count"] == 0
    assert _decision_log(state_dir)[-1]["reason"] == "max_continuations_reached"


@pytest.mark.parametrize("legacy", [False, True])
def test_remote_expired_state_is_reset_and_legacy_int_is_supported(tmp_path, legacy):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    key = _state_key("codex", "expired-session")
    old = time.time() - 25 * 60 * 60
    if legacy:
        state_path.write_text(json.dumps({key: 9}), encoding="utf-8")
        os.utime(state_path, (old, old))
    else:
        state_path.write_text(
            json.dumps(
                {
                    key: {
                        "count": 9,
                        "updated_at": old,
                        "message_hash": "old",
                        "repeat_count": 9,
                        "scope_hash": _scope_hash("codex", "expired-session"),
                    }
                }
            ),
            encoding="utf-8",
        )

    result = _run_remote(
        tmp_path,
        body,
        _settings(max_continuations=1, incomplete_patterns=["still need"], blocker_patterns=[]),
        {
            "hook_event_name": "Stop",
            "session_id": "expired-session",
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir,
    )

    assert json.loads(result.stdout)["decision"] == "block"
    record = json.loads(state_path.read_text(encoding="utf-8"))[key]
    assert record["count"] == 1
    assert record["repeat_count"] == 1
    assert record["updated_at"] > old + 24 * 60 * 60


def test_remote_fresh_legacy_count_is_migrated_without_losing_budget(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    key = _state_key("codex", "legacy-session")
    state_path.write_text(json.dumps({key: 4}), encoding="utf-8")

    result = _run_remote(
        tmp_path,
        body,
        _settings(
            max_continuations=-1,
            max_stagnant_continuations=3,
            incomplete_patterns=["still need"],
            blocker_patterns=[],
        ),
        {
            "hook_event_name": "Stop",
            "session_id": "legacy-session",
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir,
    )

    assert json.loads(result.stdout)["decision"] == "block"
    record = json.loads(state_path.read_text(encoding="utf-8"))[key]
    assert record["count"] == 5
    assert record["repeat_count"] == 1


def test_remote_prompt_migrates_legacy_once_without_deleting_other_session(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    current_key = _state_key("codex", "current-legacy")
    other_key = _state_key("codex", "other-legacy")
    state_path.write_text(json.dumps({current_key: 5, other_key: 7}), encoding="utf-8")
    legacy_mtime = time.time() - 60
    os.utime(state_path, (legacy_mtime, legacy_mtime))

    result = _run_remote(
        tmp_path,
        body,
        _settings(),
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "current-legacy",
        },
        state_dir,
    )

    assert result.returncode == 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert current_key not in state
    assert state[other_key]["count"] == 7
    assert state[other_key]["scope_hash"] == ""
    assert state[other_key]["updated_at"] == pytest.approx(legacy_mtime, abs=2)


def test_remote_prompt_reset_lazily_clears_legacy_subagent_count(tmp_path):
    body = _remote_body(tmp_path, "claude")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    session_id = "legacy-subagent"
    agent_id = "agent-1"
    main_key = _state_key("claude", session_id)
    subagent_key = _state_key("claude", session_id, "SubagentStop", agent_id)
    state_path.write_text(json.dumps({main_key: 9, subagent_key: 8}), encoding="utf-8")

    prompt = _run_remote(
        tmp_path,
        body,
        _settings(),
        {"hook_event_name": "UserPromptSubmit", "session_id": session_id},
        state_dir,
    )
    assert prompt.returncode == 0
    after_prompt = json.loads(state_path.read_text(encoding="utf-8"))
    assert main_key not in after_prompt
    assert after_prompt[subagent_key]["count"] == 8
    assert after_prompt[subagent_key]["scope_hash"] == ""

    stop = _run_remote(
        tmp_path,
        body,
        _settings(
            max_continuations=1,
            max_stagnant_continuations=0,
            incomplete_patterns=["still need"],
            blocker_patterns=[],
        ),
        {
            "hook_event_name": "SubagentStop",
            "session_id": session_id,
            "agent_id": agent_id,
            "last_assistant_message": "I still need to finish the subagent work.",
        },
        state_dir,
    )

    assert json.loads(stop.stdout)["decision"] == "block"
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state[subagent_key]["count"] == 1
    assert final_state[subagent_key]["scope_hash"] == _scope_hash("claude", session_id)


def test_remote_prompt_reset_clears_whole_session_scope_only(tmp_path):
    body = _remote_body(tmp_path, "claude")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    now = time.time()

    def record(scope):
        return {
            "count": 1,
            "updated_at": now,
            "message_hash": "message",
            "repeat_count": 1,
            "scope_hash": scope,
        }

    current_scope = _scope_hash("claude", "scope-session")
    other_scope = _scope_hash("claude", "other-session")
    state_path.write_text(
        json.dumps(
            {
                _state_key("claude", "scope-session"): record(current_scope),
                _state_key("claude", "scope-session", "SubagentStop", "agent-1"): record(current_scope),
                _state_key("claude", "other-session"): record(other_scope),
            }
        ),
        encoding="utf-8",
    )

    result = _run_remote(
        tmp_path,
        body,
        _settings(max_continuations=1),
        {"hook_event_name": "UserPromptSubmit", "session_id": "scope-session", "prompt": "new work"},
        state_dir,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert list(_state_records(state)) == [_state_key("claude", "other-session")]
    assert STATE_META_KEY not in state


@pytest.mark.parametrize("source", ["compact", "resume", "", "future-source"])
def test_remote_session_start_preserves_budget_unless_source_explicitly_resets(tmp_path, source):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    settings = _settings(
        max_continuations=1,
        max_stagnant_continuations=0,
        incomplete_patterns=["still need"],
        blocker_patterns=[],
    )
    stop_payload = {
        "hook_event_name": "Stop",
        "session_id": "session-start-preserve",
        "last_assistant_message": "I still need to finish verification.",
    }
    first = _run_remote(tmp_path, body, settings, stop_payload, state_dir)
    assert json.loads(first.stdout)["decision"] == "block"

    start_payload = {
        "hook_event_name": "SessionStart",
        "session_id": "session-start-preserve",
    }
    if source:
        start_payload["source"] = source
    start = _run_remote(tmp_path, body, settings, start_payload, state_dir)
    second = _run_remote(tmp_path, body, settings, stop_payload, state_dir)

    assert start.returncode == 0
    assert start.stdout.strip() == ""
    assert second.stdout.strip() == ""
    assert _decision_log(state_dir)[-1]["reason"] == "max_continuations_reached"


@pytest.mark.parametrize("source_field", ["source", "session_start_source", "sessionStartSource"])
@pytest.mark.parametrize("source", ["startup", "clear"])
def test_remote_session_start_explicit_reset_sources_start_new_budget(
    tmp_path,
    source_field,
    source,
):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    settings = _settings(
        max_continuations=1,
        max_stagnant_continuations=0,
        incomplete_patterns=["still need"],
        blocker_patterns=[],
    )
    stop_payload = {
        "hook_event_name": "Stop",
        "session_id": "session-start-reset",
        "last_assistant_message": "I still need to finish verification.",
    }
    first = _run_remote(tmp_path, body, settings, stop_payload, state_dir)
    assert json.loads(first.stdout)["decision"] == "block"

    _run_remote(
        tmp_path,
        body,
        settings,
        {
            "hook_event_name": "SessionStart",
            "session_id": "session-start-reset",
            source_field: source,
        },
        state_dir,
    )
    second = _run_remote(tmp_path, body, settings, stop_payload, state_dir)

    assert json.loads(second.stdout)["decision"] == "block"
    assert json.loads(
        (state_dir / "auto_continue_stop_state.json").read_text(encoding="utf-8")
    )[_state_key("codex", "session-start-reset")]["count"] == 1


def test_remote_failed_prompt_reset_is_consumed_by_next_stop(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    lock_path = Path(str(state_path) + ".lock")
    session_id = "pending-reset"
    scope = _scope_hash("codex", session_id)
    key = _state_key("codex", session_id)
    state_path.write_text(
        json.dumps(
            {
                key: {
                    "count": 1,
                    "updated_at": time.time(),
                    "message_hash": "old",
                    "repeat_count": 1,
                    "scope_hash": scope,
                }
            }
        ),
        encoding="utf-8",
    )
    namespace = _remote_namespace(body)
    lock_fd = namespace["acquire_state_lock"](str(lock_path), attempts=1)
    assert lock_fd is not None
    settings = _settings(max_continuations=1, incomplete_patterns=["still need"], blocker_patterns=[])

    try:
        prompt = _run_remote(
            tmp_path,
            body,
            settings,
            {"hook_event_name": "UserPromptSubmit", "session_id": session_id, "prompt": "new work"},
            state_dir,
        )
        marker = Path(str(state_path) + f".reset.{scope}")
        assert prompt.returncode == 0
        assert marker.exists()
    finally:
        namespace["release_state_lock"](lock_fd, str(lock_path))

    stop = _run_remote(
        tmp_path,
        body,
        settings,
        {
            "hook_event_name": "Stop",
            "session_id": session_id,
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir,
    )

    assert json.loads(stop.stdout)["decision"] == "block"
    assert not marker.exists()
    record = json.loads(state_path.read_text(encoding="utf-8"))[key]
    assert record["count"] == 1
    assert record["repeat_count"] == 1
    assert lock_path.exists()


def test_remote_existing_marker_is_consumed_once_when_prompt_cannot_replace_or_delete_it(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    session_id = "undeletable-marker"
    scope = _scope_hash("codex", session_id)
    key = _state_key("codex", session_id)
    state_path.write_text(
        json.dumps(
            {
                key: {
                    "count": 1,
                    "updated_at": time.time(),
                    "message_hash": "old",
                    "repeat_count": 1,
                    "scope_hash": scope,
                }
            }
        ),
        encoding="utf-8",
    )
    marker = Path(f"{state_path}.reset.{scope}")
    marker.mkdir()
    settings = _settings(
        max_continuations=1,
        max_stagnant_continuations=0,
        incomplete_patterns=["still need"],
        blocker_patterns=[],
    )

    prompt = _run_remote(
        tmp_path,
        body,
        settings,
        {"hook_event_name": "UserPromptSubmit", "session_id": session_id},
        state_dir,
    )
    assert prompt.returncode == 0
    prompt_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert key not in prompt_state
    first_marker_id = prompt_state[STATE_META_KEY]["consumed_scope_resets"][scope]["marker_id"]
    assert marker.is_dir()

    payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "last_assistant_message": "I still need to finish verification.",
    }
    first_stop = _run_remote(tmp_path, body, settings, payload, state_dir)
    second_stop = _run_remote(tmp_path, body, settings, payload, state_dir)

    assert json.loads(first_stop.stdout)["decision"] == "block"
    assert second_stop.stdout.strip() == ""
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state[key]["count"] == 1
    consumed = final_state[STATE_META_KEY]["consumed_scope_resets"][scope]
    assert consumed["marker_id"] == first_marker_id
    assert _decision_log(state_dir)[-1]["reason"] == "max_continuations_reached"


def test_remote_expired_reset_marker_does_not_reset_current_budget(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    session_id = "expired-marker"
    scope = _scope_hash("codex", session_id)
    key = _state_key("codex", session_id)
    state_path.write_text(
        json.dumps(
            {
                key: {
                    "count": 1,
                    "updated_at": time.time(),
                    "message_hash": "old",
                    "repeat_count": 1,
                    "scope_hash": scope,
                }
            }
        ),
        encoding="utf-8",
    )
    marker = Path(f"{state_path}.reset.{scope}")
    old = time.time() - 25 * 60 * 60
    marker.write_text(str(old), encoding="utf-8")
    os.utime(marker, (old, old))

    result = _run_remote(
        tmp_path,
        body,
        _settings(
            max_continuations=1,
            max_stagnant_continuations=0,
            incomplete_patterns=["still need"],
            blocker_patterns=[],
        ),
        {
            "hook_event_name": "Stop",
            "session_id": session_id,
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir,
    )

    assert result.stdout.strip() == ""
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state[key]["count"] == 1
    assert state[STATE_META_KEY]["consumed_scope_resets"][scope]["expired"] is True
    assert not marker.exists()
    assert _decision_log(state_dir)[-1]["reason"] == "max_continuations_reached"


def test_remote_expired_reset_marker_does_not_reset_fresh_legacy_budget(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    session_id = "expired-marker-legacy"
    scope = _scope_hash("codex", session_id)
    key = _state_key("codex", session_id)
    # A v1 count has no embedded timestamp. Its fresh state-file mtime proves
    # it is still active even though an abandoned marker is older than the TTL.
    state_path.write_text(json.dumps({key: 1}), encoding="utf-8")
    marker = Path(f"{state_path}.reset.{scope}")
    old = time.time() - 25 * 60 * 60
    marker.write_text(str(old), encoding="utf-8")
    os.utime(marker, (old, old))

    result = _run_remote(
        tmp_path,
        body,
        _settings(
            max_continuations=1,
            max_stagnant_continuations=0,
            incomplete_patterns=["still need"],
            blocker_patterns=[],
        ),
        {
            "hook_event_name": "Stop",
            "session_id": session_id,
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state[key]["count"] == 1
    assert state[key]["scope_hash"] == scope
    assert state[STATE_META_KEY]["consumed_scope_resets"][scope]["expired"] is True
    assert not marker.exists()
    assert _decision_log(state_dir)[-1]["reason"] == "max_continuations_reached"


@pytest.mark.parametrize("classification", ["background", "terminal", "empty", "recovery"])
def test_remote_pending_reset_is_consumed_before_every_stop_classification(tmp_path, classification):
    provider = "codex" if classification == "recovery" else "claude"
    body = _remote_body(tmp_path, provider)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    session_id = f"early-{classification}"
    scope = _scope_hash(provider, session_id)
    key = _state_key(provider, session_id)
    state_path.write_text(
        json.dumps(
            {
                key: {
                    "count": 4,
                    "updated_at": time.time(),
                    "message_hash": "old",
                    "repeat_count": 1,
                    "scope_hash": scope,
                }
            }
        ),
        encoding="utf-8",
    )
    marker = Path(f"{state_path}.reset.{scope}")
    marker.write_text(
        json.dumps({"id": f"marker-{classification}", "created_at": time.time()}),
        encoding="utf-8",
    )
    payload = {"hook_event_name": "Stop", "session_id": session_id}
    if classification == "background":
        payload.update(
            {
                "backgroundTasks": [{"id": "task"}],
                "last_assistant_message": "I still need to wait.",
            }
        )
    elif classification == "terminal":
        payload["last_assistant_message"] = "All requested work is complete."
    elif classification == "recovery":
        payload["error_message"] = "stream disconnected before completion"

    settings = _settings(error_recovery_enabled=classification == "recovery")
    result = _run_remote(tmp_path, body, settings, payload, state_dir)

    assert result.returncode == 0
    if classification != "recovery":
        assert result.stdout.strip() == ""
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert key not in state
    assert state[STATE_META_KEY]["consumed_scope_resets"][scope]["expired"] is False
    assert not marker.exists()


@pytest.mark.parametrize(
    "background_field",
    ["background_tasks", "backgroundTasks", "session_crons", "sessionCrons"],
)
def test_remote_claude_main_stop_defers_to_background_work_before_snapshot(tmp_path, background_field):
    body = _remote_body(tmp_path, "claude")
    state_dir = tmp_path / "state"
    project_dir = tmp_path / "project"
    state_dir.mkdir()
    project_dir.mkdir()
    settings = _settings(git_auto_snapshot=True, git_snapshot_on_start=True)

    main_payload = {
        "hook_event_name": "Stop",
        "session_id": "background-main",
        "cwd": str(project_dir),
        "last_assistant_message": "I still need to wait for the background task.",
    }
    main_payload[background_field] = [{"id": "task-1"}]
    if background_field == "backgroundTasks":
        main_payload["background_tasks"] = []
    elif background_field == "sessionCrons":
        main_payload["session_crons"] = []
    main_stop = _run_remote(
        tmp_path,
        body,
        settings,
        main_payload,
        state_dir,
    )

    assert main_stop.returncode == 0
    assert main_stop.stdout.strip() == ""
    assert not (project_dir / ".git").exists()
    assert not (state_dir / "auto_continue_stop_state.json").exists()
    assert _decision_log(state_dir)[-1]["reason"] == "background_work_pending"

    subagent_stop = _run_remote(
        tmp_path,
        body,
        _settings(incomplete_patterns=["still need"], blocker_patterns=[]),
        {
            "hook_event_name": "SubagentStop",
            "session_id": "background-main",
            "agent_id": "agent-1",
            "background_tasks": [{"id": "task-1"}],
            "last_assistant_message": "I still need to finish the subagent work.",
        },
        state_dir,
    )
    assert json.loads(subagent_stop.stdout)["decision"] == "block"


def test_remote_flatten_text_ignores_non_visible_hook_blocks(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    settings = _settings(incomplete_patterns=["still need"], blocker_patterns=[])

    hidden_only = _run_remote(
        tmp_path,
        body,
        settings,
        {
            "hook_event_name": "Stop",
            "session_id": "hidden-blocks",
            "content": [
                {"type": "thinking", "text": "I still need to expose chain of thought."},
                {"type": "tool_use", "content": "I still need to call a tool."},
                {"type": "tool_result", "content": "I still need a tool result."},
                {"type": "redacted_thinking", "text": "I still need hidden reasoning."},
                {"type": "metadata", "body": "I still need metadata."},
                {"type": "image", "text": "I still need image metadata."},
                {"type": "input_json_delta", "text": "I still need an input delta."},
                {"type": "text", "text": "Visible summary only."},
            ],
        },
        state_dir,
    )
    assert hidden_only.stdout.strip() == ""
    assert _decision_log(state_dir)[-1]["reason"] == "no_incomplete_match"

    visible = _run_remote(
        tmp_path,
        body,
        settings,
        {
            "hook_event_name": "Stop",
            "session_id": "visible-block",
            "content": [{"type": "text", "text": "I still need to finish verification."}],
        },
        state_dir,
    )
    assert json.loads(visible.stdout)["decision"] == "block"


def test_remote_transcript_uses_only_assistant_records(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "All work is complete."}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_result",
                        "content": "I still need to retry the hidden tool.",
                    }
                ),
                json.dumps({"content": "I still need to process roleless metadata."}),
            ]
        ),
        encoding="utf-8",
    )

    result = _run_remote(
        tmp_path,
        body,
        _settings(incomplete_patterns=["still need"], blocker_patterns=[]),
        {
            "hook_event_name": "Stop",
            "session_id": "transcript-role-filter",
            "transcript_path": str(transcript),
        },
        state_dir,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert _decision_log(state_dir)[-1]["reason"] == "terminal_completion_detected"


def test_remote_wrapper_requires_python_37_and_body_parses_as_python_37(tmp_path):
    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
        "codex",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    assert "sys.version_info >= (3, 7)" in script
    assert "Python 3.7+ not found" in script
    assert "sys.version_info >= (3, 6)" not in script
    compile(body, "remote_codex_hook.py", "exec", _feature_version=7)


def test_remote_git_timeout_owns_and_kills_the_posix_process_group(tmp_path):
    body_path = _remote_body(tmp_path, "codex")
    source = body_path.read_text(encoding="utf-8")
    namespace = _remote_namespace(body_path)

    assert "process = subprocess.Popen(args, **popen_kwargs)" in source
    assert 'popen_kwargs["start_new_session"] = True' in source
    assert "os.killpg(process.pid, signal.SIGKILL)" in source
    assert "process.wait(timeout=1.0)" in source
    assert "subprocess.CompletedProcess(args, process.returncode, stdout=stdout)" in source

    result = namespace["git_command"](
        [sys.executable, "-c", "print('git-command-ok')"],
        time.monotonic() + 5.0,
        capture=True,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stdout.strip() == "git-command-ok"

    killed_groups = []

    class ExitedGroupLeader:
        pid = 4242

        @staticmethod
        def poll():
            return 0

    namespace["os"] = SimpleNamespace(
        name="posix",
        killpg=lambda pid, sig: killed_groups.append((pid, sig)),
    )
    namespace["signal"] = SimpleNamespace(SIGKILL=9)
    namespace["terminate_git_process_tree"](ExitedGroupLeader())
    assert killed_groups == [(4242, 9)]


def test_remote_git_snapshot_only_runs_for_block_after_state_unlock(tmp_path):
    body = _remote_body(tmp_path, "codex")
    _instrument_git_snapshot_probe(body)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    probe_path = tmp_path / "snapshot-probes.jsonl"
    env = os.environ.copy()
    env["SNAPSHOT_PROBE_PATH"] = str(probe_path)
    settings = _settings(
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
        max_continuations=1,
        max_stagnant_continuations=0,
        incomplete_patterns=["still need"],
        blocker_patterns=[],
    )

    completed = _run_remote(
        tmp_path,
        body,
        settings,
        {
            "hook_event_name": "Stop",
            "session_id": "git-completed",
            "last_assistant_message": "All work is complete.",
        },
        state_dir,
        env=env,
    )
    assert completed.stdout.strip() == ""
    assert _snapshot_probes(probe_path) == []

    block_payload = {
        "hook_event_name": "Stop",
        "session_id": "git-block",
        "last_assistant_message": "I still need to finish verification.",
    }
    blocked = _run_remote(tmp_path, body, settings, block_payload, state_dir, env=env)
    assert json.loads(blocked.stdout)["decision"] == "block"
    assert _snapshot_probes(probe_path) == [{"auto_push": False, "lock_acquired": True}]

    max_reached = _run_remote(tmp_path, body, settings, block_payload, state_dir, env=env)
    assert max_reached.stdout.strip() == ""
    assert len(_snapshot_probes(probe_path)) == 1

    no_progress = _run_remote(
        tmp_path,
        body,
        settings | {"max_continuations": -1, "max_stagnant_continuations": 1},
        {
            "hook_event_name": "Stop",
            "session_id": "git-no-progress",
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir,
        env=env,
    )
    assert no_progress.stdout.strip() == ""
    assert len(_snapshot_probes(probe_path)) == 1
    assert _decision_log(state_dir)[-1]["reason"] == "no_progress_detected"


def test_remote_git_timeout_warns_but_still_outputs_block_decision(tmp_path):
    body = _remote_body(tmp_path, "codex")
    source = body.read_text(encoding="utf-8")
    start = source.index("def git_command(args, deadline, capture=False, combine_stderr=False):")
    end = source.index("\n\ndef push_git_snapshot", start)
    replacement = '''def git_command(args, deadline, capture=False, combine_stderr=False):
    raise subprocess.TimeoutExpired(args, GIT_SNAPSHOT_BUDGET_SECONDS)
'''
    body.write_text(source[:start] + replacement + source[end:], encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    result = _run_remote(
        tmp_path,
        body,
        _settings(
            git_auto_snapshot=True,
            git_snapshot_on_start=True,
            incomplete_patterns=["still need"],
            blocker_patterns=[],
        ),
        {
            "hook_event_name": "Stop",
            "session_id": "git-timeout",
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"
    assert "Git snapshot skipped after 5s time budget" in result.stderr


def test_remote_uninstall_removes_only_well_formed_reset_marker_files(monkeypatch):
    prefix = "auto_continue_stop_state.json.reset."
    state_dir = "/home/test/.codex/tmp"
    valid_lower = prefix + "a" * 64
    valid_upper = prefix + "B" * 64
    too_short = prefix + "c" * 63
    extra_suffix = prefix + "d" * 64 + ".tmp"
    non_hex = prefix + "g" * 64
    valid_named_directory = prefix + "e" * 64

    class FakeSFTP:
        def __init__(self):
            self.entries = {
                posixpath.join(state_dir, valid_lower): stat.S_IFREG | 0o600,
                posixpath.join(state_dir, valid_upper): stat.S_IFREG | 0o600,
                posixpath.join(state_dir, too_short): stat.S_IFREG | 0o600,
                posixpath.join(state_dir, extra_suffix): stat.S_IFREG | 0o600,
                posixpath.join(state_dir, non_hex): stat.S_IFREG | 0o600,
                posixpath.join(state_dir, valid_named_directory): stat.S_IFDIR | 0o700,
            }
            self.removed = []

        def listdir(self, directory):
            return [
                posixpath.basename(path)
                for path in self.entries
                if posixpath.dirname(path) == directory
            ]

        def lstat(self, path):
            return SimpleNamespace(st_mode=self.entries[path])

        def remove(self, path):
            self.removed.append(path)
            self.entries.pop(path, None)

        def close(self):
            return None

    fake_sftp = FakeSFTP()

    class FakeClient:
        def open_sftp(self):
            return fake_sftp

    paths = SimpleNamespace(
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        state_dir=state_dir,
        guidance_path="/home/test/AGENTS.md",
    )
    monkeypatch.setattr(
        remote_auto_continue,
        "_connect",
        lambda _name: (SimpleNamespace(host="test-host"), FakeClient()),
    )
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda *_args: paths)
    monkeypatch.setattr(remote_auto_continue, "_unregister_codex_hook", lambda *_args: None)
    monkeypatch.setattr(remote_auto_continue, "_uninstall_guidance", lambda *_args: None)

    result = remote_auto_continue.uninstall_remote_auto_continue("server", "codex")

    assert result == "已卸载 test-host 的 Codex 远端自动续跑"
    assert posixpath.join(state_dir, valid_lower) in fake_sftp.removed
    assert posixpath.join(state_dir, valid_upper) in fake_sftp.removed
    assert posixpath.join(state_dir, too_short) not in fake_sftp.removed
    assert posixpath.join(state_dir, extra_suffix) not in fake_sftp.removed
    assert posixpath.join(state_dir, non_hex) not in fake_sftp.removed
    assert posixpath.join(state_dir, valid_named_directory) not in fake_sftp.removed
