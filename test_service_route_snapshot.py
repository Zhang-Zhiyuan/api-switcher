"""One consistent subscription snapshot per build; no live state is touched."""
from unittest.mock import Mock

import pytest

from core import local_proxy, remote_proxy


def _cache(*servers):
    return remote_proxy.ProxySubscriptionResult(
        nodes=tuple(remote_proxy.ProxySubscriptionNode(index, {
            "name": f"node-{index}", "type": "http", "server": server, "port": 8080,
        }) for index, server in enumerate(servers, 1)),
        saved_path="memory-only",
    )


def _profile(profile_id="same-sub"):
    return {"id": profile_id, "name": profile_id, "saved_path": "memory-only", "selected_node_key": ""}


def _preferences():
    return {"builtin_sites": {"google": True},
            "service_profile_bindings": {"openai": "same-sub", "google": "same-sub"}}


@pytest.fixture
def profile_state(monkeypatch):
    profile = _profile()
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {"profiles": {profile["id"]: profile}})
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda _profile: {})
    return profile


def test_ai_and_website_groups_share_cache_snapshot_during_refresh(monkeypatch, profile_state):
    old, refreshed = _cache("old.example.test"), _cache("new.example.test")
    loader = Mock(side_effect=[old, refreshed])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", loader)
    groups = local_proxy._resolve_service_subscription_routes(_preferences())["additional_proxy_groups"]
    assert len(groups) == 2
    assert {group["proxy_node"]["server"] for group in groups} == {"old.example.test"}
    assert loader.call_count == 1


def test_pinned_service_uses_same_snapshot_as_automatic_service(monkeypatch, profile_state):
    old, refreshed = _cache("first.example.test", "pinned.example.test"), _cache("replacement.example.test")
    loader = Mock(side_effect=[old, refreshed])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", loader)
    preferences = _preferences()
    preferences["service_profile_bindings"]["claude"] = "same-sub"
    preferences["service_node_bindings"] = {"claude": remote_proxy.proxy_subscription_node_key(old.nodes[1])}
    groups = local_proxy._resolve_service_subscription_routes(preferences)["additional_proxy_groups"]
    pinned = next(group for group in groups if "anthropic.com" in group["health_check_url"])
    assert pinned["proxy_node"]["server"] == "pinned.example.test"
    assert pinned["fallback_proxy_nodes"] == ()
    assert loader.call_count == 1


def test_missing_pin_does_not_accept_a_newer_cache_mid_build(monkeypatch, profile_state):
    old, refreshed = _cache("first.example.test"), _cache("pinned-only-after-refresh.example.test")
    loader = Mock(side_effect=[old, refreshed])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", loader)
    preferences = _preferences()
    preferences["service_node_bindings"] = {"google": remote_proxy.proxy_subscription_node_key(refreshed.nodes[0])}
    with pytest.raises(RuntimeError, match="固定节点已失效"):
        local_proxy._resolve_service_subscription_routes(preferences)
    assert loader.call_count == 1


def test_next_build_loads_fresh_snapshot_not_process_wide_cached_nodes(monkeypatch, profile_state):
    loader = Mock(side_effect=[_cache("old.example.test"), _cache("new.example.test")])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", loader)
    for expected in ("old.example.test", "new.example.test"):
        groups = local_proxy._resolve_service_subscription_routes(_preferences())["additional_proxy_groups"]
        assert {group["proxy_node"]["server"] for group in groups} == {expected}
    assert loader.call_count == 2


def test_different_subscriptions_never_share_the_cache_snapshot(monkeypatch):
    profiles = {key: _profile(key) for key in ("home", "dc")}
    caches = {key: _cache(f"{key}.example.test") for key in profiles}
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {"profiles": profiles})
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda _profile: {})
    loader = Mock(side_effect=lambda profile: caches[profile["id"]])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", loader)
    preferences = {"builtin_sites": {"google": True}, "service_profile_bindings": {"openai": "home", "google": "dc"}}
    groups = local_proxy._resolve_service_subscription_routes(preferences)["additional_proxy_groups"]
    assert {group["proxy_node"]["server"] for group in groups} == {"home.example.test", "dc.example.test"}
    assert loader.call_count == 2


@pytest.mark.parametrize("cached", [None, _cache()])
def test_explicitly_supplied_empty_snapshot_never_reloads(monkeypatch, cached):
    loader = Mock(side_effect=AssertionError("empty snapshot must not re-read a newer cache"))
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", loader)
    with pytest.raises(RuntimeError, match="没有可用缓存"):
        local_proxy._selected_subscription_route_pool(_profile(), ai_sensitive=False, cached=cached)
    loader.assert_not_called()


def test_missing_snapshot_fails_once_without_trying_later_version(monkeypatch, profile_state):
    loader = Mock(side_effect=[None, _cache("new.example.test")])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", loader)
    with pytest.raises(RuntimeError, match="没有可用缓存"):
        local_proxy._resolve_service_subscription_routes(_preferences())
    assert loader.call_count == 1


def test_legacy_helper_call_still_loads_cache_once(monkeypatch):
    loader = Mock(return_value=_cache("current.example.test"))
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", loader)
    primary, fallback = local_proxy._selected_subscription_route_pool(_profile(), ai_sensitive=False)
    assert primary["server"] == "current.example.test" and fallback == ()
    loader.assert_called_once()
