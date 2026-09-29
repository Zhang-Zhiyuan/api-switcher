"""Offline range safety: explicit pins, no geographic guesses, no live writes."""
import copy

import pytest

from core import proxy_routing
from core.route_presets import plan_route_preset
from test_route_presets import catalog, preferences


@pytest.mark.parametrize("preset", ["balanced", "ai_only", "datacenter"])
def test_fixed_ai_preset_pins_only_ai_to_known_primary(preset):
    sources, original = catalog(), preferences()
    before = copy.deepcopy((sources, original))
    plan = plan_route_preset(original, sources, preset, ai_strategy="fixed")
    draft = plan["draft"]
    assert draft["service_node_bindings"] == dict.fromkeys(
        ("openai", "claude", "google_ai"), "d1" if preset == "datacenter" else "h1")
    assert not draft["service_node_pools"]
    assert proxy_routing.normalize_routes(draft) == draft
    assert (sources, original) == before
    if preset == "ai_only":
        assert draft["service_route_modes"]["youtube"] == "direct"
    else:
        assert draft["service_profile_bindings"]["youtube"] == "dc"
    assert "固定" in plan["decisions"][0]["reason"]


@pytest.mark.parametrize("primary", ["deleted", " h1 ", [], False, "x" * 129, "h1\n"])
def test_fixed_preset_never_promotes_another_node_when_saved_primary_is_invalid(primary):
    sources, original = catalog(), preferences()
    sources[0]["selected_node_key"] = primary
    original["service_profile_bindings"]["claude"] = "old"
    original["service_node_bindings"]["claude"] = "old-key"
    plan = plan_route_preset(original, sources, ai_strategy="fixed", replace_existing=True)
    assert plan["draft"]["service_node_bindings"]["claude"] == "old-key"
    assert len(plan["changed_services"]) == 8
    assert all(item["status"] == "unavailable" for item in plan["decisions"][:3])
    assert not plan["draft"]["service_node_pools"]


@pytest.mark.parametrize("nodes", [[{"key": "h1"}, {"key": "h1"}], [None, {"key": "valid"}],
                                   [{"key": " "}, {"key": "valid"}]])
def test_ambiguous_or_invalid_first_node_is_not_silently_pinned(nodes):
    sources = catalog()
    sources[0].update(selected_node_key="", nodes=nodes)
    plan = plan_route_preset(preferences(), sources, ai_strategy="fixed")
    assert not plan["draft"]["service_node_bindings"]
    assert len(plan["changed_services"]) == 8


def test_new_subscription_primary_does_not_move_saved_fixed_ai_without_explicit_replanning():
    sources = catalog()
    original = plan_route_preset(preferences(), sources, ai_strategy="fixed")["draft"]
    sources[0]["selected_node_key"] = "h2"
    preserved = plan_route_preset(original, sources, ai_strategy="fixed")
    assert preserved["draft"] == original and not preserved["changed_services"]
    changed = plan_route_preset(original, sources, ai_strategy="fixed", replace_existing=True)
    assert changed["draft"]["service_node_bindings"]["claude"] == "h2"
    assert set(changed["changed_services"]) == {"openai", "claude", "google_ai"}


def test_fixed_ai_option_keeps_existing_automatic_and_custom_pool_choices():
    original = preferences()
    original["service_profile_bindings"].update(claude="dc", custom="home")
    original["service_node_pools"]["custom"] = ["h2", "h1"]
    plan = plan_route_preset(original, catalog(), ai_strategy="fixed")
    assert "claude" not in plan["draft"]["service_node_bindings"]
    assert plan["draft"]["service_profile_bindings"]["claude"] == "dc"
    assert plan["draft"]["service_node_pools"] == original["service_node_pools"]


@pytest.mark.parametrize("strategy", [None, [], 0, "same-country"])
def test_no_unsupported_country_lock_can_be_claimed_by_preset(strategy):
    with pytest.raises(ValueError, match="AI 节点策略"):
        plan_route_preset(preferences(), catalog(), ai_strategy=strategy)


@pytest.mark.parametrize("limited", ["pin", "pool"])
@pytest.mark.parametrize("destination", ["auto", "other-subscription", "default", "direct", "pin", "pool"])
def test_range_expansions_compare_final_authority(limited, destination):
    original = preferences()
    original["service_profile_bindings"]["claude"] = "home"
    field = "service_node_bindings" if limited == "pin" else "service_node_pools"
    original[field]["claude"] = "h1" if limited == "pin" else ["h1", "h2"]
    draft = copy.deepcopy(original)
    draft[field].clear()
    if destination in {"direct", "default"}:
        draft["service_profile_bindings"].clear()
        draft["service_route_modes"]["claude"] = destination
    elif destination == "pin":
        draft["service_node_bindings"]["claude"] = "h2"
    elif destination == "pool":
        draft["service_node_pools"]["claude"] = ["h2"]
    elif destination == "other-subscription":
        draft["service_profile_bindings"]["claude"] = "dc"
    snapshot = copy.deepcopy((original, draft))
    assert proxy_routing.automatic_scope_expansions(original, draft) == (
        ["claude"] if destination in {"auto", "other-subscription", "default"} else [])
    assert (original, draft) == snapshot


def test_inherited_custom_ranges_and_disabled_targets_are_handled():
    original = preferences()
    original["service_profile_bindings"].update(custom="home", youtube="home")
    original["service_node_pools"].update(custom=["h1"], youtube=["h1"])
    original["builtin_sites"]["youtube"] = False
    original["custom_targets"] = [
        {"id": "api", "kind": "domain", "value": "api.example.test", "enabled": True},
        {"id": "off", "kind": "domain", "value": "off.example.test", "enabled": False}]
    draft = copy.deepcopy(original)
    draft["service_node_pools"].clear()
    assert proxy_routing.automatic_scope_expansions(original, draft) == ["custom", "custom:api"]
    draft = copy.deepcopy(original)
    draft["service_profile_bindings"]["custom:api"] = "dc"
    assert proxy_routing.automatic_scope_expansions(original, draft) == ["custom:api"]


def test_reapplying_same_restricted_or_unrestricted_settings_needs_no_range_confirmation():
    auto = plan_route_preset(preferences(), catalog())["draft"]
    pinned = plan_route_preset(preferences(), catalog(), ai_strategy="fixed")["draft"]
    assert not proxy_routing.automatic_scope_expansions(auto, auto)
    assert not proxy_routing.automatic_scope_expansions(pinned, pinned)
    assert not proxy_routing.automatic_scope_expansions(auto, pinned)
    assert proxy_routing.automatic_scope_expansions(pinned, auto) == ["openai", "claude", "google_ai"]
