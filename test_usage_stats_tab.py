from ui.tabs.usage_stats_tab import _summary_success_rate_text


def test_summary_success_rate_text_handles_empty_summary():
    assert _summary_success_rate_text({"total_errors": 0, "total_successes": 0}) == "N/A"


def test_summary_success_rate_text_formats_percentage():
    assert _summary_success_rate_text({"total_errors": 1, "total_successes": 3}) == "75.0%"


def test_summary_success_rate_text_tolerates_missing_values():
    assert _summary_success_rate_text({}) == "N/A"


def test_summary_success_rate_text_tolerates_malformed_values():
    assert _summary_success_rate_text({"total_errors": "bad", "total_successes": "4"}) == "100.0%"
