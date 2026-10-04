"""Native synthetic-only route-summary layout checks; run serially by release owner."""
from dataclasses import replace
from pathlib import Path
import socket
import threading
import time

import customtkinter as ctk
import pytest

from core import proxy_route_diagnostics as diagnostics
from test_route_runtime_overview import synthetic_snapshot, ssh_tab
from ui.dialogs.route_diagnostics_dialog import RouteDiagnosticsDialog
from ui.tabs.ssh_tab import SSHTab
from ui.widgets.service_route_overview import RuntimeRouteOverview, ServiceRouteOverview


def settle(root, seconds=0.25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.005)


def capture(window, name):
    from tools.ui_visual_audit import capture_window_image
    folder = Path(__file__).resolve().parent / "dist" / "route-runtime-ui"
    folder.mkdir(parents=True, exist_ok=True)
    capture_window_image(window).save(folder / f"{name}.png")


def reveal_label(root, viewport, label):
    """Scroll the synthetic canvas only; validate the full label, not just mapping."""
    bounds = viewport.bbox("all")
    assert bounds and bounds[3] > bounds[1]
    content_top = label.winfo_rooty() - viewport.winfo_rooty() + viewport.canvasy(0)
    # A small top margin preserves context while allowing labels near the end
    # of a short document to use Tk's normal bottom clamp.
    viewport.yview_moveto(max(0, content_top - bounds[1] - 16) / (bounds[3] - bounds[1]))
    settle(root)
    assert label.winfo_viewable()
    assert label.winfo_rooty() >= viewport.winfo_rooty() - 2
    assert label.winfo_rooty() + label.winfo_height() <= viewport.winfo_rooty() + viewport.winfo_height() + 2
    assert label.winfo_rootx() >= viewport.winfo_rootx() - 2
    assert label.winfo_rootx() + label.winfo_width() <= viewport.winfo_rootx() + viewport.winfo_width() + 2
    # CTk's outer frame fitting is insufficient if its wrapped Tk label is
    # clipped internally. Check the full requested text drawing dimensions.
    assert label._label.winfo_reqwidth() <= label.winfo_width() + 2
    assert label._label.winfo_reqheight() <= label.winfo_height() + 2


@pytest.fixture
def no_external_io(tk_root, monkeypatch):
    errors = []
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *args: errors.append(args))
    def fail(*_args, **_kwargs):
        pytest.fail("Synthetic runtime overview checks must never access the network")
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(diagnostics, "load_snapshot", fail)
    yield tk_root
    assert not errors


@pytest.mark.parametrize("scale", [1.0, 2.5])
@pytest.mark.parametrize("kind", ["win", "ssh"])
def test_snapshot_rows_fit_dpi_and_expire_without_changing_saved_intent(no_external_io, kind, scale):
    root = no_external_io
    previous = ctk.ScalingTracker.widget_scaling
    ctk.set_widget_scaling(scale)
    window = ctk.CTkToplevel(root)
    window.title("Synthetic runtime route overview")
    window.geometry("1100x800")
    scroll = ctk.CTkScrollableFrame(window)
    scroll.pack(fill="both", expand=True, padx=10, pady=10)
    snapshot = synthetic_snapshot("ssh-a" if kind == "ssh" else "Win11 本机（含共享 WSL）")
    summary = diagnostics.snapshot_overview(snapshot)
    try:
        if kind == "win":
            view = ServiceRouteOverview(scroll, command=lambda _service: None, inspect_command=lambda: None,
                                        preset_command=lambda: None)
            view.set_routes(snapshot.preferences, [])
        else:
            view = RuntimeRouteOverview(scroll)
        view.pack(fill="x")
        assert view.set_runtime_summary(summary, view.begin_runtime_read())
        settle(root, 1.25)
        labels = view._runtime_labels()
        viewport = scroll._parent_canvas
        for label in labels.values():
            if not label.winfo_manager():
                continue
            assert label.winfo_width() > 40
            assert label.winfo_rootx() >= viewport.winfo_rootx() - 2
            assert label.winfo_rootx() + label.winfo_width() <= viewport.winfo_rootx() + viewport.winfo_width() + 2
            assert "探针历史" not in label.cget("text")
        assert "内核当前选择（读取时）" in labels["openai"].cget("text")
        capture(window, f"{kind}-{int(scale * 100)}-current")
        note = (view._note if kind == "win" else next(
            child for child in view.winfo_children()
            if isinstance(child, ctk.CTkLabel) and child.cget("text").startswith("以下每项目标")))
        for label in (view._runtime_status, note):
            reveal_label(root, viewport, label)
        if scale == 2.5:
            reveal_label(root, viewport, labels["openai"])
            capture(window, f"{kind}-{int(scale * 100)}-focus")
        else:
            viewport.yview_moveto(0)
            settle(root)
        if view._runtime_after_id is not None:
            view.after_cancel(view._runtime_after_id)
        view._expire_runtime_summary()
        settle(root)
        assert "已过期" in view._runtime_status.cget("text")
        assert "历史内核选择" in labels["openai"].cget("text")
        capture(window, f"{kind}-{int(scale * 100)}-expired")
        if kind == "win":
            view.set_routes({**snapshot.preferences, "service_route_modes": {}}, [])
        else:
            view.invalidate_runtime("服务器选择已变化，请重新检查")
        assert view._runtime_snapshot is None
        assert not any(label.winfo_manager() for label in labels.values())
    finally:
        window.destroy()
        ctk.set_widget_scaling(previous)
        settle(root, 0.05)


def test_completed_async_diagnostic_publishes_only_selected_scope_and_drops_old_token(no_external_io):
    root = no_external_io
    host = ctk.CTkToplevel(root)
    host.geometry("1000x700")
    view = RuntimeRouteOverview(host)
    view.pack(fill="x", padx=10, pady=10)
    tab = ssh_tab(view)
    calls = []
    def loader(scope):
        calls.append((scope, threading.get_ident()))
        return synthetic_snapshot(scope)
    dialog = RouteDiagnosticsDialog(
        host, scopes={"ssh-a": "ssh-a", "ssh-b": "ssh-b"}, loader=loader,
        on_read_started=lambda scope: SSHTab._begin_proxy_runtime_read(tab, scope),
        on_loaded=lambda scope, summary, token: SSHTab._accept_proxy_runtime_summary(tab, scope, summary, token),
    )
    try:
        deadline = time.monotonic() + 5
        while view._runtime_snapshot is None and time.monotonic() < deadline:
            settle(root, 0.05)
        assert view._runtime_snapshot.scope == "ssh-a"
        assert calls == [("ssh-a", calls[0][1])]
        assert calls[0][1] != threading.get_ident()
        old = SSHTab._begin_proxy_runtime_read(tab, "ssh-a")
        newer = SSHTab._begin_proxy_runtime_read(tab, "ssh-b")
        summary = diagnostics.snapshot_overview(synthetic_snapshot("ssh-b"))
        SSHTab._accept_proxy_runtime_summary(tab, "ssh-b", summary, newer)
        SSHTab._accept_proxy_runtime_summary(tab, "ssh-a", replace(summary, scope="ssh-a"), old)
        assert view._runtime_snapshot.scope == "ssh-b"
        assert len(calls) == 1, "summary invalidation must never connect to the second host"
    finally:
        dialog.destroy()
        host.destroy()
        settle(root, 0.05)
