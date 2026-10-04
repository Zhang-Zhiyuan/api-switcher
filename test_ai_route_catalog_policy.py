"""Offline AI eligibility travels through the real sanitized subscription catalog."""
import copy
import json
from types import SimpleNamespace

import pytest

from core import proxy_routing, remote_proxy
from core.local_proxy_constants import LOCAL_PROXY_AI_SERVICE_IDS, LOCAL_PROXY_BUILTIN_SITE_IDS
from core.route_presets import plan_route_preset
from core.subscription_routing_policy import (
    ai_route_candidate_count, ai_route_candidate_nodes, suggest_tagged_routes,
)


def node(index, label, *, port=8080, **fields):
    return remote_proxy.ProxySubscriptionNode(
        index, {"name": label, "type": "http", "server": "hidden-endpoint.example.test",
                "port": port, "username": "hidden-user", "password": "hidden-password", **fields},
        source="https://hidden-source.example.test/sub?token=hidden-subscription-token",
    )


@pytest.fixture
def load_catalog(monkeypatch):
    original_qualities = remote_proxy.load_proxy_subscription_qualities

    def load(entries):
        profiles, cached = [], {}
        cache_reads, quality_reads = [], []
        for profile_id, network_type, nodes, qualities in entries:
            profiles.append({"id": profile_id, "name": "合成订阅 " + profile_id,
                             "url": "https://hidden-source.example.test/sub?token=hidden-subscription-token",
                             "network_type": network_type,
                             "selected_node_key": remote_proxy.proxy_subscription_node_key(nodes[0]) if nodes else "",
                             "node_qualities": qualities})
            cached[profile_id] = SimpleNamespace(nodes=nodes)

        def cached_read(profile):
            cache_reads.append(profile["id"])
            return cached[profile["id"]]

        def quality_read(profile):
            quality_reads.append(profile["id"])
            return original_qualities(profile)

        monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {})
        monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda *_: profiles)
        monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", cached_read)
        monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", quality_read)
        before = copy.deepcopy((profiles, entries))
        catalog = proxy_routing.load_route_catalog()
        assert (profiles, entries) == before
        assert cache_reads == quality_reads == [profile["id"] for profile in profiles]
        return catalog

    return load


def test_real_nodes_catalog_has_only_sanitized_ai_hints_and_no_connection_secrets(load_catalog):
    candidate = node(0, "美国节点")
    key = remote_proxy.proxy_subscription_node_key(candidate)
    qualities = {key: {"ok": True, "region": "美国", "host": "hidden-quality-host",
                       "ip": "192.0.2.199", "detail": "hidden-quality-detail"}}
    entry, = load_catalog([("home", "residential", [candidate], qualities)])
    assert set(entry["nodes"][0]) == {"key", "label", "region", "ai_auto_selectable"}
    assert entry["nodes"][0] == {"key": key, "label": "美国节点", "region": "美国", "ai_auto_selectable": True}
    assert entry["ai_auto_route_candidate_count"] == entry["auto_route_candidate_count"] == 1
    encoded = json.dumps(entry, ensure_ascii=False)
    assert "hidden-" not in encoded and "192.0.2.199" not in encoded
    assert not {"server", "password", "username", "source", "url", "node_qualities", "connection_keys"}.intersection(entry)


@pytest.mark.parametrize("strategy", ["fixed", "auto"])
def test_hong_kong_primary_stays_visible_but_ai_uses_only_us_candidate(load_catalog, strategy):
    hk, us = node(0, "香港首选", port=8080), node(1, "美国候选", port=8081)
    catalog = load_catalog([("home", "residential", [hk, us], {})])
    entry, = catalog
    hk_key, us_key = map(remote_proxy.proxy_subscription_node_key, (hk, us))
    assert entry["selected_node_key"] == hk_key
    assert len(entry["nodes"]) == 2  # Manual selection still sees Hong Kong.
    assert [row["ai_auto_selectable"] for row in entry["nodes"]] == [False, True]
    assert entry["auto_route_candidate_count"] == 2
    assert entry["ai_auto_route_candidate_count"] == ai_route_candidate_count(entry) == 1
    assert [row["key"] for row in ai_route_candidate_nodes(entry)] == [us_key]
    before = copy.deepcopy(catalog)
    plan = plan_route_preset({}, catalog, ai_strategy=strategy)
    assert catalog == before
    assert plan["sources"]["residential"]["ai_candidate_count"] == 1
    for service in LOCAL_PROXY_AI_SERVICE_IDS:
        assert plan["draft"]["service_profile_bindings"][service] == "home"
        if strategy == "fixed":
            assert plan["draft"]["service_node_bindings"][service] == us_key
        else:
            assert service not in plan["draft"].get("service_node_bindings", {})
            decision = next(item for item in plan["decisions"] if item["service"] == service)
            assert "暂无备用" in decision["reason"]


@pytest.mark.parametrize("region", ["香港", "HK", "Hong Kong"])
def test_cached_hong_kong_evidence_overrides_us_label_without_leaking_quality(load_catalog, region):
    candidate = node(0, "美国节点")
    qualities = {remote_proxy.proxy_subscription_node_key(candidate): {
        "ok": True, "region": region, "checked_at": "2000-01-01T00:00:00+00:00",
        "host": "hidden-quality-host", "detail": "hidden-quality-detail",
    }}
    entry, = load_catalog([("home", "residential", [candidate], qualities)])
    assert entry["nodes"][0]["label"] == "美国节点"
    assert entry["nodes"][0]["ai_auto_selectable"] is False
    assert entry["auto_route_candidate_count"] == 1
    assert entry["ai_auto_route_candidate_count"] == ai_route_candidate_count(entry) == 0
    assert not ai_route_candidate_nodes(entry)
    assert "hidden-" not in json.dumps(entry, ensure_ascii=False)


def test_all_hong_kong_subscription_is_not_seeded_for_ai_but_websites_are_unchanged(load_catalog):
    catalog = load_catalog([
        ("home", "residential", [node(0, "香港家宽")], {}),
        ("dc", "datacenter", [node(0, "香港机房", port=8081)], {}),
    ])
    original = {"custom_targets": [{"id": "keep", "kind": "domain", "value": "example.test"}]}
    before = copy.deepcopy((original, catalog))
    draft, notices = suggest_tagged_routes(original, catalog)
    assert (original, catalog) == before
    assert not LOCAL_PROXY_AI_SERVICE_IDS.intersection(draft["service_profile_bindings"])
    assert draft["service_profile_bindings"] == dict.fromkeys(LOCAL_PROXY_BUILTIN_SITE_IDS, "dc")
    assert all(draft["builtin_sites"][service] is True for service in LOCAL_PROXY_BUILTIN_SITE_IDS)
    assert draft["custom_targets"] == original["custom_targets"]
    assert any("无符合 AI 自动筛选" in notice for notice in notices)


def test_unknown_region_keeps_legacy_eligibility_without_claiming_verified_access(load_catalog):
    entry, = load_catalog([("home", "residential", [node(0, "未标地区节点")], {})])
    assert entry["nodes"][0]["region"] == "其他"
    assert entry["nodes"][0]["ai_auto_selectable"] is True
    assert ai_route_candidate_count(entry) == 1
    legacy = {"id": "home", "network_type": "residential",
              "nodes": [{"key": "legacy-unknown", "label": "未标地区节点"}]}
    assert ai_route_candidate_nodes(legacy) == legacy["nodes"]
    draft, _ = suggest_tagged_routes({}, [legacy])
    assert draft["service_profile_bindings"] == dict.fromkeys(LOCAL_PROXY_AI_SERVICE_IDS, "home")


@pytest.mark.parametrize("hint", [{"label": "香港候选"}, {"label": "HK candidate"},
                                  {"region": "Hong Kong"}, {"ai_auto_selectable": False}])
def test_legacy_or_mixed_catalog_never_ignores_an_explicit_hong_kong_or_policy_rejection(hint):
    row = {"key": "candidate", "label": "unknown", **hint}
    assert ai_route_candidate_nodes({"nodes": [row]}) == []


def test_identical_real_catalog_node_keys_are_deduplicated_for_ai_candidates(load_catalog):
    first = node(0, "美国候选")
    duplicate = remote_proxy.ProxySubscriptionNode(1, copy.deepcopy(first.node))
    entry, = load_catalog([("home", "residential", [first, duplicate], {})])
    assert len(entry["nodes"]) == 2
    assert len({row["key"] for row in entry["nodes"]}) == 1
    assert entry["auto_route_candidate_count"] == entry["ai_auto_route_candidate_count"] == 1
    assert len(ai_route_candidate_nodes(entry)) == ai_route_candidate_count(entry) == 1


@pytest.mark.parametrize("conflict", [{"region": "香港"}, {"label": "different"}, {"ai_auto_selectable": False}])
def test_duplicate_key_with_conflicting_metadata_is_not_an_automatic_candidate(conflict):
    first = {"key": "ambiguous", "label": "美国候选", "region": "美国", "ai_auto_selectable": True}
    profile = {"nodes": [first, {**first, **conflict}], "ai_auto_route_candidate_count": 1}
    assert not ai_route_candidate_nodes(profile)
    assert ai_route_candidate_count(profile) == 0


def test_alias_connections_do_not_advertise_nonexistent_ai_backup(load_catalog):
    first = node(0, "美国候选")
    alias = node(1, "美国同线路别名", port="8080")
    entry, = load_catalog([("home", "residential", [first, alias], {})])
    assert len(entry["nodes"]) == 2
    assert len(ai_route_candidate_nodes(entry)) == 2
    assert entry["auto_route_candidate_count"] == entry["ai_auto_route_candidate_count"] == 1
    assert ai_route_candidate_count(entry) == 1


def test_tagged_ai_suggestion_does_not_advertise_a_hong_kong_node_as_backup(load_catalog):
    catalog = load_catalog([("home", "residential", [
        node(0, "香港首选"), node(1, "美国候选", port=8081),
    ], {})])
    draft, notices = suggest_tagged_routes({}, catalog)
    assert draft["service_profile_bindings"] == dict.fromkeys(LOCAL_PROXY_AI_SERVICE_IDS, "home")
    ai_notice = next(notice for notice in notices if "已为OpenAI / Codex" in notice)
    assert "暂无备用" in ai_notice
    assert "同订阅故障切换" not in ai_notice


def test_ai_source_recommendation_prefers_real_ai_backup_over_hong_kong_node_count(load_catalog):
    catalog = load_catalog([
        ("a", "residential", [node(0, "香港首选"), node(1, "美国候选", port=8081)], {}),
        ("z", "residential", [node(0, "美国首选", port=8082), node(1, "日本备用", port=8083)], {}),
    ])
    assert [profile["auto_route_candidate_count"] for profile in catalog] == [2, 2]
    assert [ai_route_candidate_count(profile) for profile in catalog] == [1, 2]
    plan = plan_route_preset({}, catalog, ai_strategy="auto")
    assert plan["sources"]["residential"]["id"] == "z"
    assert plan["sources"]["residential"]["ai_candidate_count"] == 2
    assert plan["draft"]["service_profile_bindings"] == dict.fromkeys(LOCAL_PROXY_AI_SERVICE_IDS, "z")


@pytest.mark.parametrize("key", ["", " key", "key ", "bad\nkey", "a" * 129])
def test_invalid_catalog_keys_cannot_become_generated_ai_pins(key):
    profile = {"nodes": [{"key": key, "label": "美国候选", "ai_auto_selectable": True}]}
    assert not ai_route_candidate_nodes(profile)
