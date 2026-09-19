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
    nodes = [SimpleNamespace(node={"name": "first", "dialer-proxy": "chain" if first_chained else ""}),
             SimpleNamespace(node={"name": "second"})]
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {})
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda *_: [profile])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda *_: SimpleNamespace(nodes=nodes))
    monkeypatch.setattr(remote_proxy, "proxy_subscription_node_key", lambda item: item.node["name"])
    entry, = proxy_routing.load_route_catalog()
    assert entry["auto_route_usable"] is expected
    assert entry["network_type"] == "residential"
    assert len(entry["nodes"]) == (1 if first_chained else 2)
