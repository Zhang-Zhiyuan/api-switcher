from ui.tabs.log_viewer_tab import LOG_LEVELS, LogViewerTab, _prepare_log_entries
from core.log_handler import LogManager


def test_prepare_log_entries_filters_and_counts_by_level():
    visible, counts = _prepare_log_entries(
        [
            {"level": "DEBUG", "levelno": 10, "message": "debug"},
            {"level": "INFO", "levelno": 20, "message": "info"},
            {"level": "ERROR", "levelno": 40, "message": "error"},
        ],
        "WARNING",
    )

    assert visible == [("ERROR", "error")]
    assert counts["DEBUG"] == 1
    assert counts["INFO"] == 1
    assert counts["ERROR"] == 1
    assert counts["WARNING"] == 0


def test_prepare_log_entries_handles_malformed_entries():
    visible, counts = _prepare_log_entries(
        [
            "bad",
            {"level": "custom", "message": None},
            {"level": "WARNING", "levelno": "bad", "message": 123},
        ],
        "DEBUG",
    )

    assert visible == [("INFO", ""), ("WARNING", "123")]
    assert counts["INFO"] == 1
    assert counts["WARNING"] == 1
    assert counts["DEBUG"] == 0


def test_log_manager_bounds_history_and_queue():
    manager = LogManager()

    for index in range(manager.MAX_HISTORY + 25):
        manager.publish({"level": "INFO", "levelno": 20, "message": f"line {index}"})

    history = manager.get_recent_entries()

    assert len(history) == manager.MAX_HISTORY
    assert history[0]["message"] == "line 25"
    assert manager.get_log_queue().qsize() <= manager.MAX_QUEUE

    manager.clear_history()

    assert manager.get_recent_entries() == []
    assert manager.get_log_queue().qsize() == 0


def test_log_manager_initial_snapshot_consumes_duplicate_queue_backlog():
    manager = LogManager()
    manager.publish({"level": "INFO", "levelno": 20, "message": "startup"})

    initial = manager.consume_recent_entries()

    assert [entry["message"] for entry in initial] == ["startup"]
    assert manager.get_log_queue().qsize() == 0

    manager.publish({"level": "INFO", "levelno": 20, "message": "live"})

    assert manager.get_log_queue().get_nowait()["message"] == "live"


def test_log_viewer_updates_counts_from_batch_without_rescanning_history():
    tab = object.__new__(LogViewerTab)
    tab.MAX_STORED_ENTRIES = 3
    tab._log_entries = [
        {"level": "INFO", "levelno": 20, "message": "old-1"},
        {"level": "INFO", "levelno": 20, "message": "old-2"},
        {"level": "WARNING", "levelno": 30, "message": "old-3"},
    ]
    tab._filter_level = "CRITICAL"
    tab._log_counts = {level: 0 for level in LOG_LEVELS}
    tab._log_counts.update({"INFO": 2, "WARNING": 1})
    tab._filtered_entry_count = 0
    status_updates = []
    tab._update_render_status = lambda total_visible=None: status_updates.append(total_visible)

    LogViewerTab._append_log_entries(
        tab,
        [{"level": "ERROR", "levelno": 40, "message": "new"}],
    )

    assert [entry["message"] for entry in tab._log_entries] == ["old-2", "old-3", "new"]
    assert tab._log_counts["INFO"] == 1
    assert tab._log_counts["WARNING"] == 1
    assert tab._log_counts["ERROR"] == 1
    assert tab._filtered_entry_count == 0
    assert status_updates == [None]


def test_log_viewer_rerenders_when_overflow_removes_a_visible_line():
    render_calls = []
    tab = object.__new__(LogViewerTab)
    tab.MAX_STORED_ENTRIES = 3
    tab.MAX_RENDERED_LINES = 3
    tab._log_entries = [
        {"level": "ERROR", "levelno": 40, "message": f"old-{index}"}
        for index in range(3)
    ]
    tab._filter_level = "ERROR"
    tab._log_counts = {level: 0 for level in LOG_LEVELS}
    tab._log_counts["ERROR"] = 3
    tab._filtered_entry_count = 3
    tab._render_log_entries = lambda: render_calls.append(list(tab._log_entries))

    LogViewerTab._append_log_entries(
        tab,
        [{"level": "DEBUG", "levelno": 10, "message": "new-hidden"}],
    )

    assert [entry["message"] for entry in tab._log_entries] == ["old-1", "old-2", "new-hidden"]
    assert tab._log_counts["ERROR"] == 2
    assert tab._log_counts["DEBUG"] == 1
    assert tab._filtered_entry_count == 2
    assert len(render_calls) == 1
