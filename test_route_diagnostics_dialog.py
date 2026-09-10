import threading
import time

import pytest

from test_proxy_route_diagnostics import snapshot
from ui.dialogs.route_diagnostics_dialog import RouteDiagnosticsDialog


def wait(root, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not predicate():
        root.update()
        time.sleep(0.01)
    root.update()
    assert predicate()


@pytest.fixture
def dialog(tk_root):
    loaded = []
    def load(scope):
        loaded.append((scope, threading.get_ident()))
        return snapshot()
    view = RouteDiagnosticsDialog(tk_root, scopes={"SSH A": "a", "SSH B": "b"}, loader=load)
    try:
        wait(tk_root, lambda: view._snapshot is not None)
        yield view, loaded
    finally:
        view.destroy()
        tk_root.update()


def test_load_is_async_and_scope_selection_reads_only_selected_host(dialog, tk_root):
    view, loaded = dialog
    assert len(loaded) == 1 and loaded[0][0] == "a"
    assert loaded[0][1] != threading.get_ident()
    view._switch_scope("SSH B")
    wait(tk_root, lambda: not view._busy)
    assert [scope for scope, _ in loaded] == ["a", "b"]
    assert view._scope_combo.get() == "SSH B"


def test_url_query_is_local_and_drops_query_secrets(dialog):
    view, loaded = dialog
    view._entry.insert(0, "https://api.openai.com/v1?token=PRIVATE_QUERY_SECRET")
    view._show_query()
    assert len(loaded) == 1
    assert view._entry.get() == "api.openai.com"
    text = view._report.get("1.0", "end")
    assert "PRIVATE_QUERY_SECRET" not in text and "日本家宽 01" in text
    view._show_overview()
    assert "运行规则快照" in view._report.get("1.0", "end")


def test_close_while_worker_pending_never_updates_destroyed_widgets(tk_root):
    ready = threading.Event()
    done = threading.Event()
    def load(_scope):
        ready.wait(3)
        done.set()
        return snapshot()
    view = RouteDiagnosticsDialog(tk_root, loader=load)
    tk_root.update()
    assert view._busy
    view.destroy()
    view.destroy()
    ready.set()
    assert done.wait(3)
    tk_root.update()
    assert not view.winfo_exists()


def test_failure_clears_old_snapshot_and_allows_retry(dialog, tk_root):
    view, _ = dialog
    def fail(_scope):
        raise RuntimeError("synthetic failure")
    view._loader = fail
    view.refresh()
    assert view._snapshot is None
    wait(tk_root, lambda: not view._busy)
    assert "读取失败" in view._status.cget("text")
    assert view._refresh.cget("state") == "normal"
    assert view._query.cget("state") == "disabled"


def test_new_controls_fit_narrow_window_and_dpi(dialog, tk_root):
    view, _ = dialog
    import customtkinter as ctk
    scale = ctk.ScalingTracker.widget_scaling
    try:
        for scaling in (1.0, 1.5, 2.0):
            ctk.set_widget_scaling(scaling)
            view.geometry("680x540")
            tk_root.update()
            for button in (view._query, view._refresh, view._overview, view._copy):
                assert button.winfo_rootx() >= view.winfo_rootx()
                assert button.winfo_rootx() + button.winfo_width() <= view.winfo_rootx() + view.winfo_width() + 2
                assert button.winfo_rooty() + button.winfo_height() <= view.winfo_rooty() + view.winfo_height() + 2
    finally:
        ctk.set_widget_scaling(scale)


def test_copy_report_is_explicit_and_only_copies_redacted_visible_text(dialog, monkeypatch):
    view, _ = dialog
    copied = []
    monkeypatch.setattr(view, "clipboard_clear", lambda: None)
    monkeypatch.setattr(view, "clipboard_append", copied.append)
    view._set_report("探针失败\nAuthorization: Bearer synthetic-private-key")
    assert copied == []
    view._copy_report()
    assert len(copied) == 1 and "synthetic-private-key" not in copied[0]
    assert "[REDACTED]" in copied[0]
    assert view._report._textbox.tag_ranges("warning")


def test_empty_exception_does_not_break_result_pump(dialog, tk_root):
    view, _ = dialog
    def fail(_scope):
        raise TimeoutError()
    view._loader = fail
    view.refresh()
    wait(tk_root, lambda: not view._busy)
    assert "读取失败" in view._status.cget("text")
    assert view._poll_id is not None
    assert view._query.cget("state") == "disabled"
    view._loader = lambda _scope: snapshot()
    view.refresh()
    wait(tk_root, lambda: view._snapshot is not None)
    assert view._query.cget("state") == "normal"


def test_invalid_result_does_not_crash_render_or_disable_retry(dialog, tk_root):
    view, _ = dialog
    data = snapshot()
    data.runtime["mode"] = None
    view._loader = lambda _scope: data
    view.refresh()
    wait(tk_root, lambda: not view._busy)
    assert view._snapshot is None
    assert "失败" in view._status.cget("text")
    assert view._poll_id is not None
    assert view._refresh.cget("state") == "normal"


def test_manual_refresh_before_startup_cancels_pending_initial_read(tk_root, monkeypatch):
    errors = []
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *args: errors.append(args))
    view = RouteDiagnosticsDialog(tk_root, loader=lambda _: snapshot())
    initial = view._start_id
    view.refresh()
    assert initial not in tk_root.tk.call("after", "info")
    view.destroy()
    tk_root.update()
    assert errors == []
