"""Native final-save guards cover normal, bulk, copied and stale route drafts."""
import copy
from pathlib import Path
import time

import customtkinter as ctk
import pytest

from test_service_routes_dialog import _catalog, _preferences, _wait
from ui.dialogs.route_selection_dialogs import AUTO_MODE, POOL_MODE, RouteNodeDialog
from ui.dialogs.service_route_bulk_dialog import SET_ROUTE
from ui.dialogs.service_routes_dialog import DEFAULT_NODE, ServiceRoutesDialog


@pytest.fixture
def editor(tk_root):
    saved = []
    dialog = ServiceRoutesDialog(
        tk_root, scopes=["合成 Win11", "合成 SSH"], load_preferences=lambda _scope: _preferences(),
        catalog_loader=_catalog,
        apply_preferences=lambda *args: saved.append(copy.deepcopy(args)) or "合成已应用")
    _wait(tk_root, lambda: not dialog._busy)
    yield dialog, saved
    if dialog.winfo_exists():
        dialog.destroy()
    tk_root.update()


def capture(window, name):
    from tools.ui_visual_audit import capture_window_image
    directory = Path("dist/route-scope-ui")
    directory.mkdir(parents=True, exist_ok=True)
    capture_window_image(window).save(directory / name)


@pytest.mark.parametrize("method", ["node", "subscription", "bulk", "copy"])
def test_every_edit_path_confirms_before_removing_fixed_range(editor, tk_root, method):
    dialog, saved = editor
    if method == "node":
        dialog._select_node("claude", DEFAULT_NODE)
    elif method == "subscription":
        label = next(label for label, key in dialog._profile_values("claude").items() if key == "dc")
        dialog._select_profile("claude", label)
    elif method == "bulk":
        dialog._accept_bulk_edit(dialog._scope, ["claude"], SET_ROUTE, profile_id="home")
    else:
        dialog._select_node("claude", DEFAULT_NODE)
        dialog._copy_to_scopes([dialog._scopes[1]])
    expected = copy.deepcopy(dialog._drafts)
    dialog._save_button.invoke()
    tk_root.update()
    confirmation = dialog._scope_confirm_dialog
    assert confirmation and confirmation.winfo_exists() and not saved and not dialog._busy
    dialog._apply()  # Double clicking save must not open another confirmation.
    assert dialog._scope_confirm_dialog is confirmation
    if method == "node":
        _wait(tk_root, confirmation.winfo_viewable)
        capture(confirmation, "confirm-expanded-range.png")
    confirmation.destroy()
    tk_root.update()
    assert not saved and dialog._drafts == expected and dialog.grab_current() is dialog
    dialog._apply()
    dialog._scope_confirm_dialog._confirm()
    _wait(tk_root, lambda: not dialog._busy)
    assert len(saved) == (2 if method == "copy" else 1)


@pytest.mark.parametrize("change", ["draft", "original", "close"])
def test_confirmation_never_applies_a_different_snapshot(editor, tk_root, change):
    dialog, saved = editor
    dialog._select_node("claude", DEFAULT_NODE)
    dialog._apply()
    confirmation = dialog._scope_confirm_dialog
    if change == "draft":
        dialog._drafts[dialog._scope]["service_profile_bindings"]["claude"] = "dc"
    elif change == "original":
        dialog._originals[dialog._scope]["service_node_bindings"]["claude"] = "one"
    else:
        dialog.destroy()
    confirmation._on_confirm()
    tk_root.update()
    assert not saved and not dialog._busy
    if change != "close":
        assert "旧的范围确认已失效" in dialog._status.cget("text")


def test_narrowing_or_reapplying_fixed_routes_does_not_prompt(editor, tk_root):
    dialog, saved = editor
    dialog._select_node("claude", "日本 · 家宽 01")
    dialog._apply()
    _wait(tk_root, lambda: not dialog._busy)
    assert len(saved) == 1 and dialog._scope_confirm_dialog is None
    dialog._apply()
    _wait(tk_root, lambda: not dialog._busy)
    assert len(saved) == 2 and dialog._scope_confirm_dialog is None


@pytest.mark.parametrize("mode", [AUTO_MODE, POOL_MODE])
def test_small_scaled_node_picker_keeps_footer_visible_and_content_scrollable(tk_root, mode):
    selected = []
    child = RouteNodeDialog(
        tk_root, service_label="合成 Claude Code", profile_name="合成长订阅名 · " * 8,
        nodes=_catalog()[0]["nodes"], selected_key="", selected_keys=["one", "two"],
        on_select=selected.append, on_select_pool=selected.append)
    scale = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(1.5)
        deadline = time.monotonic() + 1.1
        while time.monotonic() < deadline:
            tk_root.update()
            time.sleep(0.01)
        child.geometry("480x540")
        child._set_mode(mode)
        tk_root.update()
        canvas = child._body._parent_canvas
        assert canvas.winfo_height() >= 100 * child._body._get_widget_scaling()
        assert child._choose.winfo_rooty() + child._choose.winfo_height() <= child.winfo_rooty() + child.winfo_height()
        assert child._modes.winfo_width() <= canvas.winfo_width()
        capture(child, "node-auto-small-top.png" if mode == AUTO_MODE else "node-pool-small-top.png")
        if mode == AUTO_MODE:
            assert "可能跨国家" in child._selection.cget("text")
        canvas.yview_moveto(1)
        tk_root.update()
        assert child._list_frame.winfo_rooty() + child._list_frame.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height() + 2
        assert not selected
        capture(child, "node-auto-small.png" if mode == AUTO_MODE else "node-pool-small.png")
        child._choose.invoke()
        assert selected == ([""] if mode == AUTO_MODE else [["one", "two"]])
    finally:
        if child.winfo_exists():
            child.destroy()
        ctk.set_widget_scaling(scale)
        tk_root.update()


@pytest.mark.parametrize("list_name", ["_list", "_pool_list"])
def test_wheel_scrolls_only_inner_list_until_boundary(tk_root, list_name):
    nodes = [{"key": str(index), "label": "合成滚轮节点 · " * 10 + str(index)} for index in range(40)]
    child = RouteNodeDialog(
        tk_root, service_label="合成候选池", profile_name="合成订阅", nodes=nodes,
        selected_key="", selected_keys=[str(index) for index in range(12)],
        on_select=lambda *_: None, on_select_pool=lambda *_: None)
    try:
        _wait(tk_root, child.winfo_viewable)
        child.geometry("480x540")
        tk_root.update()
        canvas, inner = child._body._parent_canvas, getattr(child, list_name)
        for delta, boundary in ((-120, 1), (120, 0)):
            inner.yview_moveto(0.3)
            canvas.yview_moveto(0.5)
            tk_root.update()
            before_list, before_page = inner.yview(), canvas.yview()
            inner.event_generate("<MouseWheel>", delta=delta)
            tk_root.update()
            assert inner.yview() != before_list and canvas.yview() == before_page
            inner.yview_moveto(boundary)
            # A short scroll region can clamp 0.5 to its bottom; begin at
            # the opposite edge so the page actually has room to move.
            canvas.yview_moveto(0 if delta < 0 else 1)
            tk_root.update()
            before_page = canvas.yview()
            inner.event_generate("<MouseWheel>", delta=delta)
            tk_root.update()
            assert canvas.yview() != before_page
    finally:
        child.destroy()
        tk_root.update()


def test_shift_wheel_never_moves_outer_vertical_page(tk_root):
    child = RouteNodeDialog(
        tk_root, service_label="合成横向滚轮", profile_name="合成长名称 · " * 12,
        nodes=[{"key": str(i), "label": "合成长节点名 · " * 20} for i in range(40)],
        selected_key="0", on_select=lambda *_: None)
    try:
        _wait(tk_root, child.winfo_viewable)
        child.geometry("480x410")
        tk_root.update()
        inner, canvas = child._list, child._body._parent_canvas
        canvas.yview_moveto(0.5)
        before_page = canvas.yview()
        inner.xview_moveto(0.3)
        before_list = inner.xview()
        inner.event_generate("<Shift-MouseWheel>", delta=-120, state=1)
        tk_root.update()
        assert inner.xview() != before_list and canvas.yview() == before_page
        inner.xview_moveto(1)
        inner.event_generate("<Shift-MouseWheel>", delta=-120, state=1)
        tk_root.update()
        assert canvas.yview() == before_page
    finally:
        child.destroy()
        tk_root.update()


def test_keyboard_focus_reveals_list_without_changing_fixed_selection(tk_root):
    child = RouteNodeDialog(
        tk_root, service_label="合成键盘操作", profile_name="合成长订阅名 · " * 24,
        nodes=_catalog()[0]["nodes"], selected_key="two", on_select=lambda *_: None)
    try:
        _wait(tk_root, child.winfo_viewable)
        child.geometry("480x410")
        tk_root.update()
        canvas = child._body._parent_canvas
        canvas.yview_moveto(0)
        assert canvas.yview()[1] < 1
        assert child._focus_list() == "break"
        tk_root.update()
        assert child._selected_key == "two"
        assert child._list_frame.winfo_rooty() + child._list_frame.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height() + 2
    finally:
        child.destroy()
        tk_root.update()
