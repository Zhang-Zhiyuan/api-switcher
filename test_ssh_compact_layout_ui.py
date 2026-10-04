"""Native SSH layout/selection checks using only synthetic cached servers."""
from types import SimpleNamespace

import customtkinter as ctk
import pytest

from models.profile import SSHProfile
from ui.tabs import ssh_tab
from ui.tabs.ssh_tab import SSHTab


@pytest.fixture
def ssh_window(tk_root, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("native layout check must not refresh or access profiles")

    monkeypatch.setattr(ssh_tab, "profile_manager", SimpleNamespace(list_ssh_profiles=forbidden))
    monkeypatch.setattr(ssh_tab, "is_active_tab", lambda _widget: True)
    monkeypatch.setattr(ssh_tab, "recent_user_scroll", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(SSHTab, "refresh", forbidden)
    for name in ("_load_saved_proxy_subscription_ui", "_build_remote_auto_section",
                 "_build_proxy_subscription_picker", "_on_remote_auto_provider_change"):
        monkeypatch.setattr(SSHTab, name, lambda _self: None)
    window = ctk.CTkToplevel(tk_root)
    window.geometry("1160x850+40+40")
    window.title("Synthetic SSH compact layout check")
    tab = SSHTab(window)
    tab._cancel_initial_after_callbacks()
    tab._set_server_profile_cache([SSHProfile("Synthetic A", "a.invalid"), SSHProfile("Synthetic B", "b.invalid")])
    tab.pack(fill="both", expand=True)
    tk_root.update()
    try:
        yield window, tab
    finally:
        window.destroy()
        tk_root.update()


@pytest.mark.parametrize("width", [1160, 740])
def test_sync_heading_does_not_reserve_empty_frame_height(ssh_window, tk_root, width):
    window, tab = ssh_window
    window.geometry(f"{width}x850")
    tab._render_server_card({"profile": tab._server_profiles[0]})
    tk_root.update()
    tab._apply_responsive_layout()
    tk_root.update_idletasks()
    header = tab._batch_target_label.master
    scale = tab._get_widget_scaling()
    assert header.winfo_reqheight() < 65 * scale
    assert tab._sync_frame.winfo_y() - (tab._cards_frame.winfo_y() + tab._cards_frame.winfo_height()) < 100 * scale


def test_wide_proxy_heading_spacer_keeps_toolbar_compact(ssh_window, tk_root):
    _window, tab = ssh_window
    tab._build_deployment_sections()
    tab._cancel_initial_after_callbacks()
    tk_root.update()
    tab._apply_deployment_responsive_layout(False)
    tk_root.update_idletasks()
    scale = tab._get_widget_scaling()
    assert tab._proxy_header_spacer.cget("height") == 1
    assert tab._proxy_header_spacer.cget("width") == 1
    assert tab._proxy_header.winfo_reqheight() < 100 * scale


def test_bulk_selection_reuses_widgets_and_does_not_invoke_checkbox_commands(ssh_window, tk_root, monkeypatch):
    _window, tab = ssh_window
    for profile in tab._server_profiles:
        tab._render_server_card({"profile": profile})
    tab._update_batch_target_label()
    tk_root.update()
    cards = tuple(tab._cards_frame.winfo_children())
    variables = dict(tab._server_selection_vars)
    toggles = []
    monkeypatch.setattr(tab, "_toggle_batch_server", lambda *args: toggles.append(args))
    tab._batch_select_all_button.invoke()
    tk_root.update()
    assert tab._selected_sync_server_names() == ["Synthetic A", "Synthetic B"]
    assert all(variable.get() for variable in variables.values())
    tab._batch_clear_button.invoke()
    tk_root.update()
    assert tab._selected_sync_server_names() == []
    assert not any(variable.get() for variable in variables.values())
    assert tuple(tab._cards_frame.winfo_children()) == cards
    assert tab._server_selection_vars == variables
    assert not toggles


def test_empty_bulk_buttons_are_disabled(ssh_window, tk_root):
    _window, tab = ssh_window
    tab._set_server_profile_cache([])
    tab._update_batch_target_label()
    assert tab._batch_select_all_button.cget("state") == "disabled"
    assert tab._batch_clear_button.cget("state") == "disabled"
    tab._batch_select_all_button.invoke()
    tab._batch_clear_button.invoke()
    tk_root.update()
    assert tab._selected_sync_server_names() == []


def test_refresh_rebuilds_variables_and_removes_deleted_targets(ssh_window, tk_root, monkeypatch):
    _window, tab = ssh_window
    for profile in tab._server_profiles:
        tab._render_server_card({"profile": profile})
    tab._select_all_batch_servers()
    old_cards = tuple(tab._cards_frame.winfo_children())
    old_variables = dict(tab._server_selection_vars)
    for name in ("_refresh_sync_profile_combo", "_update_remote_auto_feature_label",
                 "_refresh_remote_auto_switch_availability"):
        monkeypatch.setattr(tab, name, lambda: None)
    tab._render_server_refresh_payload(
        {"ok": True, "profiles": [{"profile": tab._server_profiles[1]}]},
        tab._server_refresh_generation,
    )
    tk_root.update()
    assert all(not card.winfo_exists() for card in old_cards)
    assert set(tab._server_selection_vars) == {"Synthetic B"}
    assert tab._server_selection_vars["Synthetic B"] is not old_variables["Synthetic B"]
    assert tab._server_selection_vars["Synthetic B"].get()
    assert tab._selected_sync_server_names() == ["Synthetic B"]
    tab._batch_clear_button.invoke()
    assert not tab._server_selection_vars["Synthetic B"].get()


@pytest.mark.parametrize("width,widget_scale", [(1160, 1.0), (740, 1.0), (960, 1.5), (740, 1.5)])
def test_workflow_hint_wraps_compactly_and_keeps_server_visible(ssh_window, tk_root, width, widget_scale):
    window, tab = ssh_window
    previous_scale = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(widget_scale)
        window.geometry(f"{width}x850")
        tab._render_server_card({"profile": tab._server_profiles[0]})
        tk_root.update()
        tab._apply_responsive_layout()
        tk_root.update_idletasks()
        hint = tab._workflow_hint
        scale = hint._get_widget_scaling()
        assert hint.winfo_height() <= 85 * scale
        assert hint._label.winfo_reqheight() <= hint.winfo_height()
        assert hint._label.winfo_reqwidth() <= hint.winfo_width() + 2
        assert hint.cget("wraplength") <= hint.winfo_width() / scale
        assert "同步 API / 账号 / Git 登录" in hint.cget("text")
        assert "_overview_items" not in tab.__dict__
        card = tab._cards_frame.winfo_children()[0]
        canvas = tab._parent_canvas
        assert card.winfo_rooty() >= hint.winfo_rooty() + hint.winfo_height()
        assert card.winfo_rooty() + card.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height()
    finally:
        ctk.set_widget_scaling(previous_scale)
