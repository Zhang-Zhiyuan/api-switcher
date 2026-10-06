"""Native paging tests use only synthetic subscriptions and never probe hosts."""
import time
from types import SimpleNamespace

import customtkinter as ctk
import pytest

from core.remote_proxy import ProxySubscriptionNode
from ui.widgets import proxy_node_picker as module


def nodes(count=1000, offset=0):
    return [ProxySubscriptionNode(index + offset, {
        "name": f"美国-合成长节点名称-{index:04}-" + "线路详情" * 12,
        "type": "http", "server": "synthetic.example.invalid", "port": 1000 + offset + index,
    }) for index in range(count)]


def drain(view):
    picker = view.picker
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        view.root.update()
        if not (picker._render_after_id or picker._render_batch_after_id
                or picker._render_plan_pending or picker._checkbox_sync_after_id):
            return
        time.sleep(0.003)
    pytest.fail("bounded native page did not finish within 25 seconds")


def settle(root):
    until = time.monotonic() + 0.2
    while time.monotonic() < until:
        root.update()
        time.sleep(0.005)


@pytest.fixture
def view(tk_root, monkeypatch):
    errors = []
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *args: errors.append(args))
    monkeypatch.setattr(module, "is_active_tab", lambda _widget: True)
    monkeypatch.setattr(module, "recent_user_scroll", lambda *_args, **_kwargs: False)
    window = ctk.CTkToplevel(tk_root)
    window.geometry("740x760+30+30")
    picker = module.ProxyNodePicker(window)
    picker.pack(fill="both", expand=True, padx=12, pady=12)
    view = SimpleNamespace(root=tk_root, window=window, picker=picker, nodes=nodes())
    picker.set_nodes(view.nodes)
    drain(view)
    try:
        yield view
    finally:
        window.destroy()
        tk_root.update()
        assert not errors


def test_large_subscription_has_bounded_native_rows_but_complete_test_scope(view):
    picker = view.picker
    assert len(picker._row_cache) == len(picker._visible_node_rows) == 120
    assert picker.filtered_items() == picker.batch_items() == view.nodes
    picker._visible_group_headers[0]["toggle"].invoke()
    assert picker.checked_items() == picker.batch_items() == view.nodes
    drain(view)
    assert all(variable.get() for _checkbox, variable in picker._visible_checkboxes.values())
    picker._visible_group_headers[0]["toggle"].invoke()
    picker._set_matching_checked(True)
    assert picker.checked_items() == view.nodes
    emitted = []
    picker._on_group_quality = lambda region, items: emitted.append((region, list(items)))
    picker._emit_group_quality("美国")
    assert emitted == [("美国", view.nodes)]


def test_last_page_search_and_return_preserve_hidden_selection_and_checks(view):
    picker = view.picker
    selected = picker._node_key(view.nodes[950])
    assert picker.select_by_key(selected)
    picker._set_matching_checked(True)
    # Invoke real navigation commands without waiting for intermediate native
    # paints; only the latest render generation may create the final page.
    for _ in range(8):
        picker._next_page_button.invoke()
    drain(view)
    assert picker._page_index == 8
    assert len(picker._row_cache) == len(picker._visible_node_rows) == 40
    assert set(picker._visible_node_rows) == {picker._node_key(item) for item in view.nodes[960:]}
    assert picker.selected_key() == selected
    assert picker.checked_items() == picker.batch_items() == view.nodes
    assert picker._next_page_button.cget("state") == "disabled"
    assert "9/9" in picker._page_label.cget("text")
    picker._search_entry.insert(0, "-0999-")
    picker._render_nodes()
    drain(view)
    assert picker._page_index == 0
    assert not picker._page_bar.winfo_ismapped()
    assert picker.filtered_items() == [view.nodes[999]]
    assert picker.batch_items() == view.nodes  # Hidden checked nodes still take priority.
    picker._reset_filters()
    drain(view)
    assert picker._page_index == 0 and len(picker._visible_node_rows) == 120


def test_busy_and_stale_subscription_navigation_cannot_act_on_new_scope(view):
    picker = view.picker
    stale = picker._next_page_button.cget("command")
    picker.set_enabled(False)
    assert picker._previous_page_button.cget("state") == "disabled"
    assert picker._next_page_button.cget("state") == "disabled"
    stale()
    assert picker._page_index == 0
    picker.set_enabled(True)
    assert picker._next_page_button.cget("state") == "normal"
    picker.set_nodes(nodes(250, offset=2000))
    picker.set_nodes(view.nodes)
    stale()
    drain(view)
    assert picker._page_index == 0
    assert picker.batch_items() == view.nodes
    assert len(picker._visible_node_rows) == 120


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.5])
def test_paging_controls_fit_above_list_at_supported_dpi_and_capture(view, scale, tmp_path):
    from tools.ui_visual_audit import capture_window_image
    previous = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(scale)
        width = min(view.window.winfo_screenwidth() - 80, int(740 * scale))
        height = min(view.window.winfo_screenheight() - 120, int(760 * scale))
        view.window.geometry(f"{width}x{height}+30+30")
        settle(view.root)
        picker = view.picker
        bar = picker._page_bar
        left, right = bar.winfo_rootx(), bar.winfo_rootx() + bar.winfo_width()
        last_right = left
        for widget in (picker._previous_page_button, picker._page_label, picker._next_page_button):
            assert widget.winfo_viewable() and widget.winfo_width() > 1
            assert widget.winfo_rootx() >= last_right
            last_right = widget.winfo_rootx() + widget.winfo_width()
            assert last_right <= right
        assert bar.winfo_rooty() + bar.winfo_height() <= picker._list_frame._parent_canvas.winfo_rooty()
        assert picker._page_label._label.winfo_reqheight() <= picker._page_label.winfo_height()
        # Capture only this HWND; automated regression must not steal focus
        # from the user or depend on Windows accepting foreground activation.
        # Temporary output also avoids rewriting an older release's evidence.
        capture_window_image(view.window).save(tmp_path / f"picker-pages-{int(scale * 100)}.png")
    finally:
        ctk.set_widget_scaling(previous)
        settle(view.root)
