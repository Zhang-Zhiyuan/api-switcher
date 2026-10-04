"""Native progressive-disclosure checks using synthetic drafts and no I/O."""
import os
from pathlib import Path
import socket
import time

import customtkinter as ctk
import pytest

from ui.tabs import local_proxy_tab, ssh_tab


def settle(root):
    until = time.monotonic() + 0.2
    while time.monotonic() < until:
        root.update()
        time.sleep(0.01)


@pytest.mark.parametrize("kind", ["win", "ssh"])
@pytest.mark.parametrize("scale", [1, 2.5])
def test_advanced_controls_stay_folded_and_preserve_draft_across_layout(tk_root, monkeypatch, kind, scale):
    def forbidden(*_args, **_kwargs):
        pytest.fail("disclosure checks must never open a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    module = local_proxy_tab if kind == "win" else ssh_tab
    cls = module.LocalProxyTab if kind == "win" else module.SSHTab
    monkeypatch.setattr(module, "is_active_tab", lambda _tab: True)
    monkeypatch.setattr(module, "recent_user_scroll", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(cls, "refresh", lambda _self: None)
    deferred = ("_build_subscription_picker",) if kind == "win" else (
        "_build_proxy_subscription_picker", "_build_remote_auto_section", "_load_saved_proxy_subscription_ui",
    )
    for name in deferred:
        monkeypatch.setattr(cls, name, lambda _self: None)
    errors = []
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *error: errors.append(error))
    window = ctk.CTkToplevel(tk_root)
    window.title("合成代理页面检查")
    window.geometry("1040x760")
    tab = cls(window)
    tab.pack(fill="both", expand=True)
    if kind == "win":
        assert tab._node_text is None
        tab._node_editor_toggle.invoke()
        assert tab._node_text is not None
        tab._node_editor_toggle.invoke()
        editor = tab._node_text
        editor_toggle, maintenance_toggle = tab._node_editor_toggle, tab._maintenance_toggle
        maintenance = tab._maintenance_tools
        import_button = tab._load_file_button
        actions = (tab._start_button, tab._inspect_button, tab._stop_button)
    else:
        tab._build_deployment_sections()
        editor = tab._proxy_node_text
        editor_toggle, maintenance_toggle = tab._proxy_node_editor_toggle, tab._proxy_maintenance_toggle
        maintenance = tab._proxy_maintenance_tools
        import_button = tab._proxy_load_file_button
        actions = (tab._proxy_deploy_button, tab._proxy_inspect_button, tab._proxy_remote_cleanup_button)
    try:
        settle(tk_root)
        editor.insert("1.0", "name: synthetic-draft\nserver: example.test")
        before = editor.get("1.0", "end")
        for width in (1040, 620):
            window.geometry(f"{width}x760")
            ctk.set_widget_scaling(scale)
            settle(tk_root)
            assert not editor.winfo_viewable()
            assert not maintenance.winfo_viewable()
            assert not import_button.winfo_viewable()
            assert all(button.winfo_viewable() for button in actions)
            editor_toggle.invoke()
            maintenance_toggle.invoke()
            settle(tk_root)
            assert editor.winfo_viewable() and maintenance.winfo_viewable() and import_button.winfo_viewable()
            assert editor.get("1.0", "end") == before
            for widget in (editor_toggle, maintenance_toggle, *actions):
                assert widget.winfo_rootx() >= tab.winfo_rootx() - 2
                assert widget.winfo_rootx() + widget.winfo_width() <= tab.winfo_rootx() + tab.winfo_width() + 2
            editor_toggle.invoke()
            maintenance_toggle.invoke()
            settle(tk_root)
        assert editor.get("1.0", "end") == before
        assert not errors
        destination = os.environ.get("API_SWITCHER_PROXY_PAGE_CAPTURE_DIR")
        if destination:
            from tools.ui_visual_audit import capture_window_image
            folder = Path(destination).resolve()
            assert (Path(__file__).resolve().parent / "dist") in folder.parents
            folder.mkdir(parents=True, exist_ok=True)
            tab._parent_canvas.yview_moveto(1)
            settle(tk_root)
            capture_window_image(window).save(folder / f"proxy-{kind}-scale-{scale}.png")
    finally:
        window.destroy()
        ctk.set_widget_scaling(1)
        tk_root.update()
