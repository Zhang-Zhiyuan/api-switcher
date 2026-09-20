"""Native UI regressions for tagged routing drafts and persistent opt-outs."""

import copy
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from core import proxy_routing
from ui.dialogs import service_routes_dialog as routes_ui


HOME_SERVICES = {"openai", "claude", "google_ai", "x_twitter", "reddit"}
DC_SERVICES = {"youtube", "google", "github", "huggingface", "discord", "telegram"}
EXPECTED = {**dict.fromkeys(HOME_SERVICES, "home"), **dict.fromkeys(DC_SERVICES, "dc")}


def _catalog():
    return [
        {"id": "home", "name": "合成家宽 A", "network_type": "residential",
         "nodes": [{"key": "home-one", "label": "合成家宽节点"}], "selected_node_key": "home-one"},
        {"id": "dc", "name": "合成机房 B", "network_type": "datacenter",
         "nodes": [{"key": "dc-one", "label": "合成机房节点"}], "selected_node_key": "dc-one"},
    ]


def _wait(root, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    root.update()
    assert predicate(), "routing draft worker did not finish"


@pytest.fixture
def route_harness(tk_root):
    dialogs = []

    def create(*, catalog=None, preferences=None, scopes=("Win11 合成本机", "SSH 合成服务器")):
        state = {scope: copy.deepcopy(preferences or {}) for scope in scopes}
        entries = copy.deepcopy(_catalog() if catalog is None else catalog)
        saved = []

        def apply(scope, draft, expected):
            assert expected == proxy_routing.route_snapshot(state[scope])
            saved.append((scope, copy.deepcopy(draft)))
            state[scope] = copy.deepcopy(draft)
            return "合成配置已应用；未执行网络或系统修改"

        def open_dialog():
            dialog = routes_ui.ServiceRoutesDialog(
                tk_root, scopes=scopes,
                load_preferences=lambda scope: copy.deepcopy(state[scope]),
                catalog_loader=lambda: copy.deepcopy(entries),
                apply_preferences=apply,
            )
            dialogs.append(dialog)
            _wait(tk_root, lambda: not dialog._busy)
            return dialog

        return SimpleNamespace(
            open=open_dialog, saved=saved, state=state, catalog=entries,
            scopes=scopes, root=tk_root,
        )

    yield create
    for dialog in reversed(dialogs):
        if dialog.winfo_exists():
            dialog.destroy()
        tk_root.update()


def test_tagged_defaults_fill_only_current_scope_and_remain_unapplied(route_harness):
    harness = route_harness()
    dialog = harness.open()
    first, second = harness.scopes
    assert dialog._drafts[first]["service_profile_bindings"] == EXPECTED
    assert dialog._drafts[second] == dialog._originals[second]
    assert not dialog._originals[first]["service_profile_bindings"]
    assert not harness.saved
    assert all(harness.state[scope] == {} for scope in harness.scopes)
    assert dialog._changes
    assert {change["scope"] for change in dialog._changes} == {first}


def test_first_scope_visit_fills_that_scope_without_touching_other_draft(route_harness):
    harness = route_harness()
    dialog = harness.open()
    first, second = harness.scopes
    dialog._select_profile("claude", routes_ui.DEFAULT_PROFILE)
    before = copy.deepcopy(dialog._drafts[first])
    dialog._switch_scope(second)
    assert dialog._drafts[second]["service_profile_bindings"] == EXPECTED
    assert dialog._drafts[first] == before
    dialog._switch_scope(first)
    assert dialog._drafts[first] == before
    assert not harness.saved


def test_auto_fill_preserves_saved_binding_pin_and_disabled_website(route_harness):
    harness = route_harness(preferences={
        "service_profile_bindings": {"claude": "dc"},
        "service_node_bindings": {"claude": "dc-one"},
        "builtin_sites": {"youtube": False},
    })
    dialog = harness.open()
    draft = dialog._drafts[dialog._scope]
    assert draft["service_profile_bindings"]["claude"] == "dc"
    assert draft["service_node_bindings"]["claude"] == "dc-one"
    assert "youtube" not in draft["service_profile_bindings"]
    assert draft["builtin_sites"]["youtube"] is False
    assert draft["service_profile_bindings"]["openai"] == "home"
    assert draft["service_profile_bindings"]["google"] == "dc"
    assert not harness.saved


def test_follow_default_opt_out_survives_apply_and_reopening(route_harness):
    harness = route_harness()
    dialog = harness.open()
    dialog._select_profile("claude", routes_ui.DEFAULT_PROFILE)
    draft = dialog._drafts[dialog._scope]
    assert "claude" not in draft["service_profile_bindings"]
    assert draft["service_route_modes"]["claude"] == "default"
    assert not harness.saved
    dialog._apply()
    _wait(harness.root, lambda: not dialog._busy)
    assert len(harness.saved) == 1
    dialog.destroy()
    harness.root.update()
    reopened = harness.open()
    restored = reopened._drafts[reopened._scope]
    assert restored["service_route_modes"]["claude"] == "default"
    assert "claude" not in restored["service_profile_bindings"]
    assert reopened._rows["claude"]["profile"].get() == routes_ui.DEFAULT_PROFILE
    assert restored == reopened._originals[reopened._scope]
    assert len(harness.saved) == 1


def test_choose_auto_again_only_reallocates_that_row(route_harness):
    harness = route_harness()
    dialog = harness.open()
    dialog._select_profile("openai", routes_ui.DEFAULT_PROFILE)
    dialog._select_profile("claude", routes_ui.DEFAULT_PROFILE)
    assert routes_ui.AUTO_PROFILE in dialog._rows["claude"]["profile"].cget("values")
    dialog._select_profile("claude", routes_ui.AUTO_PROFILE)
    draft = dialog._drafts[dialog._scope]
    assert draft["service_profile_bindings"]["claude"] == "home"
    assert draft.get("service_route_modes", {}).get("claude") != "default"
    assert "openai" not in draft["service_profile_bindings"]
    assert draft["service_route_modes"]["openai"] == "default"
    assert not harness.saved


def test_untagged_catalog_keeps_legacy_unassigned_behavior(route_harness):
    catalog = _catalog()
    for profile in catalog:
        profile["network_type"] = "unknown"
    harness = route_harness(catalog=catalog)
    dialog = harness.open()
    assert dialog._drafts == dialog._originals
    assert not dialog._drafts[dialog._scope]["service_profile_bindings"]
    assert not dialog._changes and not harness.saved


@pytest.mark.parametrize("unusable", ["no-cache", "ambiguous"])
def test_defaults_do_not_guess_without_unique_cached_subscription(route_harness, unusable):
    catalog = _catalog()
    if unusable == "no-cache":
        catalog[0]["nodes"] = []
    else:
        catalog.append({**copy.deepcopy(catalog[0]), "id": "second-home", "name": "另一合成家宽"})
    harness = route_harness(catalog=catalog)
    dialog = harness.open()
    bindings = dialog._drafts[dialog._scope]["service_profile_bindings"]
    assert not HOME_SERVICES.intersection(bindings)
    assert all(bindings[service] == "dc" for service in DC_SERVICES)
    assert not harness.saved


@pytest.mark.parametrize("reload_method", ["_reload_catalog", "_subscription_tags_saved"])
def test_reloading_tags_or_cache_fills_only_missing_unprotected_current_targets(route_harness, reload_method):
    catalog = _catalog()
    for entry in catalog:
        entry["network_type"] = "unknown"
    harness = route_harness(catalog=catalog)
    dialog = harness.open()
    dialog._select_profile("claude", routes_ui.DEFAULT_PROFILE)
    harness.catalog[0]["network_type"] = "residential"
    harness.catalog[1]["network_type"] = "datacenter"
    getattr(dialog, reload_method)()
    _wait(harness.root, lambda: not dialog._busy)
    bindings = dialog._drafts[dialog._scope]["service_profile_bindings"]
    assert bindings == {service: profile for service, profile in EXPECTED.items() if service != "claude"}
    assert dialog._drafts[dialog._scope]["service_route_modes"]["claude"] == "default"
    second = harness.scopes[1]
    assert dialog._drafts[second] == dialog._originals[second]
    assert not harness.saved


def test_catalog_refresh_reseeds_previously_visited_scope_only_when_revisited(route_harness):
    catalog = _catalog()
    for profile in catalog:
        profile["network_type"] = "unknown"
    harness = route_harness(catalog=catalog)
    dialog = harness.open()
    first, second = harness.scopes
    dialog._switch_scope(second)
    assert not dialog._drafts[second]["service_profile_bindings"]
    dialog._select_profile("google", routes_ui.DEFAULT_PROFILE)
    protected_second = copy.deepcopy(dialog._drafts[second])
    assert protected_second["service_route_modes"]["google"] == "default"
    dialog._switch_scope(first)
    harness.catalog[0]["network_type"] = "residential"
    harness.catalog[1]["network_type"] = "datacenter"
    dialog._reload_catalog()
    _wait(harness.root, lambda: not dialog._busy)
    assert dialog._drafts[first]["service_profile_bindings"] == EXPECTED
    assert dialog._drafts[second] == protected_second
    dialog._switch_scope(second)
    assert dialog._drafts[second]["service_profile_bindings"] == {
        service: profile for service, profile in EXPECTED.items() if service != "google"
    }
    assert dialog._drafts[second]["service_route_modes"]["google"] == "default"
    assert not harness.saved
    dialog._apply()
    _wait(harness.root, lambda: not dialog._busy)
    assert {scope for scope, _draft in harness.saved} == {first, second}
    assert harness.state[second]["service_route_modes"]["google"] == "default"
    assert "google" not in harness.state[second]["service_profile_bindings"]


def test_mode_only_change_is_visible_in_preview_and_saved(route_harness):
    harness = route_harness(catalog=[])
    dialog = harness.open()
    dialog._select_profile("claude", routes_ui.DEFAULT_PROFILE)
    assert not dialog._drafts[dialog._scope]["service_profile_bindings"]
    assert dialog._drafts[dialog._scope]["service_route_modes"]["claude"] == "default"
    assert dialog._changes, "manual follow-default must not be invisible just because both routes currently inherit"
    assert any("Claude" in change["label"] and change["before"] != change["after"] for change in dialog._changes)
    assert not harness.saved
    dialog._apply()
    _wait(harness.root, lambda: not dialog._busy)
    assert harness.saved[0][1]["service_route_modes"]["claude"] == "default"


@pytest.mark.parametrize("unavailable", ["no-tag", "ambiguous"])
def test_failed_auto_reallocation_preserves_existing_manual_subscription_and_pin(route_harness, unavailable):
    catalog = _catalog()
    if unavailable == "no-tag":
        catalog[0]["network_type"] = "unknown"
    else:
        catalog.append({**copy.deepcopy(catalog[0]), "id": "second-home", "name": "另一合成家宽"})
    harness = route_harness(catalog=catalog, preferences={
        "service_profile_bindings": {"claude": "dc"},
        "service_node_bindings": {"claude": "dc-one"},
    })
    dialog = harness.open()
    before = copy.deepcopy(dialog._drafts)
    combo = dialog._rows["claude"]["profile"]
    original_label = combo.get()
    # CTkComboBox sets its visible value before invoking its command.
    combo.set(routes_ui.AUTO_PROFILE)
    dialog._select_profile("claude", routes_ui.AUTO_PROFILE)
    assert dialog._drafts == before
    assert combo.get() == original_label
    assert "原选择已保留" in dialog._status.cget("text")
    assert not harness.saved


def test_subscription_named_like_auto_action_remains_separately_selectable(route_harness):
    catalog = _catalog()
    catalog[1]["name"] = routes_ui.AUTO_PROFILE
    catalog[1]["network_type"] = "unknown"
    harness = route_harness(catalog=catalog)
    dialog = harness.open()
    labels = dialog._profile_values("claude")
    manual_label = next(label for label, profile_id in labels.items() if profile_id == "dc")
    assert manual_label != routes_ui.AUTO_PROFILE
    assert manual_label in dialog._rows["claude"]["profile"].cget("values")
    dialog._select_profile("claude", manual_label)
    assert dialog._drafts[dialog._scope]["service_profile_bindings"]["claude"] == "dc"
    assert not harness.saved


def capture_preview(directory):
    """Capture only synthetic routing windows; never load real preferences."""
    import customtkinter as ctk
    from PIL import ImageGrab

    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    catalog = _catalog()
    for profile in catalog:
        profile["nodes"].append({"key": profile["id"] + "-backup", "label": "合成备用节点"})
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.withdraw()
    dialog = routes_ui.ServiceRoutesDialog(
        root, scopes=["Win11 合成本机", "SSH 合成服务器"],
        load_preferences=lambda _scope: {}, catalog_loader=lambda: copy.deepcopy(catalog),
        apply_preferences=lambda *_args: "隔离演示：未执行网络或系统修改",
    )
    try:
        _wait(root, lambda: not dialog._busy)
        assert dialog._drafts[dialog._scope]["service_profile_bindings"] == EXPECTED
        for label, geometry in (("wide", "1040x760"), ("narrow", "720x680")):
            dialog.geometry(geometry)
            dialog.lift()
            for _ in range(15):
                root.update()
                time.sleep(0.03)
            ImageGrab.grab(bbox=(
                dialog.winfo_rootx(), dialog.winfo_rooty(),
                dialog.winfo_rootx() + dialog.winfo_width(), dialog.winfo_rooty() + dialog.winfo_height(),
            )).save(destination / f"defaults-{label}.png")
    finally:
        dialog.destroy()
        root.destroy()


if __name__ == "__main__":
    import sys
    capture_preview(sys.argv[1])
