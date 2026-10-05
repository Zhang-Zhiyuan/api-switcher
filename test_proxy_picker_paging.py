"""Large subscription rendering is bounded; logical test scopes stay complete."""
from types import SimpleNamespace

import pytest

from core import remote_proxy
from test_ui_render_performance import Root, node, picker_widget
from ui.widgets import proxy_node_picker as module


@pytest.fixture
def view(monkeypatch):
    picker, rendered, pending, drain = picker_widget(monkeypatch)
    picker._enabled = True
    picker._destroyed = False
    picker._page_bar = Root()
    picker._page_label = Root()
    picker._previous_page_button = Root()
    picker._next_page_button = Root()
    picker._visible_checkboxes = {}
    picker._visible_group_headers = []
    picker._summary_label = Root()
    picker._update_summary_label = module.ProxyNodePicker._update_summary_label.__get__(picker)
    picker._search_entry = None
    picker._filter_reset_button = None
    picker._batch_buttons = []
    return SimpleNamespace(picker=picker, rendered=rendered, pending=pending, drain=drain)


def displayed(view):
    return [item for kind, item in view.rendered if kind == "row"]


def change_page(view, direction):
    button = view.picker._next_page_button if direction > 0 else view.picker._previous_page_button
    view.rendered.clear()
    button.values["command"]()
    view.drain()


@pytest.mark.parametrize("count", [0, 1, 25, 120, 121, 1000])
def test_cold_render_is_bounded_without_limiting_batch_scope(view, count):
    nodes = [node(index) for index in range(count)]
    view.picker.set_nodes(nodes)
    assert view.picker.filtered_items() == nodes
    assert view.picker.batch_items() == nodes
    view.drain()
    assert displayed(view) == nodes[:120]
    assert view.picker._last_match_count == count
    assert view.picker._last_visible_count == min(count, 120)
    assert view.picker._page_bar.packed is (count > 120)


def test_navigation_reaches_every_node_and_keeps_selection_and_all_checks(view):
    nodes = [node(index) for index in range(1000)]
    picker = view.picker
    picker.set_nodes(nodes)
    view.drain()
    picker.select_by_key(picker._node_key(nodes[7]))
    picker._set_matching_checked(True)
    assert picker.checked_items() == nodes
    visited = displayed(view)
    while picker._next_page_button.values["state"] == "normal":
        change_page(view, 1)
        visited.extend(displayed(view))
        assert picker.checked_items() == nodes
        assert picker.batch_items() == nodes
        assert picker.selected_item() == nodes[7]
    assert visited == nodes
    assert picker._last_visible_count == 40
    assert "9/9" in picker._page_label.values["text"]
    assert "961–1000" in picker._page_label.values["text"]
    assert "翻页不改变勾选/检测范围" in picker._page_label.values["text"]
    change_page(view, -1)
    assert displayed(view) == nodes[840:960]


def test_search_resets_page_but_select_all_matching_includes_hidden_pages(view):
    nodes = [node(index) for index in range(1000)]
    picker = view.picker
    picker.set_nodes(nodes)
    view.drain()
    change_page(view, 1)
    picker._search_entry = SimpleNamespace(get=lambda: "synthetic query")
    picker._filtered_nodes = lambda: nodes[200:700]
    view.rendered.clear()
    picker._render_nodes()
    view.drain()
    assert displayed(view) == nodes[200:320]
    assert picker._page_index == 0
    assert picker.batch_items() == nodes[200:700]
    picker._set_matching_checked(True)
    assert picker.checked_items() == nodes[200:700]
    change_page(view, 1)
    assert displayed(view) == nodes[320:440]
    picker._set_matching_checked(False)
    assert not picker.checked_items()


def test_busy_navigation_cannot_change_page_and_buttons_restore_boundary_states(view):
    picker = view.picker
    picker.set_nodes([node(index) for index in range(250)])
    view.drain()
    command = picker._next_page_button.values["command"]
    picker.set_enabled(False)
    assert picker._next_page_button.values["state"] == "disabled"
    assert picker._previous_page_button.values["state"] == "disabled"
    command()
    assert picker._page_index == 0
    picker.set_enabled(True)
    assert picker._next_page_button.values["state"] == "normal"
    assert picker._previous_page_button.values["state"] == "disabled"


def test_old_navigation_callback_cannot_modify_new_subscription_or_filter(view):
    picker = view.picker
    original = [node(index) for index in range(250)]
    picker.set_nodes(original)
    view.drain()
    old_command = picker._next_page_button.values["command"]
    picker.set_nodes([node(index + 2000) for index in range(250)])
    old_command()
    view.drain()
    assert picker._page_index == 0
    stale_filter_command = picker._next_page_button.values["command"]
    picker._search_entry = SimpleNamespace(get=lambda: "changed query")
    picker._request_render_nodes(220)
    stale_filter_command()
    assert picker._page_index == 0


def test_result_reorder_keeps_page_and_membership_shrink_resets_safely(view):
    picker = view.picker
    nodes = [node(index) for index in range(250)]
    picker.set_nodes(nodes)
    view.drain()
    change_page(view, 1)
    view.rendered.clear()
    ranked = list(reversed(nodes))
    picker.set_nodes(ranked)
    view.drain()
    assert picker._page_index == 1
    assert displayed(view) == ranked[120:240]
    picker.set_nodes(nodes[:25])
    view.drain()
    assert picker._page_index == 0
    assert not picker._page_bar.packed


def test_filter_results_shrink_clamps_page_without_dropping_checked_nodes(view):
    picker = view.picker
    nodes = [node(index) for index in range(1000)]
    picker.set_nodes(nodes)
    view.drain()
    for _ in range(3):
        change_page(view, 1)
    picker._checked_keys = {picker._node_key(nodes[-1])}
    picker._filtered_nodes = lambda: nodes[:250]
    view.rendered.clear()
    picker._render_nodes()
    view.drain()
    assert picker._page_index == 2
    assert displayed(view) == nodes[240:250]
    assert picker.checked_items() == [nodes[-1]]
    assert picker.batch_items() == [nodes[-1]]


def test_group_action_includes_all_members_outside_rendered_page(view):
    nodes = [node(index, "美国") for index in range(250)]
    picker = view.picker
    picker.set_nodes(nodes)
    view.drain()
    emitted = []
    picker._on_group_quality = lambda region, items: emitted.append((region, list(items)))
    picker._emit_group_quality("美国")
    assert emitted == [("美国", nodes)]
    keys = [remote_proxy.proxy_subscription_node_key(item) for item in picker.group_items("美国")]
    picker._set_group_checked(keys, True)
    assert picker.checked_items() == nodes


def test_destroyed_picker_rejects_old_navigation_before_reading_widgets(view):
    picker = view.picker
    picker.set_nodes([node(index) for index in range(250)])
    view.drain()
    command = picker._next_page_button.values["command"]
    picker._destroyed = True
    picker._page_filter_state = lambda: pytest.fail("must not query destroyed widgets")
    command()
    assert picker._page_index == 0


def test_page_bar_packs_before_scrollable_wrapper_not_canvas_content(view):
    picker = view.picker
    wrapper = object()
    picker._list_frame._parent_frame = wrapper
    placements = []
    picker._page_bar.pack = lambda **options: placements.append(options)
    picker.set_nodes([node(index) for index in range(121)])
    assert placements[-1]["before"] is wrapper
