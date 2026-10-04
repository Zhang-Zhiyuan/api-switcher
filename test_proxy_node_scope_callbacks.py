"""Old native node controls must not change the newly selected subscription.

These tests use synthetic widgets only: no Tk interpreter, proxy, or user data.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from core.remote_proxy import ProxySubscriptionNode
from ui.widgets import proxy_node_picker as module


class Widget:
    def __init__(self, _master=None, **kwargs):
        self.values = kwargs

    def configure(self, **kwargs):
        self.values.update(kwargs)

    def grid(self, **_kwargs):
        pass

    def pack(self, **_kwargs):
        pass

    def grid_columnconfigure(self, *_args, **_kwargs):
        pass

    def bind(self, *_args, **_kwargs):
        pass

    def invoke(self):
        self.values["command"]()


class Variable:
    def __init__(self, value=False):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def node(index):
    return ProxySubscriptionNode(index, {
        "name": f"美国-合成-{index}", "type": "http",
        "server": f"node-{index}.example.invalid", "port": 8080,
    })


@pytest.fixture
def view(monkeypatch):
    picker = object.__new__(module.ProxyNodePicker)
    picker._nodes = []
    picker._selected_key = ""
    picker._checked_keys = set()
    picker._enabled = True
    picker._metadata_version = 0
    picker._row_cache = {}
    picker._header_cache = {}
    picker._visible_checkboxes = {}
    picker._visible_node_rows = {}
    picker._visible_group_headers = []
    picker._list_frame = Widget()
    picker._search_entry = picker._filter_combo = picker._region_combo = picker._quality_combo = None
    selected, quality = [], []
    picker._on_select = selected.append
    picker._on_group_quality = lambda region, items: quality.append((region, items))
    for name in ("_render_nodes", "_update_region_options", "_update_visible_selection",
                 "_update_scope_label", "_update_summary_label", "_update_group_headers",
                 "_emit_scope_change", "_sync_visible_checkboxes", "_layout_node_row"):
        monkeypatch.setattr(picker, name, lambda *_args, **_kwargs: None)
    for name in ("CTkFrame", "CTkLabel", "CTkCheckBox", "CTkButton"):
        monkeypatch.setattr(module.ctk, name, Widget)
    monkeypatch.setattr(module.ctk, "BooleanVar", Variable)
    monkeypatch.setattr(module, "font", lambda *_args: None)
    monkeypatch.setattr(module, "button_style", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "bind_wraplength", lambda *_args, **_kwargs: None)
    original = [node(1), node(2)]
    picker.set_nodes(original)
    picker._render_group_header("美国", original)
    for item in original:
        picker._render_row(item)
    return SimpleNamespace(picker=picker, original=original, selected=selected, quality=quality)


def old_action(view, kind):
    picker = view.picker
    key = picker._node_key(view.original[0])
    row = picker._row_cache[key]
    header = picker._header_cache["美国"]
    if kind == "select":
        return row["button"].invoke
    if kind == "check":
        row["variable"].set(True)
        return row["checkbox"].invoke
    if kind == "group":
        return header["toggle"].invoke
    return header["quality"].invoke


@pytest.mark.parametrize("kind", ["select", "check", "group", "quality"])
def test_previous_subscription_native_callback_is_ignored(view, kind):
    action = old_action(view, kind)
    picker = view.picker
    replacement = [node(11), node(12)]
    picker.set_nodes(replacement)
    selected = picker.selected_key()
    action()  # Old widgets can still exist while batched teardown is pending.
    assert picker.selected_key() == selected
    assert not picker._checked_keys
    assert picker.batch_scope_label() == "全部 2 个节点"
    assert not view.selected and not view.quality


@pytest.mark.parametrize("kind", ["select", "check", "group", "quality"])
def test_scope_changed_away_and_back_does_not_revive_old_callback(view, kind):
    action = old_action(view, kind)
    picker = view.picker
    picker.set_nodes([node(11)])
    picker.set_nodes(deepcopy(view.original))
    action()
    assert not picker._checked_keys
    assert not view.selected and not view.quality


@pytest.mark.parametrize("kind", ["select", "check", "group", "quality"])
def test_unchanged_scope_refresh_keeps_reused_controls_functional(view, kind):
    action = old_action(view, kind)
    picker = view.picker
    refreshed = deepcopy(view.original)
    picker.set_nodes(refreshed)
    action()
    if kind == "select":
        assert view.selected == refreshed[:1]
        assert view.selected[0] is refreshed[0]
    elif kind == "quality":
        assert view.quality == [("美国", tuple(refreshed))]
        assert view.quality[0][1][0] is refreshed[0]
    else:
        assert picker.checked_items() == (refreshed[:1] if kind == "check" else refreshed)


@pytest.mark.parametrize("kind", ["select", "check", "group", "quality"])
@pytest.mark.parametrize("state", ["disabled", "destroyed"])
def test_queued_actions_reject_disabled_or_destroyed_picker(view, kind, state):
    action = old_action(view, kind)
    picker = view.picker
    if state == "disabled":
        picker._enabled = False
    else:
        picker._destroyed = True
    action()
    assert not picker._checked_keys
    assert not view.selected and not view.quality


@pytest.mark.parametrize("kind", ["select", "check", "group"])
def test_direct_stale_keys_cannot_create_a_phantom_batch_scope(view, kind):
    picker = view.picker
    old_key = picker._node_key(view.original[0])
    picker.set_nodes([node(11), node(12)])
    before = picker.selected_key()
    if kind == "select":
        picker._select(old_key)
    elif kind == "check":
        picker._toggle_checked(old_key, True)
    else:
        picker._set_group_checked([old_key, picker.selected_key()], True)
    assert picker.selected_key() == before
    assert not picker._checked_keys
    assert not view.selected


def test_programmatic_selection_is_still_allowed_while_picker_disabled(view):
    picker = view.picker
    picker._enabled = False
    key = picker._node_key(view.original[1])
    assert picker.select_by_key(key)
    assert picker.selected_key() == key


@pytest.mark.parametrize("state", ["disabled", "destroyed", "changed", "changed_back"])
def test_rejected_checkbox_command_never_reads_its_stale_tcl_variable(view, state):
    picker = view.picker
    action = old_action(view, "check")
    variable = picker._row_cache[picker._node_key(view.original[0])]["variable"]
    reads = []

    def stale_variable():
        reads.append(True)
        raise RuntimeError("synthetic destroyed Tcl variable")

    variable.get = stale_variable
    if state == "disabled":
        picker._enabled = False
    elif state == "destroyed":
        picker._destroyed = True
    else:
        picker.set_nodes([node(11)])
        if state == "changed_back":
            picker.set_nodes(view.original)
    action()
    assert not reads
    assert not picker._checked_keys


def test_disappeared_checkbox_variable_is_ignored_without_changing_scope(view):
    picker = view.picker
    action = old_action(view, "check")
    variable = picker._row_cache[picker._node_key(view.original[0])]["variable"]

    def stale_variable():
        raise RuntimeError("synthetic destroyed Tcl variable")

    variable.get = stale_variable
    action()
    assert not picker._checked_keys


@pytest.mark.parametrize("temporary_scope", ["replacement", "empty", "reordered"])
def test_hidden_scope_round_trip_rebuilds_controls_with_current_generation(monkeypatch, temporary_scope):
    # Use the real render coordinator, rather than the callback fixture's
    # no-op renderer. Identical displayed data is not sufficient for reuse
    # after a hidden tab visited a different subscription in between.
    from test_ui_render_performance import install_row_cache, picker_widget

    picker, rendered, _pending, drain = picker_widget(monkeypatch)
    original = [node(1), node(2)]
    picker.set_nodes(original)
    drain()
    render = picker._render_plan_item
    completed_signature = picker._rendered_signature
    install_row_cache(picker)
    picker._render_plan_item = render
    old_roots = list(picker._list_frame.roots)
    original_generation = picker._node_scope_generation
    monkeypatch.setattr(module, "is_active_tab", lambda _widget: False)
    temporary = {
        "replacement": [node(11)], "empty": [], "reordered": list(reversed(original)),
    }[temporary_scope]
    picker.set_nodes(temporary)
    picker.set_nodes(deepcopy(original))
    assert picker._node_scope_generation > original_generation
    assert picker._rendered_signature is None
    assert picker._pending_render_signature is None
    assert not any(root.destroyed for root in old_roots)

    monkeypatch.setattr(module, "is_active_tab", lambda _widget: True)
    picker._resume_background_work()
    drain()
    assert all(root.destroyed for root in old_roots)
    assert len(rendered) == 6  # Header and two rows recreated with fresh commands.
    assert picker._rendered_signature == completed_signature
