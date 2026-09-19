from types import SimpleNamespace

import pytest

from core import proxy_routing, remote_proxy


@pytest.mark.parametrize("selected,first_chained,expected", [
    ("", True, False), ("", False, True),
    ("first", True, False), ("second", True, True),
    ("gone", True, False), ("gone", False, True),
])
def test_route_catalog_primary_matches_deployment_and_preserves_tag(monkeypatch, selected, first_chained, expected):
    profile = {"id": "tagged", "name": "线路 A", "network_type": "residential", "selected_node_key": selected}
    nodes = [SimpleNamespace(node={"name": "first", "type": "http", "server": "first.example.test", "port": 8080,
                                   "dialer-proxy": "chain" if first_chained else ""}),
             SimpleNamespace(node={"name": "second", "type": "http", "server": "second.example.test", "port": 8080})]
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {})
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda *_: [profile])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda *_: SimpleNamespace(nodes=nodes))
    monkeypatch.setattr(remote_proxy, "proxy_subscription_node_key", lambda item: item.node["name"])
    entry, = proxy_routing.load_route_catalog()
    assert entry["auto_route_usable"] is expected
    assert entry["network_type"] == "residential"
    assert len(entry["nodes"]) == (1 if first_chained else 2)
    assert entry["auto_route_candidate_count"] == (1 if first_chained else 2)


def test_catalog_counts_connections_not_names_without_more_cache_reads_or_secrets(monkeypatch):
    profile = {"id": "tagged", "name": "线路 A", "network_type": "datacenter"}
    original = {"name": "first", "type": "http", "server": "hidden-endpoint.example.test", "port": 8080,
                "username": "hidden-user", "password": "hidden-password"}
    nodes = [remote_proxy.ProxySubscriptionNode(0, original),
             remote_proxy.ProxySubscriptionNode(1, {**original, "name": "alias", "port": "8080"}),
             remote_proxy.ProxySubscriptionNode(2, {**original, "name": "dependent", "dialer-proxy": "chain"})]
    cache_reads = []

    def cached(value):
        cache_reads.append(value)
        return SimpleNamespace(nodes=nodes)

    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {})
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda *_: [profile])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", cached)
    entry, = proxy_routing.load_route_catalog()
    assert len(cache_reads) == 1
    assert len(entry["nodes"]) == 2
    assert len({node["key"] for node in entry["nodes"]}) == 2
    assert entry["auto_route_candidate_count"] == 1
    assert entry["auto_route_usable"] is True
    assert "hidden-" not in str(entry)
    assert "connection_keys" not in entry


def test_catalog_counts_different_normalized_connections_separately(monkeypatch):
    profile = {"id": "tagged", "name": "线路 A", "network_type": "datacenter"}
    first = {"name": "first", "type": "http", "server": "endpoint.example.test", "port": 8080}
    nodes = [remote_proxy.ProxySubscriptionNode(index, {**first, "name": f"node-{index}", "port": port})
             for index, port in enumerate((8080, 8081, 8082))]
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {})
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda *_: [profile])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda *_: SimpleNamespace(nodes=nodes))
    entry, = proxy_routing.load_route_catalog()
    assert entry["auto_route_candidate_count"] == 3


@pytest.mark.parametrize("cache", [None, "empty", "dependent", "invalid", "error"])
def test_catalog_reports_zero_candidates_when_none_are_independent_and_valid(monkeypatch, cache):
    profile = {"id": "tagged", "name": "线路 A"}

    def cached(_profile):
        if cache == "error":
            raise OSError("synthetic unreadable cache")
        if cache is None:
            return None
        nodes = []
        if cache in {"dependent", "invalid"}:
            node = ({"name": "chain", "type": "http", "server": "endpoint.example.test", "port": 8080,
                     "dialer-proxy": "another"} if cache == "dependent" else {"name": "broken", "type": "unknown"})
            nodes = [remote_proxy.ProxySubscriptionNode(0, node, node_key="cached-key")]
        return SimpleNamespace(nodes=nodes)

    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {})
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda *_: [profile])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", cached)
    entry, = proxy_routing.load_route_catalog()
    assert entry["auto_route_candidate_count"] == 0
