"""Single access choices have explicit, reversible semantics and no live effects."""
import copy
from pathlib import Path
import socket
import time

import customtkinter as ctk
import pytest

import test_service_routes_dialog as fixtures
from core import proxy_routing
from ui.dialogs import service_route_bulk_dialog as bulk
from ui.dialogs.service_routes_dialog import DEFAULT_CUSTOM_PROFILE, DEFAULT_PROFILE, DIRECT_PROFILE
from ui.route_labels import ORIGINAL_RULES, RESUME_ROUTE

editor = fixtures.editor


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Route choice tests must not access the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.mark.parametrize("strategy", ["fixed", "pool", "direct", "default"])
def test_original_rules_and_resume_preserve_exact_previous_choice(editor, strategy):
    root, dialog, saved = editor
    scope = dialog._scope
    draft = dialog._drafts[scope]
    if strategy == "pool":
        draft["service_node_bindings"].pop("youtube")
        draft["service_node_pools"]["youtube"] = ["four", "three"]
    elif strategy in {"direct", "default"}:
        dialog._select_profile("youtube", DIRECT_PROFILE if strategy == "direct" else DEFAULT_PROFILE)
    dialog._changed()
    before = copy.deepcopy(dialog._drafts)
    row = dialog._rows["youtube"]
    row["profile"].cget("command")(ORIGINAL_RULES)
    root.update()
    assert row["profile"].get() == ORIGINAL_RULES
    assert not row["enabled"].get() and not row["node"].winfo_manager()
    for field in ("service_profile_bindings", "service_node_bindings", "service_node_pools", "service_route_modes"):
        assert draft[field] == before[scope][field]
    row["profile"].cget("command")(RESUME_ROUTE)
    assert dialog._drafts == before and not saved


@pytest.mark.parametrize("choice", [DEFAULT_PROFILE, DIRECT_PROFILE, "机房订阅 B"])
def test_explicit_route_choices_always_enable_the_website(editor, choice):
    _root, dialog, saved = editor
    dialog._select_profile("youtube", ORIGINAL_RULES)
    dialog._select_profile("youtube", choice)
    assert dialog._rows["youtube"]["enabled"].get()
    assert dialog._rows["youtube"]["profile"].get() == choice
    assert not saved


def test_custom_inheritance_is_distinct_from_explicit_default_and_suspend(editor):
    _root, dialog, saved = editor
    dialog._custom_entry.insert(0, "example.test")
    dialog._add_custom()
    key = next(key for key in dialog._rows if key.startswith("custom:"))
    dialog._select_profile(key, ORIGINAL_RULES)
    dialog._select_profile(key, DEFAULT_CUSTOM_PROFILE)
    draft = dialog._drafts[dialog._scope]
    assert dialog._rows[key]["enabled"].get()
    assert key not in draft["service_route_modes"]
    dialog._select_profile(key, DEFAULT_PROFILE)
    assert draft["service_route_modes"][key] == "default"
    dialog._select_profile(key, ORIGINAL_RULES)
    assert draft["service_route_modes"][key] == "default"
    assert not saved


def test_always_on_targets_and_reserved_subscription_labels(editor):
    _root, dialog, saved = editor
    before = copy.deepcopy(dialog._drafts)
    for service in ("openai", "claude", "google_ai", "custom"):
        assert ORIGINAL_RULES not in dialog._rows[service]["profile"].cget("values")
        dialog._select_profile(service, ORIGINAL_RULES)
    assert dialog._drafts == before
    for name in (ORIGINAL_RULES, RESUME_ROUTE, DIRECT_PROFILE, DEFAULT_PROFILE):
        dialog._catalog = [{**item, "name": name if item["id"] == "dc" else item["name"]}
                           for item in dialog._catalog]
        choices = dialog._profile_values("youtube")
        label = next(label for label, value in choices.items() if value == "dc")
        assert label != name
        dialog._select_profile("youtube", label)
        assert dialog._drafts[dialog._scope]["service_profile_bindings"]["youtube"] == "dc"
    assert not saved


@pytest.mark.parametrize("operation", [bulk.SET_ROUTE, bulk.FOLLOW, bulk.DIRECT, bulk.TAGGED])
def test_batch_explicit_choices_enable_only_selected_sites_and_keep_input(operation):
    before = proxy_routing.normalize_routes({"builtin_sites": {"youtube": False, "google": False}})
    catalog = [{**item, "network_type": "datacenter" if item["id"] == "dc" else "residential"}
               for item in fixtures._catalog()]
    result, _ = bulk.apply_route_batch(before, catalog, ["youtube"], operation, profile_id="dc")
    assert result["builtin_sites"]["youtube"]
    assert not result["builtin_sites"]["google"]
    assert not before["builtin_sites"]["youtube"]


@pytest.mark.parametrize("scale", [1.0, 2.5])
def test_access_choices_visible_and_original_rules_have_no_node_control(editor, tk_root, scale):
    root, dialog, saved = editor
    from tools.ui_visual_audit import capture_window_image
    previous = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(scale)
        dialog.geometry("1040x760")
        root.update()
        dialog._select_profile("youtube", ORIGINAL_RULES)
        until = time.monotonic() + 0.25
        while time.monotonic() < until:
            root.update()
            time.sleep(0.01)
        assert not any(isinstance(child, ctk.CTkCheckBox) for row in dialog._rows.values()
                       for child in row["target"].winfo_children())
        assert not dialog._rows["youtube"]["node"].winfo_manager()
        assert dialog._save_button.winfo_viewable()
        assert dialog._save_button.winfo_rooty() + dialog._save_button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
        folder = Path(__file__).resolve().parent / "dist" / "proxy-simple-workflow-v2469"
        folder.mkdir(parents=True, exist_ok=True)
        capture_window_image(dialog).save(folder / f"routing-{int(scale * 100)}.png")
        assert not saved
    finally:
        ctk.set_widget_scaling(previous)
        root.update()


def test_batch_hides_irrelevant_node_controls_without_resetting_selection(tk_root):
    accepted = []
    dialog = bulk.RouteBulkDialog(tk_root, rows=proxy_routing.route_rows({}),
                                 catalog=fixtures._catalog(), on_apply=lambda *args, **kw: accepted.append((args, kw)))
    previous = ctk.ScalingTracker.widget_scaling
    try:
        dialog._select_profile("机房订阅 B")
        dialog._choose_node("three", profile_id="dc")
        for operation in (bulk.DIRECT, bulk.DISABLE, bulk.FOLLOW):
            dialog._operation.set(operation)
            dialog._operation_changed(operation)
            ctk.set_widget_scaling(1.25)
            tk_root.update()
            assert dialog._profile.winfo_viewable()
            assert dialog._profile.get() == operation
            assert not dialog._node_button.winfo_manager()
            assert dialog._node_key == "three"
        dialog._operation.set(bulk.SET_ROUTE)
        dialog._operation_changed(bulk.SET_ROUTE)
        tk_root.update()
        assert dialog._profile.winfo_viewable() and dialog._node_button.winfo_viewable()
        assert dialog._node_key == "three" and not accepted
    finally:
        dialog.destroy()
        ctk.set_widget_scaling(previous)
        tk_root.update()
