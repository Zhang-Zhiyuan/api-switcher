"""Legacy-default cleanup is opt-in and changes only the current draft."""

import copy

import pytest

from test_service_route_defaults_ui import DC_SERVICES, EXPECTED, _catalog, _wait
from test_service_route_defaults_ui import route_harness as route_harness
from ui.dialogs import service_routes_dialog as routes_ui


def _legacy_preferences():
    profiles = dict(EXPECTED)
    profiles.update(x_twitter="home", reddit="home")
    return {
        "service_profile_bindings": profiles,
        "builtin_sites": dict.fromkeys(DC_SERVICES, True),
    }


def test_open_detects_legacy_candidates_without_changing_or_applying_routes(route_harness):
    harness = route_harness(preferences=_legacy_preferences())
    saved_before = copy.deepcopy(harness.state)
    dialog = harness.open()
    assert dialog._drafts == dialog._originals
    assert dialog._legacy_cleanup_button.cget("state") == "normal"
    assert "2" in dialog._legacy_cleanup_button.cget("text")
    assert not dialog._changes and not harness.saved
    assert harness.state == saved_before


def test_cleanup_only_changes_current_scope_draft_and_previews_exact_targets(route_harness):
    harness = route_harness(preferences=_legacy_preferences())
    dialog = harness.open()
    first, second = harness.scopes
    before = copy.deepcopy(dialog._drafts)
    dialog._cleanup_legacy_routes()
    assert dialog._originals == before
    assert dialog._drafts[second] == before[second]
    expected = copy.deepcopy(before[first])
    expected["service_profile_bindings"].update(x_twitter="dc", reddit="dc")
    assert dialog._drafts[first] == expected
    assert {change["service"] for change in dialog._changes} == {"x_twitter", "reddit"}
    assert {change["scope"] for change in dialog._changes} == {first}
    assert dialog._preview_open
    assert dialog._legacy_cleanup_button.cget("state") == "disabled"
    assert not harness.saved


@pytest.mark.parametrize("protection", ["fixed", "pool", "disabled", "default"])
def test_cleanup_preserves_protected_social_target_and_other_routes(route_harness, protection):
    preferences = _legacy_preferences()
    if protection == "fixed":
        preferences["service_node_bindings"] = {"x_twitter": "home-one"}
    elif protection == "pool":
        preferences["service_node_pools"] = {"x_twitter": ["home-one"]}
    elif protection == "disabled":
        preferences["builtin_sites"]["x_twitter"] = False
    else:
        preferences["service_profile_bindings"].pop("x_twitter")
        preferences["service_route_modes"] = {"x_twitter": "default"}
    harness = route_harness(preferences=preferences)
    dialog = harness.open()
    before = copy.deepcopy(dialog._drafts[dialog._scope])
    assert "1" in dialog._legacy_cleanup_button.cget("text")
    dialog._cleanup_legacy_routes()
    expected = copy.deepcopy(before)
    expected["service_profile_bindings"]["reddit"] = "dc"
    assert dialog._drafts[dialog._scope] == expected
    assert not harness.saved


def test_current_session_manual_reselection_is_protected_even_without_visible_diff(route_harness):
    harness = route_harness(preferences=_legacy_preferences())
    dialog = harness.open()
    original_label = dialog._rows["x_twitter"]["profile"].get()
    dialog._select_profile("x_twitter", original_label)
    assert "x_twitter" in dialog._manually_edited[dialog._scope]
    assert "1" in dialog._legacy_cleanup_button.cget("text")
    dialog._cleanup_legacy_routes()
    bindings = dialog._drafts[dialog._scope]["service_profile_bindings"]
    assert bindings["x_twitter"] == "home" and bindings["reddit"] == "dc"
    assert not harness.saved


def test_reset_restores_saved_routes_and_detection_count(route_harness):
    harness = route_harness(preferences=_legacy_preferences())
    dialog = harness.open()
    dialog._cleanup_legacy_routes()
    dialog._reset()
    assert dialog._drafts == dialog._originals
    assert not dialog._changes
    assert dialog._legacy_cleanup_button.cget("state") == "normal"
    assert "2" in dialog._legacy_cleanup_button.cget("text")
    assert not harness.saved


def test_scope_change_recomputes_count_and_does_not_clean_another_scope(route_harness):
    harness = route_harness(preferences=_legacy_preferences())
    dialog = harness.open()
    first, second = harness.scopes
    dialog._cleanup_legacy_routes()
    dialog._switch_scope(second)
    assert "2" in dialog._legacy_cleanup_button.cget("text")
    assert dialog._legacy_cleanup_button.cget("state") == "normal"
    assert dialog._drafts[second] == dialog._originals[second]
    dialog._switch_scope(first)
    assert dialog._legacy_cleanup_button.cget("state") == "disabled"
    assert not harness.saved


@pytest.mark.parametrize("unavailable", ["unmarked", "empty", "ambiguous"])
def test_unavailable_destination_preserves_routes_and_explains_no_change(route_harness, unavailable):
    catalog = _catalog()
    if unavailable == "unmarked":
        catalog[1]["network_type"] = "unknown"
    elif unavailable == "empty":
        catalog[1]["nodes"] = []
    else:
        catalog.append({**copy.deepcopy(catalog[1]), "id": "second-dc", "name": "另一合成非家宽"})
    harness = route_harness(preferences=_legacy_preferences(), catalog=catalog)
    dialog = harness.open()
    before = copy.deepcopy(dialog._drafts)
    dialog._cleanup_legacy_routes()
    assert dialog._drafts == before and not harness.saved
    feedback = dialog._status.cget("text") + dialog._preview.get("1.0", "end")
    assert any(word in feedback for word in ("保留", "未分配", "无法", "未整理", "没有", "未修改"))


def test_cleanup_is_saved_only_by_existing_explicit_apply_and_reopens_without_candidates(route_harness):
    harness = route_harness(preferences=_legacy_preferences())
    dialog = harness.open()
    first, second = harness.scopes
    dialog._cleanup_legacy_routes()
    assert not harness.saved
    dialog._apply()
    _wait(harness.root, lambda: not dialog._busy)
    assert [scope for scope, _draft in harness.saved] == [first]
    assert harness.state[first]["service_profile_bindings"]["reddit"] == "dc"
    assert harness.state[second]["service_profile_bindings"]["reddit"] == "home"
    dialog.destroy()
    harness.root.update()
    reopened = harness.open()
    assert reopened._drafts == reopened._originals
    assert reopened._legacy_cleanup_button.cget("state") == "disabled"


def test_explicit_follow_default_choice_remains_out_of_cleanup(route_harness):
    harness = route_harness(preferences=_legacy_preferences())
    dialog = harness.open()
    dialog._select_profile("reddit", routes_ui.DEFAULT_PROFILE)
    dialog._cleanup_legacy_routes()
    draft = dialog._drafts[dialog._scope]
    assert draft["service_profile_bindings"]["x_twitter"] == "dc"
    assert "reddit" not in draft["service_profile_bindings"]
    assert draft["service_route_modes"]["reddit"] == "default"
    assert not harness.saved


def test_cleanup_action_and_save_remain_inside_narrow_window(route_harness):
    harness = route_harness(preferences=_legacy_preferences())
    dialog = harness.open()
    dialog.geometry("720x680")
    harness.root.update()
    assert dialog._tools_columns == 3
    buttons = (dialog._custom_toggle, dialog._tags_button, dialog._tag_routes_button,
               dialog._bulk_button, dialog._legacy_cleanup_button, dialog._preview_toggle)
    assert {int(button.grid_info()["row"]) for button in buttons} == {0, 1}
    for button in (*buttons, dialog._save_button):
        assert button.winfo_rootx() >= dialog.winfo_rootx()
        assert button.winfo_rootx() + button.winfo_width() <= dialog.winfo_rootx() + dialog.winfo_width()
        assert button.winfo_rooty() + button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
    dialog._cleanup_legacy_routes()
    harness.root.update()
    assert dialog._preview_open
    assert dialog._save_button.winfo_rooty() + dialog._save_button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()


def capture_preview(directory):
    """Inspect the legacy-cleanup controls using synthetic data only."""
    from pathlib import Path
    import time

    import customtkinter as ctk
    from PIL import ImageGrab

    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.withdraw()
    dialog = routes_ui.ServiceRoutesDialog(
        root, scopes=["Win11 合成本机", "SSH 合成服务器"],
        load_preferences=lambda _scope: _legacy_preferences(), catalog_loader=_catalog,
        apply_preferences=lambda *_args: "仅供隔离展示，未修改真实配置",
    )
    try:
        _wait(root, lambda: not dialog._busy)
        for name, geometry, cleanup in (("before-wide", "1040x760", False),
                                         ("before-narrow", "720x680", False),
                                         ("after-narrow", "720x680", True)):
            if cleanup:
                dialog._cleanup_legacy_routes()
            dialog._search.set("reddit")
            dialog.geometry(geometry)
            dialog.lift()
            for _ in range(15):
                root.update()
                time.sleep(0.03)
            ImageGrab.grab(bbox=(
                dialog.winfo_rootx(), dialog.winfo_rooty(),
                dialog.winfo_rootx() + dialog.winfo_width(), dialog.winfo_rooty() + dialog.winfo_height(),
            )).save(destination / f"legacy-cleanup-{name}.png")
    finally:
        dialog.destroy()
        root.destroy()


if __name__ == "__main__":
    import sys
    capture_preview(sys.argv[1])
