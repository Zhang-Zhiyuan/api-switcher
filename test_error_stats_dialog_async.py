import threading

from core.auto_continue import error_analyzer
from ui.dialogs.error_stats_dialog import ErrorStatsDialog


class _ThreadBoundVar:
    def __init__(self, value):
        self.value = value
        self.owner = threading.get_ident()

    def get(self):
        assert threading.get_ident() == self.owner
        return self.value


class _Widget:
    def configure(self, **_kwargs):
        return None

    def delete(self, *_args):
        return None

    def insert(self, *_args):
        return None


def test_error_stats_captures_tk_values_before_starting_worker(monkeypatch):
    analyzed_days = []
    applied = threading.Event()

    class _Analyzer:
        def analyze(self, days):
            analyzed_days.append(days)
            return error_analyzer.ErrorStats(total_errors=2)

    monkeypatch.setattr(error_analyzer, "get_analyzer", lambda _provider: _Analyzer())

    dialog = object.__new__(ErrorStatsDialog)
    dialog.provider = "claude"
    dialog._load_generation = 0
    dialog.days_var = _ThreadBoundVar("30")
    dialog.status_label = _Widget()
    dialog.detail_text = _Widget()
    dialog._safe_after = lambda callback: callback()
    dialog._display_stats = lambda stats: applied.set()
    dialog._display_error = lambda error: (_ for _ in ()).throw(AssertionError(error))

    ErrorStatsDialog._load_stats(dialog)

    assert applied.wait(1)
    assert analyzed_days == [30]
    assert dialog.stats.total_errors == 2


def test_error_stats_ignores_stale_background_result(monkeypatch):
    release_first = threading.Event()
    first_started = threading.Event()
    first_finished = threading.Event()
    second_applied = threading.Event()
    applied_totals = []

    class _Analyzer:
        def analyze(self, days):
            if days == 7:
                first_started.set()
                release_first.wait(1)
                first_finished.set()
            return error_analyzer.ErrorStats(total_errors=days)

    monkeypatch.setattr(error_analyzer, "get_analyzer", lambda _provider: _Analyzer())

    dialog = object.__new__(ErrorStatsDialog)
    dialog.provider = "claude"
    dialog._load_generation = 0
    dialog.days_var = _ThreadBoundVar("7")
    dialog.status_label = _Widget()
    dialog.detail_text = _Widget()
    dialog._safe_after = lambda callback: callback()
    def display_stats(stats):
        applied_totals.append(stats.total_errors)
        if stats.total_errors == 30:
            second_applied.set()

    dialog._display_stats = display_stats
    dialog._display_error = lambda error: (_ for _ in ()).throw(AssertionError(error))

    ErrorStatsDialog._load_stats(dialog)
    assert first_started.wait(1)
    dialog.days_var.value = "30"
    ErrorStatsDialog._load_stats(dialog)
    assert second_applied.wait(1)
    release_first.set()
    assert first_finished.wait(1)
    assert applied_totals == [30]


def test_error_stats_thread_start_failure_reports_error(monkeypatch):
    class BrokenThread:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def start():
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr("ui.dialogs.error_stats_dialog.threading.Thread", BrokenThread)
    dialog = object.__new__(ErrorStatsDialog)
    dialog.provider = "claude"
    dialog._load_generation = 0
    dialog.days_var = _ThreadBoundVar("7")
    dialog.status_label = _Widget()
    dialog.detail_text = _Widget()
    errors = []
    dialog._display_error = lambda error: errors.append(error)

    ErrorStatsDialog._load_stats(dialog)

    assert errors == ["thread unavailable"]
