from ui.tabs.log_viewer_tab import _prepare_log_entries


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
