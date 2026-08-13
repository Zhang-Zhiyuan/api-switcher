import json
from datetime import datetime, timedelta

from core.auto_continue.error_analyzer import ErrorRecoveryAnalyzer


def _write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def test_error_analyzer_accepts_timezone_timestamps_and_merges_log_sources(tmp_path):
    now = datetime.now().astimezone()
    legacy = tmp_path / "error_recovery_log.jsonl"
    current = tmp_path / "tmp" / "error_recovery_log.jsonl"
    attempted = {
        "timestamp": (now - timedelta(minutes=2)).isoformat(),
        "session_id": "attempted",
        "error_type": "rate_limit",
        "action": "attempting_recovery",
        "recovery_count": 1,
    }
    succeeded = {
        "timestamp": (now - timedelta(minutes=1)).isoformat(),
        "session_id": "succeeded",
        "error_type": "server_error",
        "action": "recovery_succeeded",
        "recovery_count": 2,
    }
    _write_jsonl(legacy, [attempted, succeeded])
    # A migration may leave an identical entry in both locations.
    _write_jsonl(current, [attempted])

    stats = ErrorRecoveryAnalyzer(current, [legacy]).analyze(days=1)

    assert stats.total_errors == 2
    assert stats.total_recoveries == 2
    assert stats.recovery_success_rate == 100.0
    assert stats.avg_recovery_count == 1.5
    assert [entry["session_id"] for entry in stats.recent_errors] == ["attempted", "succeeded"]


def test_error_analyzer_counts_current_production_recovery_dispatches(tmp_path):
    log_path = tmp_path / "error_recovery_log.jsonl"
    timestamp = datetime.now().astimezone().isoformat()
    _write_jsonl(
        log_path,
        [
            {
                "timestamp": timestamp,
                "session_id": "claude-retry",
                "error_type": "rate_limit_exceeded",
                "error_code": "rate_limit_error",
                "error_message": "Too many requests",
                "http_status": 429,
                "recovery_strategy": "wait_and_retry",
                "action": "attempting_recovery",
                "recovery_count": 1,
            },
            {
                "timestamp": timestamp,
                "session_id": "codex-retry",
                "error_type": "rate_limit",
                "error_code": "rate_limit_error",
                "error_message": "Too many requests",
                "http_status": 429,
                "action": "attempting_recovery",
                "recovery_count": 2,
            },
            {
                "timestamp": timestamp,
                "session_id": "claude-notify",
                "error_type": "authentication_error",
                "error_code": "unauthorized",
                "error_message": "Invalid API key",
                "http_status": 401,
                "recovery_strategy": "notify_user",
                "action": "attempting_recovery",
                "recovery_count": 1,
            },
            {
                "timestamp": timestamp,
                "session_id": "codex-maxed",
                "error_type": "server",
                "error_code": "server_error",
                "error_message": "Service unavailable",
                "http_status": 503,
                "action": "max_recoveries_reached",
                "recovery_count": 3,
            },
        ],
    )

    stats = ErrorRecoveryAnalyzer(log_path).analyze(days=1)

    assert stats.total_errors == 4
    assert stats.total_recoveries == 2
    assert stats.recovery_success_rate == 50.0
    assert stats.avg_recovery_count == 1.5


def test_error_analyzer_explicit_failure_overrides_dispatch_action(tmp_path):
    log_path = tmp_path / "error_recovery_log.jsonl"
    _write_jsonl(
        log_path,
        [
            {
                "timestamp": datetime.now().astimezone().isoformat(),
                "session_id": "future-format",
                "error_type": "network",
                "action": "attempting_recovery",
                "recovery_count": 1,
                "recovery_success": False,
            }
        ],
    )

    stats = ErrorRecoveryAnalyzer(log_path).analyze(days=1)

    assert stats.total_recoveries == 0


def test_error_analyzer_keeps_same_file_repeats_but_dedupes_migrated_copy(tmp_path):
    now = datetime.now().astimezone()
    current = tmp_path / "tmp" / "error_recovery_log.jsonl"
    legacy = tmp_path / "error_recovery_log.jsonl"
    repeated_event = {
        "timestamp": now.isoformat(),
        "session_id": "same-session",
        "error_type": "network",
        "error_code": "stream_disconnected",
        "error_message": "connection reset",
        "http_status": 0,
        "action": "attempting_recovery",
        "recovery_count": 1,
    }
    _write_jsonl(current, [repeated_event, repeated_event])
    _write_jsonl(legacy, [repeated_event])

    analyzer = ErrorRecoveryAnalyzer(current, [legacy])
    stats = analyzer.analyze(days=1)

    assert stats.total_errors == 2
    assert stats.total_recoveries == 2
    assert len(analyzer.get_error_timeline(days=1)[now.strftime("%Y-%m-%d")]) == 2
    assert len(analyzer.get_session_details("same-session")) == 2
