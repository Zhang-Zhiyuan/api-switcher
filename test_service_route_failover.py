"""Same-subscription website failover contracts without live network changes."""
import copy
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from core import local_proxy, proxy_routing, remote_proxy
from test_local_proxy_service_routing import _patch_profiles


def _node(index, **updates):
    return {"name": f"node-{index}", "type": "http", "server": f"node-{index}.example.test", "port": 8080, **updates}


def _items(count):
    return [remote_proxy.ProxySubscriptionNode(index, _node(index)) for index in range(count)]


def _latency(ok, delay=25, *, age_seconds=0):
    return {"ok": ok, "latency_ms": delay if ok else None,
            "measured_at": (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()}


def test_website_pool_includes_more_than_old_first_five_and_stays_bounded():
    items = _items(40)
    selected = local_proxy._explicit_subscription_fallback_nodes(items[0].node, items)
    assert len(selected) == remote_proxy.SERVICE_PROXY_FALLBACK_MAX_NODES - 1 == 15
    assert selected[-1]["server"] == "node-15.example.test"
    assert all(node["server"] != items[0].node["server"] for node in selected)


def test_fresh_connectivity_prioritizes_late_success_without_permanently_dropping_failed_nodes():
    items = _items(7)
    latencies = {
        remote_proxy.proxy_subscription_node_key(items[1]): _latency(False),
        remote_proxy.proxy_subscription_node_key(items[3]): _latency(True, 300),
        remote_proxy.proxy_subscription_node_key(items[5]): _latency(True, 40),
    }
    selected = local_proxy._explicit_subscription_fallback_nodes(items[0].node, items, latencies)
    assert [node["name"] for node in selected] == ["node-5", "node-3", "node-2", "node-4", "node-6", "node-1"]


def test_late_known_connected_node_is_not_lost_to_unknown_prefix():
    items = _items(40)
    latencies = {remote_proxy.proxy_subscription_node_key(items[-1]): _latency(True)}
    selected = local_proxy._explicit_subscription_fallback_nodes(items[0].node, items, latencies)
    assert selected[0] == items[-1].node
    assert len(selected) == remote_proxy.SERVICE_PROXY_FALLBACK_MAX_NODES - 1


@pytest.mark.parametrize("result", [
    _latency(True, age_seconds=remote_proxy.PROXY_LATENCY_CACHE_TTL_SECONDS + 1),
    _latency(False, age_seconds=remote_proxy.PROXY_LATENCY_CACHE_TTL_SECONDS + 1),
    {"ok": True, "latency_ms": float("inf"), "measured_at": datetime.now(timezone.utc).isoformat()},
    {"ok": True, "latency_ms": -1, "measured_at": datetime.now(timezone.utc).isoformat()},
])
def test_expired_or_malformed_latency_is_only_unknown(result):
    items = _items(3)
    selected = local_proxy._explicit_subscription_fallback_nodes(
        items[0].node, items, {remote_proxy.proxy_subscription_node_key(items[1]): result},
    )
    assert [node["name"] for node in selected] == ["node-1", "node-2"]


def test_pool_excludes_duplicates_invalid_and_dependent_nodes_but_not_hong_kong():
    items = _items(3)
    duplicate = remote_proxy.ProxySubscriptionNode(5, {**items[1].node, "name": "renamed duplicate"})
    dependent = remote_proxy.ProxySubscriptionNode(6, _node(6, **{"dialer-proxy": "another-provider"}))
    invalid = remote_proxy.ProxySubscriptionNode(7, {"name": "invalid", "type": "unknown"})
    hk = remote_proxy.ProxySubscriptionNode(8, _node(8), region="香港")
    original = copy.deepcopy([*items, duplicate, dependent, invalid, hk])
    selected = local_proxy._explicit_subscription_fallback_nodes(
        items[0].node, [*original, None, {}],
    )
    assert [node["name"] for node in selected] == ["node-1", "node-2", "node-8"]
    assert original == [*items, duplicate, dependent, invalid, hk]


def test_non_ai_selected_pool_uses_only_its_profile_cache_and_latency_hints(monkeypatch):
    nodes = tuple(_node(index) for index in range(22))
    profiles = _patch_profiles(monkeypatch, {"dc": nodes, "home": (_node(100),)})
    profiles["dc"]["node_latencies"] = {remote_proxy.proxy_node_key(nodes[-1]): _latency(True)}
    primary, fallback = local_proxy._selected_subscription_route_pool(profiles["dc"], ai_sensitive=False)
    assert primary == nodes[0]
    assert fallback[0] == nodes[-1]
    assert all(node["server"] != "node-100.example.test" for node in fallback)


def test_explicit_node_remains_pinned_even_when_recently_failed(monkeypatch):
    nodes = (_node(1), _node(2))
    profiles = _patch_profiles(monkeypatch, {"dc": nodes})
    profiles["dc"]["node_latencies"] = {remote_proxy.proxy_node_key(nodes[1]): _latency(False)}
    primary, fallback = local_proxy._selected_subscription_route_pool(
        profiles["dc"], ai_sensitive=False, node_key=remote_proxy.proxy_node_key(nodes[1]),
    )
    assert primary == nodes[1] and fallback == ()


@pytest.mark.parametrize("service,url,status,pool_size", [
    ("youtube", "https://www.youtube.com/generate_204", "204", 16),
    ("google", "https://www.gstatic.com/generate_204", "204", 16),
    ("github", "https://www.gstatic.com/generate_204", "204", 16),
    ("openai", "https://api.openai.com/v1/models", "200/401", 5),
])
def test_generated_groups_use_bounded_live_https_fallback_without_other_exits(monkeypatch, service, url, status, pool_size):
    nodes = tuple(_node(index) for index in range(30))
    _patch_profiles(monkeypatch, {"bound": nodes, "other": (_node(100),)})
    preferences = {"builtin_sites": {service: True}, "service_profile_bindings": {service: "bound"}}
    config = remote_proxy.build_mihomo_config(_node(101), strict_privacy=True,
                                             **proxy_routing.config_options(preferences))
    parsed = yaml.safe_load(config)
    group = next(group for group in parsed["proxy-groups"] if group["name"] != "AI-PROXY")
    assert group["type"] == "fallback"
    assert group["url"] == url and group["expected-status"] == status
    assert group["interval"] == 10 and group["timeout"] == 5000
    assert group["lazy"] is False and group["max-failed-times"] == 1
    assert len(group["proxies"]) == pool_size
    assert not ({"DIRECT", "AI-PROXY", "REJECT"} & set(group["proxies"]))
    members = {node["name"]: node for node in parsed["proxies"]}
    assert {members[name]["server"] for name in group["proxies"]} <= {node["server"] for node in nodes}
    assert remote_proxy._managed_config_strict_privacy_enabled(config)


def test_wider_website_pool_does_not_change_ai_pool_or_mutable_tag_group_identity(monkeypatch):
    nodes = tuple(_node(index) for index in range(30))
    profiles = _patch_profiles(monkeypatch, {"bound": nodes})
    prefs = {"builtin_sites": {"google": True}, "service_profile_bindings": {"google": "bound"}}
    before = proxy_routing.config_options(prefs)
    profiles["bound"]["network_type"] = "residential"
    assert proxy_routing.config_options(prefs) == before
    assert len(remote_proxy._managed_mihomo_proxy_nodes(nodes[0], nodes[1:])) == 5


def test_strict_privacy_recognizes_more_than_seventeen_builder_owned_groups():
    specs = [{"name": f"SUB-{index:012X}-PROXY", "proxy_node": _node(index)} for index in range(18)]
    routes = {f"target-{index}.example.test": spec["name"] for index, spec in enumerate(specs)}
    config = remote_proxy.build_mihomo_config(_node(100), strict_privacy=True, additional_proxy_groups=specs,
                                             extra_proxy_domains=tuple(routes), proxy_domain_routes=routes)
    assert remote_proxy._managed_config_strict_privacy_enabled(config)


def test_oversized_pool_and_cross_group_injection_are_not_recognized_as_managed(monkeypatch):
    nodes = tuple(_node(index) for index in range(30))
    _patch_profiles(monkeypatch, {"dc": nodes})
    config = remote_proxy.build_mihomo_config(_node(100), strict_privacy=True,
        **proxy_routing.config_options({"builtin_sites": {"youtube": True}, "service_profile_bindings": {"youtube": "dc"}}))
    parsed = yaml.safe_load(config)
    marker = remote_proxy.AI_PROXY_CONFIG_MARKER + "\n" + remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER + "\n"
    assert remote_proxy._managed_config_strict_privacy_enabled(marker + yaml.safe_dump(parsed))
    group = parsed["proxy-groups"][1]
    for injected in ("DIRECT", "AI-PROXY"):
        modified = copy.deepcopy(parsed)
        modified["proxy-groups"][1]["proxies"][-1] = injected
        assert not remote_proxy._managed_config_strict_privacy_enabled(marker + yaml.safe_dump(modified))
    added_name = group["proxies"][-1].rsplit("-", 1)[0] + "-16"
    group["proxies"].append(added_name)
    parsed["proxies"].append(_node(90, name=added_name))
    assert not remote_proxy._managed_config_strict_privacy_enabled(marker + yaml.safe_dump(parsed))
