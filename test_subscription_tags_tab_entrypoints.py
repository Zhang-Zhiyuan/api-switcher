"""Visible subscription classification actions preserve drafts and live routes."""

from types import SimpleNamespace

import pytest

from core import remote_proxy
from test_subscription_profile_ui import (
    _isolated_subscription_storage, _local_tab_stub, _seed_two_profiles, _ssh_tab_stub,
)
from ui.dialogs import subscription_tags_dialog
from ui.tabs.local_proxy_tab import LocalProxyTab, NEW_SUBSCRIPTION_PROFILE_LABEL
from ui.tabs.ssh_tab import NEW_PROXY_SUBSCRIPTION_PROFILE_LABEL, SSHTab


@pytest.fixture(params=["local", "ssh"])
def form(request, monkeypatch, tmp_path):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    first, second = _seed_two_profiles()
    tab = _local_tab_stub() if request.param == "local" else _ssh_tab_stub()
    prefix = "_" if request.param == "local" else "_proxy_"
    tab._destroyed = False
    events = []
    setattr(tab, prefix + "subscription_tags_dialog", None)
    setattr(tab, "_service_routes_dialog" if request.param == "local" else "_proxy_service_routes_dialog", None)
    if request.param == "local":
        tab._refresh_service_route_profile_options = LocalProxyTab._refresh_service_route_profile_options.__get__(tab)
        tab._request_route_catalog_refresh = lambda: events.append("catalog")
        tab._set_status = lambda *args: events.append(args)
        open_tags = tab._open_subscription_tags
        refresh = tab._refresh_subscription_profile_options
        apply_inputs = tab._apply_subscription_profile_inputs
        select = tab._on_subscription_profile_selected
        new_label = NEW_SUBSCRIPTION_PROFILE_LABEL
    else:
        tab._set_proxy_status = lambda *args: events.append(args)
        open_tags = tab._open_proxy_subscription_tags
        refresh = tab._refresh_proxy_subscription_profile_options
        apply_inputs = tab._apply_proxy_subscription_profile_inputs
        select = tab._on_proxy_subscription_profile_selected
        new_label = NEW_PROXY_SUBSCRIPTION_PROFILE_LABEL
    state = remote_proxy.load_proxy_subscription_state()
    refresh(state)
    apply_inputs(state)
    dialogs = []

    class Dialog:
        def __init__(self, master, *, catalog, on_saved):
            self.master, self.catalog, self.on_saved = master, catalog, on_saved
            self.lifts = self.focuses = 0
            self.exists = True
            dialogs.append(self)

        def winfo_exists(self):
            return self.exists

        def lift(self):
            self.lifts += 1

        def focus(self):
            self.focuses += 1

    monkeypatch.setattr(subscription_tags_dialog, "SubscriptionTagsDialog", Dialog)
    return SimpleNamespace(tab=tab, prefix=prefix, open=open_tags, dialogs=dialogs, events=events,
                           first=first, second=second, select=select, new_label=new_label, kind=request.param)


@pytest.mark.parametrize("new_draft", [False, True])
def test_tags_action_preserves_existing_or_new_name_url_draft_and_selection(form, new_draft):
    tab, prefix = form.tab, form.prefix
    if new_draft:
        form.select(form.new_label)
    name = getattr(tab, prefix + "subscription_name_entry")
    url = getattr(tab, prefix + "subscription_entry")
    name.value = "unsaved-name"
    url.value = "https://unsaved.example/subscription"
    snapshot = getattr(tab, prefix + "subscription_form_snapshot")
    edit_id = getattr(tab, prefix + "subscription_profile_edit_id")
    generation = getattr(tab, "_saved_subscription_load_generation" if form.kind == "local" else "_proxy_saved_subscription_load_generation")

    form.open()
    assert len(form.dialogs) == 1
    dialog = form.dialogs[0]
    assert {entry["id"] for entry in dialog.catalog} == {form.first["id"], form.second["id"]}
    assert all(set(entry) == {"id", "name", "network_type"} for entry in dialog.catalog)
    remote_proxy.set_proxy_subscription_network_type(form.first["id"], "residential")
    dialog.on_saved()

    assert (name.get(), url.get()) == ("unsaved-name", "https://unsaved.example/subscription")
    assert getattr(tab, prefix + "subscription_form_snapshot") == snapshot
    assert getattr(tab, prefix + "subscription_profile_edit_id") == edit_id
    assert getattr(tab, "_saved_subscription_load_generation" if form.kind == "local" else "_proxy_saved_subscription_load_generation") == generation
    assert remote_proxy.load_proxy_subscription_state()["active_profile_id"] == form.first["id"]
    combo = getattr(tab, prefix + "subscription_profile_combo")
    if new_draft:
        assert combo.get() == form.new_label
    else:
        assert "家宽" in combo.get()
    profiles = {entry["id"]: entry for entry in remote_proxy.list_proxy_subscription_profiles()}
    assert profiles[form.first["id"]]["url"] == form.first["url"]
    assert profiles[form.first["id"]]["name"] == form.first["name"]
    if form.kind == "local":
        assert "catalog" in form.events
        entry = next(item for item in tab._service_route_catalog if item["id"] == form.first["id"])
        assert entry["network_type"] == "residential"
    assert any(isinstance(event, tuple) and "保存并应用后生效" in event[0] for event in form.events)


def test_tags_action_reuses_existing_window_without_reloading_or_saving(form, monkeypatch):
    form.open()
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda: pytest.fail("existing dialog must be reused"))
    form.open()
    assert len(form.dialogs) == 1
    assert form.dialogs[0].lifts == form.dialogs[0].focuses == 1


def test_tags_action_is_blocked_while_proxy_operation_is_running(form):
    setattr(form.tab, "_busy" if form.kind == "local" else "_proxy_busy", True)
    form.open()
    assert form.dialogs == []
    assert any(isinstance(event, tuple) and "正在进行" in event[0] for event in form.events)


def test_tags_save_refreshes_open_route_editor_catalog_without_applying(form):
    calls = []
    dialog = SimpleNamespace(_closed=False, winfo_exists=lambda: True,
                             _reload_catalog=lambda: calls.append("catalog"))
    setattr(form.tab, "_service_routes_dialog" if form.kind == "local" else "_proxy_service_routes_dialog", dialog)
    form.open()
    form.dialogs[0].on_saved()
    assert calls == ["catalog"]


def test_tags_callback_does_nothing_after_tab_destruction(form, monkeypatch):
    form.open()
    form.tab._destroyed = True
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: pytest.fail("closed tab must not refresh"))
    form.dialogs[0].on_saved()


def test_tags_open_reports_load_error_without_overwriting_draft(form, monkeypatch):
    entry = getattr(form.tab, form.prefix + "subscription_name_entry")
    entry.value = "unsaved-name"

    def unavailable():
        raise OSError("synthetic storage unavailable")

    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", unavailable)
    form.open()
    assert form.dialogs == []
    assert entry.get() == "unsaved-name"
    assert any(isinstance(event, tuple) and "打开订阅类型标记失败" in event[0] for event in form.events)


def _capture_synthetic_tab(window, name):
    """Opt-in screenshots contain only this test's synthetic, isolated data."""
    import os
    from pathlib import Path

    capture_dir = os.environ.get("API_SWITCHER_UI_CAPTURE_DIR")
    if not capture_dir:
        return
    from PIL import ImageGrab

    target = Path(capture_dir)
    target.mkdir(parents=True, exist_ok=True)
    left, top = window.winfo_rootx(), window.winfo_rooty()
    ImageGrab.grab(bbox=(left, top, left + window.winfo_width(), top + window.winfo_height())).save(target / f"{name}.png")


@pytest.mark.parametrize("kind", ["local", "ssh"])
@pytest.mark.parametrize("width", [1160, 740])
def test_subscription_tag_button_real_layout_and_action(tk_root, monkeypatch, tmp_path, kind, width):
    """Build actual tab widgets without loading accounts, starting probes or SSH."""
    import time

    import customtkinter as ctk

    from ui.tabs import ssh_tab

    _isolated_subscription_storage(monkeypatch, tmp_path)
    first, _second = _seed_two_profiles()
    remote_proxy.set_proxy_subscription_network_type(first["id"], "residential")
    opened = []
    monkeypatch.setattr(LocalProxyTab, "refresh", lambda self: None)
    monkeypatch.setattr(LocalProxyTab, "_build_subscription_picker", lambda self: None)
    monkeypatch.setattr(LocalProxyTab, "_open_subscription_tags", lambda self: opened.append("local"))
    monkeypatch.setattr(SSHTab, "refresh", lambda self: None)
    monkeypatch.setattr(SSHTab, "_load_saved_proxy_subscription_ui", lambda self: None)
    monkeypatch.setattr(SSHTab, "_build_remote_auto_section", lambda self: None)
    monkeypatch.setattr(SSHTab, "_build_proxy_subscription_picker", lambda self: None)
    monkeypatch.setattr(SSHTab, "_open_proxy_subscription_tags", lambda self: opened.append("ssh"))
    monkeypatch.setattr(ssh_tab, "is_active_tab", lambda self: True)
    monkeypatch.setattr(ssh_tab, "recent_user_scroll", lambda *args, **kwargs: False)
    window = ctk.CTkToplevel(tk_root)
    window.title("Synthetic subscription tags · " + kind)
    window.geometry(f"{width}x800+40+40")
    try:
        tab = LocalProxyTab(window) if kind == "local" else SSHTab(window)
        tab.pack(fill="both", expand=True)
        tk_root.update_idletasks()
        state = remote_proxy.load_proxy_subscription_state()
        if kind == "local":
            tab._refresh_subscription_profile_options(state)
            tab._apply_subscription_profile_inputs(state)
            button = tab._subscription_tags_button
            combo = tab._subscription_profile_combo
        else:
            # Focus the real deployment portion; the server/account sections
            # are unrelated to this entrypoint and intentionally stay unloaded.
            for child in tab.winfo_children():
                if child is not tab._deployment_sections_frame:
                    child.pack_forget()
            tab._build_deployment_sections()
            tab._refresh_proxy_subscription_profile_options(state)
            tab._apply_proxy_subscription_profile_inputs(state)
            button = tab._proxy_subscription_tags_button
            combo = tab._proxy_subscription_profile_combo
        window.deiconify()
        window.lift()
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            tk_root.update()
            time.sleep(0.01)
        assert button.cget("text") == "标记家宽 / 非家宽"
        assert button.winfo_ismapped()
        assert "家宽" in combo.get()
        assert window.winfo_rootx() <= button.winfo_rootx()
        assert button.winfo_rootx() + button.winfo_width() <= window.winfo_rootx() + window.winfo_width()
        assert window.winfo_rooty() <= button.winfo_rooty()
        assert button.winfo_rooty() + button.winfo_height() <= window.winfo_rooty() + window.winfo_height()
        button.invoke()
        assert opened == [kind]
        _capture_synthetic_tab(window, f"subscription-tags-{kind}-{width}")
    finally:
        window.destroy()
        tk_root.update()
