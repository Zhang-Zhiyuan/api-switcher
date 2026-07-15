import threading
import time

from ui.dialogs import auto_continue_logs_dialog as logs_module


class _Label:
    def __init__(self):
        self.calls = []

    def configure(self, **kwargs):
        self.calls.append(kwargs)


def test_auto_continue_logs_refresh_runs_off_ui_thread(monkeypatch):
    load_started = threading.Event()
    applied = threading.Event()
    detail_updates = []
    payloads = []

    def slow_load(provider, limit):
        assert provider == "claude"
        assert limit == 100
        load_started.set()
        time.sleep(0.15)
        return ["event"]

    def slow_format(provider, limit):
        assert provider == "claude"
        assert limit == 100
        return "diagnostics"

    monkeypatch.setattr(logs_module, "_load_auto_continue_events", slow_load)
    monkeypatch.setattr(logs_module, "_format_auto_continue_diagnostics", slow_format)

    dialog = object.__new__(logs_module.AutoContinueLogsDialog)
    dialog.provider = "claude"
    dialog._refresh_generation = 0
    dialog._limit = lambda: 100
    dialog._status_label = _Label()
    dialog._set_detail = lambda text: detail_updates.append(text)
    dialog._apply_refresh_payload = lambda payload: (payloads.append(payload), applied.set())
    dialog._apply_refresh_error = lambda error: (_ for _ in ()).throw(AssertionError(error))
    dialog.winfo_exists = lambda: True
    dialog._run_on_ui_thread = lambda callback: callback()
    dialog.after = lambda *_args: (_ for _ in ()).throw(AssertionError("worker must not call Tk.after"))

    started_at = time.perf_counter()
    logs_module.AutoContinueLogsDialog._refresh(dialog)
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.05
    assert load_started.wait(1)
    assert applied.wait(1)
    assert payloads == [{"ok": True, "events": ["event"], "diagnostics": "diagnostics", "error": ""}]
    assert any("后台读取自动续跑日志" in text for text in detail_updates)
    assert dialog._status_label.calls[0]["text"] == "正在后台读取自动续跑日志..."


def test_auto_continue_logs_thread_start_failure_reports_error(monkeypatch):
    class BrokenThread:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def start():
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(logs_module.threading, "Thread", BrokenThread)
    dialog = object.__new__(logs_module.AutoContinueLogsDialog)
    dialog.provider = "claude"
    dialog._refresh_generation = 0
    dialog._limit = lambda: 100
    dialog._status_label = _Label()
    dialog._set_detail = lambda _text: None
    errors = []
    dialog._apply_refresh_error = lambda error: errors.append(error)

    logs_module.AutoContinueLogsDialog._refresh(dialog)

    assert errors == ["thread unavailable"]
