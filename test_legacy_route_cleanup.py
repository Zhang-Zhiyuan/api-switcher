"""Legacy social cleanup is an explicit, conservative and pure draft change."""
import copy

import pytest

from core.subscription_routing_policy import cleanup_legacy_social_routes, legacy_social_route_candidates


def _catalog():
    return [
        {"id": "home", "network_type": "residential", "nodes": [{"key": "home-one"}]},
        {"id": "dc", "network_type": "datacenter", "nodes": [{"key": "dc-one"}], "auto_route_usable": True},
    ]


def _preferences():
    return {"service_profile_bindings": {"x_twitter": "home", "reddit": "home", "claude": "home", "custom:kept": "home"},
            "builtin_sites": {"x_twitter": True, "reddit": True},
            "custom_targets": [{"id": "kept", "value": "example.test"}], "unknown_future_setting": ["keep"]}


def test_unique_datacenter_cleanup_changes_only_candidate_bindings_and_preserves_inputs():
    preferences, catalog = _preferences(), _catalog()
    before = copy.deepcopy((preferences, catalog))
    assert legacy_social_route_candidates(preferences, catalog) == ("x_twitter", "reddit")
    draft, notices = cleanup_legacy_social_routes(preferences, catalog)
    expected = copy.deepcopy(preferences)
    expected["service_profile_bindings"].update(x_twitter="dc", reddit="dc")
    assert draft == expected
    assert (preferences, catalog) == before
    assert "旧版未记录来源，可能是手动选择" in notices[0]
    assert "保存并应用后生效" in notices[-1]
    draft["custom_targets"][0]["value"] = "changed"
    draft["unknown_future_setting"].append("changed")
    assert preferences == before[0]


@pytest.mark.parametrize("service", ["x_twitter", "reddit"])
@pytest.mark.parametrize("strategy", ["pin", "pool", "default", "disabled", "protected", "empty-pin"])
def test_explicit_manual_strategies_and_disabled_sites_are_never_cleanup_candidates(service, strategy):
    preferences = _preferences()
    protected = ()
    if strategy in {"pin", "empty-pin"}:
        preferences["service_node_bindings"] = {service: "home-one" if strategy == "pin" else ""}
    elif strategy == "pool":
        preferences["service_node_pools"] = {service: ["home-one"]}
    elif strategy == "default":
        preferences["service_profile_bindings"].pop(service)
        preferences["service_route_modes"] = {service: "default"}
    elif strategy == "disabled":
        preferences["builtin_sites"][service] = False
    else:
        protected = service
    before = copy.deepcopy(preferences)
    assert service not in legacy_social_route_candidates(preferences, _catalog(), protected)
    draft, _ = cleanup_legacy_social_routes(preferences, _catalog(), protected)
    assert draft["service_profile_bindings"].get(service) == preferences["service_profile_bindings"].get(service)
    for field in ("service_node_bindings", "service_node_pools", "service_route_modes", "builtin_sites"):
        assert draft.get(field) == preferences.get(field)
    assert preferences == before


def test_detection_does_not_require_a_replacement_but_cleanup_preserves_original_when_none():
    preferences, catalog = _preferences(), _catalog()[:1]
    assert legacy_social_route_candidates(preferences, catalog) == ("x_twitter", "reddit")
    draft, notices = cleanup_legacy_social_routes(preferences, catalog)
    assert draft == preferences
    assert "没有可用的非家宽订阅" in notices[-1]


def test_multiple_eligible_datacenter_sources_do_not_guess():
    catalog = _catalog()
    catalog.append({**catalog[1], "id": "dc-two"})
    preferences = _preferences()
    draft, notices = cleanup_legacy_social_routes(preferences, catalog)
    assert draft == preferences
    assert "2 个可用非家宽订阅" in notices[-1]


@pytest.mark.parametrize("updates", [
    {"nodes": []}, {"nodes": None}, {"auto_route_usable": False},
    {"auto_route_usable": "true"}, {"auto_route_candidate_count": 0},
    {"id": ""}, {"id": []}, {"id": " dc "}, {"network_type": "unknown"},
])
def test_unusable_or_malformed_replacement_is_not_selected(updates):
    catalog = _catalog()
    catalog[1].update(updates)
    preferences = _preferences()
    draft, _ = cleanup_legacy_social_routes(preferences, catalog)
    assert draft == preferences


def test_existing_eligibility_rule_rejects_missing_primary_without_catalog_override():
    catalog = _catalog()
    catalog[1].pop("auto_route_usable")
    catalog[1]["selected_node_key"] = "missing"
    preferences = _preferences()
    draft, _ = cleanup_legacy_social_routes(preferences, catalog)
    assert draft == preferences
    catalog[1]["auto_route_usable"] = True
    draft, _ = cleanup_legacy_social_routes(preferences, catalog)
    assert draft["service_profile_bindings"]["reddit"] == "dc"


@pytest.mark.parametrize("tag", ["unknown", "datacenter", "", None, "住宅家宽"])
def test_detection_requires_current_explicit_residential_tag_not_names(tag):
    catalog = _catalog()
    catalog[0].update(network_type=tag, name="旧家宽 residential")
    preferences = _preferences()
    assert legacy_social_route_candidates(preferences, catalog) == ()
    assert cleanup_legacy_social_routes(preferences, catalog)[0] == preferences


@pytest.mark.parametrize("field", ["service_profile_bindings", "service_node_bindings", "service_node_pools", "builtin_sites", "service_route_modes"])
@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_malformed_authority_is_preserved_without_partial_cleanup(field, value):
    preferences = _preferences()
    preferences[field] = value
    before = copy.deepcopy(preferences)
    assert legacy_social_route_candidates(preferences, _catalog()) == ()
    draft, notices = cleanup_legacy_social_routes(preferences, _catalog())
    assert draft == preferences == before
    assert "格式无效" in notices[-1]


@pytest.mark.parametrize("field,value", [
    ("service_profile_bindings", {"x_twitter": "home", "reddit": ["home"]}),
    ("service_node_bindings", {"reddit": {"key": "home-one"}}),
    ("service_node_pools", {"reddit": []}),
    ("service_node_pools", {"reddit": ["home-one", "home-one"]}),
    ("service_route_modes", {"reddit": "invalid"}),
    ("service_route_modes", {"reddit": "default"}),
    ("builtin_sites", {"reddit": "false"}),
])
def test_invalid_nested_authority_is_not_silently_repaired(field, value):
    preferences = _preferences()
    preferences[field] = value
    assert legacy_social_route_candidates(preferences, _catalog()) == ()
    assert cleanup_legacy_social_routes(preferences, _catalog())[0] == preferences


def test_duplicate_catalog_rows_are_one_source_but_conflicting_tags_are_not_guessed():
    catalog = _catalog()
    catalog.extend(copy.deepcopy(catalog))
    assert cleanup_legacy_social_routes(_preferences(), catalog)[0]["service_profile_bindings"]["reddit"] == "dc"
    catalog.append({**catalog[0], "network_type": "datacenter"})
    assert legacy_social_route_candidates(_preferences(), catalog) == ()


def test_cleanup_is_idempotent_and_does_not_enable_legacy_unspecified_site_flags():
    preferences = {"service_profile_bindings": {"reddit": "home"}}
    first, _ = cleanup_legacy_social_routes(preferences, _catalog())
    second, _ = cleanup_legacy_social_routes(first, _catalog())
    assert first == second == {"service_profile_bindings": {"reddit": "dc"}}
    assert legacy_social_route_candidates(second, _catalog()) == ()


@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_non_object_preferences_raise_clear_error(value):
    for helper in (legacy_social_route_candidates, cleanup_legacy_social_routes):
        with pytest.raises(ValueError, match="草稿必须是对象"):
            helper(value, _catalog())
