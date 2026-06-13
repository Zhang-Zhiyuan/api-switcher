from types import SimpleNamespace

from ui.tabs.session_migration_tab import _session_record_summary


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
