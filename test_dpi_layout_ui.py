"""Native layout tests with synthetic forms; no account/proxy writes or network."""
import os
import json
from pathlib import Path
import socket
import time

import customtkinter as ctk
import pytest

from ui import theme
from ui.dialogs.profile_editor import ProfileEditorDialog
from ui.dialogs.ssh_editor import SSHEditorDialog
from ui.dialogs.confirm_dialog import _bind_two_button_footer
from ui.widgets.auto_continue_control import _auto_continue_layout, _bind_responsive_grid


def settle(root, seconds=0.3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.005)


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def inside(widget, parent):
    assert widget.winfo_viewable()
    assert widget.winfo_rootx() >= parent.winfo_rootx() - 2
    assert widget.winfo_rooty() >= parent.winfo_rooty() - 2
    assert widget.winfo_rootx() + widget.winfo_width() <= parent.winfo_rootx() + parent.winfo_width() + 2
    assert widget.winfo_rooty() + widget.winfo_height() <= parent.winfo_rooty() + parent.winfo_height() + 2


@pytest.fixture
def isolated_ui(tk_root, monkeypatch):
    errors = []
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *error: errors.append(error))
    monkeypatch.setattr(theme, "_screen_bounds", lambda _window: (0, 0, 1280, 720))

    def no_network(*_args, **_kwargs):
        pytest.fail("DPI checks must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    yield tk_root
    assert not errors


def capture(window, name):
    destination = os.environ.get("API_SWITCHER_DPI_CAPTURE_DIR")
    if destination:
        from tools.ui_visual_audit import capture_window_image

        folder = Path(destination)
        folder.mkdir(parents=True, exist_ok=True)
        capture_window_image(window).save(folder / (name + ".png"))
        # Geometry only; never persist text, field values or credentials.
        metrics = {}
        for key, (field, _kind) in window.__dict__.get("_fields", {}).items():
            inner = getattr(field, "_canvas", None)
            metrics[key] = {"x": field.winfo_rootx() - window.winfo_rootx(),
                            "width": field.winfo_width(), "parent_width": field.master.winfo_width(),
                            "draw_width": field._current_width * field._get_widget_scaling(),
                            "canvas_width": inner.winfo_width() if inner is not None else None,
                            "border_bounds": inner.bbox("border_parts") if inner is not None else None}
        if "name" in window.__dict__.get("_fields", {}):
            scroll = window._fields["name"][0].master.master
            if hasattr(scroll, "_parent_canvas"):
                canvas = scroll._parent_canvas
                metrics["__scroll"] = {"content_width": scroll.winfo_width(),
                                         "canvas_width": canvas.winfo_width(),
                                         "embedded_width": canvas.itemcget(scroll._create_window_id, "width"),
                                         "configure_binding": canvas.bind("<Configure>")}
        (folder / (name + ".json")).write_text(json.dumps(metrics, indent=2), encoding="utf-8")


@pytest.mark.parametrize("dpi", [100, 125, 150, 175, 200, 250])
@pytest.mark.parametrize("kind", ["claude", "codex", "ssh"])
def test_editors_keep_actions_visible_on_short_high_dpi_screen(isolated_ui, monkeypatch, dpi, kind):
    monkeypatch.setattr(ctk.ScalingTracker, "get_window_dpi_scaling", lambda _window: dpi / 100)
    dialog = (SSHEditorDialog(isolated_ui) if kind == "ssh"
              else ProfileEditorDialog(isolated_ui, profile_type=kind))
    try:
        # CTk releases DPI-induced temporary min/max constraints after 1s.
        # Assert settled geometry, not a transient Windows remapping frame.
        settle(isolated_ui, 1.1)
        capture(dialog, f"{kind}-editor-{dpi}-initial")
        scroll = (dialog._fields["name"][0].master.master if kind == "ssh"
                  else dialog._form_scroll)
        canvas = scroll._parent_canvas
        for field, _field_type in dialog._fields.values():
            if not field.winfo_viewable():
                continue
            bounds = (field.winfo_rootx(), field.winfo_width(), field.master.winfo_width(),
                      scroll.winfo_width(), canvas.winfo_rootx(), canvas.winfo_width(),
                      canvas.bbox("all"), canvas.xview(),
                      round(field._current_width * field._get_widget_scaling()),
                      field._canvas.winfo_width() if hasattr(field, "_canvas") else None)
            assert field.winfo_rootx() >= canvas.winfo_rootx() - 2, bounds
            assert field.winfo_rootx() + field.winfo_width() <= canvas.winfo_rootx() + canvas.winfo_width() + 2, bounds
            if isinstance(field, ctk.CTkEntry):
                assert abs(field._current_width * field._get_widget_scaling() - field.winfo_width()) <= 2, bounds
        if kind != "ssh":
            dialog._show_error("合成布局检查：请检查配置格式，当前内容没有保存。" * 18)
            settle(isolated_ui, 0.6)
            assert dialog._error_label.master is dialog._form_scroll
            capture(dialog, f"{kind}-editor-{dpi}")
            canvas = dialog._form_scroll._parent_canvas
            assert canvas.yview()[1] == pytest.approx(1.0, abs=0.01), (
                canvas.yview(), canvas.bbox("all"), canvas.winfo_height())
        assert dialog.winfo_width() <= 1280
        assert dialog.winfo_height() <= 720
        buttons = [widget for widget in descendants(dialog)
                   if isinstance(widget, ctk.CTkButton) and widget.cget("text") in {"保存", "取消"}]
        assert len(buttons) == 2
        for button in buttons:
            inside(button, dialog)
            assert button._text_label.winfo_reqwidth() <= button.winfo_width()
        if kind != "ssh":
            inside(dialog._test_btn, dialog)
        capture(dialog, f"{kind}-editor-{dpi}")
    finally:
        dialog.destroy()
        isolated_ui.update()


def test_action_grids_reflow_when_dpi_changes_at_fixed_physical_width(isolated_ui, monkeypatch):
    current = [1.0]
    monkeypatch.setattr(ctk.ScalingTracker, "get_window_dpi_scaling", lambda _window: current[0])
    window = ctk.CTkToplevel(isolated_ui)
    window.geometry("720x650")
    footer = ctk.CTkFrame(window)
    footer.pack(fill="x", padx=10, pady=10)
    pair = [ctk.CTkButton(footer, text=text, width=1) for text in ("保存", "取消")]
    _bind_two_button_footer(footer, *pair)
    actions = ctk.CTkFrame(window)
    actions.pack(fill="x", padx=10, pady=10)
    buttons = [ctk.CTkButton(actions, text=f"操作{i}", width=1) for i in range(7)]
    _bind_responsive_grid(actions, buttons, lambda width: _auto_continue_layout(width)[1])
    try:
        for dpi in (100, 150, 250, 125, 200, 100):
            current[0] = dpi / 100
            tracker = ctk.ScalingTracker
            tracker.window_dpi_scaling_dict[window] = current[0]
            tracker.update_scaling_callbacks_for_window(window)
            settle(isolated_ui, 1.1)  # Let CTk release its temporary min/max constraint.
            window.geometry(f"{round(720 / current[0])}x{round(650 / current[0])}")
            settle(isolated_ui)
            expected = _auto_continue_layout(round(actions.winfo_width() / current[0]))[1]
            assert len({button.grid_info()["column"] for button in buttons}) == expected
            for button in [*pair, *buttons]:
                inside(button, window)
            capture(window, f"dynamic-actions-{dpi}")
    finally:
        window.destroy()
        isolated_ui.update()


def test_wraplength_tracks_label_scaling_in_a_fixed_container(isolated_ui):
    window = ctk.CTkToplevel(isolated_ui)
    window.geometry("640x480")
    container = ctk.CTkFrame(window, width=400, height=200)
    container.pack()
    container.pack_propagate(False)
    label = ctk.CTkLabel(container, text="长文本需要随字体缩放自动换行。" * 5)
    label.pack(fill="x")
    theme.bind_wraplength(container, label, padding=10)
    try:
        settle(isolated_ui)
        physical_width = container.winfo_width()
        scale = label._get_widget_scaling()
        label._set_scaling(scale * 1.5, window._get_window_scaling())
        settle(isolated_ui)
        assert container.winfo_width() == physical_width
        assert label.cget("wraplength") == round(physical_width / (scale * 1.5)) - 10
    finally:
        window.destroy()
        isolated_ui.update()


@pytest.mark.parametrize("kind", ["claude", "codex", "ssh"])
def test_editor_content_width_follows_resize_without_crossing_breakpoint(isolated_ui, monkeypatch, kind):
    monkeypatch.setattr(ctk.ScalingTracker, "get_window_dpi_scaling", lambda _window: 2.0)
    dialog = (SSHEditorDialog(isolated_ui) if kind == "ssh"
              else ProfileEditorDialog(isolated_ui, profile_type=kind))
    try:
        settle(isolated_ui, 1.1)
        entry = dialog._fields["name"][0]
        entry.insert(0, "synthetic-dpi-unsaved")
        scroll = dialog._form_scroll
        fields = dict(dialog._fields)
        for width in (480, 560, 450, 600):
            dialog.geometry(f"{width}x330")
            settle(isolated_ui)
            assert dialog._responsive_stacked
            assert abs(scroll.winfo_width() - scroll._parent_canvas.winfo_width()) <= 1
            assert dialog._fields == fields
            assert entry.get() == "synthetic-dpi-unsaved"
    finally:
        dialog.destroy()
        isolated_ui.update()
