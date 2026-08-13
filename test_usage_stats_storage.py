import json
import threading
import time
from datetime import datetime, timedelta

from core import usage_stats as usage_stats_module


def _manager(tmp_path, monkeypatch):
    stats_file = tmp_path / "usage_stats.json"
    monkeypatch.setattr(usage_stats_module, "STATS_FILE", stats_file)
    return usage_stats_module.UsageStatsManager(), stats_file


def test_usage_stats_load_skips_only_malformed_entries(tmp_path, monkeypatch):
    stats_file = tmp_path / "usage_stats.json"
    stats_file.write_text(
        json.dumps({
            "claude:valid": {
                "profile_name": "valid",
                "profile_type": "claude",
                "switch_count": 7,
            },
            "codex:broken": {
                "profile_name": "broken",
                "profile_type": "codex",
                "daily_history": [],
            },
            "not-an-entry": ["broken"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_stats_module, "STATS_FILE", stats_file)

    manager = usage_stats_module.UsageStatsManager()

    assert set(manager.stats) == {"claude:valid"}
    assert manager.stats["claude:valid"].switch_count == 7


def test_usage_stats_bounds_daily_history_without_losing_totals(tmp_path, monkeypatch):
    stats_file = tmp_path / "usage_stats.json"
    today = datetime.now().strftime("%Y-%m-%d")
    old_day = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    future_day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    stats_file.write_text(
        json.dumps({
            "claude:history": {
                "profile_name": "history",
                "profile_type": "claude",
                "switch_count": 99,
                "daily_history": {
                    today: {"date": today, "switch_count": 1},
                    old_day: {"date": old_day, "switch_count": 98},
                    future_day: {"date": future_day, "switch_count": 1},
                    "invalid-date": {"date": "invalid-date", "switch_count": 1},
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_stats_module, "STATS_FILE", stats_file)
    monkeypatch.setattr(usage_stats_module, "MAX_DAILY_HISTORY_DAYS", 3)

    manager = usage_stats_module.UsageStatsManager()
    stats = manager.stats["claude:history"]

    assert stats.switch_count == 99
    assert set(stats.daily_history) == {today}
    manager.save()
    persisted = json.loads(stats_file.read_text(encoding="utf-8"))
    assert set(persisted["claude:history"]["daily_history"]) == {today}


def test_usage_stats_serializes_concurrent_mutation_and_save(tmp_path, monkeypatch):
    manager, stats_file = _manager(tmp_path, monkeypatch)
    state_lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def delayed_write(path, content, encoding="utf-8"):
        with state_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        try:
            time.sleep(0.01)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding=encoding)
        finally:
            with state_lock:
                state["active"] -= 1

    monkeypatch.setattr(usage_stats_module, "atomic_write_text", delayed_write)
    threads = [
        threading.Thread(target=manager.record_switch, args=("shared", "claude"))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert manager.stats["claude:shared"].switch_count == 8
    assert state["max_active"] == 1
    persisted = json.loads(stats_file.read_text(encoding="utf-8"))
    assert persisted["claude:shared"]["switch_count"] == 8
