"""Native UI lifecycle tests: synthetic nodes, never starts or tests a proxy."""
import time
from types import SimpleNamespace

import customtkinter as ctk
import pytest

from core.remote_proxy import ProxySubscriptionNode
from ui import theme
from ui.widgets import proxy_node_picker


def node(index):
    return ProxySubscriptionNode(index, {
        "name": f"{'美国' if index < 12 else '日本'}-{index:03}",
        "type": "http", "server": "example.invalid", "port": 1000 + index,
    })


def drain(root, picker):
    def pending():
        return (picker._render_after_id or picker._render_batch_after_id
                or picker._render_plan_pending or picker._checkbox_sync_after_id)

    deadline = time.monotonic() + 10
    while pending() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.003)
    root.update()
    assert not pending()


@pytest.fixture
def view(tk_root, monkeypatch):
    errors, active = [], [True]
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *args: errors.append(args))
    monkeypatch.setattr(proxy_node_picker, "is_active_tab", lambda _: active[0])
    window = ctk.CTkToplevel(tk_root)
    window.geometry("700x600")
    picker = proxy_node_picker.ProxyNodePicker(window)
    picker.pack(fill="both", expand=True, padx=10, pady=10)
    nodes = [node(i) for i in range(24)]
    picker.set_nodes(nodes)
    drain(tk_root, picker)
    try:
        yield SimpleNamespace(root=tk_root, window=window, picker=picker, nodes=nodes, active=active)
    finally:
        window.destroy()
        tk_root.update()
        assert not errors


def search(view, query):
    entry = view.picker._search_entry
    entry.delete(0, "end")
    entry.insert(0, query)
    view.picker._render_nodes()


def assert_layout(picker):
    expected = []
    for region, items in picker._group_visible_nodes(picker.filtered_items()):
        expected.append(picker._header_cache[region]["frame"])
        expected.extend(picker._row_cache[picker._node_key(item)]["row"] for item in items)
    if not expected:
        expected = [picker._empty_label]
    assert picker._list_frame.pack_slaves() == expected


def test_filter_uses_bottom_up_hide_and_preserves_widgets_and_order(view, monkeypatch):
    picker = view.picker
    original = picker._row_cache.copy()
    packed = picker._list_frame.pack_slaves()
    hidden = []
    render = picker._render_plan_item

    def track(kind, payload, extra):
        if kind == "hide":
            hidden.append(payload)
        render(kind, payload, extra)

    monkeypatch.setattr(picker, "_render_plan_item", track)
    search(view, "日本")
    # Logical scope is already complete before the slow native painting ends.
    assert picker.batch_items() == view.nodes[12:]
    drain(view.root, picker)
    assert_layout(picker)
    assert [packed.index(widget) for widget in hidden] == sorted(
        (packed.index(widget) for widget in hidden), reverse=True,
    )
    search(view, "")
    drain(view.root, picker)
    assert_layout(picker)
    assert picker._row_cache == original


def test_latest_filter_wins_when_hide_or_restore_is_interrupted(view):
    picker = view.picker
    cached = picker._row_cache.copy()
    for query in ("日本", "美国-00", "", "日本-02", "missing", "", "美国"):
        search(view, query)  # Interrupt before the next batch runs.
    drain(view.root, picker)
    assert picker.filtered_items() == view.nodes[:12]
    assert picker._row_cache == cached
    assert_layout(picker)
    search(view, "")
    drain(view.root, picker)
    assert_layout(picker)


def test_empty_search_reason_and_hidden_rows_update_without_recreation(view):
    picker = view.picker
    cached = picker._row_cache.copy()
    search(view, "missing")
    drain(view.root, picker)
    assert picker._empty_label.cget("text") == "没有匹配的节点"
    picker._quality_combo.set("家宽高质")
    picker._render_nodes()
    drain(view.root, picker)
    assert "暂无质量结果" in picker._empty_label.cget("text")
    assert_layout(picker)
    picker._reset_filters()
    drain(view.root, picker)
    assert_layout(picker)
    assert picker._row_cache == cached


def test_bulk_checks_are_logically_complete_before_batched_paint(view):
    picker = view.picker
    picker._set_matching_checked(True)
    assert picker.checked_items() == view.nodes
    assert picker.batch_items() == view.nodes
    assert picker._checkbox_sync_after_id is not None
    assert "正在更新勾选显示" in picker._summary_label.cget("text")
    assert sum(bool(row[1].get()) for row in picker._visible_checkboxes.values()) < len(view.nodes)
    assert next(iter(picker._visible_checkboxes.values()))[1].get()  # Visible top row paints first.
    drain(view.root, picker)
    assert all(row[1].get() for row in picker._visible_checkboxes.values())
    assert "正在更新勾选显示" not in picker._summary_label.cget("text")


def test_rapid_bulk_changes_and_stale_paint_preserve_latest_selection(view):
    picker = view.picker
    picker._set_matching_checked(True)
    old_generation = picker._checkbox_sync_generation
    keys = list(picker._visible_checkboxes)
    picker._set_group_checked(keys[:12], False)
    picker._set_group_checked(keys[:3], True)
    token = picker._checkbox_sync_after_id
    picker._sync_checkbox_batch(keys, 0, old_generation)
    assert picker._checkbox_sync_after_id == token
    drain(view.root, picker)
    expected = set(keys[:3] + keys[12:])
    assert picker._checked_keys == expected
    assert {key for key, row in picker._visible_checkboxes.items() if row[1].get()} == expected
    picker._set_matching_checked(True)
    picker._set_matching_checked(False)
    drain(view.root, picker)
    assert not picker._checked_keys
    assert not any(row[1].get() for row in picker._visible_checkboxes.values())


def test_hidden_pending_checks_resume_and_destroy_cancels_timer(view):
    picker = view.picker
    picker._set_matching_checked(True)
    old_token = picker._checkbox_sync_after_id
    assert old_token
    view.active[0] = False
    picker._suspend_background_work()
    assert picker._checkbox_sync_after_id is None
    assert old_token not in view.root.tk.call("after", "info")
    view.active[0] = True
    picker._resume_background_work()
    drain(view.root, picker)
    assert all(row[1].get() for row in picker._visible_checkboxes.values())
    picker._set_matching_checked(False)
    old_token = picker._checkbox_sync_after_id
    picker.destroy()
    assert picker._checkbox_sync_after_id is None
    assert old_token not in view.root.tk.call("after", "info")


def test_subscription_change_during_pending_paint_discards_old_widgets(view):
    picker = view.picker
    old = list(picker._row_cache.values())
    picker._set_matching_checked(True)
    new_nodes = [node(30), node(31)]
    picker.set_nodes(new_nodes)
    drain(view.root, picker)
    assert not picker.checked_items()
    assert picker.batch_items() == new_nodes
    assert len(picker._row_cache) == 2
    assert not any(cached["row"].winfo_exists() for cached in old)
    assert not any(row[1].get() for row in picker._visible_checkboxes.values())
    assert_layout(picker)


def test_picker_summary_wraps_on_narrow_scaled_window(view):
    old = ctk.ScalingTracker.widget_scaling
    try:
        for scale in (1.0, 1.5):
            ctk.set_widget_scaling(scale)
            view.window.geometry("360x600")
            view.root.update()
            label = view.picker._summary_label
            logical = view.picker.winfo_width() / label._get_widget_scaling()
            assert label.cget("wraplength") <= logical
            assert label.winfo_height() > theme.font(12).metrics("linespace")
    finally:
        ctk.set_widget_scaling(old)
        view.root.update()


def test_long_node_names_details_and_badges_fit_on_resize_and_dpi_change(view):
    long_node = node(1)
    long_node.node.update(name="美国-长节点名称-合成测试" * 8, server="long-synthetic-host.example.invalid")
    view.picker.set_nodes([long_node])
    drain(view.root, view.picker)
    old = ctk.ScalingTracker.widget_scaling
    try:
        for scale, width in ((1.0, 360), (1.5, 360), (1.0, 900), (1.5, 900)):
            ctk.set_widget_scaling(scale)
            view.window.geometry(f"{width}x650")
            view.root.update()
            cached = next(iter(view.picker._row_cache.values()))
            row = cached["row"]
            row_right = row.winfo_rootx() + row.winfo_width()
            for label in cached["labels"]:
                assert label.winfo_width() > 1
                assert label.winfo_rootx() >= row.winfo_rootx()
                assert label.winfo_rootx() + label.winfo_width() <= row_right, {
                    "scale": scale, "width": width, "layout": cached["layout"],
                    "row_bounds": (row.winfo_rootx(), row_right),
                    "label_bounds": (label.winfo_rootx(), label.winfo_width()),
                }
            title, detail, latency, quality = cached["labels"]
            assert title._label.winfo_reqheight() <= title.winfo_height()
            assert detail._label.winfo_reqheight() <= detail.winfo_height()
            assert title.cget("wraplength") <= title.winfo_width() / title._get_widget_scaling()
            assert int(latency.grid_info()["row"]) == (2 if cached["layout"][0] else 0)
            assert quality.winfo_rootx() >= latency.winfo_rootx() + latency.winfo_width()
    finally:
        ctk.set_widget_scaling(old)
        view.root.update()


def test_toplevel_wrapping_reacts_to_widget_only_dpi_change(tk_root):
    old = ctk.ScalingTracker.widget_scaling
    window = ctk.CTkToplevel(tk_root)
    window.geometry("600x300")
    label = ctk.CTkLabel(window, text="Synthetic wrapping content " * 10, justify="left")
    label.pack(fill="x")
    theme.bind_wraplength(window, label, padding=40)
    try:
        tk_root.update()
        for scale in (1.0, 1.5, 2.0):
            ctk.set_widget_scaling(scale)
            tk_root.update()
            expected = round(window.winfo_width() / label._get_widget_scaling()) - 40
            assert label.cget("wraplength") == expected
    finally:
        ctk.set_widget_scaling(old)
        window.destroy()
        tk_root.update()
