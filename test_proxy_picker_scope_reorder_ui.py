"""Native proof that quick-test ranking reorders retained controls, not subscriptions."""
import pytest

from core import remote_proxy
from test_proxy_picker_responsiveness import assert_layout, drain, node, view as _view_fixture


view = _view_fixture


def test_latency_sort_keeps_native_rows_and_previously_bound_commands(view, tk_root):
    picker = view.picker
    original = picker._row_cache.copy()
    generation = picker._node_scope_generation
    selected = []
    picker._on_select = selected.append
    chosen = view.nodes[2]
    key = picker._node_key(chosen)
    old_button = original[key]["button"]
    old_checkbox = original[key]["checkbox"]
    old_checkbox.toggle()
    results = {
        picker._node_key(item): remote_proxy.ProxyNodeLatencyResult(
            picker._node_key(item), True, latency_ms=len(view.nodes) - index,
        ) for index, item in enumerate(view.nodes)
    }
    ranked = remote_proxy.sort_proxy_subscription_nodes(view.nodes, results)
    assert list(ranked) != view.nodes
    picker.set_nodes(ranked, results)
    drain(tk_root, picker)
    assert picker._node_scope_generation == generation
    assert all(picker._row_cache[key] is value for key, value in original.items())
    assert_layout(picker)
    assert picker._checked_keys == {key}
    old_button.invoke()
    assert picker.selected_key() == key and selected == [chosen]
    old_checkbox.toggle()
    assert not picker._checked_keys


@pytest.mark.parametrize("count", [24, 120])
def test_interrupted_native_reorders_finish_with_latest_order_without_new_rows(view, tk_root, count):
    picker = view.picker
    view.nodes = [node(index) for index in range(count)]
    picker.set_nodes(view.nodes)
    drain(tk_root, picker)
    original = picker._row_cache.copy()
    generation = picker._node_scope_generation
    for nodes in (list(reversed(view.nodes)), view.nodes[4:] + view.nodes[:4], view.nodes[::2] + view.nodes[1::2]):
        picker.set_nodes(nodes)
    drain(tk_root, picker)
    assert picker._nodes == view.nodes[::2] + view.nodes[1::2]
    assert picker._node_scope_generation == generation
    assert all(picker._row_cache[key] is value for key, value in original.items())
    assert_layout(picker)
