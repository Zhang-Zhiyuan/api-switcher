"""Native preset shortcuts and region explanations; synthetic, no live writes."""
import copy
from pathlib import Path
import threading

import customtkinter as ctk
import pytest

from test_route_presets import catalog, preferences
from test_service_routes_dialog import _wait
from test_ssh_compact_layout_ui import ssh_window as ssh_window_fixture
from ui.dialogs.service_routes_dialog import ServiceRoutesDialog
from ui.widgets.service_route_overview import ServiceRouteOverview


ssh_entry_window = ssh_window_fixture


def _capture(window, name):
    from tools.ui_visual_audit import capture_window_image
    destination = Path("dist/route-preset-ui/entry-region-review")
    destination.mkdir(parents=True, exist_ok=True)
    capture_window_image(window).save(destination / name)


@pytest.fixture
def opened(tk_root):
    windows, saved = [], []

    def create(**kwargs):
        options = dict(scopes=["合成 Win11", "合成 SSH"], load_preferences=lambda _: preferences(),
                       catalog_loader=catalog, apply_preferences=lambda *args: saved.append(args),
                       initial_preset=True)
        options.update(kwargs)
        window = ServiceRoutesDialog(tk_root, **options)
        windows.append(window)
        return window, saved

    yield create
    for window in windows:
        if window.winfo_exists():
            window.destroy()
    tk_root.update()


def _ready(root, parent):
    _wait(root, lambda: not parent._busy and parent._preset_dialog is not None
          and parent._preset_dialog.winfo_viewable())
    return parent._preset_dialog


def test_shortcut_skips_auto_seed_so_fixed_recommendations_actually_change_draft(tk_root, opened):
    parent, saved = opened()
    child = _ready(tk_root, parent)
    assert parent._drafts[parent._scope] == preferences()
    assert len(child._plan["changed_services"]) == 11
    assert child._plan["draft"]["service_node_bindings"]["openai"] == "h1"
    assert parent._originals == parent._drafts and not saved
    child.destroy()
    tk_root.update()
    assert parent._originals == parent._drafts and not saved


def test_normal_manage_entry_preserves_existing_auto_seed_behavior(tk_root, opened):
    parent, saved = opened(initial_preset=False)
    _wait(tk_root, lambda: not parent._busy)
    assert parent._preset_dialog is None
    assert parent._drafts[parent._scope]["service_profile_bindings"]["openai"] == "home"
    assert not saved


def test_repeated_loading_shortcut_only_opens_once_and_does_not_seed(tk_root, opened):
    release, entered = threading.Event(), threading.Event()

    def load():
        entered.set()
        assert release.wait(5)
        return catalog()

    parent, saved = opened(initial_preset=False, catalog_loader=load)
    try:
        _wait(tk_root, entered.is_set)
        parent.show_preset()
        parent.show_preset()
        assert parent._preset_requested and parent._preset_dialog is None
        release.set()
        child = _ready(tk_root, parent)
        parent.show_preset()
        assert child is parent._preset_dialog
        assert parent._drafts[parent._scope] == preferences() and not saved
    finally:
        release.set()


def test_failed_load_clears_pending_shortcut_without_popup(tk_root, opened):
    def fail(_):
        raise ValueError("合成读取失败")
    parent, saved = opened(load_preferences=fail)
    _wait(tk_root, lambda: not parent._busy)
    assert not parent._preset_requested and parent._preset_dialog is None and not saved


def test_existing_draft_and_busy_operation_are_not_reloaded_or_deferred(tk_root, opened):
    parent, saved = opened(initial_preset=False)
    _wait(tk_root, lambda: not parent._busy)
    parent._drafts[parent._scope]["service_node_bindings"]["openai"] = "h2"
    before = copy.deepcopy(parent._drafts)
    parent._busy = True
    parent.show_preset()
    assert not parent._preset_requested and parent._preset_dialog is None
    parent._busy = False
    parent.show_preset()
    child = _ready(tk_root, parent)
    assert child._plan["draft"]["service_node_bindings"]["openai"] == "h2"
    assert parent._drafts == before and not saved


def test_shortcut_does_not_steal_grab_from_another_child(tk_root, opened):
    parent, saved = opened(initial_preset=False)
    _wait(tk_root, lambda: not parent._busy)
    modal = ctk.CTkToplevel(parent)
    try:
        tk_root.update()
        modal.grab_set()
        parent.show_preset()
        assert parent.grab_current() is modal and parent._preset_dialog is None and not saved
    finally:
        modal.destroy()


@pytest.mark.parametrize("all_hk", [False, True])
def test_preset_previews_hong_kong_exclusion_before_any_save(tk_root, opened, all_hk):
    sources = catalog()
    sources[0]["nodes"] = [
        {"key": "h1", "label": "香港 · 家宽", "region": "香港", "ai_auto_selectable": False},
        {"key": "h2", "label": "日本 · 家宽", "region": "日本", "ai_auto_selectable": True},
    ][:1 if all_hk else 2]
    parent, saved = opened(catalog_loader=lambda: sources)
    child = _ready(tk_root, parent)
    child._preview_tab.invoke()
    tk_root.update()
    text = child._preview_rows["openai"][1].cget("text")
    assert "香港" in text
    if all_hk:
        assert "openai" not in child._plan["draft"]["service_profile_bindings"]
        assert "3 项待处理" in child._status.cget("text")
    else:
        assert "日本" in text and "改选" in text
        assert child._plan["draft"]["service_node_bindings"]["openai"] == "h2"
    assert parent._drafts == parent._originals and not saved
    _capture(child, "all-hk-blocked.png" if all_hk else "hk-excluded-jp-selected.png")


def test_manual_hk_is_kept_but_visible_as_needing_attention(tk_root, opened):
    sources = catalog()
    sources[0]["nodes"][0].update(label="香港家宽", ai_auto_selectable=False, region="香港")
    original = preferences()
    original["service_profile_bindings"]["openai"] = "home"
    original["service_node_bindings"]["openai"] = "h1"
    parent, saved = opened(catalog_loader=lambda: sources, load_preferences=lambda _: original)
    child = _ready(tk_root, parent)
    child._preview_tab.invoke()
    tk_root.update()
    assert "保留 · 需检查" in child._preview_rows["openai"][0].cget("text")
    assert "香港" in child._preview_rows["openai"][1].cget("text")
    assert child._plan["draft"]["service_node_bindings"]["openai"] == "h1" and not saved
    _capture(child, "manual-hk-preserved-warning.png")


def test_ssh_smart_entry_is_visible_in_routes_card_and_dispatches_preset(ssh_entry_window, tk_root, monkeypatch):
    window, tab = ssh_entry_window
    tab._build_deployment_sections()
    tab._cancel_initial_after_callbacks()
    tk_root.update()
    button, canvas = tab._proxy_route_preset_button, tab._parent_canvas
    bounds = canvas.bbox("all")
    offset = button.master.winfo_rooty() - tab.winfo_rooty() - 30
    canvas.yview_moveto(max(0, offset) / max(1, bounds[3] - bounds[1]))
    _wait(tk_root, lambda: button.winfo_viewable()
          and button.winfo_rooty() >= canvas.winfo_rooty()
          and button.winfo_rooty() + button.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height())
    assert button.cget("text") == "一键套用智能分流方案"
    calls = []
    monkeypatch.setattr(tab, "_open_proxy_service_routes", lambda **kwargs: calls.append(kwargs))
    button.invoke()
    assert calls == [{"preset": True}]
    _capture(window, "ssh-visible-entry.png")


@pytest.mark.parametrize("scale", [1, 1.5, 2.5])
def test_visible_preset_button_wraps_and_keeps_actions_reachable(tk_root, scale):
    old_scale = ctk.ScalingTracker.widget_scaling
    window = ctk.CTkToplevel(tk_root)
    calls = []
    try:
        ctk.set_widget_scaling(scale)
        overview = ServiceRouteOverview(window, command=lambda _: None, inspect_command=lambda: None,
                                        preset_command=lambda: calls.append("preset"))
        overview.pack(fill="both", expand=True, padx=8, pady=8)
        overview.set_routes(preferences(), catalog())
        for width in (900, 370, 900):
            physical_width = round(width * overview._get_widget_scaling())
            window.geometry(f"{round(physical_width / window._get_window_scaling())}x700")
            _wait(tk_root, lambda: window.winfo_viewable() and abs(window.winfo_width() - physical_width) <= 2)
            for button in (overview._preset, overview._manage, overview._inspect):
                assert button.winfo_viewable()
                assert button.winfo_rootx() >= window.winfo_rootx()
                assert button.winfo_rootx() + button.winfo_width() <= window.winfo_rootx() + window.winfo_width() + 2
            if width == 370:
                _capture(window, f"entry-narrow-{scale}.png")
        overview.set_enabled(False)
        overview._preset.invoke()
        assert calls == []
        overview.set_enabled(True)
        overview._preset.invoke()
        assert calls == ["preset"]
    finally:
        window.destroy()
        ctk.set_widget_scaling(old_scale)
        tk_root.update()
