import copy
from pathlib import Path
import time

import customtkinter as ctk
import pytest

from core import proxy_routing
from ui.dialogs.service_routes_dialog import DEFAULT_CUSTOM_PROFILE, DEFAULT_PROFILE, MISSING_NODE, ServiceRoutesDialog


def _catalog():
    return [
        {"id": "home", "name": "家宽订阅 A", "nodes": [
            {"key": "one", "label": "日本 · 家宽 01"}, {"key": "two", "label": "美国 · 家宽 02"},
        ]},
        {"id": "dc", "name": "机房订阅 B", "nodes": [{"key": "three", "label": "香港 · 流媒体 01"}]},
    ]


def _preferences():
    return proxy_routing.route_snapshot({
        "builtin_sites": {"youtube": True},
        "service_profile_bindings": {"openai": "home", "claude": "home", "youtube": "dc"},
        "service_node_bindings": {"openai": "one", "claude": "two", "youtube": "three"},
    })


def _wait(root, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    root.update()
    assert predicate(), "editor worker did not finish"


@pytest.fixture
def editor(tk_root):
    root = tk_root
    saved = []
    dialog = ServiceRoutesDialog(
        root, scopes=["SSH 开发服务器", "SSH 推理服务器"],
        load_preferences=lambda _scope: _preferences(),
        catalog_loader=_catalog,
        apply_preferences=lambda scope, prefs, expected: saved.append((scope, copy.deepcopy(prefs), expected)) or "已应用",
    )
    try:
        _wait(root, lambda: not dialog._busy)
        yield root, dialog, saved
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        # Let native Windows focus/grab teardown finish before the next
        # Toplevel is constructed (a real mainloop also processes these events).
        root.update()


def test_editor_changes_are_drafts_until_explicit_apply(editor):
    root, dialog, saved = editor
    assert dialog._scope_combo.get() == dialog._scope
    assert dialog._rows["google_ai"]["node"].get() == DEFAULT_PROFILE
    dialog._select_node("claude", "日本 · 家宽 01")
    assert saved == []
    assert dialog._originals[dialog._scope]["service_node_bindings"]["claude"] == "two"
    dialog._apply()
    _wait(root, lambda: not dialog._busy)
    assert len(saved) == 1
    assert saved[0][1]["service_node_bindings"]["claude"] == "one"
    assert dialog._originals[dialog._scope] == dialog._drafts[dialog._scope]


def test_tagged_routes_only_fill_current_scope_and_preserve_pinned_bindings(editor):
    _root, dialog, saved = editor
    first, second = dialog._scopes
    dialog._catalog[0]["network_type"] = "residential"
    dialog._catalog[1]["network_type"] = "datacenter"
    before = copy.deepcopy(dialog._drafts)
    dialog._suggest_tagged_routes()
    draft = dialog._drafts[first]
    assert draft["service_profile_bindings"]["google_ai"] == "home"
    assert draft["service_profile_bindings"]["google"] == "dc"
    assert draft["builtin_sites"]["google"] is True
    assert draft["service_node_bindings"] == before[first]["service_node_bindings"]
    assert dialog._drafts[second] == before[second]
    assert dialog._originals == before and not saved
    assert dialog._preview_open
    assert "非家宽" in dialog._rows["google"]["profile"].get()


def test_tagged_routes_do_not_undo_explicit_default_choice_even_if_unchanged(editor):
    _root, dialog, saved = editor
    dialog._select_profile("claude", DEFAULT_PROFILE)
    dialog._select_profile("google_ai", DEFAULT_PROFILE)
    dialog._catalog[0]["network_type"] = "residential"
    dialog._catalog[1]["network_type"] = "datacenter"
    dialog._suggest_tagged_routes()
    bindings = dialog._drafts[dialog._scope]["service_profile_bindings"]
    assert "claude" not in bindings and "google_ai" not in bindings
    assert bindings["google"] == "dc"
    assert not saved
    dialog._reset()
    assert not dialog._manually_edited[dialog._scope]


def test_scope_copy_preserves_source_default_protection_and_replaces_old_target_protection(editor):
    _root, dialog, saved = editor
    first, second = dialog._scopes
    dialog._select_profile("google_ai", DEFAULT_PROFILE)
    dialog._switch_scope(second)
    dialog._select_profile("google", DEFAULT_PROFILE)
    dialog._switch_scope(first)
    dialog._copy_to_scopes([second])
    assert dialog._manually_edited[second] == {"google_ai"}
    assert dialog._manually_edited[second] is not dialog._manually_edited[first]
    dialog._switch_scope(second)
    dialog._catalog[0]["network_type"] = "residential"
    dialog._catalog[1]["network_type"] = "datacenter"
    dialog._suggest_tagged_routes()
    assert "google_ai" not in dialog._drafts[second]["service_profile_bindings"]
    assert dialog._drafts[second]["service_profile_bindings"]["google"] == "dc"
    assert not saved


def test_subscription_tag_editor_reload_keeps_routing_drafts(editor, monkeypatch):
    from ui.dialogs import subscription_tags_dialog
    root, dialog, saved = editor
    callbacks = []
    tag_updates = []
    dialog._on_tags_saved = lambda: tag_updates.append(True)
    class TagsWindow:
        def __init__(self, master, *, catalog, on_saved):
            assert master is dialog and catalog is dialog._catalog
            callbacks.append(on_saved)
        def winfo_exists(self):
            return False
    monkeypatch.setattr(subscription_tags_dialog, "SubscriptionTagsDialog", TagsWindow)
    dialog._select_profile("claude", DEFAULT_PROFILE)
    before = copy.deepcopy(dialog._drafts)
    dialog._open_subscription_tags()
    callbacks[0]()
    _wait(root, lambda: not dialog._busy)
    assert dialog._drafts == before and not saved
    assert tag_updates == [True]


def test_editor_reselecting_profile_preserves_pinned_node(editor):
    _root, dialog, _saved = editor
    dialog._select_profile("claude", "家宽订阅 A")
    assert dialog._drafts[dialog._scope]["service_node_bindings"]["claude"] == "two"
    dialog._select_profile("claude", "机房订阅 B")
    assert "claude" not in dialog._drafts[dialog._scope]["service_node_bindings"]


def test_server_switch_preserves_drafts_and_copy_is_independent(editor):
    _root, dialog, _saved = editor
    first, second = dialog._scopes
    dialog._select_profile("claude", DEFAULT_PROFILE)
    dialog._switch_scope(second)
    assert dialog._drafts[second]["service_profile_bindings"]["claude"] == "home"
    dialog._switch_scope(first)
    dialog._copy_to_scopes(dialog._scopes)
    assert "claude" not in dialog._drafts[second]["service_profile_bindings"]
    dialog._drafts[first]["service_profile_bindings"]["claude"] = "dc"
    assert "claude" not in dialog._drafts[second]["service_profile_bindings"]


def test_missing_node_is_shown_and_not_replaced_by_default(editor):
    _root, dialog, _saved = editor
    dialog._drafts[dialog._scope]["service_node_bindings"]["claude"] = "missing"
    dialog._refresh_row("claude")
    assert dialog._rows["claude"]["node"].get() == MISSING_NODE
    assert dialog._drafts[dialog._scope]["service_node_bindings"]["claude"] == "missing"


def test_failed_apply_preserves_draft_and_successful_servers_are_not_reapplied(editor):
    root, dialog, saved = editor
    first, second = dialog._scopes
    dialog._select_profile("claude", "机房订阅 B")
    dialog._copy_to_scopes(dialog._scopes)
    def apply(scope, preferences, _expected):
        if scope == second:
            raise RuntimeError("服务器暂时无法连接")
        saved.append(scope)
        return "已应用"
    dialog._applier = apply
    dialog._apply()
    _wait(root, lambda: not dialog._busy)
    assert saved == [first]
    assert dialog._originals[first] == dialog._drafts[first]
    assert dialog._originals[second] != dialog._drafts[second]
    dialog._apply()
    _wait(root, lambda: not dialog._busy)
    assert saved == [first]
    assert "服务器暂时无法连接" in dialog._details.get("1.0", "end")


def test_custom_target_add_remove_and_invalid_input_feedback(editor):
    _root, dialog, saved = editor
    dialog._custom_entry.insert(0, "https://api.example.com/v1")
    dialog._add_custom()
    target = dialog._drafts[dialog._scope]["custom_targets"][0]
    service = f"custom:{target['id']}"
    assert target["value"] == "api.example.com"
    dialog._select_profile(service, "家宽订阅 A")
    dialog._select_node(service, "日本 · 家宽 01")
    dialog._remove_custom(service)
    assert service not in dialog._drafts[dialog._scope]["service_node_bindings"]
    assert service not in dialog._drafts[dialog._scope]["service_profile_bindings"]
    dialog._custom_entry.insert(0, "bad,domain")
    dialog._add_custom()
    assert "每次只能添加" in dialog._status.cget("text")
    assert not saved


def test_custom_default_label_and_reset_preserve_inherited_subscription(editor):
    _root, dialog, _saved = editor
    dialog._select_profile("custom", "家宽订阅 A")
    dialog._custom_entry.insert(0, "api.example.com")
    dialog._add_custom()
    target = dialog._drafts[dialog._scope]["custom_targets"][0]
    service = f"custom:{target['id']}"
    assert dialog._rows[service]["profile"].get() == DEFAULT_CUSTOM_PROFILE
    assert dialog._rows[service]["node"].get() == DEFAULT_CUSTOM_PROFILE
    dialog._select_profile(service, "机房订阅 B")
    dialog._select_node(service, "香港 · 流媒体 01")
    dialog._select_profile(service, DEFAULT_CUSTOM_PROFILE)
    draft = dialog._drafts[dialog._scope]
    assert draft["service_profile_bindings"]["custom"] == "home"
    assert service not in draft["service_profile_bindings"]
    assert service not in draft["service_node_bindings"]
    assert dialog._rows[service]["profile"].get() == DEFAULT_CUSTOM_PROFILE


def test_filters_match_subscription_node_and_category_and_can_be_cleared(editor):
    _root, dialog, saved = editor
    dialog._search.set("家宽订阅 A")
    dialog._filter_rows()
    assert dialog._rows["claude"]["tile"].winfo_manager() == "pack"
    assert dialog._rows["youtube"]["tile"].winfo_manager() == ""
    dialog._search.set("香港 · 流媒体")
    dialog._filter_rows()
    assert dialog._rows["youtube"]["tile"].winfo_manager() == "pack"
    dialog._set_category("AI 服务")
    assert dialog._empty.winfo_manager() == "pack"
    dialog._clear_filters()
    assert all(row["tile"].winfo_manager() == "pack" for row in dialog._rows.values())
    assert dialog._empty.winfo_manager() == ""
    assert not saved


def test_dirty_and_disabled_badges_update_and_reset_without_applying(editor):
    _root, dialog, saved = editor
    assert dialog._reset_button.cget("state") == "disabled"
    dialog._toggle("youtube", False)
    assert "未启用" in dialog._rows["youtube"]["state_label"].cget("text")
    assert "未保存" in dialog._rows["youtube"]["state_label"].cget("text")
    assert dialog._rows["youtube"]["description"]["enabled"] is False
    assert dialog._reset_button.cget("state") == "normal"
    dialog._reset()
    assert "未保存" not in dialog._rows["youtube"]["state_label"].cget("text")
    assert dialog._rows["youtube"]["enabled"].get()
    assert dialog._reset_button.cget("state") == "disabled"
    assert not saved


def test_reloading_catalog_preserves_drafts_and_surfaces_missing_fixed_node(editor):
    root, dialog, saved = editor
    dialog._select_node("claude", "日本 · 家宽 01")
    draft = copy.deepcopy(dialog._drafts)
    catalog = _catalog()
    catalog[0]["nodes"] = [{"key": "two", "label": "美国 · 家宽 02"}]
    dialog._catalog_loader = lambda: catalog
    dialog._reload_catalog()
    assert dialog._rows["claude"]["profile"].cget("state") == "disabled"
    _wait(root, lambda: not dialog._busy)
    assert dialog._drafts == draft
    assert dialog._rows["claude"]["node"].get() == MISSING_NODE
    dialog._set_category("待修复")
    assert dialog._rows["claude"]["tile"].winfo_manager() == "pack"
    assert dialog._rows["youtube"]["tile"].winfo_manager() == ""
    assert not saved


def test_catalog_reload_failure_keeps_editor_usable_and_drafts_intact(editor):
    root, dialog, saved = editor
    dialog._select_profile("claude", "机房订阅 B")
    draft = copy.deepcopy(dialog._drafts)
    def fail():
        raise OSError("缓存读取异常")
    dialog._catalog_loader = fail
    dialog._reload_catalog()
    _wait(root, lambda: not dialog._busy)
    assert dialog._drafts == draft
    assert "已保留草稿" in dialog._status.cget("text")
    assert dialog._save_button.cget("state") == "normal"
    assert not saved


def test_duplicate_custom_target_is_located_even_when_other_category_selected(editor):
    _root, dialog, _saved = editor
    dialog._custom_entry.insert(0, "api.example.com")
    dialog._add_custom()
    dialog._set_category("AI 服务")
    dialog._custom_entry.insert(0, "https://api.example.com/v1")
    dialog._add_custom()
    assert len(dialog._drafts[dialog._scope]["custom_targets"]) == 1
    assert dialog._category == "自定义"
    assert "已为你定位" in dialog._status.cget("text")


def test_footer_reserves_space_and_actions_wrap_using_widget_scaling(editor):
    from types import SimpleNamespace
    _root, dialog, _saved = editor
    scale = dialog._actions._get_widget_scaling()
    assert dialog.pack_slaves()[0] is dialog._actions.master
    assert dialog._actions.master.pack_info()["side"] == "bottom"
    dialog._layout_actions(SimpleNamespace(width=560 * scale))
    assert dialog._save_button.grid_info()["row"] == 1
    assert dialog._save_button.grid_info()["column"] == 1
    dialog._layout_actions(SimpleNamespace(width=900 * scale))
    assert dialog._save_button.grid_info()["row"] == 0
    assert dialog._save_button.grid_info()["column"] == 3
    dialog._layout_scope_toolbar(SimpleNamespace(width=560 * scale))
    assert dialog._copy_button.grid_info()["row"] == 1
    dialog._layout_scope_toolbar(SimpleNamespace(width=900 * scale))
    assert dialog._copy_button.grid_info()["row"] == 0
    assert dialog._copy_button.grid_info()["column"] == 2


def test_catalog_reload_and_scope_switch_reuse_existing_rows(editor):
    root, dialog, _saved = editor
    rows = {key: row["tile"] for key, row in dialog._rows.items()}
    dialog._reload_catalog()
    _wait(root, lambda: not dialog._busy)
    assert rows == {key: row["tile"] for key, row in dialog._rows.items()}
    dialog._switch_scope(dialog._scopes[1])
    assert rows == {key: row["tile"] for key, row in dialog._rows.items()}


def test_node_search_choice_only_changes_draft_and_restores_editor_grab(editor):
    from ui.dialogs.route_selection_dialogs import FIXED_MODE
    root, dialog, saved = editor
    dialog._open_node_picker("claude")
    picker = dialog._node_dialog
    assert root.grab_current() is picker
    picker._search.insert(0, "日本 家宽")
    picker._filter()
    assert [node["key"] for node in picker._visible] == ["one"]
    picker._list.selection_set(0)
    picker._select_visible()
    assert picker._mode == FIXED_MODE
    assert dialog._drafts[dialog._scope]["service_node_bindings"]["claude"] == "two"
    picker._commit()
    assert root.grab_current() is dialog
    assert dialog._drafts[dialog._scope]["service_node_bindings"]["claude"] == "one"
    assert not saved


def test_node_picker_cannot_overwrite_changed_subscription(editor):
    _root, dialog, saved = editor
    dialog._select_profile("claude", "机房订阅 B")
    before = copy.deepcopy(dialog._drafts)
    dialog._accept_node_choice(dialog._scope, "claude", "home", "one")
    assert dialog._drafts == before
    assert "已经变化" in dialog._status.cget("text")
    assert not saved


def test_scope_copy_requires_explicit_targets_and_preserves_other_drafts(editor):
    _root, dialog, saved = editor
    first, second = dialog._scopes
    third = "SSH 暂不修改"
    dialog._scopes.append(third)
    dialog._drafts[third] = copy.deepcopy(dialog._drafts[first])
    dialog._originals[third] = copy.deepcopy(dialog._originals[first])
    unchanged = copy.deepcopy(dialog._drafts[third])
    dialog._select_profile("claude", "机房订阅 B")
    dialog._open_copy_dialog()
    selector = dialog._scope_copy_dialog
    assert not any(var.get() for var in selector._vars.values())
    selector._commit()
    assert dialog._drafts[second] == unchanged
    selector._vars[second].set(True)
    selector._changed()
    selector._commit()
    assert dialog._drafts[second]["service_profile_bindings"]["claude"] == "dc"
    assert dialog._drafts[third] == unchanged
    assert dialog._preview_open
    preview = dialog._preview.get("1.0", "end")
    assert first in preview and second in preview and third not in preview
    assert not saved


def test_change_preview_shows_removed_target_and_subscription_strategy_reset(editor):
    _root, dialog, saved = editor
    dialog._select_profile("claude", "机房订阅 B")
    assert "原固定节点" in dialog._status.cget("text")
    dialog._toggle_preview()
    text = dialog._preview.get("1.0", "end")
    assert "家宽订阅 A" in text and "机房订阅 B" in text
    assert "美国 · 家宽 02" in text and "订阅首选 + 故障切换" in text
    dialog._reset()
    assert not dialog._changes
    assert not saved


def test_custom_form_is_collapsed_until_opened_without_losing_input(editor):
    _root, dialog, _saved = editor
    assert dialog._custom_form.winfo_manager() == ""
    dialog._toggle_custom_form()
    dialog._custom_entry.insert(0, "api.example.com")
    dialog._toggle_custom_form()
    dialog._toggle_custom_form()
    assert dialog._custom_entry.get() == "api.example.com"


def capture_preview(directory):
    """Isolated visual verification; no real preferences or SSH connections."""
    from PIL import ImageGrab

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.withdraw()
    dialog = ServiceRoutesDialog(
        root, scopes=["SSH 开发服务器", "SSH 推理服务器"],
        load_preferences=lambda _scope: _preferences(), catalog_loader=_catalog,
        apply_preferences=lambda *_args: "隔离演示：规则已应用",
    )
    try:
        _wait(root, lambda: not dialog._busy)
        for label, geometry in (("wide", "1040x760"), ("narrow", "720x680")):
            dialog.geometry(geometry)
            for _ in range(15):
                root.update()
                time.sleep(0.03)
            ImageGrab.grab(bbox=(dialog.winfo_rootx(), dialog.winfo_rooty(),
                                dialog.winfo_rootx() + dialog.winfo_width(),
                                dialog.winfo_rooty() + dialog.winfo_height())).save(directory / f"routes-{label}.png")
    finally:
        dialog.destroy()
        root.destroy()


if __name__ == "__main__":
    import sys
    capture_preview(sys.argv[1])
