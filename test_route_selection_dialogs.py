import copy

import pytest

from test_service_routes_dialog import _catalog, _preferences
from ui.dialogs.route_selection_dialogs import AUTO_MODE, FIXED_MODE, RouteNodeDialog, matching_nodes
from ui.widgets.service_route_overview import route_changes


def test_candidate_search_matches_multiple_terms_and_never_changes_source():
    nodes = [{"key": str(i), "label": f"Japan 家宽 {i:04d}"} for i in range(2000)]
    before = copy.deepcopy(nodes)
    assert matching_nodes(nodes, "JAPAN  家宽  0199") == [nodes[199]]
    assert len(matching_nodes(nodes, "  ")) == 2000
    assert matching_nodes(nodes, "不存在") == []
    assert nodes == before


@pytest.fixture
def picker(tk_root):
    selected = []
    dialog = RouteNodeDialog(tk_root, service_label="Claude Code", profile_name="家宽 A",
                              nodes=_catalog()[0]["nodes"], selected_key="two", on_select=selected.append)
    tk_root.update()
    try:
        yield tk_root, dialog, selected
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        tk_root.update()


def test_search_does_not_silently_select_a_different_node_or_mode(picker):
    root, dialog, selected = picker
    dialog._set_mode(AUTO_MODE)
    dialog._search.insert(0, "日本")
    dialog._filter()
    root.update()
    assert dialog._mode == AUTO_MODE
    assert dialog._selected_key == "two"
    assert selected == []
    dialog._commit()
    assert selected == [""]


def test_missing_fixed_node_cannot_be_committed_or_silently_replaced(picker):
    _root, dialog, selected = picker
    dialog._selected_key = "deleted"
    dialog._set_mode(FIXED_MODE)
    assert dialog._choose.cget("state") == "disabled"
    dialog._commit()
    assert selected == []
    assert dialog.winfo_exists()


@pytest.mark.parametrize("candidate_count", [1, 2])
def test_auto_strategy_explains_candidate_constraints_without_pinning(picker, candidate_count):
    _root, dialog, selected = picker
    dialog._candidate_count = candidate_count
    dialog._set_mode(AUTO_MODE)
    assert ("暂无备用" if candidate_count == 1 else "按服务策略筛选") in dialog._selection.cget("text")
    assert dialog._choose.cget("state") == "normal"
    dialog._commit()
    assert selected == [""]


def test_invalid_automatic_primary_can_still_be_replaced_with_a_fixed_node(picker):
    _root, dialog, selected = picker
    dialog._auto_route_usable = False
    dialog._set_mode(AUTO_MODE)
    assert "无法独立运行" in dialog._selection.cget("text")
    assert dialog._choose.cget("state") == "disabled"
    dialog._commit()
    assert selected == [] and dialog.winfo_exists()
    dialog._set_mode(FIXED_MODE)
    assert dialog._choose.cget("state") == "normal"
    dialog._commit()
    assert selected == ["two"]


def test_empty_search_keeps_explicit_selection_and_cancel_does_not_commit(picker):
    _root, dialog, selected = picker
    dialog._search.insert(0, "missing")
    dialog._filter()
    assert dialog._list.size() == 0
    assert "无匹配结果" in dialog._count.cget("text")
    assert "美国" in dialog._selection.cget("text")
    dialog.destroy()
    assert selected == []


def test_native_list_font_tracks_dpi_and_keyboard_starts_at_selection(picker):
    import customtkinter as ctk
    import tkinter.font
    root, dialog, _selected = picker
    previous = ctk.ScalingTracker.widget_scaling
    try:
        before = abs(tkinter.font.Font(root=root, font=dialog._list.cget("font")).actual("size"))
        ctk.set_widget_scaling(previous * 1.5)
        after = abs(tkinter.font.Font(root=root, font=dialog._list.cget("font")).actual("size"))
        assert after > before
        assert dialog._list.index("active") == 1
    finally:
        ctk.set_widget_scaling(previous)


def test_nested_close_does_not_run_timer_callbacks(picker):
    root, dialog, _selected = picker
    called = []
    timer = root.after(0, lambda: called.append(True))
    dialog.destroy()
    assert called == []
    root.after_cancel(timer)


def test_change_preview_includes_custom_inheritance_and_removals_without_mutation():
    original = _preferences()
    original["custom_targets"] = [{"id": "x", "value": "api.example.com", "enabled": True}]
    original["service_profile_bindings"]["custom"] = "home"
    draft = copy.deepcopy(original)
    draft["service_profile_bindings"]["custom"] = "dc"
    before = copy.deepcopy(original)
    changes = route_changes({"win": original}, {"win": draft}, _catalog())
    assert {item["service"] for item in changes} == {"custom", "custom:x"}
    draft["custom_targets"] = []
    changes = route_changes({"win": original}, {"win": draft}, _catalog())
    assert next(item for item in changes if item["service"] == "custom:x")["removed"]
    assert original == before


def test_same_named_subscriptions_are_still_distinguished_in_change_detection():
    original = _preferences()
    draft = copy.deepcopy(original)
    draft["service_profile_bindings"]["claude"] = "dc"
    catalog = _catalog()
    catalog[1]["name"] = catalog[0]["name"]
    changes = route_changes({"win": original}, {"win": draft}, catalog)
    assert any(item["service"] == "claude" for item in changes)


def test_inherited_changes_compare_stable_ids_even_if_display_names_match():
    original = _preferences()
    original["custom_targets"] = [{"id": "x", "value": "api.example.com", "enabled": True}]
    original["service_profile_bindings"]["custom"] = "home"
    draft = copy.deepcopy(original)
    draft["service_profile_bindings"]["custom"] = "dc"
    catalog = _catalog()
    catalog[1]["name"] = catalog[0]["name"]
    changes = route_changes({"win": original}, {"win": draft}, catalog)
    assert {item["service"] for item in changes} == {"custom", "custom:x"}
