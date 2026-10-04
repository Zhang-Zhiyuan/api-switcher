"""Read-only overview simplification, using synthetic routes and native Tk."""
import copy
from pathlib import Path
import socket

import customtkinter as ctk
import pytest

from core import proxy_route_diagnostics as diagnostics
from test_route_runtime_overview import synthetic_snapshot
from test_route_runtime_overview_ui import reveal_label, settle
from test_service_routes_dialog import _catalog, _preferences
from ui.widgets.service_route_overview import ServiceRouteOverview


@pytest.fixture
def simple_overview(tk_root, monkeypatch):
    def no_network(*_args, **_kwargs):
        pytest.fail("Overview presentation tests must not contact real services")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    errors, events = [], []
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *args: errors.append(args))
    window = ctk.CTkToplevel(tk_root)
    window.geometry("1100x800")
    scroll = ctk.CTkScrollableFrame(window)
    scroll.pack(fill="both", expand=True, padx=10, pady=10)
    view = ServiceRouteOverview(
        scroll, command=lambda service: events.append(("edit", service)),
        inspect_command=lambda: events.append(("inspect", "")),
        preset_command=lambda: events.append(("preset", "")),
    )
    view.pack(fill="x")
    preferences = _preferences()
    view.set_routes(preferences, _catalog())
    settle(tk_root)
    try:
        yield window, scroll, view, preferences, events
        assert not errors
    finally:
        window.destroy()
        settle(tk_root, 0.05)


def test_normal_overview_is_compact_and_detail_toggle_is_read_only(tk_root, simple_overview):
    _, _, view, preferences, events = simple_overview
    before = copy.deepcopy(preferences)
    signature = view._signature
    rows = {key: item["tile"] for key, item in view._rows.items()}
    assert not view._rows["claude"]["hint"].winfo_manager()
    assert not view._rows["claude"]["state"].winfo_manager()
    assert not view._preset_note.winfo_manager()
    assert "已保存" in view._summary.cget("text")
    assert "不代表实时连通" in view._note.cget("text")
    for expanded in (True, False):
        view._details_toggle.invoke()
        tk_root.update()
        assert bool(view._rows["claude"]["hint"].winfo_manager()) is expanded
        assert bool(view._rows["claude"]["state"].winfo_manager()) is expanded
        assert bool(view._preset_note.winfo_manager()) is expanded
    assert rows == {key: item["tile"] for key, item in view._rows.items()}
    assert preferences == before and view._signature == signature
    assert events == []


def test_disabled_binding_is_not_presented_as_an_active_route(tk_root, simple_overview):
    _, _, view, preferences, events = simple_overview
    preferences["builtin_sites"]["youtube"] = False
    before = copy.deepcopy(preferences)
    view.set_routes(preferences, _catalog())
    view._toggle_inactive()
    row = view._rows["youtube"]
    assert row["profile"].cget("text") == "沿用原规则"
    assert row["node"].cget("text") == "不新增专属规则"
    view._toggle_details()
    assert row["profile"].cget("text") == "机房订阅 B"
    assert row["node"].cget("text") == "香港 · 流媒体 01"
    assert "不新增专属规则" in row["hint"].cget("text")
    assert preferences == before and events == []


def test_warning_is_visible_without_expanding_and_disappears_after_repair(tk_root, simple_overview):
    _, _, view, preferences, _ = simple_overview
    preferences["service_profile_bindings"]["github"] = "synthetic-missing"
    view.set_routes(preferences, _catalog())
    row = view._rows["github"]
    assert row["tile"].winfo_manager() == "pack"
    assert row["hint"].winfo_manager() == "pack"
    assert "订阅已失效" in row["hint"].cget("text")
    assert row["profile"].cget("text") == "沿用原规则"
    view._toggle_details()
    view._toggle_details()
    assert row["hint"].winfo_manager() == "pack"
    view.set_routes(_preferences(), _catalog())
    assert not row["hint"].winfo_manager()
    assert not row["tile"].winfo_manager()


def test_selected_pool_is_summarized_but_details_preserve_order(tk_root, simple_overview):
    _, _, view, preferences, _ = simple_overview
    preferences["service_node_bindings"].pop("claude")
    preferences["service_node_pools"]["claude"] = ["two", "one"]
    before = copy.deepcopy(preferences)
    view.set_routes(preferences, _catalog())
    row = view._rows["claude"]
    assert row["node"].cget("text") == "自选 2 个候选 · 顺序切换"
    view._toggle_details()
    assert "美国 · 家宽 02 → 日本 · 家宽 01" in row["node"].cget("text")
    view._toggle_details()
    assert row["node"].cget("text") == "自选 2 个候选 · 顺序切换"
    assert preferences == before


def test_details_do_not_reset_runtime_snapshot_or_schedule_another_read(tk_root, simple_overview):
    _, _, view, _, events = simple_overview
    snapshot = synthetic_snapshot()
    view.set_routes(snapshot.preferences, [])
    summary = diagnostics.snapshot_overview(snapshot)
    assert view.set_runtime_summary(summary, view.begin_runtime_read())
    timer, generation = view._runtime_after_id, view._runtime_generation
    for expanded in (True, False, True):
        view._toggle_details()
        assert view._show_details is expanded
        assert view._runtime_snapshot is summary
        assert view._runtime_after_id == timer and view._runtime_generation == generation
        assert view._rows["openai"]["runtime"].winfo_manager() == "pack"
    assert not events


@pytest.mark.parametrize("scale", [1.0, 2.5])
def test_compact_overview_fits_dpi_and_keeps_actions_reachable(tk_root, simple_overview, scale):
    window, scroll, view, _, events = simple_overview
    previous = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(scale)
        settle(tk_root, 0.5)
        viewport = scroll._parent_canvas
        for widget in (view._preset, view._manage, view._inspect):
            assert widget.winfo_viewable()
            assert widget.winfo_rootx() >= viewport.winfo_rootx() - 2
            assert widget.winfo_rootx() + widget.winfo_width() <= viewport.winfo_rootx() + viewport.winfo_width() + 2
        folder = Path(__file__).resolve().parent / "dist" / "routing-access-ui"
        folder.mkdir(parents=True, exist_ok=True)
        from tools.ui_visual_audit import capture_window_image
        capture_window_image(window).save(folder / f"overview-simple-{int(scale * 100)}.png")
        reveal_label(tk_root, viewport, view._note)
        view._toggle_details()
        settle(tk_root)
        reveal_label(tk_root, viewport, view._rows["claude"]["hint"])
        assert not events
    finally:
        ctk.set_widget_scaling(previous)
        settle(tk_root, 0.05)
