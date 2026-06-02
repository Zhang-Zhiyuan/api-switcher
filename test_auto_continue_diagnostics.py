import json
from types import SimpleNamespace

from core.auto_continue import diagnostics
from models.auto_continue import AutoContinueSettings


class _FakeProvider:
    def __init__(self, config_dir):
        self._config_dir = config_dir

    def get_config_dir(self):
        return self._config_dir


def test_auto_continue_diagnostics_reads_stop_and_recovery_logs(tmp_path, monkeypatch):
    settings = AutoContinueSettings(
        enabled=True,
        training_auto_continue_enabled=True,
        training_prompt_template_key="classification",
        error_recovery_enabled=True,
        git_auto_snapshot=True,
    )

    monkeypatch.setattr(
        diagnostics.auto_continue_manager,
        "get_provider",
        lambda _provider: _FakeProvider(tmp_path),
    )
    monkeypatch.setattr(
        diagnostics.auto_continue_manager,
        "get_settings",
        lambda _provider: settings,
    )
    monkeypatch.setattr(
        diagnostics.auto_continue_manager,
        "get_status",
        lambda _provider: SimpleNamespace(
            hook_script_exists=True,
            hook_registered=True,
            error_recovery_installed=True,
        ),
    )

    state_dir = tmp_path / "tmp"
    state_dir.mkdir()
    (state_dir / "auto_continue_stop_log.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-05-21T10:00:00+08:00",
                "session_id": "s1",
                "hook_event": "Stop",
                "decision": "block_stop",
                "reason": "incomplete_work_detected",
                "match": "未完成",
                "count": 2,
                "git_commit_hash": "abc1234",
                "excerpt": "项目实际上没有完成，可以继续跑",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "error_recovery_log.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-05-21T10:01:00+08:00",
                "session_id": "s1",
                "hook_event_name": "ResponseError",
                "error_type": "network",
                "recovery_strategy": "retry_with_backoff",
                "action": "attempting_recovery",
                "recovery_count": 3,
                "git_commit_hash": "def5678",
                "error_message": "stream disconnected before completion",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    report = diagnostics.format_auto_continue_diagnostics("Codex", 20)

    assert "Provider: codex" in report
    assert "block_stop=1" in report
    assert "API恢复=1" in report
    assert "Stop Hook: OK" in report
    assert "API恢复 Hook: OK" in report
    assert "Git=abc1234" in report
    assert "恢复次数=3" in report
    assert "分类/表格模型" in report

    events = diagnostics.load_auto_continue_events("Codex", 20)
    assert any(event.hook_event == "ResponseError" for event in events)
