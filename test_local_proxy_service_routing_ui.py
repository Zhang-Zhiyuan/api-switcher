import inspect

from ui.tabs.local_proxy_tab import LocalProxyTab


class _OverviewStub:
    def __init__(self):
        self.calls = []

    def set_routes(self, preferences, catalog):
        self.calls.append((preferences, catalog))


def test_route_overview_refreshes_names_without_losing_nodes_or_activating_profile():
    tab = object.__new__(LocalProxyTab)
    tab._route_overview = _OverviewStub()
    tab._routing_preferences_snapshot = {"service_profile_bindings": {"openai": "a"}}
    nodes = [{"key": "node-a", "label": "家宽 01"}]
    tab._service_route_catalog = [{"id": "a", "name": "旧名字", "nodes": nodes}]
    tab._refresh_service_route_profile_options([{"id": "a", "name": "家宽订阅 A"}])
    prefs, catalog = tab._route_overview.calls[-1]
    assert prefs == {"service_profile_bindings": {"openai": "a"}}
    assert catalog == [{"id": "a", "name": "家宽订阅 A", "nodes": nodes, "network_type": "unknown"}]
    tab._refresh_service_route_profile_options([{"id": "a", "name": "家宽订阅 A", "network_type": "residential"}])
    _, updated = tab._route_overview.calls[-1]
    assert updated[0]["network_type"] == "residential"
    assert updated[0]["nodes"] is nodes


def test_deleted_subscription_is_removed_from_catalog_without_dropping_binding():
    tab = object.__new__(LocalProxyTab)
    tab._route_overview = _OverviewStub()
    tab._routing_preferences_snapshot = {"service_profile_bindings": {"openai": "deleted-profile"}}
    tab._service_route_catalog = [{"id": "deleted-profile", "name": "旧名字", "nodes": []}]
    tab._refresh_service_route_profile_options([])
    prefs, catalog = tab._route_overview.calls[-1]
    assert catalog == []
    assert prefs["service_profile_bindings"]["openai"] == "deleted-profile"


def test_subscription_refresh_keeps_bound_profiles_out_of_main_node_promotion():
    hot_update_source = inspect.getsource(LocalProxyTab._start_subscription_hot_update)
    fetch_source = inspect.getsource(LocalProxyTab._fetch_subscription)

    assert "local_proxy_service_bindings_for_profile" in hot_update_source
    assert "refresh_running_local_service_routes_from_subscription" in hot_update_source
    assert "refresh_running_local_ai_proxy_from_subscription" in hot_update_source
    assert "refresh_running_local_service_routes_from_subscription" in fetch_source
