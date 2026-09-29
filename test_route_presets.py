"""Offline preset policies: no IP inference, network calls or live writes."""
import copy

import pytest

from core.route_presets import plan_route_preset, preset_sources
from core.subscription_routing_policy import preferred_network_type


def catalog():
    return [
        {"id": "home", "name": "合成家宽 A", "network_type": "residential", "auto_route_usable": True,
         "selected_node_key": "h1", "nodes": [{"key": "h1", "label": "节点一"}, {"key": "h2", "label": "节点二"}]},
        {"id": "dc", "name": "合成非家宽 B", "network_type": "datacenter", "auto_route_usable": True,
         "nodes": [{"key": "d1", "label": "节点三"}, {"key": "d2", "label": "节点四"}]},
    ]


def preferences():
    return {"builtin_sites": {}, "service_profile_bindings": {}, "service_node_bindings": {},
            "service_node_pools": {}, "service_route_modes": {}, "custom_targets": []}


def test_balanced_plan_fills_known_targets_without_mutating_inputs():
    original, sources = preferences(), catalog()
    before = copy.deepcopy((original, sources))
    plan = plan_route_preset(original, sources)
    assert (original, sources) == before
    assert len(plan["changed_services"]) == 11
    for service in plan["changed_services"]:
        expected = "home" if preferred_network_type(service) == "residential" else "dc"
        assert plan["draft"]["service_profile_bindings"][service] == expected
    assert len(plan["draft"]["builtin_sites"]) == 8
    assert all(plan["draft"]["builtin_sites"].values())
    assert not plan["draft"]["service_node_bindings"]
    assert not plan["draft"]["service_route_modes"]
    assert all("故障切换" in item["reason"] for item in plan["decisions"])
    assert any("未实时测速" in text for text in plan["notices"])
    plan["draft"]["custom_targets"].append("independent")
    assert original == before[0]


def test_existing_pin_pool_direct_default_disabled_and_custom_authority_win():
    original = preferences()
    original.update(service_profile_bindings={"openai": "dc", "claude": "home", "youtube": "home", "custom": "dc"},
                    service_node_bindings={"openai": "d1", "custom": "d2"},
                    service_node_pools={"claude": ["h2", "h1"]},
                    service_route_modes={"google_ai": "default", "google": "direct"},
                    builtin_sites={"youtube": False},
                    custom_targets=[{"id": "keep", "kind": "domain", "value": "example.test", "enabled": True}])
    plan = plan_route_preset(original, catalog(), protected_services={"reddit"})
    draft = plan["draft"]
    for field in ("service_node_bindings", "service_node_pools", "service_route_modes", "custom_targets"):
        assert draft[field] == original[field]
    for service, profile in original["service_profile_bindings"].items():
        assert draft["service_profile_bindings"][service] == profile
    assert draft["builtin_sites"]["youtube"] is False
    assert "reddit" not in plan["changed_services"]
    assert "reddit" not in draft["service_profile_bindings"]


@pytest.mark.parametrize("preset", ["balanced", "ai_only", "datacenter"])
def test_replanning_never_touches_disabled_or_custom_targets(preset):
    original = preferences()
    original.update(service_profile_bindings={"youtube": "deleted", "custom": "home", "custom:x": "dc"},
                    service_node_bindings={"youtube": "stale", "custom:x": "d1"},
                    builtin_sites={"youtube": False},
                    custom_targets=[{"id": "x", "kind": "domain", "value": "example.test", "enabled": True}])
    draft = plan_route_preset(original, catalog(), preset, replace_existing=True)["draft"]
    assert draft["custom_targets"] == original["custom_targets"]
    assert draft["builtin_sites"]["youtube"] is False
    for service in ("youtube", "custom", "custom:x"):
        for field in ("service_profile_bindings", "service_node_bindings"):
            assert draft[field].get(service) == original[field].get(service)


def test_ai_only_direct_is_explicit_clears_old_node_authority_and_is_idempotent():
    original = preferences()
    original.update(service_profile_bindings={"google": "dc", "youtube": "home"},
                    service_node_bindings={"google": "d1"}, service_node_pools={"youtube": ["h2", "h1"]})
    plan = plan_route_preset(original, catalog(), "ai_only", replace_existing=True)
    draft = plan["draft"]
    assert set(draft["service_route_modes"].values()) == {"direct"}
    assert set(draft["service_profile_bindings"].values()) == {"home"}
    assert len(draft["service_route_modes"]) == 8 and len(draft["service_profile_bindings"]) == 3
    assert not draft["service_node_bindings"] and not draft["service_node_pools"]
    again = plan_route_preset(draft, catalog(), "ai_only", replace_existing=True)
    assert again["draft"] == draft and not again["changed_services"]


def test_datacenter_preset_does_not_require_residential_source():
    plan = plan_route_preset(preferences(), catalog()[1:], "datacenter")
    assert len(plan["changed_services"]) == 11
    assert set(plan["draft"]["service_profile_bindings"].values()) == {"dc"}
    assert set(plan["sources"]) == {"datacenter"}


def test_replacement_removes_old_explicit_default_and_pool_but_preservation_does_not():
    original = preferences()
    original.update(service_profile_bindings={"claude": "home"}, service_node_pools={"claude": ["h1"]},
                    service_route_modes={"openai": "default", "google_ai": "direct"})
    plan = plan_route_preset(original, catalog(), "datacenter", replace_existing=True)
    assert not plan["draft"]["service_node_pools"] and not plan["draft"]["service_route_modes"]
    assert set(plan["draft"]["service_profile_bindings"].values()) == {"dc"}


@pytest.mark.parametrize("unavailable", ["unlabelled", "empty", "chained", "duplicate-id", "invalid-id"])
def test_unavailable_family_keeps_previous_route_without_cross_label_fallback(unavailable):
    sources = catalog()
    if unavailable == "unlabelled":
        sources[0]["network_type"] = "unknown"  # The name still contains 家宽.
    elif unavailable == "empty":
        sources[0]["nodes"] = []
    elif unavailable == "chained":
        sources[0]["auto_route_usable"] = False
    elif unavailable == "duplicate-id":
        sources.append(copy.deepcopy(sources[0]))
    else:
        sources[0]["id"] = " home "
    original = preferences()
    original.update(service_profile_bindings={"claude": "old"}, service_node_bindings={"claude": "old-node"})
    plan = plan_route_preset(original, sources, replace_existing=True)
    assert plan["draft"]["service_profile_bindings"]["claude"] == "old"
    assert plan["draft"]["service_node_bindings"]["claude"] == "old-node"
    assert "openai" not in plan["draft"]["service_profile_bindings"]
    assert len(plan["changed_services"]) == 8
    assert not plan["draft"]["service_route_modes"]
    assert sum(item["status"] == "unavailable" for item in plan["decisions"]) == 3


def test_multiple_sources_use_existing_family_then_standby_then_stable_id():
    sources = catalog()
    other = copy.deepcopy(sources[0])
    other.update(id="a-home", name="另一订阅", nodes=[{"key": "x", "label": "唯一节点"}])
    sources.append(other)
    assert plan_route_preset(preferences(), sources)["sources"]["residential"]["id"] == "home"
    original = preferences()
    original["service_profile_bindings"]["claude"] = "a-home"
    assert plan_route_preset(original, sources)["sources"]["residential"]["id"] == "a-home"
    other["nodes"].append({"key": "y", "label": "备用节点"})
    assert plan_route_preset(preferences(), sources)["sources"]["residential"]["id"] == "a-home"
    sources.reverse()
    other["name"] = "改名不影响选择"
    assert plan_route_preset(preferences(), sources)["sources"]["residential"]["id"] == "a-home"
    explicit = plan_route_preset(preferences(), sources, sources={"residential": "home"})
    assert explicit["sources"]["residential"]["id"] == "home"
    assert "指定" in explicit["sources"]["residential"]["reason"]


def test_connection_count_not_alias_count_controls_standby_recommendation():
    sources = catalog()
    sources[0]["auto_route_candidate_count"] = 1
    other = copy.deepcopy(sources[0])
    other.update(id="z-home", auto_route_candidate_count=2)
    sources.append(other)
    assert plan_route_preset(preferences(), sources)["sources"]["residential"]["id"] == "z-home"
    plan = plan_route_preset(preferences(), sources, sources={"residential": "home"})
    assert "暂无备用" in plan["decisions"][0]["reason"]


def test_datacenter_preset_counts_existing_ai_bindings_when_choosing_a_source():
    sources = catalog()
    other = copy.deepcopy(sources[1])
    other["id"] = "used-for-ai"
    sources.append(other)
    original = preferences()
    original["service_profile_bindings"]["claude"] = "used-for-ai"
    plan = plan_route_preset(original, sources, "datacenter")
    assert plan["sources"]["datacenter"]["id"] == "used-for-ai"


def test_empty_catalog_does_not_infer_or_change_any_route():
    original = preferences()
    plan = plan_route_preset(original, [])
    assert plan["draft"] == original and not plan["changed_services"]
    assert all(item["status"] == "unavailable" for item in plan["decisions"])
    assert preset_sources(None, "residential") == []
    assert preset_sources(catalog(), "unknown") == []


@pytest.mark.parametrize("override", [{"residential": "missing"}, {"residential": "dc"},
                                      {"datacenter": "home"}, {"bad": "home"}, {"residential": 1}, []])
def test_invalid_explicit_source_is_atomic_not_silently_auto_corrected(override):
    original = preferences()
    before = copy.deepcopy(original)
    with pytest.raises(ValueError):
        plan_route_preset(original, catalog(), sources=override)
    assert original == before


@pytest.mark.parametrize("original", [None, [], {"service_node_pools": []}, {"builtin_sites": {"youtube": "false"}},
                                     {"service_node_bindings": {"claude": "orphan"}},
                                     {"service_profile_bindings": {"claude": "home"}, "service_route_modes": {"claude": "direct"}}])
def test_invalid_authority_never_repaired_as_a_side_effect(original):
    before = copy.deepcopy(original)
    with pytest.raises(ValueError, match="格式无效"):
        plan_route_preset(original, catalog(), replace_existing=True)
    assert original == before


def test_strict_privacy_blocks_direct_preset_without_changing_privacy():
    original = preferences()
    before = copy.deepcopy(original)
    with pytest.raises(ValueError, match="严格隐私"):
        plan_route_preset(original, catalog(), "ai_only", strict_privacy=True)
    assert original == before
    for preset in ("balanced", "datacenter"):
        plan = plan_route_preset(original, catalog(), preset, strict_privacy=True)
        assert not plan["draft"]["service_route_modes"]


@pytest.mark.parametrize("preset", ["unknown", [], None])
def test_invalid_preset_rejected(preset):
    with pytest.raises(ValueError, match="有效的分流预设"):
        plan_route_preset(preferences(), catalog(), preset)


@pytest.mark.parametrize("preset", ["balanced", "ai_only", "datacenter"])
def test_preset_output_matches_existing_save_and_privacy_contracts(preset):
    from core import proxy_routing
    from core.proxy_route_preview import route_preflight

    plan = plan_route_preset(preferences(), catalog(), preset)
    assert proxy_routing.normalize_routes(plan["draft"]) == plan["draft"]
    preflight = route_preflight(plan["draft"], strict_privacy=True)
    assert preflight["privacy_conflict"] is (preset == "ai_only")
