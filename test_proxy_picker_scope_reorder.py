"""Pure widget doubles: latency ranking must not masquerade as a new subscription."""
import socket

import pytest

from core import remote_proxy
from test_ui_render_performance import install_row_cache, node, picker_widget
from ui.widgets import proxy_node_picker


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("node ordering tests must not access the network")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


def _expected_widgets(picker, *, current_page=False):
    expected = []
    matches = picker.filtered_items()
    if current_page:
        start = picker._page_index * picker.PAGE_SIZE
        matches = matches[start:start + picker.PAGE_SIZE]
    for region, items in picker._group_visible_nodes(matches):
        expected.append(picker._header_cache[region]["frame"])
        expected.extend(picker._row_cache[picker._node_key(item)]["row"] for item in items)
    return expected


def _retained_picker(monkeypatch, nodes):
    picker, _, pending, drain = picker_widget(monkeypatch)
    picker.set_nodes(nodes)
    drain()
    install_row_cache(picker)
    # The ordinary renderer double does not simulate Tk's pack order. Give
    # retained roots an actual deterministic before=/append layout here.
    frame = picker._list_frame
    frame.roots = _expected_widgets(picker)
    for root in frame.roots:
        def pack(*, widget=root, **options):
            frame.roots.remove(widget)
            before = options.get("before")
            frame.roots.insert(frame.roots.index(before) if before is not None else len(frame.roots), widget)
            widget.packed = True
        root.pack = pack
    return picker, pending, drain


@pytest.mark.parametrize("count", [2, 64, 512])
def test_latency_rank_changes_keep_controls_generation_and_new_order(monkeypatch, count):
    nodes = [node(index) for index in range(count)]
    picker, _, drain = _retained_picker(monkeypatch, nodes)
    rows = picker._row_cache.copy()
    roots = tuple(picker._list_frame.roots)
    generation = picker._node_scope_generation
    selected = picker._node_key(nodes[0])
    checked = picker._node_key(nodes[-1])
    picker._checked_keys = {checked}
    results = {
        picker._node_key(item): remote_proxy.ProxyNodeLatencyResult(
            picker._node_key(item), True, latency_ms=count - index,
        ) for index, item in enumerate(nodes)
    }
    ranked = remote_proxy.sort_proxy_subscription_nodes(nodes, results)
    assert list(ranked) != nodes
    picker.set_nodes(ranked, results, selected_key=selected)
    drain()
    assert picker._node_scope_generation == generation
    assert all(picker._row_cache[key] is value for key, value in rows.items())
    assert not any(root.destroyed for root in roots)
    assert picker._list_frame.pack_slaves() == _expected_widgets(picker, current_page=True)
    assert picker.filtered_items() == list(ranked)
    assert len(picker.group_items("美国")) == count
    assert picker.batch_items() == [item for item in ranked if picker._node_key(item) == checked]
    assert picker.selected_key() == selected and picker._checked_keys == {checked}
    assert picker._header_cache["美国"]["ok"] == count


def test_latest_reorder_wins_when_prior_layout_is_pending(monkeypatch):
    nodes = [node(index) for index in range(40)]
    picker, pending, drain = _retained_picker(monkeypatch, nodes)
    rows = picker._row_cache.copy()
    generation = picker._node_scope_generation
    for ordered in (list(reversed(nodes)), nodes[10:] + nodes[:10], nodes[::2] + nodes[1::2]):
        picker.set_nodes(ordered)
    assert pending
    drain()
    assert picker._nodes == nodes[::2] + nodes[1::2]
    assert picker._node_scope_generation == generation
    assert picker._list_frame.pack_slaves() == _expected_widgets(picker)
    assert all(picker._row_cache[key] is value for key, value in rows.items())


def test_hidden_reorder_keeps_scope_and_live_callbacks(monkeypatch):
    nodes = [node(1), node(2)]
    picker, _, drain = _retained_picker(monkeypatch, nodes)
    rows = picker._row_cache.copy()
    generation = picker._node_scope_generation
    selected = []
    picker._on_select = selected.append
    monkeypatch.setattr(proxy_node_picker, "is_active_tab", lambda _widget: False)
    refreshed = [node(2), node(1)]
    picker.set_nodes(refreshed)
    assert picker._node_scope_generation == generation
    monkeypatch.setattr(proxy_node_picker, "is_active_tab", lambda _widget: True)
    picker._render_nodes()
    drain()
    picker._select(picker._node_key(nodes[1]), scope=generation)
    assert selected == [refreshed[0]]
    assert selected[0] is refreshed[0]
    assert picker._list_frame.pack_slaves() == _expected_widgets(picker)
    assert all(picker._row_cache[key] is value for key, value in rows.items())


@pytest.mark.parametrize("change", ["add", "remove", "same_key_region"])
def test_actual_scope_changes_invalidate_old_callback_before_paint(monkeypatch, change):
    monkeypatch.setattr(remote_proxy, "proxy_subscription_node_key", lambda item: f"synthetic-key-{item.index}")
    first, second = node(1), node(2)
    picker, _, _, _ = picker_widget(monkeypatch)
    # No retained native controls are needed: inspect invalidation immediately
    # after set_nodes, before the next teardown/render batch could run.
    picker._enabled = True
    picker.set_nodes([first, second])
    original_generation = picker._node_scope_generation
    picker._checked_keys = {picker._node_key(first)}
    replacement = [first, second, node(3)] if change == "add" else [first]
    if change == "same_key_region":
        replacement = [node(1, "日本"), second]
    picker.set_nodes(replacement)
    assert picker._node_scope_generation == original_generation + 1
    assert not picker._node_action_is_current(original_generation, (picker._node_key(first),))
    assert picker._rendered_signature is None


def test_hidden_subscription_round_trip_does_not_revive_old_scope(monkeypatch):
    picker, _, _, drain = picker_widget(monkeypatch)
    picker.set_nodes([node(1), node(2)])
    drain()
    generation = picker._node_scope_generation
    monkeypatch.setattr(proxy_node_picker, "is_active_tab", lambda _widget: False)
    picker.set_nodes([node(3), node(4)])
    picker.set_nodes([node(2), node(1)])
    assert picker._node_scope_generation == generation + 2
    assert not picker._node_action_is_current(generation, (picker._node_key(node(1)),))
    assert picker._rendered_signature is None and picker._pending_render_signature is None


def test_duplicate_keys_never_use_retained_row_mapping(monkeypatch):
    picker, _, _, _ = picker_widget(monkeypatch)
    previous = (None, (("same", "美国", True, False), ("same", "美国", False, False)), ())
    current = (None, tuple(reversed(previous[1])), ())
    assert not picker._reuse_rendered_rows(previous, current, [], 0)


def test_duplicate_member_count_change_invalidates_scope(monkeypatch):
    picker, _, _, _ = picker_widget(monkeypatch)
    first, second = node(1), node(2)
    picker.set_nodes([first, second])
    generation = picker._node_scope_generation
    picker.set_nodes([first, second, first])
    assert picker._node_scope_generation == generation + 1
