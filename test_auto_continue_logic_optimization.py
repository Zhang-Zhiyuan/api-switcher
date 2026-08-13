import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from core import remote_auto_continue
from core.auto_continue.error_recovery_script import (
    generate_codex_error_recovery_script,
    generate_error_recovery_script,
)
from core.auto_continue.script_generator import generate_hook_script
from models.auto_continue import AutoContinueSettings


def _settings(**updates) -> dict:
    settings = AutoContinueSettings(
        enabled=True,
        max_continuations=10,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )
    for name, value in updates.items():
        setattr(settings, name, value)
    return settings.to_dict()


def _remote_body(tmp_path: Path, provider: str) -> Path:
    script = remote_auto_continue._generate_remote_hook_script(
        f"/home/test/.{provider}/auto_continue_settings.json",
        f"/home/test/.{provider}/tmp",
        provider,
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = tmp_path / f"remote_{provider}_hook.py"
    body_path.write_text(body, encoding="utf-8")
    return body_path


def _run_remote(
    tmp_path: Path,
    body_path: Path,
    settings: dict,
    payload: dict,
    *,
    state_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = state_dir or (tmp_path / "state")
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
        timeout=15,
        check=False,
    )


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        pytest.skip("PowerShell is not available")
    return executable


def _local_hook(tmp_path: Path, settings: dict, provider: str = "claude") -> Path:
    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    settings_path.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\"), False, provider),
        encoding="utf-8-sig",
    )
    return script_path


def _run_local(
    script_path: Path,
    payload: dict,
    *,
    cwd: Path,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    "message",
    [
        "No unfinished work remains; all tests pass and the task is complete.",
        "Everything is complete. No need to implement anything else.",
        "The previously missing tests were added and now pass.",
        "No test failures remain.",
        "Fixed request timed out handling; all work is complete.",
        'Fixed the "not tested" error; task complete.',
        "Removed the final TODO marker; implementation complete.",
        "已完成全部修改，所有测试都已通过，没有测试失败，也没有未完成项。",
    ],
)
def test_remote_completion_conclusions_do_not_trigger_another_run(tmp_path, message):
    body = _remote_body(tmp_path, "codex")
    result = _run_remote(
        tmp_path,
        body,
        _settings(),
        {
            "hook_event_name": "Stop",
            "session_id": hashlib.sha256(message.encode()).hexdigest(),
            "last_assistant_message": message,
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    log_text = (tmp_path / "state" / "auto_continue_stop_log.jsonl").read_text(encoding="utf-8")
    assert "terminal_completion_detected" in log_text


@pytest.mark.parametrize(
    "message",
    [
        "The implementation is complete, but I still need to add the remaining tests.",
        "Billing implementation is still missing and needs to be added.",
        "Everything is not complete.",
        "Everything is complete, but the implementation is not complete.",
        "I still need to ensure everything is complete.",
        "We must verify that all work is done.",
        "No test failures have been fixed yet.",
        "修改已完成，但仍需补充测试并验证。",
    ],
)
def test_remote_later_unresolved_work_still_continues(tmp_path, message):
    result = _run_remote(
        tmp_path,
        _remote_body(tmp_path, "codex"),
        _settings(),
        {
            "hook_event_name": "Stop",
            "session_id": hashlib.sha256(message.encode()).hexdigest(),
            "last_assistant_message": message,
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"


def test_remote_training_negation_cannot_mark_target_complete(tmp_path):
    body = _remote_body(tmp_path, "claude")
    settings = _settings(
        enabled=False,
        training_auto_continue_enabled=True,
        training_continue_prompt="continue training",
    )

    not_met = _run_remote(
        tmp_path,
        body,
        settings,
        {
            "hook_event_name": "Stop",
            "session_id": "training-not-met",
            "last_assistant_message": "The training target was not met; TRAINING_TARGET_MET has not been reached yet.",
        },
    )
    assert not_met.returncode == 0, not_met.stderr
    assert "continue training" in json.loads(not_met.stdout)["reason"]

    met = _run_remote(
        tmp_path,
        body,
        settings,
        {
            "hook_event_name": "Stop",
            "session_id": "training-met",
            "last_assistant_message": "TRAINING_TARGET_MET. The evaluation target has been reached.",
        },
    )
    assert met.returncode == 0, met.stderr
    assert met.stdout.strip() == ""


def test_remote_unlimited_count_and_prompt_reset_use_one_chain(tmp_path):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"

    unlimited_seed = "codex|session-unlimited|Stop|"
    unlimited_key = hashlib.sha256(unlimited_seed.encode()).hexdigest()
    state_path.write_text(json.dumps({unlimited_key: 100}), encoding="utf-8")
    unlimited = _run_remote(
        tmp_path,
        body,
        _settings(max_continuations=-1, incomplete_patterns=["still need"], blocker_patterns=[]),
        {
            "hook_event_name": "Stop",
            "session_id": "session-unlimited",
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir=state_dir,
    )
    assert unlimited.returncode == 0, unlimited.stderr
    assert json.loads(unlimited.stdout)["decision"] == "block"
    assert json.loads(state_path.read_text(encoding="utf-8"))[unlimited_key]["count"] == 101

    reset_seed = "codex|session-reset|Stop|"
    reset_key = hashlib.sha256(reset_seed.encode()).hexdigest()
    state_path.write_text(json.dumps({reset_key: 1}), encoding="utf-8")
    settings = _settings(max_continuations=1, incomplete_patterns=["still need"], blocker_patterns=[])
    before_reset = _run_remote(
        tmp_path,
        body,
        settings,
        {
            "hook_event_name": "Stop",
            "session_id": "session-reset",
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir=state_dir,
    )
    assert before_reset.stdout.strip() == ""

    prompt = _run_remote(
        tmp_path,
        body,
        settings,
        {"hook_event_name": "UserPromptSubmit", "session_id": "session-reset", "prompt": "continue"},
        state_dir=state_dir,
    )
    assert prompt.returncode == 0, prompt.stderr
    after_reset = _run_remote(
        tmp_path,
        body,
        settings,
        {
            "hook_event_name": "Stop",
            "session_id": "session-reset",
            "last_assistant_message": "I still need to finish verification.",
        },
        state_dir=state_dir,
    )
    assert json.loads(after_reset.stdout)["decision"] == "block"


def test_remote_provider_identity_error_isolation_and_subagent_transcript(tmp_path):
    codex_body = _remote_body(tmp_path, "codex")
    settings = _settings(
        conservative_mode=True,
        incomplete_patterns=["still need"],
        blocker_patterns=[],
    )
    codex_stop = _run_remote(
        tmp_path,
        codex_body,
        settings,
        {
            "hook_event_name": "Stop",
            "session_id": "codex-provider",
            "stop_hook_active": True,
            "last_assistant_message": "I still need to finish tests.",
        },
    )
    assert json.loads(codex_stop.stdout)["decision"] == "block"

    unknown_error = _run_remote(
        tmp_path,
        codex_body,
        settings,
        {
            "hook_event_name": "Error",
            "session_id": "codex-error-isolation",
            "error_message": "error: I still need to finish tests",
        },
    )
    assert unknown_error.returncode == 0, unknown_error.stderr
    assert unknown_error.stdout.strip() == ""

    parent_transcript = tmp_path / "parent.jsonl"
    agent_transcript = tmp_path / "agent.jsonl"
    parent_transcript.write_text(
        json.dumps({"message": {"role": "assistant", "content": "Everything is complete."}}),
        encoding="utf-8",
    )
    agent_transcript.write_text(
        json.dumps({"message": {"role": "assistant", "content": "I still need to finish tests."}}),
        encoding="utf-8",
    )
    subagent = _run_remote(
        tmp_path,
        _remote_body(tmp_path, "claude"),
        settings,
        {
            "hook_event_name": "SubagentStop",
            "session_id": "subagent-transcript",
            "agent_id": "agent-1",
            "transcript_path": str(parent_transcript),
            "agent_transcript_path": str(agent_transcript),
        },
    )
    assert json.loads(subagent.stdout)["decision"] == "block"


def test_remote_repairs_corrupt_state_and_reads_response_headers(tmp_path):
    body = _remote_body(tmp_path, "claude")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "auto_continue_stop_state.json"
    state_path.write_text("{broken", encoding="utf-8")

    completed = _run_remote(
        tmp_path,
        body,
        _settings(),
        {
            "hook_event_name": "Stop",
            "session_id": "repair-corrupt-state",
            "last_assistant_message": "No unfinished work remains; the task is complete.",
        },
        state_dir=state_dir,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(state_path.read_text(encoding="utf-8")) == {}

    recovered = _run_remote(
        tmp_path,
        body,
        _settings(
            enabled=False,
            error_recovery_enabled=True,
            max_error_recoveries=3,
            error_retry_max_delay_seconds=180,
        ),
        {
            "hook_event_name": "ResponseError",
            "session_id": "retry-after-response-headers",
            "status": 429,
            "error_message": "rate limit exceeded",
            "response_headers": {"Retry-After": "2 minutes"},
        },
        state_dir=state_dir,
    )
    output = json.loads(recovered.stdout)
    assert output["commands"][0] == {"type": "wait", "seconds": 120}


def test_remote_rejects_pathological_regex_without_hanging(tmp_path):
    body = _remote_body(tmp_path, "codex")
    started = time.monotonic()
    result = _run_remote(
        tmp_path,
        body,
        _settings(incomplete_patterns=[r"(a+)+$"], blocker_patterns=[]),
        {
            "hook_event_name": "Stop",
            "session_id": "regex-redos",
            "last_assistant_message": ("a" * 32767) + "!",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert time.monotonic() - started < 5
    assert "unsafe regex pattern ignored" in result.stderr.lower()


@pytest.mark.parametrize(
    "payload",
    [
        {"status": 401, "error_message": "unauthorized API key"},
        {"status": 403, "error_message": "permission denied"},
        {"error_message": "quota exceeded, insufficient balance"},
    ],
)
def test_remote_non_retryable_errors_always_notify_outside_budget(
    tmp_path,
    payload,
):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    settings = _settings(
        enabled=False,
        error_recovery_enabled=True,
        max_error_recoveries=0,
        git_auto_snapshot=True,
        git_snapshot_on_recovery=True,
    )
    event = {"hook_event_name": "Error", "session_id": "notify-budget", **payload}

    first = _run_remote(tmp_path, body, settings, event, state_dir=state_dir)
    second = _run_remote(tmp_path, body, settings, event, state_dir=state_dir)

    for result in (first, second):
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output["notify"] is True
        assert output["userMessage"]
    assert not (state_dir / "error_recovery_state.json").exists()
    log_text = (state_dir / "error_recovery_log.jsonl").read_text(encoding="utf-8")
    assert log_text.count('"action":"notify_user"') == 2
    assert "max_recoveries_reached" not in log_text


@pytest.mark.parametrize(
    "payload",
    [
        {"status": 400, "error_message": "malformed request"},
        {"error_message": "an unclassified client event"},
    ],
)
def test_remote_unknown_or_invalid_error_is_logged_without_retry_state(tmp_path, payload):
    body = _remote_body(tmp_path, "codex")
    state_dir = tmp_path / "state"
    result = _run_remote(
        tmp_path,
        body,
        _settings(
            enabled=False,
            error_recovery_enabled=True,
            max_error_recoveries=3,
            git_auto_snapshot=True,
            git_snapshot_on_recovery=True,
        ),
        {"hook_event_name": "Error", "session_id": "ignored-error", **payload},
        state_dir=state_dir,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert not (state_dir / "error_recovery_state.json").exists()
    log_text = (state_dir / "error_recovery_log.jsonl").read_text(encoding="utf-8")
    assert '"action":"ignored_non_recoverable"' in log_text


def test_local_completion_structured_text_training_and_provider_identity(tmp_path):
    settings = _settings()
    script = _local_hook(tmp_path, settings, "codex")

    for index, message in enumerate(
        [
            "No unfinished work remains; all tests pass and the task is complete.",
            "Fixed request timed out handling; all work is complete.",
            "已完成全部修改，所有测试都已通过，没有未完成项。",
        ]
    ):
        result = _run_local(
            script,
            {
                "hook_event_name": "Stop",
                "session_id": f"local-complete-{index}",
                "last_assistant_message": message,
            },
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""

    for index, message in enumerate(
        [
            "Everything is not complete.",
            "Everything is complete, but the implementation is not complete.",
            "I still need to ensure everything is complete.",
            "We must verify that all work is done.",
            "No test failures have been fixed yet.",
        ]
    ):
        result = _run_local(
            script,
            {
                "hook_event_name": "Stop",
                "session_id": f"local-unresolved-{index}",
                "last_assistant_message": message,
            },
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["decision"] == "block"

    structured = _run_local(
        script,
        {
            "hook_event_name": "Stop",
            "session_id": "local-structured",
            "content": [{"type": "text", "text": "I still need to finish tests."}],
        },
        cwd=tmp_path,
    )
    assert structured.returncode == 0, structured.stderr
    assert json.loads(structured.stdout)["decision"] == "block"

    codex_active = _run_local(
        script,
        {
            "hook_event_name": "Stop",
            "session_id": "local-codex-provider",
            "stop_hook_active": True,
            "last_assistant_message": "I still need to finish tests.",
        },
        cwd=tmp_path,
    )
    assert json.loads(codex_active.stdout)["decision"] == "block"

    training_script = _local_hook(
        tmp_path,
        _settings(
            enabled=False,
            training_auto_continue_enabled=True,
            training_continue_prompt="continue local training",
        ),
        "claude",
    )
    training = _run_local(
        training_script,
        {
            "hook_event_name": "Stop",
            "session_id": "local-training-not-met",
            "last_assistant_message": "The training target was not met; TRAINING_TARGET_MET has not been reached yet.",
        },
        cwd=tmp_path,
    )
    assert training.returncode == 0, training.stderr
    assert "continue local training" in json.loads(training.stdout)["reason"]


def test_local_session_fallback_prompt_reset_and_state_write_failure(tmp_path):
    settings = _settings(
        max_continuations=1,
        incomplete_patterns=["still need"],
        blocker_patterns=[],
    )
    script = _local_hook(tmp_path, settings, "codex")
    project_one = tmp_path / "project-one"
    project_two = tmp_path / "project-two"
    project_one.mkdir()
    project_two.mkdir()
    payload_without_session = {
        "hook_event_name": "Stop",
        "last_assistant_message": "I still need to finish tests.",
    }
    first_project = _run_local(script, payload_without_session, cwd=project_one)
    second_project = _run_local(script, payload_without_session, cwd=project_two)
    assert json.loads(first_project.stdout)["decision"] == "block"
    assert json.loads(second_project.stdout)["decision"] == "block"

    session_payload = {
        "hook_event_name": "Stop",
        "session_id": "local-reset",
        "last_assistant_message": "I still need to finish tests.",
    }
    first = _run_local(script, session_payload, cwd=tmp_path)
    exhausted = _run_local(script, session_payload, cwd=tmp_path)
    assert json.loads(first.stdout)["decision"] == "block"
    assert exhausted.stdout.strip() == ""
    prompt = _run_local(
        script,
        {"hook_event_name": "UserPromptSubmit", "session_id": "local-reset", "prompt": "continue"},
        cwd=tmp_path,
    )
    assert prompt.returncode == 0, prompt.stderr
    after_prompt = _run_local(script, session_payload, cwd=tmp_path)
    assert json.loads(after_prompt.stdout)["decision"] == "block"

    state_path = tmp_path / "tmp" / "auto_continue_stop_state.json"
    state_path.write_text("{broken", encoding="utf-8")
    repaired = _run_local(
        script,
        {
            "hook_event_name": "Stop",
            "session_id": "local-corrupt-state",
            "last_assistant_message": "Everything is complete.",
        },
        cwd=tmp_path,
    )
    assert repaired.returncode == 0, repaired.stderr
    assert repaired.stdout.strip() == ""
    assert json.loads(state_path.read_text(encoding="utf-8-sig")) == {}

    state_path.unlink(missing_ok=True)
    broken_state_target = tmp_path / "tmp" / "auto_continue_stop_state.json.tmp"
    broken_state_target.mkdir()
    failed_save = _run_local(
        script,
        {
            "hook_event_name": "Stop",
            "session_id": "local-state-write-failure",
            "last_assistant_message": "I still need to finish tests.",
        },
        cwd=tmp_path,
    )
    assert failed_save.returncode == 0, failed_save.stderr
    assert failed_save.stdout.strip() == ""
    log_text = (tmp_path / "tmp" / "auto_continue_stop_log.jsonl").read_text(encoding="utf-8")
    assert "state_persist_failed" in log_text


def test_settings_bound_regex_cost_and_generated_recovery_headers():
    settings = AutoContinueSettings(continuation_prompt="x" * 8001)
    assert settings.validate()[0] is False

    settings = AutoContinueSettings(incomplete_patterns=["x" * 513])
    assert settings.validate()[0] is False

    settings = AutoContinueSettings(blocker_patterns=["safe"] * 129)
    assert settings.validate()[0] is False

    for script in (
        generate_error_recovery_script("/tmp/settings.json"),
        generate_codex_error_recovery_script("/tmp/settings.json"),
    ):
        assert "response_headers" in script
        assert "responseHeaders" in script

    local_stop_script = generate_hook_script(
        "C:\\Users\\Test\\.codex\\auto_continue_settings.json"
    )
    assert "Regex decision budget exhausted" in local_stop_script
    remote_stop_script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
        "codex",
    )
    assert "REGEX_TOTAL_BUDGET_SECONDS = 1.0" in remote_stop_script
    assert "Potentially unsafe regex pattern ignored" in remote_stop_script


def test_manager_rolls_back_settings_when_recovery_hook_sync_fails(monkeypatch):
    from core.auto_continue.manager import AutoContinueManager

    old_settings = AutoContinueSettings(enabled=True, error_recovery_enabled=False)

    class FakeProvider:
        def __init__(self):
            self.current = AutoContinueSettings.from_dict(old_settings.to_dict())
            self.uninstall_calls = 0

        def load_settings(self):
            return AutoContinueSettings.from_dict(self.current.to_dict())

        def update_settings(self, settings):
            self.current = AutoContinueSettings.from_dict(settings.to_dict())

        def install_error_recovery(self):
            raise OSError("recovery hook disk failure")

        def uninstall_error_recovery(self):
            self.uninstall_calls += 1

        def install_guidance(self):
            pass

        def uninstall_guidance(self):
            pass

        def _rollback_settings_update(self, settings):
            self.current = AutoContinueSettings.from_dict(settings.to_dict())
            return ""

    provider = FakeProvider()
    manager = AutoContinueManager()
    monkeypatch.setattr(manager, "get_provider", lambda _name: provider)

    with pytest.raises(RuntimeError, match="recovery hook disk failure"):
        manager.update_settings(
            "codex",
            AutoContinueSettings(enabled=True, error_recovery_enabled=True),
        )

    assert provider.current.to_dict() == old_settings.to_dict()
    assert provider.uninstall_calls == 1
