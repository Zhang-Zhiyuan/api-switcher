from types import SimpleNamespace

from ui.tabs.session_migration_tab import SessionMigrationTab, _session_record_summary


def test_session_record_summary_counts_visible_selection_and_size():
    records = [
        SimpleNamespace(key="a", size_bytes=100),
        SimpleNamespace(key="b", size_bytes=200),
    ]

    summary = _session_record_summary(records, {"a", "missing"})

    assert summary["visible_keys"] == {"a", "b"}
    assert summary["selected_count"] == 1
    assert summary["total_size"] == 300


def test_session_record_summary_tolerates_empty_and_negative_sizes():
    records = [
        SimpleNamespace(key="a", size_bytes=None),
        SimpleNamespace(key="b", size_bytes=-10),
        SimpleNamespace(key="c", size_bytes="bad"),
    ]

    summary = _session_record_summary(records, {"a", "b"})

    assert summary["selected_count"] == 2
    assert summary["total_size"] == 0


def test_session_migration_suspend_cancels_initial_refresh():
    tab = object.__new__(SessionMigrationTab)
    tab._initial_refresh_after_id = "initial"
    tab._record_render_after_id = None
    tab._deferred_refresh_pending = False
    tab._deferred_render_pending = False
    cancelled = []
    tab.after_cancel = lambda after_id: cancelled.append(after_id)
    tab._schedule_inactive_clear = lambda: None

    SessionMigrationTab._suspend_background_work(tab)

    assert cancelled == ["initial"]
    assert tab._initial_refresh_after_id is None
    assert tab._deferred_refresh_pending is True


def test_session_migration_refresh_defers_when_inactive(monkeypatch):
    tab = object.__new__(SessionMigrationTab)
    tab._destroyed = False
    tab._initial_refresh_after_id = "initial"
    tab._deferred_refresh_pending = False
    tab._cards_frame = object()

    monkeypatch.setattr("ui.tabs.session_migration_tab.is_active_tab", lambda _widget: False)

    SessionMigrationTab.refresh(tab)

    assert tab._initial_refresh_after_id is None
    assert tab._deferred_refresh_pending is True
