"""Native dialog checks; no proxy, credentials or background services are started."""
import os
from pathlib import Path
import time

import customtkinter as ctk
import pytest

from ui.dialogs.close_choice_dialog import CloseChoiceDialog
from ui.proxy_lifecycle import PROXY_MAINTENANCE_NOTICE


def settle(root):
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@pytest.mark.parametrize("scale", [1, 1.5])
@pytest.mark.parametrize("geometry", ["500x280", "420x300"])
def test_maintenance_notice_and_close_actions_fit(tk_root, scale, geometry):
    tk_root.geometry("320x180")
    tk_root.title("合成代理 UI 检查")
    tk_root.deiconify()
    ctk.set_widget_scaling(scale)
    dialog = CloseChoiceDialog(tk_root)
    dialog.geometry(geometry)
    try:
        settle(tk_root)
        children = list(descendants(dialog))
        notice = next(child for child in children if isinstance(child, ctk.CTkLabel)
                      and child.cget("text") == PROXY_MAINTENANCE_NOTICE)
        assert "维护暂停" in notice.cget("text")
        assert "节点切换" in notice.cget("text")
        assert notice.winfo_height() >= notice._label.winfo_reqheight()
        buttons = [child for child in children if isinstance(child, ctk.CTkButton)]
        assert len(buttons) == 3
        for button in buttons:
            assert button.winfo_viewable()
            assert button.winfo_rootx() >= dialog.winfo_rootx()
            assert button.winfo_rootx() + button.winfo_width() <= dialog.winfo_rootx() + dialog.winfo_width()
            assert button.winfo_rooty() + button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
            assert button._text_label.winfo_reqwidth() <= button.winfo_width()
        destination = os.environ.get("API_SWITCHER_UI_CAPTURE_DIR")
        if destination:
            from tools.ui_visual_audit import capture_window_image
            folder = Path(destination).resolve()
            assert (Path(__file__).resolve().parent / "dist") in folder.parents
            folder.mkdir(parents=True, exist_ok=True)
            capture_window_image(dialog).save(folder / f"proxy-lifecycle-{geometry}-scale-{scale}.png")
    finally:
        dialog.destroy()
        ctk.set_widget_scaling(1)
        tk_root.update()


@pytest.mark.parametrize("action", ["_minimize", "_exit", "_cancel"])
def test_explanatory_notice_does_not_change_explicit_close_choice(tk_root, action):
    calls = []
    dialog = CloseChoiceDialog(
        tk_root, on_minimize=lambda: calls.append("_minimize"),
        on_exit=lambda: calls.append("_exit"), on_cancel=lambda: calls.append("_cancel"),
    )
    getattr(dialog, action)()
    assert calls == [action]
