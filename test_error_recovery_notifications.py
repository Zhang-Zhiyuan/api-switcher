import json
from pathlib import Path
import shutil
import subprocess

import pytest

from core.auto_continue.error_recovery_script import (
    generate_codex_error_recovery_script,
    generate_error_recovery_script,
)
from models.auto_continue import AutoContinueSettings


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        pytest.skip("PowerShell is not available")
    return executable


def _run_hook(
    directory: Path,
    generator,
    payload: dict,
) -> subprocess.CompletedProcess:
    directory.mkdir()
    settings_path = directory / "auto_continue_settings.json"
    script_path = directory / "error_recovery.ps1"
    settings = AutoContinueSettings(
        enabled=False,
        error_recovery_enabled=True,
        max_error_recoveries=0,
        git_auto_snapshot=True,
        git_snapshot_on_recovery=True,
    )
    settings_path.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )
    script_path.write_text(
        generator(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )
    return subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=directory,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    ("name", "generator", "payload", "expected_message"),
    [
        (
            "claude",
            generate_error_recovery_script,
            {
                "hook_event_name": "ResponseError",
                "session_id": "claude-auth-zero-budget",
                "status": 401,
                "error_message": "invalid API key",
            },
            "API 密钥",
        ),
        (
            "codex",
            generate_codex_error_recovery_script,
            {
                "hook_event_name": "Error",
                "session_id": "codex-quota-zero-budget",
                "status": 400,
                "error_message": "insufficient quota",
            },
            "配额",
        ),
    ],
)
def test_non_retryable_errors_notify_with_zero_budget_without_state_or_snapshot(
    tmp_path,
    name,
    generator,
    payload,
    expected_message,
):
    directory = tmp_path / name
    result = _run_hook(directory, generator, payload)

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert expected_message in output["userMessage"]
    assert output.get("recover", False) is False
    assert not (directory / "tmp" / "error_recovery_state.json").exists()
    assert not (directory / ".git").exists()


def test_unknown_codex_error_does_not_consume_budget_or_create_snapshot(tmp_path):
    directory = tmp_path / "codex-unknown"
    result = _run_hook(
        directory,
        generate_codex_error_recovery_script,
        {
            "hook_event_name": "Error",
            "session_id": "codex-unknown-error",
            "error_message": "an application-specific condition occurred",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert not (directory / "tmp" / "error_recovery_state.json").exists()
    assert not (directory / ".git").exists()
