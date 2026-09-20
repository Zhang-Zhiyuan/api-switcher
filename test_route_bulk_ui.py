"""Isolated draft-only contracts for batch routing and candidate-pool editing."""

import copy
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from core import proxy_routing
from ui.dialogs import service_route_bulk_dialog as bulk_ui
from ui.dialogs.service_routes_dialog import ServiceRoutesDialog
from ui.widgets.service_route_overview import route_changes, route_description


def _catalog():
    return [
        {"id": "home", "name": "合成家宽", "network_type": "unknown", "nodes": [
            {"key": "one", "label": "家宽一"}, {"key": "two", "label": "家宽二"},
            {"key": "shared", "label": "共享标识"},
        ]},
        {"id": "dc", "name": "合成非家宽", "network_type": "unknown", "nodes": [
            {"key": "three", "label": "机房一"}, {"key": "four", "label": "机房二"},
            {"key": "shared", "label": "共享标识"},
        ]},
    ]


def _preferences():
    return proxy_routing.normalize_routes({
        "builtin_sites": {"youtube": False, "google": True},
        "service_profile_bindings": {"claude": "home", "openai": "home", "youtube": "dc"},
        "service_node_bindings": {"claude": "one"},
        "service_node_pools": {"youtube": ["four", "three"], "openai": ["two", "one"]},
        "service_route_modes": {"google": "default"},
        "custom_targets": [{"id": "demo", "kind": "domain", "value": "example.test", "enabled": False}],
    })


def test_batch_pool_changes_only_chosen_routes_and_not_enabled_states():
    original = _preferences()
    before = copy.deepcopy(original)
    chosen = ["claude", "youtube", "custom:demo"]
    result, notices = bulk_ui.apply_route_batch(original, _catalog(), chosen, bulk_ui.SET_ROUTE,
                                                 profile_id="dc", node_keys=["four", "three"])
    assert original == before and result is not original
    assert result["builtin_sites"] == original["builtin_sites"]
    assert result["custom_targets"] == original["custom_targets"]
    for service in chosen:
        assert result["service_profile_bindings"][service] == "dc"
        assert result["service_node_pools"][service] == ["four", "three"]
        assert service not in result["service_node_bindings"]
    assert result["service_node_pools"]["openai"] == before["service_node_pools"]["openai"]
    assert result["service_node_pools"]["claude"] is not result["service_node_pools"]["youtube"]
    assert result["service_route_modes"] == {"google": "default"}
    assert not notices


def test_batch_follow_default_clears_fixed_pool_and_preserves_other_targets():
    original = _preferences()
    result, _ = bulk_ui.apply_route_batch(original, _catalog(), ["claude", "youtube"], bulk_ui.FOLLOW)
    for service in ("claude", "youtube"):
        for field in ("service_profile_bindings", "service_node_bindings", "service_node_pools"):
            assert service not in result[field]
        assert result["service_route_modes"][service] == "default"
    assert result["service_route_modes"]["google"] == "default"
    assert result["service_node_pools"]["openai"] == original["service_node_pools"]["openai"]
    assert result["builtin_sites"] == original["builtin_sites"]


@pytest.mark.parametrize("choices", [{}, {"node_key": "three"}, {"node_keys": ["four", "three"]}])
def test_batch_set_route_clears_explicit_default_and_obsolete_strategy(choices):
    original = _preferences()
    result, _ = bulk_ui.apply_route_batch(original, _catalog(), ["google", "youtube"], bulk_ui.SET_ROUTE,
                                          profile_id="dc", **choices)
    for service in ("google", "youtube"):
        assert service not in result["service_route_modes"]
        assert result["service_profile_bindings"][service] == "dc"
        assert result["service_node_bindings"].get(service, "") == choices.get("node_key", "")
        assert result["service_node_pools"].get(service, []) == choices.get("node_keys", [])
    assert result["builtin_sites"] == original["builtin_sites"]


@pytest.mark.parametrize("choices", [
    {"profile_id": "missing"}, {"profile_id": "dc", "node_key": "missing"},
    {"profile_id": "dc", "node_key": "three", "node_keys": ["four"]},
    {"profile_id": "dc", "node_keys": ["three", "missing"]},
    {"profile_id": "dc", "node_keys": ["three", "three"]},
    {"profile_id": "dc", "node_keys": [f"n-{index}" for index in range(17)]},
])
def test_invalid_batch_is_atomic(choices):
    original = _preferences()
    before = copy.deepcopy(original)
    with pytest.raises(ValueError):
        bulk_ui.apply_route_batch(original, _catalog(), ["claude", "youtube"], bulk_ui.SET_ROUTE, **choices)
    assert original == before


@pytest.mark.parametrize("services,operation", [([], bulk_ui.FOLLOW), (["claude", "invalid"], bulk_ui.FOLLOW),
                                               (["claude"], "invalid")])
def test_invalid_targets_or_operation_reject_entire_batch(services, operation):
    original = _preferences()
    before = copy.deepcopy(original)
    with pytest.raises(ValueError):
        bulk_ui.apply_route_batch(original, _catalog(), services, operation)
    assert original == before


def test_auto_batch_requires_usable_primary_but_explicit_pool_can_choose_safe_nodes():
    catalog = _catalog()
    catalog[1]["auto_route_usable"] = False
    original = _preferences()
    with pytest.raises(ValueError, match="独立运行"):
        bulk_ui.apply_route_batch(original, catalog, ["youtube"], bulk_ui.SET_ROUTE, profile_id="dc")
    result, _ = bulk_ui.apply_route_batch(original, catalog, ["youtube"], bulk_ui.SET_ROUTE,
                                          profile_id="dc", node_keys=["four"])
    assert result["service_node_pools"]["youtube"] == ["four"]


def test_tagged_batch_reassigns_only_chosen_and_keeps_disabled_status():
    catalog = _catalog()
    catalog[0]["network_type"], catalog[1]["network_type"] = "residential", "datacenter"
    original = _preferences()
    result, _ = bulk_ui.apply_route_batch(original, catalog, ["youtube", "google"], bulk_ui.TAGGED)
    assert result["service_profile_bindings"]["youtube"] == "dc"
    assert result["service_profile_bindings"]["google"] == "dc"
    assert "youtube" not in result["service_node_pools"]
    assert "google" not in result["service_route_modes"]
    assert result["builtin_sites"] == original["builtin_sites"]
    assert result["service_node_bindings"]["claude"] == "one"
    assert result["service_node_pools"]["openai"] == ["two", "one"]
    assert "google_ai" not in result["service_profile_bindings"]


@pytest.mark.parametrize("unavailable", ["no-label", "no-cache", "ambiguous"])
def test_failed_tagged_recommendation_keeps_existing_pinned_pool(unavailable):
    catalog = _catalog()
    if unavailable != "no-label":
        catalog[1]["network_type"] = "datacenter"
    if unavailable == "no-cache":
        catalog[1]["nodes"] = []
    if unavailable == "ambiguous":
        catalog.append({**copy.deepcopy(catalog[1]), "id": "extra"})
    original = _preferences()
    result, notices = bulk_ui.apply_route_batch(original, catalog, ["youtube"], bulk_ui.TAGGED)
    assert result == original
    assert notices


def test_enable_disable_only_changes_selected_switches():
    original = _preferences()
    result, notices = bulk_ui.apply_route_batch(original, _catalog(), ["claude", "youtube", "custom:demo"], bulk_ui.ENABLE)
    assert result["builtin_sites"]["youtube"] is True
    assert result["custom_targets"][0]["enabled"] is True
    for field in ("service_profile_bindings", "service_node_bindings", "service_node_pools", "service_route_modes"):
        assert result[field] == original[field]
    assert notices
    result, notices = bulk_ui.apply_route_batch(result, _catalog(), ["claude", "google"], bulk_ui.DISABLE)
    assert result["builtin_sites"]["google"] is False
    assert result["builtin_sites"]["youtube"] is True
    assert next(row for row in proxy_routing.route_rows(result) if row["id"] == "claude")["enabled"] is True
    assert notices


def test_pool_order_and_missing_candidates_are_visible_in_overview_and_preview():
    original = _preferences()
    catalog = _catalog()
    row = next(row for row in proxy_routing.route_rows(original) if row["id"] == "openai")
    old = route_description(row, original, catalog)
    assert "家宽二 → 家宽一" in old["node"]
    changed = copy.deepcopy(original)
    changed["service_node_pools"]["openai"].reverse()
    changes = route_changes({"Win": original}, {"Win": changed}, catalog)
    assert [change["service"] for change in changes] == ["openai"]
    catalog[0]["nodes"] = [item for item in catalog[0]["nodes"] if item["key"] != "two"]
    partial = route_description(row, original, catalog)
    assert partial["warning"] and "缺失 1 / 2" in partial["hint"]
    catalog[0]["nodes"] = [{"key": "shared", "label": "无关候选"}]
    missing = route_description(row, original, catalog)
    assert missing["warning"] and "全部失效" in missing["hint"]
    assert "不会扩大" in missing["hint"]


def _wait(root, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    root.update()
    assert predicate(), "synthetic route editor did not load"


@pytest.fixture
def editor(tk_root):
    applied = []
    dialog = ServiceRoutesDialog(
        tk_root, scopes=["Win 合成本机", "SSH 合成远端"],
        load_preferences=lambda _scope: _preferences(), catalog_loader=_catalog,
        apply_preferences=lambda *args: applied.append(args) or "仅合成回调",
    )
    _wait(tk_root, lambda: not dialog._busy)
    try:
        yield SimpleNamespace(dialog=dialog, root=tk_root, applied=applied)
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        tk_root.update()


def test_editor_bulk_is_current_scope_draft_only_and_preview_lists_selected_targets(editor):
    dialog = editor.dialog
    before = copy.deepcopy(dialog._drafts)
    first, second = dialog._scopes
    dialog._accept_bulk_edit(first, ["claude", "youtube"], bulk_ui.SET_ROUTE,
                              profile_id="dc", node_keys=["four", "three"])
    assert dialog._drafts[second] == before[second]
    assert dialog._originals == before and not editor.applied
    assert dialog._drafts[first]["builtin_sites"] == before[first]["builtin_sites"]
    assert {change["service"] for change in dialog._changes} == {"claude"}
    assert {change["scope"] for change in dialog._changes} == {first}
    assert "自选 2" in dialog._rows["claude"]["node"].get()


def test_editor_bulk_stale_scope_and_invalid_pool_do_not_modify_either_scope(editor):
    dialog = editor.dialog
    first, second = dialog._scopes
    dialog._switch_scope(second)
    before = copy.deepcopy(dialog._drafts)
    with pytest.raises(ValueError, match="位置"):
        dialog._accept_bulk_edit(first, ["claude"], bulk_ui.FOLLOW)
    with pytest.raises(ValueError):
        dialog._accept_bulk_edit(second, ["claude", "youtube"], bulk_ui.SET_ROUTE,
                                  profile_id="dc", node_keys=["missing"])
    assert dialog._drafts == before and not editor.applied


def test_editor_pool_callback_only_updates_intended_binding_and_keeps_order(editor):
    dialog = editor.dialog
    first, second = dialog._scopes
    before = copy.deepcopy(dialog._drafts)
    keys = ["two", "one"]
    dialog._accept_node_pool(first, "claude", "home", keys)
    keys.append("shared")
    assert dialog._drafts[first]["service_node_pools"]["claude"] == ["two", "one"]
    assert "claude" not in dialog._drafts[first]["service_node_bindings"]
    assert dialog._drafts[second] == before[second]
    assert "claude" in dialog._manually_edited[first]
    assert not editor.applied


def test_editor_nested_picker_loads_and_commits_ordered_pool_without_applying(editor):
    dialog = editor.dialog
    dialog._open_node_picker("openai")
    picker = dialog._node_dialog
    assert picker._selected_keys == ["two", "one"]
    picker._pool_list.selection_set(1)
    picker._move_pool(-1)
    assert dialog._drafts[dialog._scope]["service_node_pools"]["openai"] == ["two", "one"]
    picker._commit()
    assert dialog._drafts[dialog._scope]["service_node_pools"]["openai"] == ["one", "two"]
    assert editor.root.grab_current() is dialog
    assert not editor.applied


@pytest.mark.parametrize("stale", ["scope", "profile", "nodes", "busy"])
def test_editor_pool_callback_rejects_stale_context(editor, stale):
    dialog = editor.dialog
    first, second = dialog._scopes
    if stale == "scope":
        dialog._switch_scope(second)
    elif stale == "profile":
        dialog._select_profile("claude", "合成非家宽")
    elif stale == "nodes":
        dialog._catalog[0]["nodes"] = [{"key": "one", "label": "家宽一"}]
    elif stale == "busy":
        dialog._busy = True
    before = copy.deepcopy(dialog._drafts)
    dialog._accept_node_pool(first, "claude", "home", ["two", "one"])
    assert dialog._drafts == before and not editor.applied
    dialog._busy = False


@pytest.fixture
def bulk(tk_root):
    applied = []
    dialog = bulk_ui.RouteBulkDialog(
        tk_root, rows=proxy_routing.route_rows(_preferences()), catalog=_catalog(),
        on_apply=lambda *args, **kwargs: applied.append((args, kwargs)),
    )
    tk_root.update()
    try:
        yield SimpleNamespace(dialog=dialog, root=tk_root, applied=applied)
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        tk_root.update()


def _select_bulk_profile(dialog, profile_id):
    label = next(label for label, key in dialog._profiles.items() if key == profile_id)
    dialog._profile.set(label)
    dialog._select_profile(label)


def test_bulk_ui_starts_without_targets_and_groups_select_only_expected_targets(bulk):
    dialog = bulk.dialog
    assert not any(var.get() for var in dialog._vars.values())
    assert dialog._apply_button.cget("state") == "disabled"
    dialog._select_group("ai")
    assert {key for key, var in dialog._vars.items() if var.get()} == {"openai", "claude", "google_ai"}
    dialog._select_group("sites")
    assert not dialog._vars["custom:demo"].get()
    assert not dialog._vars["custom"].get()
    assert dialog._vars["youtube"].get()
    dialog._select_group("none")
    dialog._commit()
    assert not bulk.applied and dialog.winfo_exists()


def test_bulk_ui_pool_selection_is_draft_and_subscription_change_clears_strategy(bulk):
    dialog = bulk.dialog
    _select_bulk_profile(dialog, "home")
    dialog._open_nodes()
    picker = dialog._node_dialog
    picker._on_select_pool(["two", "one"])
    assert dialog._node_keys == ["two", "one"]
    assert not bulk.applied
    picker.destroy()
    _select_bulk_profile(dialog, "dc")
    assert dialog._node_keys == [] and dialog._node_key == ""
    dialog._vars["youtube"].set(True)
    dialog._changed()
    dialog._commit()
    assert bulk.applied == [((["youtube"], bulk_ui.SET_ROUTE), {"profile_id": "dc", "node_key": "", "node_keys": []})]


def test_bulk_pool_picker_callback_cannot_overwrite_new_subscription_even_shared_key(bulk):
    dialog = bulk.dialog
    _select_bulk_profile(dialog, "home")
    dialog._open_nodes()
    old_picker = dialog._node_dialog
    _select_bulk_profile(dialog, "dc")
    old_picker._on_select_pool(["shared"])
    assert dialog._node_keys == [] and dialog._node_key == ""
    assert not bulk.applied


def test_bulk_node_picker_callback_rejects_deleted_candidates(bulk):
    dialog = bulk.dialog
    _select_bulk_profile(dialog, "home")
    dialog._open_nodes()
    picker = dialog._node_dialog
    dialog._catalog[0]["nodes"] = [{"key": "shared", "label": "唯一剩余节点"}]
    picker._on_select_pool(["one", "two"])
    assert dialog._node_keys == []
    picker._on_select("one")
    assert dialog._node_key == ""
    assert "已变化" in dialog._status.cget("text")


def test_bulk_validation_failure_keeps_window_and_selected_targets_for_correction(bulk):
    dialog = bulk.dialog
    original = _preferences()

    def validate(services, operation, **choices):
        return bulk_ui.apply_route_batch(original, _catalog(), services, operation, **choices)

    dialog._on_apply = validate
    dialog._vars["youtube"].set(True)
    dialog._changed()
    dialog._commit()
    assert dialog.winfo_exists() and dialog._vars["youtube"].get()
    assert "可用缓存" in dialog._status.cget("text")
    assert original == _preferences()


def test_bulk_cancel_does_not_apply_selection(bulk):
    dialog = bulk.dialog
    dialog._select_group("all")
    _select_bulk_profile(dialog, "home")
    dialog.destroy()
    assert not bulk.applied


def capture_preview(directory):
    """Capture synthetic batch choices without opening the app or persisting."""
    import customtkinter as ctk
    from PIL import ImageGrab

    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.withdraw()
    catalog = _catalog()
    catalog[0]["network_type"], catalog[1]["network_type"] = "residential", "datacenter"
    dialog = bulk_ui.RouteBulkDialog(
        root, rows=proxy_routing.route_rows(_preferences()), catalog=catalog,
        on_apply=lambda *_args, **_kwargs: None,
    )
    try:
        dialog._select_group("ai")
        _select_bulk_profile(dialog, "home")
        dialog._choose_pool(["two", "one"], profile_id="home")
        for name, geometry in (("wide", "760x700"), ("narrow", "560x560")):
            dialog.geometry(geometry)
            dialog.lift()
            for _ in range(15):
                root.update()
                time.sleep(0.03)
            ImageGrab.grab(bbox=(
                dialog.winfo_rootx(), dialog.winfo_rooty(),
                dialog.winfo_rootx() + dialog.winfo_width(), dialog.winfo_rooty() + dialog.winfo_height(),
            )).save(destination / f"bulk-{name}.png")
    finally:
        dialog.destroy()
        root.destroy()


if __name__ == "__main__":
    import sys
    capture_preview(sys.argv[1])
