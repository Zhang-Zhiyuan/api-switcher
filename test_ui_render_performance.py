"""Regression contracts for UI reuse and bounded main-thread work (no live state)."""
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core import remote_proxy
from models.profile import BackupEntry
from ui import theme
from ui.tabs import backup_tab
from ui.widgets import proxy_node_picker


@pytest.mark.parametrize("raise_during_draw", [False, True])
@pytest.mark.parametrize("custom_flush", [False, True])
def test_scrollbar_guard_is_local_idempotent_and_restores_flush(monkeypatch, raise_during_draw, custom_flush):
    flushes = []

    class Canvas:
        def update_idletasks(self):
            flushes.append("class")

    class Scrollbar:
        def __init__(self):
            self._canvas = Canvas()

        def _draw(self, no_color_updates=False):
            self._canvas.update_idletasks()
            if raise_during_draw:
                raise RuntimeError("draw failed")
            return no_color_updates

    monkeypatch.setattr(theme.ctk, "CTkScrollbar", Scrollbar)
    theme._patch_scrollbar_idle_redraw()
    patched = Scrollbar._draw
    theme._patch_scrollbar_idle_redraw()
    assert Scrollbar._draw is patched
    scrollbar = Scrollbar()
    if custom_flush:
        scrollbar._canvas.update_idletasks = lambda: flushes.append("instance")
    if raise_during_draw:
        with pytest.raises(RuntimeError, match="draw failed"):
            scrollbar._draw(no_color_updates=True)
    else:
        assert scrollbar._draw(no_color_updates=True) is True
    assert flushes == []
    assert ("update_idletasks" in scrollbar._canvas.__dict__) is custom_flush
    scrollbar._canvas.update_idletasks()
    assert flushes == ["instance" if custom_flush else "class"]
    Canvas().update_idletasks()
    assert flushes[-1] == "class"


def test_appearance_draw_does_not_pump_idle_callbacks(monkeypatch):
    import tkinter
    from customtkinter.windows.widgets.core_widget_classes import CTkBaseClass

    widget = object.__new__(CTkBaseClass)
    calls = []
    widget._draw = lambda: calls.append("draw")
    monkeypatch.setattr(tkinter.Misc, "update_idletasks", lambda _self: calls.append("idle"))
    widget._set_appearance_mode("Light")
    assert calls == ["draw"]
    assert widget._apply_appearance_mode(("light", "dark")) == "light"
    widget._set_appearance_mode("Dark")
    assert widget._apply_appearance_mode(("light", "dark")) == "dark"


def test_native_tab_header_forgets_ctk_geometry_and_main_width_uses_widget_scale():
    from ui.app import App

    hidden = []
    tabview = SimpleNamespace(
        _segmented_button=SimpleNamespace(grid_forget=lambda: hidden.append(True)),
        grid_rowconfigure=lambda *_args, **_kwargs: None,
    )
    app = object.__new__(App)
    app._tabview = tabview
    app._shell = SimpleNamespace(_get_widget_scaling=lambda: 1.5)
    app.winfo_width = lambda: 1200
    app._get_window_scaling = lambda: 1.0
    app._hide_native_tab_header()
    assert hidden == [True]
    assert app._logical_main_width() == 800


def test_adaptive_navigation_forgets_hidden_controls_and_restores_dropdown_geometry():
    from ui.widgets.adaptive_tab_bar import AdaptiveTabBar

    class Control:
        def __init__(self):
            self.geometry = {}

        def grid(self, **kwargs):
            self.geometry = kwargs

        def grid_forget(self):
            self.geometry = {}

    bar = object.__new__(AdaptiveTabBar)
    bar._destroyed = False
    bar._values = [str(i) for i in range(11)]
    bar._buttons = {value: Control() for value in bar._values}
    bar._selector = Control()
    bar._column_count = 0
    bar._layout_mode = None
    bar.grid_columnconfigure = lambda *_args, **_kwargs: None
    bar._logical_width = lambda: 400
    bar._layout_buttons()
    assert bar._selector.geometry["sticky"] == "ew"
    assert not any(control.geometry for control in bar._buttons.values())
    bar._logical_width = lambda: 1200
    bar._layout_buttons()
    assert not bar._selector.geometry
    assert all(control.geometry for control in bar._buttons.values())


@pytest.mark.parametrize("orientation", ["vertical", "horizontal"])
def test_real_scrollbar_draw_drag_resize_theme_and_scaling(tk_root, monkeypatch, orientation):
    import tkinter
    import time
    import customtkinter as ctk

    # Keep one Tcl interpreter across parameter cases. CustomTkinter's global
    # font/scaling caches may retain a destroyed CTk root; that is not a missing
    # display and must not silently skip the second orientation on Windows.
    root = ctk.CTkToplevel(tk_root)
    old_scaling = ctk.ScalingTracker.widget_scaling
    old_appearance = ctk.get_appearance_mode()
    errors = []
    root.report_callback_exception = lambda *_args: errors.append(_args)
    monkeypatch.setattr(tk_root, "report_callback_exception", root.report_callback_exception)
    try:
        root.title("API 切换器 — 隔离滚动回归")
        root.geometry("640x420")
        canvas = tkinter.Canvas(root, scrollregion=(0, 0, 4000, 4000))
        view = canvas.yview if orientation == "vertical" else canvas.xview
        bar = ctk.CTkScrollbar(root, orientation=orientation, command=view)
        bar.pack(side="right" if orientation == "vertical" else "bottom", fill="y" if orientation == "vertical" else "x")
        canvas.pack(fill="both", expand=True)
        canvas.configure(**{"yscrollcommand" if orientation == "vertical" else "xscrollcommand": bar.set})
        root.update()
        def wait_until_mapped():
            # CTk temporarily withdraws new Windows Toplevels to recolor their
            # titlebar. Switching appearance before its 5 ms restore callback
            # records "withdrawn" as the next restore state. Wait as a user
            # necessarily would before interacting with an invisible window.
            deadline = time.monotonic() + 2
            while not bar._canvas.winfo_viewable() or min(bar._canvas.winfo_width(), bar._canvas.winfo_height()) <= 1:
                assert time.monotonic() < deadline, "scrollbar was not mapped"
                root.update()
                time.sleep(0.01)
        wait_until_mapped()
        idle = []
        root.after_idle(lambda: idle.append(True))
        bar.set(0.0, 0.2)
        assert idle == []  # Drawing must not recursively process unrelated idle work.
        bar._canvas.update_idletasks()
        assert idle == [True]

        for scale, appearance in ((1.0, "Light"), (1.5, "Dark")):
            ctk.set_widget_scaling(scale)
            ctk.set_appearance_mode(appearance)
            root.geometry("680x450")
            root.update()
            wait_until_mapped()
            view("moveto", 0)
            root.update_idletasks()
            bar._canvas.event_generate(
                "<Button-1>", x=round(bar._canvas.winfo_width() * 0.85),
                y=round(bar._canvas.winfo_height() * 0.85),
            )
            root.update()
            assert view()[0] > 0.2, (orientation, scale, bar._canvas.winfo_width(), bar._canvas.winfo_height(), bar.get())
            assert 0 <= bar.get()[0] < bar.get()[1] <= 1
            bar._canvas.event_generate("<MouseWheel>", delta=120)
            root.update()
            assert 0 <= view()[0] < view()[1] <= 1
        assert errors == []
    finally:
        ctk.set_widget_scaling(old_scaling)
        ctk.set_appearance_mode(old_appearance)
        root.destroy()
        tk_root.update()  # Finish native teardown before the next orientation.


class Root:
    def __init__(self, *_args, **kwargs):
        self.destroyed = False
        self.values = kwargs
        self.packed = True

    def configure(self, **kwargs):
        self.values.update(kwargs)

    def pack(self, **_kwargs):
        self.packed = True

    def pack_forget(self):
        self.packed = False

    def destroy(self):
        self.destroyed = True


class Frame:
    def __init__(self, roots=()):
        self.roots = list(roots)

    def winfo_children(self):
        return [root for root in self.roots if not root.destroyed]

    def pack_slaves(self):
        return [root for root in self.winfo_children() if root.packed]


def scheduler(widget):
    pending = {}
    ids = []

    def after(_delay, callback):
        after_id = f"after-{len(ids)}"
        ids.append(after_id)
        pending[after_id] = callback
        return after_id

    def drain():
        for _ in range(1000):
            if not pending:
                return
            pending.pop(next(iter(pending)))()
        raise AssertionError("render did not finish")

    widget.after = after
    widget.after_cancel = lambda after_id: pending.pop(after_id, None)
    return pending, drain


def entry(name="one", description="demo"):
    return BackupEntry("2026-09-08", Path("synthetic") / name, description, ["config"])


def backup_widget(monkeypatch):
    tab = object.__new__(backup_tab.BackupTab)
    tab._list_frame = Frame()
    tab._refresh_generation = 1
    tab._render_after_id = None
    tab._rendered_backups_signature = None
    tab._backup_action_buttons = []
    tab._deferred_render_pending = False
    tab.winfo_exists = lambda: True
    monkeypatch.setattr(backup_tab, "is_active_tab", lambda _widget: True)
    rendered = []

    def render(item):
        rendered.append(item)
        tab._list_frame.roots.append(Root())

    tab._render_backup_card = render
    pending, drain = scheduler(tab)
    return tab, rendered, pending, drain


def test_backup_refresh_keeps_complete_cards_until_worker_returns(monkeypatch):
    tab, rendered, _, drain = backup_widget(monkeypatch)
    payload = {"ok": True, "backups": [entry()]}
    tab._render_backups(payload, 1)
    drain()
    old_roots = tuple(tab._list_frame.winfo_children())
    old_signature = tab._rendered_backups_signature
    workers = []
    monkeypatch.setattr(backup_tab, "threading", SimpleNamespace(
        Thread=lambda **kwargs: SimpleNamespace(start=lambda: workers.append(kwargs["target"]))
    ))
    monkeypatch.setattr(backup_tab, "backup_manager", SimpleNamespace(list_backups=lambda: [entry()]))
    monkeypatch.setattr(backup_tab, "run_on_ui_thread", lambda _widget, callback: callback())

    tab.refresh()
    assert not any(root.destroyed for root in old_roots)
    assert tab._rendered_backups_signature == old_signature
    workers.pop()()
    drain()
    assert len(rendered) == 1
    assert tuple(tab._list_frame.winfo_children()) == old_roots


@pytest.mark.parametrize("change", ["description", "files", "directory", "order"])
def test_backup_signature_detects_changes_and_order(monkeypatch, change):
    tab, rendered, _, drain = backup_widget(monkeypatch)
    items = [entry("one"), entry("two")]
    tab._render_backups({"ok": True, "backups": items}, 1)
    drain()
    before = tab._rendered_backups_signature
    if change == "order":
        items.reverse()
    elif change == "directory":
        items[0].directory = Path("synthetic/new")
    elif change == "files":
        items[0].files.append("auth")
    else:
        items[0].description = "updated"
    assert tab._backups_signature(items) != before
    tab._refresh_generation += 1
    tab._render_backups({"ok": True, "backups": items}, 2)
    drain()
    assert len(rendered) == 4
    assert tab._rendered_backups_signature == tab._backups_signature(items)


def test_backup_error_is_not_cached_as_successful_empty_list(monkeypatch):
    tab, _, _, drain = backup_widget(monkeypatch)
    messages = []
    monkeypatch.setattr(backup_tab, "EmptyState", lambda _frame, title, *_args: (
        messages.append(title) or SimpleNamespace(pack=lambda **_kwargs: None)
    ))
    tab._render_backups({"ok": False, "error": "demo error"}, 1)
    drain()
    assert tab._rendered_backups_signature is None
    tab._render_backups({"ok": True, "backups": []}, 1)
    assert messages == ["读取备份记录失败", "暂无备份记录"]
    assert tab._rendered_backups_signature == ()


def test_backup_incomplete_and_hidden_renders_are_not_cached(monkeypatch):
    tab, _, pending, _ = backup_widget(monkeypatch)
    tab.RENDER_BATCH_SIZE = 1
    tab._render_backups({"ok": True, "backups": [entry(), entry("two")]}, 1)
    assert pending
    assert tab._rendered_backups_signature is None
    monkeypatch.setattr(backup_tab, "is_active_tab", lambda _widget: False)
    pending.pop(next(iter(pending)))()
    assert tab._deferred_render_pending
    assert tab._rendered_backups_signature is None


def test_backup_stale_clear_and_render_callbacks_do_nothing(monkeypatch):
    tab, rendered, _, _ = backup_widget(monkeypatch)
    root = Root()
    tab._clear_backup_batch((root,), {"ok": True, "backups": []}, 0, 0)
    tab._begin_backup_render({"ok": True, "backups": [entry()]}, 0)
    tab._render_backup_batch([entry()], 0, 0)
    assert not root.destroyed
    assert rendered == []


def node(index, name="美国"):
    return remote_proxy.ProxySubscriptionNode(index, {
        "name": f"{name}-{index}", "type": "http", "server": "demo.example.test", "port": 1000 + index,
    })


def picker_widget(monkeypatch):
    picker = object.__new__(proxy_node_picker.ProxyNodePicker)
    picker._nodes = []
    picker._selected_key = ""
    picker._checked_keys = set()
    picker._visible_node_rows = {}
    picker._render_after_id = None
    picker._render_batch_after_id = None
    picker._render_generation = 0
    picker._render_plan_pending = False
    picker._render_deferred = False
    picker._rendered_signature = None
    picker._metadata_version = 0
    picker._list_frame = Frame()
    picker._filter_combo = None
    picker._region_combo = None
    picker._quality_combo = None
    picker._search_entry = None
    picker._update_region_options = lambda: None
    picker._update_summary_label = lambda **_kwargs: None
    picker._update_scope_label = lambda: None
    picker._emit_scope_change = lambda: None
    rendered = []

    def render(kind, payload, _extra):
        rendered.append((kind, payload))
        picker._list_frame.roots.append(Root())

    picker._render_plan_item = render
    pending, drain = scheduler(picker)
    monkeypatch.setattr(proxy_node_picker, "is_active_tab", lambda _widget: True)
    monkeypatch.setattr(proxy_node_picker, "recent_user_scroll", lambda *_args, **_kwargs: False)
    return picker, rendered, pending, drain


def test_identical_nodes_reuse_completed_rows_and_preserve_checks(monkeypatch):
    picker, rendered, pending, drain = picker_widget(monkeypatch)
    picker.set_nodes([node(1), node(2)])
    drain()
    old = tuple(picker._list_frame.winfo_children())
    count = len(rendered)
    second_key = remote_proxy.proxy_subscription_node_key(node(2))
    picker._checked_keys = {second_key}
    selection_updates = []
    picker._update_visible_selection = lambda previous, selected: selection_updates.append((previous, selected))
    picker.set_nodes([node(1), node(2)], selected_key=second_key)
    assert picker._checked_keys == {second_key}
    assert selection_updates[-1][1] == second_key
    assert not pending
    assert len(rendered) == count
    assert tuple(picker._list_frame.winfo_children()) == old
    picker._render_nodes()  # A redundant search event also leaves the rows alone.
    assert not pending


def test_mutated_latency_results_invalidate_node_view(monkeypatch):
    picker, rendered, _, drain = picker_widget(monkeypatch)
    nodes = [node(1)]
    key = remote_proxy.proxy_subscription_node_key(nodes[0])
    results = {key: {"ok": True, "latency_ms": 40, "measured_at": datetime.now(timezone.utc).isoformat()}}
    picker.set_nodes(nodes, results)
    drain()
    previous = picker._rendered_signature
    results[key]["latency_ms"] = 180
    picker.set_nodes(nodes, results)
    drain()
    assert previous != picker._rendered_signature
    assert len(rendered) == 4  # Header and row, twice.


def test_hidden_group_members_invalidate_node_view(monkeypatch):
    picker, _, pending, drain = picker_widget(monkeypatch)
    first = node(1)
    picker._filtered_nodes = lambda: [first]
    picker.set_nodes([first, node(2)])
    drain()
    previous = picker._rendered_signature
    picker.set_nodes([first, node(2), node(3)])
    assert pending
    drain()
    assert picker._rendered_signature != previous
    assert len(picker.group_items("美国")) == 3


def test_partial_node_render_is_not_reused_after_filter_interrupt(monkeypatch):
    picker, _, pending, drain = picker_widget(monkeypatch)
    picker.RENDER_BATCH_SIZE = 1
    picker.set_nodes([node(1), node(2)])
    pending.pop(next(iter(pending)))()  # Initial header, two rows still queued.
    assert picker._rendered_signature is None
    picker._request_render_nodes(1)
    drain()
    assert picker._rendered_signature is not None
    assert len(picker._list_frame.winfo_children()) == 3


def test_failed_node_row_is_not_cached_as_a_complete_view(monkeypatch):
    picker, _, _, drain = picker_widget(monkeypatch)
    picker._render_plan_item = lambda *_args: (_ for _ in ()).throw(RuntimeError("render failed"))
    picker.set_nodes([node(1)])
    drain()
    assert picker._rendered_signature is None
    assert picker._render_plan_pending is False


def install_row_cache(picker):
    """Retained widget doubles using the real incremental update methods."""
    picker._enabled = True
    picker._row_cache = {}
    picker._header_cache = {}
    picker._visible_node_rows = {}
    picker._visible_checkboxes = {}
    picker._visible_group_headers = []
    roots = []
    for item in picker._nodes:
        key = picker._node_key(item)
        row, button, checkbox = Root(), Root(), Root()
        variable = SimpleNamespace(value=key in picker._checked_keys)
        variable.get = lambda v=variable: v.value
        variable.set = lambda value, v=variable: setattr(v, "value", value)
        picker._row_cache[key] = {
            "row": row, "button": button, "checkbox": checkbox, "variable": variable,
            "labels": (Root(), Root(), Root(), Root()), "presentation": picker._row_presentation(item),
            "enabled": True, "selected": key == picker._selected_key,
        }
        picker._visible_node_rows[key] = (row, button)
        picker._visible_checkboxes[key] = (checkbox, variable)
        roots.append(row)
    for region, items in picker._group_visible_nodes(picker._nodes):
        header = {
            "frame": Root(), "label": Root(), "toggle": Root(), "quality": Root(),
            "keys": tuple(picker._node_key(item) for item in items), "region": region,
            "total": len(items), "ok": 0, "high": 0,
        }
        picker._header_cache[region] = header
        picker._visible_group_headers.append(header)
        roots.append(header["frame"])
    picker._list_frame.roots = roots
    picker._render_plan_item = proxy_node_picker.ProxyNodePicker._render_plan_item.__get__(picker)


def test_filters_reuse_rows_including_empty_search_and_restore_all(monkeypatch):
    picker, _, _, drain = picker_widget(monkeypatch)
    nodes = [node(1), node(2)]
    picker.set_nodes(nodes)
    drain()
    install_row_cache(picker)
    rows = {key: cached["row"] for key, cached in picker._row_cache.items()}
    monkeypatch.setattr(proxy_node_picker.ctk, "CTkLabel", Root)
    monkeypatch.setattr(proxy_node_picker, "font", lambda *_args: None)
    for matches in ([nodes[1]], [], nodes):
        picker._filtered_nodes = lambda items=matches: items
        picker._render_nodes()
        drain()
        assert picker._rendered_signature is not None
        assert set(picker._visible_node_rows) == {picker._node_key(item) for item in matches}
        assert all(not row.destroyed for row in rows.values())
        assert {key: value["row"] for key, value in picker._row_cache.items()} == rows


def test_latency_refresh_updates_labels_and_group_counts_without_destroy(monkeypatch):
    picker, _, _, drain = picker_widget(monkeypatch)
    nodes = [node(1), node(2)]
    picker.set_nodes(nodes)
    drain()
    install_row_cache(picker)
    key = picker._node_key(nodes[0])
    results = {key: remote_proxy.ProxyNodeLatencyResult(key, True, latency_ms=80)}
    picker.set_nodes(nodes, results)
    drain()
    assert picker._row_cache[key]["labels"][2].values["text"] == "80ms"
    assert picker._header_cache["美国"]["ok"] == 1
    assert picker._rendered_signature is not None
    assert not any(root.destroyed for root in picker._list_frame.roots)


def test_hidden_rows_restore_enabled_state_and_latest_checks(monkeypatch):
    picker, _, _, drain = picker_widget(monkeypatch)
    nodes = [node(1), node(2)]
    picker.set_nodes(nodes)
    drain()
    install_row_cache(picker)
    picker._enabled = False
    picker._set_visible_rows_enabled(False)
    picker._filtered_nodes = lambda: [nodes[0]]
    picker._render_nodes()
    drain()
    picker._enabled = True
    picker._set_visible_rows_enabled(True)
    second_key = picker._node_key(nodes[1])
    picker._checked_keys.add(second_key)
    picker._filtered_nodes = lambda: nodes
    picker._render_nodes()
    drain()
    cached = picker._row_cache[second_key]
    assert cached["checkbox"].values["state"] == "normal"
    assert cached["button"].values["state"] == "normal"
    assert cached["variable"].get() is True


def test_latency_expiry_refreshes_reused_rows(monkeypatch):
    picker, _, _, drain = picker_widget(monkeypatch)
    nodes = [node(1)]
    key = remote_proxy.proxy_subscription_node_key(nodes[0])
    results = {key: remote_proxy.ProxyNodeLatencyResult(key, True, latency_ms=20)}
    picker.set_nodes(nodes, results)
    drain()
    install_row_cache(picker)
    monkeypatch.setattr(remote_proxy, "proxy_node_latency_fresh", lambda _result: False)
    picker.set_nodes(nodes, results)
    drain()
    assert picker._header_cache["美国"]["ok"] == 0
    assert picker._row_cache[key]["labels"][2].values["text"] == "已过期"


def test_quality_update_reuses_controls_with_new_display_values(monkeypatch):
    picker, _, _, drain = picker_widget(monkeypatch)
    nodes = [node(1)]
    picker.set_nodes(nodes)
    drain()
    install_row_cache(picker)
    key = picker._node_key(nodes[0])
    cached = picker._row_cache[key]
    previous = cached["presentation"]
    quality = remote_proxy.ProxyNodeQualityResult(key, True, quality_label="家宽", quality_score=80)
    picker.set_nodes(nodes, quality_results={key: quality})
    drain()
    assert picker._row_cache[key] is cached
    assert not cached["row"].destroyed
    assert cached["presentation"] != previous
    assert picker._rendered_signature is not None


def test_checkbox_scheduler_failure_finishes_paint_without_losing_selection(monkeypatch):
    picker, _, _, drain = picker_widget(monkeypatch)
    picker.set_nodes([node(i) for i in range(12)])
    drain()
    install_row_cache(picker)
    picker._checked_keys.update(picker._visible_checkboxes)
    picker.after = lambda *_args: (_ for _ in ()).throw(RuntimeError("synthetic scheduling failure"))
    picker._sync_visible_checkboxes()
    assert picker._checkbox_sync_after_id is None
    assert len(picker._checked_keys) == 12
    assert all(variable.get() for _, variable in picker._visible_checkboxes.values())


def test_bad_signature_does_not_skip_healthy_rows(monkeypatch):
    picker, rendered, _, drain = picker_widget(monkeypatch)
    picker._row_presentation = lambda _item: (_ for _ in ()).throw(ValueError("bad display data"))
    picker.set_nodes([node(1), node(2)])
    drain()
    assert len(rendered) == 3  # Signature failure still reaches per-item rendering.
    assert picker._rendered_signature is None


@pytest.mark.parametrize("kind", ["backup_clear", "backup_render", "node_clear", "node_render"])
def test_expensive_widgets_yield_when_batch_time_budget_is_used(monkeypatch, kind):
    clock = iter([0.0, 0.02])
    fake_time = SimpleNamespace(perf_counter=lambda: next(clock))
    if kind.startswith("backup"):
        tab, rendered, pending, _ = backup_widget(monkeypatch)
        monkeypatch.setattr(backup_tab, "time", fake_time)
        if kind == "backup_render":
            tab._render_backup_batch([entry(), entry("two")], 1, 0)
            assert len(rendered) == 1
        else:
            roots = [Root(), Root()]
            tab._clear_backup_batch(roots, {"ok": True, "backups": []}, 1, 0)
            assert [root.destroyed for root in roots] == [True, False]
    else:
        picker, rendered, pending, _ = picker_widget(monkeypatch)
        monkeypatch.setattr(proxy_node_picker, "time", fake_time)
        if kind == "node_render":
            picker._render_plan_batch(0, [("row", node(1), None), ("row", node(2), None)], 0)
            assert len(rendered) == 1
        else:
            roots = [Root(), Root()]
            picker._teardown_roots_batch(0, roots, [], None, 0)
            assert [root.destroyed for root in roots] == [True, False]
    assert len(pending) == 1
