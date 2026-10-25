"""Real Windows/Tk feedback checks with synthetic messages only."""
import os
from pathlib import Path

import customtkinter as ctk
import pytest

from ui.theme import _screen_bounds, fit_window_to_screen
from ui.widgets.toast import Toast, show_toast


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_long_feedback_remains_readable_and_details_survive_toast(tk_root, monkeypatch, scale):
    ctk.set_widget_scaling(scale)
    ctk.set_window_scaling(scale)
    window = ctk.CTkToplevel(tk_root)
    window.title("代理部署反馈 · 隔离界面实测")
    window.geometry("880x560")
    fit_window_to_screen(window, tk_root)
    ctk.CTkLabel(window, text="SSH 代理部署", font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w", padx=24, pady=24)
    ctk.CTkLabel(window, text="失败原因会保留在详情中，可以滚动查看和复制。", anchor="w").pack(anchor="w", padx=24)
    dialog = None
    message = (
        "AI 代理部署未完成：测试服务器的独立分流不可用。更换默认节点不能修复；请在“目标分流”检查对应订阅或节点。\n"
        "正式代理配置未修改。\n\n"
        + "\n".join(f"第 {index} 项：节点或上游域名解析失败。" for index in range(1, 20))
        + "\n诊断 token=synthetic-secret-value"
    )
    try:
        tk_root.update()
        toast = show_toast(window, message, severity="warning", duration=60000)
        tk_root.update()
        assert toast.winfo_height() < _screen_bounds(window)[3] * 0.6
        assert toast.winfo_width() < _screen_bounds(window)[2]
        assert "synthetic-secret-value" not in toast._message
        assert show_toast(window, message, severity="warning") is toast
        output = os.environ.get("API_SWITCHER_FEEDBACK_CAPTURE_DIR")
        if output:
            from tools.ui_visual_audit import capture_window_image
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            capture_window_image(toast).save(directory / f"toast-{int(scale * 100)}.png")
        dialog = toast._show_details()
        tk_root.update()
        assert not toast.winfo_exists() and dialog.winfo_exists()
        assert not Toast._registered(window)
        assert "第 19 项" in dialog._text.get("1.0", "end")
        assert "synthetic-secret-value" not in dialog._text.get("1.0", "end")
        assert dialog._copy_button.winfo_ismapped()
        _x, screen_y, _width, screen_height = _screen_bounds(dialog)
        assert dialog.winfo_y() + dialog.winfo_height() <= screen_y + screen_height
        assert dialog._text._textbox.cget("state") == "disabled"
        copied = []
        monkeypatch.setattr(dialog, "clipboard_clear", lambda: None)
        monkeypatch.setattr(dialog, "clipboard_append", copied.append)
        dialog._copy_button.invoke()
        assert copied == [dialog._message]
        assert "已复制" == dialog._copy_button.cget("text")
        if output:
            tk_root.update()
            capture_window_image(dialog).save(directory / f"details-{int(scale * 100)}.png")
    finally:
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()
        window.destroy()
        tk_root.update()
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)
