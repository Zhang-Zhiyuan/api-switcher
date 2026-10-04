"""Offline AI recommendations must not convert blocked nodes into manual pins."""
from __future__ import annotations

import copy
import socket

import pytest

from core.local_proxy_constants import LOCAL_PROXY_AI_SERVICES
from core.route_presets import plan_route_preset


AI_IDS = tuple(item["id"] for item in LOCAL_PROXY_AI_SERVICES)


def _preferences():
    return {
        "builtin_sites": {}, "service_profile_bindings": {}, "service_node_bindings": {},
        "service_node_pools": {}, "service_route_modes": {}, "custom_targets": [],
    }


def _node(key, label, *, selectable=True, region="美国"):
    return {"key": key, "label": label, "ai_auto_selectable": selectable, "region": region}


def _catalog():
    return [
        {
            "id": "home", "name": "合成家宽", "network_type": "residential",
            "auto_route_usable": True, "selected_node_key": "hk", "nodes": [
                _node("hk", "香港家宽", selectable=False, region="香港"),
                _node("us", "美国家宽"),
            ],
        },
        {
            "id": "dc", "name": "合成机房", "network_type": "datacenter",
            "auto_route_usable": True, "selected_node_key": "dc-hk", "nodes": [
                _node("dc-hk", "香港机房", selectable=False, region="香港"),
                _node("dc-jp", "日本机房", region="日本"),
            ],
        },
    ]


def _decisions(plan):
    return {item["service"]: item for item in plan["decisions"]}


def _feedback(plan, service=None):
    reasons = [item.get("reason", "") for item in plan["decisions"]
               if service is None or item["service"] == service]
    return "\n".join([*plan.get("notices", []), *reasons])


def _assert_ai_unavailable(plan):
    decisions = _decisions(plan)
    for service in AI_IDS:
        assert decisions[service]["status"] == "unavailable"
        assert service not in plan["changed_services"]
        for field in ("service_profile_bindings", "service_node_bindings", "service_node_pools"):
            assert service not in plan["draft"][field]


@pytest.mark.parametrize("preset", ["balanced", "ai_only", "datacenter"])
def test_fixed_preset_skips_selected_hong_kong_node_without_mutating_sources(preset):
    sources, original = _catalog(), _preferences()
    before = copy.deepcopy((sources, original))
    plan = plan_route_preset(original, sources, preset, ai_strategy="fixed")
    expected_source, expected_node = ("dc", "dc-jp") if preset == "datacenter" else ("home", "us")
    for service in AI_IDS:
        assert plan["draft"]["service_profile_bindings"][service] == expected_source
        assert plan["draft"]["service_node_bindings"][service] == expected_node
        assert _decisions(plan)[service]["status"] == "changed"
    assert (sources, original) == before


@pytest.mark.parametrize("strategy", ["fixed", "auto"])
def test_all_hong_kong_home_nodes_leave_ai_unavailable_but_websites_usable(strategy):
    sources = _catalog()
    sources[0]["nodes"] = sources[0]["nodes"][:1]
    plan = plan_route_preset(_preferences(), sources, ai_strategy=strategy)
    _assert_ai_unavailable(plan)
    assert plan["draft"]["service_profile_bindings"]["youtube"] == "dc"
    assert plan["draft"]["service_profile_bindings"]["google"] == "dc"
    assert plan["draft"]["builtin_sites"]["youtube"] is True


@pytest.mark.parametrize("strategy", ["fixed", "auto"])
def test_all_hong_kong_datacenter_does_not_disable_ordinary_websites(strategy):
    sources = _catalog()[1:]
    sources[0]["nodes"] = sources[0]["nodes"][:1]
    plan = plan_route_preset(_preferences(), sources, "datacenter", ai_strategy=strategy)
    _assert_ai_unavailable(plan)
    for service in ("youtube", "google", "x_twitter"):
        assert plan["draft"]["service_profile_bindings"][service] == "dc"
        assert _decisions(plan)[service]["status"] == "changed"


@pytest.mark.parametrize("label", ["香港家宽", "HK 01", "Hong Kong residential", "🇭🇰 合成节点"])
def test_legacy_catalog_without_policy_metadata_still_excludes_named_hong_kong(label):
    sources = _catalog()
    sources[0]["nodes"] = [{"key": "hk", "label": label}, {"key": "us", "label": "US 01"}]
    plan = plan_route_preset(_preferences(), sources, ai_strategy="fixed")
    assert plan["draft"]["service_node_bindings"]["openai"] == "us"


@pytest.mark.parametrize("region", ["香港", "HK", "Hong Kong"])
def test_region_evidence_blocks_hong_kong_even_when_label_is_generic(region):
    sources = _catalog()
    sources[0]["nodes"][0] = {"key": "hk", "label": "高级节点 01", "region": region}
    plan = plan_route_preset(_preferences(), sources, ai_strategy="fixed")
    assert plan["draft"]["service_node_bindings"]["openai"] == "us"


def test_cached_rejection_is_not_overridden_by_a_non_hong_kong_label():
    sources = _catalog()
    sources[0]["nodes"][0] = _node("hk", "美国专线", selectable=False, region="美国")
    plan = plan_route_preset(_preferences(), sources, ai_strategy="fixed")
    assert plan["draft"]["service_node_bindings"]["openai"] == "us"


@pytest.mark.parametrize("invalid_key", ["", " bad ", "bad\nkey", "x" * 129, 123, None])
def test_replacement_candidate_with_invalid_key_is_never_turned_into_a_pin(invalid_key):
    sources = _catalog()
    sources[0]["nodes"].insert(1, _node(invalid_key, "美国无效标识"))
    plan = plan_route_preset(_preferences(), sources, ai_strategy="fixed")
    assert plan["draft"]["service_node_bindings"]["openai"] == "us"


def test_conflicting_candidate_keys_are_not_silently_pinned():
    sources = _catalog()
    sources[0]["nodes"] = [
        sources[0]["nodes"][0],
        _node("ambiguous", "美国候选"),
        _node("ambiguous", "香港候选", selectable=False, region="香港"),
        sources[0]["nodes"][1],
    ]
    plan = plan_route_preset(_preferences(), sources, ai_strategy="fixed")
    assert plan["draft"]["service_node_bindings"]["openai"] == "us"


def test_unknown_region_is_not_claimed_to_be_verified():
    sources = _catalog()
    sources[0]["nodes"] = [{"key": "unknown", "label": "高级节点 01"}]
    sources[0]["selected_node_key"] = "unknown"
    plan = plan_route_preset(_preferences(), sources, ai_strategy="fixed")
    assert plan["draft"]["service_node_bindings"]["openai"] == "unknown"
    feedback = _feedback(plan, "openai")
    assert any(word in feedback for word in ("地区", "国家", "出口"))
    assert any(word in feedback for word in ("未验证", "未核验", "无法确认", "无法保证", "不保证", "未知"))


@pytest.mark.parametrize("binding", ["pin", "pool"])
@pytest.mark.parametrize("strategy", ["fixed", "auto"])
def test_existing_manual_hong_kong_choices_are_preserved_with_visible_warning(binding, strategy):
    original = _preferences()
    original["service_profile_bindings"]["openai"] = "home"
    field = "service_node_bindings" if binding == "pin" else "service_node_pools"
    value = "hk" if binding == "pin" else ["us", "hk"]
    original[field]["openai"] = value
    before = copy.deepcopy(original)
    plan = plan_route_preset(original, _catalog(), ai_strategy=strategy)
    assert original == before
    assert plan["draft"][field]["openai"] == value
    assert plan["draft"]["service_profile_bindings"]["openai"] == "home"
    assert "openai" not in plan["changed_services"]
    assert _decisions(plan)["openai"]["status"] == "kept"
    feedback = _feedback(plan, "openai")
    assert "香港" in feedback or "HK" in feedback
    assert "保留" in feedback


@pytest.mark.parametrize("strategy", ["fixed", "auto"])
def test_explicit_hong_kong_only_source_never_silently_switches_subscription(strategy):
    sources = _catalog()
    alternative = copy.deepcopy(sources[0])
    alternative.update(id="safe-home", selected_node_key="us", nodes=[sources[0]["nodes"][1]])
    sources[0]["nodes"] = sources[0]["nodes"][:1]
    sources.append(alternative)
    plan = plan_route_preset(_preferences(), sources, sources={"residential": "home"}, ai_strategy=strategy)
    _assert_ai_unavailable(plan)
    assert "safe-home" not in plan["draft"]["service_profile_bindings"].values()


@pytest.mark.parametrize("strategy", ["fixed", "auto"])
def test_default_source_recommendation_prefers_ai_eligible_source(strategy):
    sources = _catalog()
    safe = copy.deepcopy(sources[0])
    safe.update(id="z-safe-home", selected_node_key="us", nodes=[sources[0]["nodes"][1]])
    sources[0]["nodes"] = sources[0]["nodes"][:1]
    sources[0]["auto_route_candidate_count"] = 3
    sources.append(safe)
    plan = plan_route_preset(_preferences(), sources, ai_strategy=strategy)
    for service in AI_IDS:
        assert plan["draft"]["service_profile_bindings"][service] == "z-safe-home"


def test_missing_saved_primary_is_not_silently_replaced_in_fixed_mode():
    sources = _catalog()
    sources[0]["selected_node_key"] = "disappeared"
    plan = plan_route_preset(_preferences(), sources, ai_strategy="fixed")
    _assert_ai_unavailable(plan)


def test_explicit_replanning_can_replace_old_hong_kong_pin_with_eligible_node():
    original = _preferences()
    original["service_profile_bindings"]["openai"] = "home"
    original["service_node_bindings"]["openai"] = "hk"
    plan = plan_route_preset(original, _catalog(), ai_strategy="fixed", replace_existing=True)
    assert plan["draft"]["service_node_bindings"]["openai"] == "us"
    assert "openai" in plan["changed_services"]


@pytest.mark.parametrize("strategy", ["fixed", "auto"])
def test_unavailable_replanning_keeps_old_manual_authority_atomic(strategy):
    original = _preferences()
    original["service_profile_bindings"]["openai"] = "old-source"
    original["service_node_bindings"]["openai"] = "old-node"
    sources = _catalog()
    sources[0]["nodes"] = sources[0]["nodes"][:1]
    plan = plan_route_preset(original, sources, ai_strategy=strategy, replace_existing=True)
    assert plan["draft"]["service_profile_bindings"]["openai"] == "old-source"
    assert plan["draft"]["service_node_bindings"]["openai"] == "old-node"
    assert _decisions(plan)["openai"]["status"] == "unavailable"


def test_planning_remains_offline_even_when_ai_region_is_unknown(monkeypatch):
    def forbidden_network(*_args, **_kwargs):
        pytest.fail("Planning must not connect to proxy nodes or AI services")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    sources = _catalog()
    sources[0]["nodes"][1] = {"key": "us", "label": "未验证节点"}
    before = copy.deepcopy(sources)
    plan = plan_route_preset(_preferences(), sources, ai_strategy="fixed")
    assert plan["draft"]["service_node_bindings"]["openai"] == "us"
    assert sources == before
