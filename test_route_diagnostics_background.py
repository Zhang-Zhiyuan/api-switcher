"""Headless thread-affinity checks for background diagnostic report preparation."""
from datetime import datetime, timedelta, timezone
from queue import Queue
import threading
import time
from types import MethodType

import pytest

from test_proxy_route_diagnostics import snapshot
from ui.dialogs import route_diagnostics_dialog as module


class _Widget:
    def __init__(self, value=""):
        self.value = value
        self.options = {}
        self.owner = threading.get_ident()
        self._textbox = self
        self.tags = []

    def _check(self):
        assert threading.get_ident() == self.owner, "worker must never access Tk widgets"

    def configure(self, **kwargs):
        self._check()
        self.options.update(kwargs)

    def get(self, *_args):
        self._check()
        return self.value

    def set(self, value):
        self._check()
        self.value = value

    def delete(self, *_args):
        self._check()
        self.value = ""

    def insert(self, _position, text):
        self._check()
        self.value = text

    def tag_add(self, *ranges):
        self._check()
        self.tags.append(ranges)


class _View:
    def __init__(self, loader=None):
        self._scopes = {"A": "a", "B": "b"}
        self._scope = "A"
        self._loader = loader or (lambda _scope: snapshot())
        self._snapshot = None
        self._closed = False
        self._busy = False
        self._reading = False
        self._generation = 0
        self._queue = Queue()
        self._cancelled = threading.Event()
        self._poll_id = None
        self._start_id = None
        self._query_host = ""
        self._stale = False
        self._visible_report_text = ""
        self._timers = {}
        self._timer_counter = 0
        self.owner = threading.get_ident()
        self.copied = []
        self.clipboard_clear = lambda: self.copied.clear()
        self.clipboard_append = self.copied.append
        for name in ("_report", "_refresh", "_query", "_copy", "_overview", "_scope_combo", "_status", "_entry"):
            setattr(self, name, _Widget())
        for name in ("_set_report", "_install_report", "_copy_report", "_set_busy", "_switch_scope", "refresh",
                     "_start_operation", "_poll", "_show_failure", "_render", "_show_query", "_show_overview"):
            setattr(self, name, MethodType(getattr(module.RouteDiagnosticsDialog, name), self))

    def after(self, _delay, callback):
        assert threading.get_ident() == self.owner
        self._timer_counter += 1
        token = str(self._timer_counter)
        self._timers[token] = callback
        return token

    def after_cancel(self, token):
        assert threading.get_ident() == self.owner
        self._timers.pop(token, None)

    def poll(self):
        token = self._poll_id
        assert token in self._timers
        self._timers.pop(token)()

    def finish(self):
        deadline = time.monotonic() + 4
        while self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not self._queue.empty()
        self.poll()

    def close(self):
        self._closed = True
        self._cancelled.set()
        self._snapshot = None
        self._generation += 1
        self._timers.clear()


def test_initial_read_and_full_report_run_off_main_thread(monkeypatch):
    calls = []
    original = module._prepare_report

    def report(data, query):
        calls.append((query, threading.get_ident()))
        return original(data, query)

    loaded = []
    def load(scope):
        loaded.append((scope, threading.get_ident()))
        return snapshot()

    monkeypatch.setattr(module, "_prepare_report", report)
    view = _View(load)
    view.refresh()
    assert view._snapshot is None and view._busy
    view.finish()
    assert loaded[0][0] == "a"
    assert loaded[0][1] != threading.get_ident()
    assert calls == [("", loaded[0][1])]
    assert "运行规则快照" in view._report.value
    assert not view._busy and view._snapshot


def test_query_and_overview_prepare_locally_off_thread_without_reloading(monkeypatch):
    loaded, reports = [], []
    original = module._prepare_report

    def report(data, query):
        reports.append((query, threading.get_ident()))
        return original(data, query)

    monkeypatch.setattr(module, "_prepare_report", report)
    view = _View(lambda scope: loaded.append(scope) or snapshot())
    view.refresh()
    view.finish()
    view._entry.set("https://api.openai.com/v1?token=PRIVATE_SYNTHETIC_QUERY")
    view._show_query()
    assert view._entry.value == "api.openai.com"
    assert view._busy and view._overview.options["state"] == "disabled"
    view._show_overview()  # Disabled actions cannot mutate the in-flight query.
    assert view._query_host == "api.openai.com"
    view.finish()
    assert "目标：api.openai.com" in view._report.value
    assert "PRIVATE_SYNTHETIC_QUERY" not in view._report.value
    view._show_overview()
    view.finish()
    assert "运行规则快照" in view._report.value
    assert loaded == ["a"]
    assert [query for query, _thread in reports] == ["", "api.openai.com", ""]
    assert all(thread != threading.get_ident() for _query, thread in reports)


def test_slow_report_does_not_block_ui_and_close_drops_its_result(monkeypatch):
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    original = module._prepare_report

    def slow(data, query):
        started.set()
        assert release.wait(4)
        result = original(data, query)
        done.set()
        return result

    monkeypatch.setattr(module, "_prepare_report", slow)
    view = _View()
    view.refresh()
    assert started.wait(4)
    try:
        view._status.configure(text="UI remains available while report is pending")
        assert view._snapshot is None
        view.close()
    finally:
        release.set()
    assert done.wait(4)
    time.sleep(0.01)
    assert view._queue.empty()
    assert not view._timers and view._snapshot is None


def test_stale_generation_cannot_replace_current_report():
    view = _View()
    view.refresh()
    view.finish()
    before = view._report.value
    view._queue.put((view._generation - 1, snapshot(), module._ReportPresentation("old response", ()), ""))
    view.poll()
    assert view._report.value == before


def test_ttl_report_refresh_schedules_only_one_poll_chain():
    view = _View()
    view.refresh()
    view.finish()
    view._snapshot.captured_at = datetime.now(timezone.utc) - timedelta(seconds=65)
    view.poll()
    assert view._busy and len(view._timers) == 1
    view.finish()
    assert view._stale and "已过期" in view._status.options["text"]
    assert len(view._timers) == 1
    view.poll()
    assert not view._busy and len(view._timers) == 1


@pytest.mark.parametrize("failure", ["report", "thread_start"])
def test_report_and_start_failures_restore_refresh_and_allow_retry(monkeypatch, failure):
    original_report, original_start = module._prepare_report, threading.Thread.start

    def fail(*_args, **_kwargs):
        raise TimeoutError()

    if failure == "report":
        monkeypatch.setattr(module, "_prepare_report", fail)
    else:
        monkeypatch.setattr(threading.Thread, "start", fail)
    view = _View()
    view.refresh()
    view.finish()
    assert not view._busy and view._snapshot is None
    assert "TimeoutError" in view._report.value
    assert view._refresh.options["state"] == "normal"
    monkeypatch.setattr(module, "_prepare_report", original_report)
    monkeypatch.setattr(threading.Thread, "start", original_start)
    view.refresh()
    view.finish()
    assert view._snapshot and not view._busy


def test_preparation_redacts_once_and_batches_tk_highlighting(monkeypatch):
    view = _View()
    data = snapshot()
    text = ("目标：example.test\n探针失败 Authorization: Bearer synthetic-private\n" * 600)
    monkeypatch.setattr(module.diagnostics, "snapshot_report", lambda *_args, **_kwargs: text)
    presentation = module._prepare_report(data, "")
    assert "synthetic-private" not in presentation.text
    view._install_report(presentation)
    assert len(view._report.tags) == 6  # Three batches per tag, not 1200 Tcl calls.
    view._snapshot = data
    view._report.get = lambda *_args: pytest.fail("copy uses the already sanitized presentation")
    view._copy_report()
    assert view.copied == [presentation.text.strip()]
