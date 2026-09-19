import copy

import pytest

from core.subscription_routing_policy import suggest_tagged_routes


def _catalog():
    return [
        {"id": "home", "name": "线路 A", "network_type": "residential",
         "nodes": [{"key": "home-one", "label": "节点一"}], "selected_node_key": "home-one"},
        {"id": "dc", "name": "线路 B", "network_type": "datacenter",
         "nodes": [{"key": "dc-one", "label": "节点二"}], "selected_node_key": "dc-one"},
    ]


def test_unique_labels_fill_ai_and_media_only_without_mutating_inputs():
    preferences = {"custom_targets": [{"id": "own", "value": "api.example.test"}],
                   "builtin_sites": {"github": False}, "other_setting": ["retained"]}
    catalog = _catalog()
    before = copy.deepcopy((preferences, catalog))
    draft, notices = suggest_tagged_routes(preferences, catalog)
    assert (preferences, catalog) == before
    assert draft["service_profile_bindings"] == {
        "openai": "home", "claude": "home", "google_ai": "home", "youtube": "dc", "google": "dc",
    }
    assert draft["builtin_sites"] == {"github": False, "youtube": True, "google": True}
    assert "service_node_bindings" not in draft
    assert len(notices) == 2
    assert all("草稿" in notice for notice in notices)
    draft["custom_targets"][0]["value"] = "changed"
    draft["other_setting"].append("changed")
    assert preferences == before[0]


def test_existing_invalid_disabled_and_empty_bindings_are_never_replaced():
    preferences = {
        "service_profile_bindings": {"openai": "deleted", "claude": "", "youtube": "home"},
        "service_node_bindings": {"google_ai": "orphan", "youtube": "pinned"},
        "builtin_sites": {"youtube": False},
    }
    draft, notices = suggest_tagged_routes(preferences, _catalog())
    assert draft["service_profile_bindings"] == {**preferences["service_profile_bindings"], "google": "dc"}
    assert draft["service_node_bindings"] == preferences["service_node_bindings"]
    assert draft["builtin_sites"] == {"youtube": False, "google": True}
    assert "已保留" in notices[-1]


def test_manual_follow_default_and_disabled_edits_are_protected():
    preferences = {"builtin_sites": {"youtube": False}}
    draft, _ = suggest_tagged_routes(preferences, _catalog(), protected_services=("claude", "youtube"))
    assert draft["service_profile_bindings"] == {"openai": "home", "google_ai": "home", "google": "dc"}
    assert draft["builtin_sites"]["youtube"] is False


def test_protected_string_is_one_service_not_characters():
    draft, _ = suggest_tagged_routes({}, _catalog(), protected_services="claude")
    assert "claude" not in draft["service_profile_bindings"]


def test_multiple_eligible_sources_do_not_guess_or_fall_back_across_labels():
    catalog = _catalog()
    catalog.append({**catalog[0], "id": "another-home"})
    draft, notices = suggest_tagged_routes({}, catalog)
    assert draft["service_profile_bindings"] == {"youtube": "dc", "google": "dc"}
    assert "2 个可用家宽订阅" in notices[0]


def test_no_tagged_source_does_not_infer_labels_from_names():
    catalog = _catalog()
    catalog[0].update(network_type="unknown", name="住宅家宽 residential")
    catalog[1].update(network_type="", name="非家宽机房 datacenter")
    draft, notices = suggest_tagged_routes({}, catalog)
    assert draft == {}
    assert len(notices) == 2
    assert all("请先设置订阅标记" in notice for notice in notices)


@pytest.mark.parametrize("updates,reason", [
    ({"nodes": []}, "无可用节点缓存"),
    ({"nodes": [{"key": ""}, None]}, "无可用节点缓存"),
    ({"selected_node_key": "missing"}, "首选节点不在可用节点列表"),
    ({"selected_node_key": {"unexpected": True}}, "首选节点不在可用节点列表"),
    ({"auto_route_usable": False}, "无可独立使用的首选节点"),
    ({"auto_route_usable": "false"}, "无可独立使用的首选节点"),
    ({"id": ""}, "订阅标识无效"),
])
def test_unusable_source_leaves_targets_unchanged_and_explains_reason(updates, reason):
    catalog = _catalog()
    catalog[0].update(updates)
    draft, notices = suggest_tagged_routes({}, catalog)
    assert draft["service_profile_bindings"] == {"youtube": "dc", "google": "dc"}
    assert reason in notices[0]


def test_catalog_safe_primary_check_takes_precedence_over_stale_selected_key():
    catalog = _catalog()
    catalog[0].update(selected_node_key="removed-old-node", auto_route_usable=True)
    draft, _ = suggest_tagged_routes({}, catalog)
    assert draft["service_profile_bindings"]["openai"] == "home"


def test_unset_preferred_node_remains_compatible_with_legacy_catalog():
    catalog = _catalog()
    del catalog[0]["selected_node_key"]
    draft, _ = suggest_tagged_routes({}, catalog)
    assert draft["service_profile_bindings"]["openai"] == "home"


def test_one_unusable_and_one_usable_source_is_unambiguous():
    catalog = _catalog()
    catalog.append({**catalog[0], "id": "broken-home", "auto_route_usable": False})
    draft, _ = suggest_tagged_routes({}, catalog)
    assert draft["service_profile_bindings"]["openai"] == "home"


def test_repeated_catalog_row_is_not_an_extra_subscription():
    catalog = _catalog()
    catalog.append(copy.deepcopy(catalog[0]))
    draft, _ = suggest_tagged_routes({}, catalog)
    assert draft["service_profile_bindings"]["openai"] == "home"


def test_suggestions_are_idempotent_and_do_not_enable_existing_disabled_site():
    first, _ = suggest_tagged_routes({}, _catalog())
    first["builtin_sites"]["youtube"] = False
    second, notices = suggest_tagged_routes(first, [])
    assert second == first
    assert len(notices) == 1
    assert "已保留" in notices[0]


@pytest.mark.parametrize("key", ["service_profile_bindings", "service_node_bindings", "builtin_sites"])
def test_malformed_authority_is_preserved_instead_of_repaired(key):
    preferences = {key: ["invalid"]}
    draft, notices = suggest_tagged_routes(preferences, _catalog())
    assert draft == preferences
    assert "格式无效" in notices[0]


def test_non_object_preferences_raise_clear_error():
    with pytest.raises(ValueError, match="草稿必须是对象"):
        suggest_tagged_routes([], _catalog())
