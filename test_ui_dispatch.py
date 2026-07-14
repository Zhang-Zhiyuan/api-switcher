from __future__ import annotations

from ui.ui_dispatch import run_on_ui_thread


class _TopLevel:
    def __init__(self):
        self.callbacks = []

    def _run_on_ui_thread(self, callback):
        self.callbacks.append(callback)


class _Widget:
    def __init__(self, top=None):
        self.top = top or _TopLevel()
        self.after_calls = []

    def winfo_toplevel(self):
        return self.top

    def after(self, delay_ms, callback):
        self.after_calls.append((delay_ms, callback))


def test_run_on_ui_thread_prefers_top_level_dispatch():
    widget = _Widget()
    called = []

    assert run_on_ui_thread(widget, lambda: called.append("done")) is True

    assert len(widget.top.callbacks) == 1
    assert widget.after_calls == []
    widget.top.callbacks[0]()
    assert called == ["done"]


def test_run_on_ui_thread_falls_back_to_after_without_dispatch():
    class _NoDispatchTop:
        pass

    widget = _Widget(top=_NoDispatchTop())

    assert run_on_ui_thread(widget, lambda: None) is True

    assert len(widget.after_calls) == 1
    assert widget.after_calls[0][0] == 0


def test_run_on_ui_thread_uses_widget_dispatch_without_touching_tk_from_worker():
    class _DirectDispatchWidget:
        def __init__(self):
            self.callbacks = []

        def _run_on_ui_thread(self, callback):
            self.callbacks.append(callback)

        def winfo_toplevel(self):
            raise AssertionError("winfo_toplevel must not be called from the worker")

    widget = _DirectDispatchWidget()
    called = []

    assert run_on_ui_thread(widget, lambda: called.append("done")) is True

    assert len(widget.callbacks) == 1
    widget.callbacks[0]()
    assert called == ["done"]


def test_run_on_ui_thread_reports_failed_dispatch():
    class _BrokenWidget:
        def _run_on_ui_thread(self, _callback):
            return False

    assert run_on_ui_thread(_BrokenWidget(), lambda: None) is False
