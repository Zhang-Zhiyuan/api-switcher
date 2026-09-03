import inspect

from ui.tabs.local_proxy_tab import (
    SERVICE_ROUTE_DEFAULT_LABEL,
    SERVICE_ROUTE_MISSING_LABEL,
    LocalProxyTab,
)


class _ComboStub:
    def __init__(self):
        self.values = []
        self.value = ""

    def configure(self, **kwargs):
        if "values" in kwargs:
            self.values = list(kwargs["values"])

    def set(self, value):
        self.value = value


def test_service_route_combos_restore_profile_bindings_without_activating_profile():
    tab = object.__new__(LocalProxyTab)
    tab._subscription_profiles_snapshot = []
    tab._service_route_combos = {
        "openai": _ComboStub(),
        "claude": _ComboStub(),
        "youtube": _ComboStub(),
    }
    tab._service_route_bindings = {
        "openai": "profile-a",
        "claude": "profile-a",
        "youtube": "profile-b",
    }
    tab._service_route_combo_loading = False

    tab._refresh_service_route_profile_options(
        [
            {"id": "profile-a", "name": "香港家宽 A", "saved_path": "a.yaml"},
            {"id": "profile-b", "name": "机房线路 B", "saved_path": "b.yaml"},
        ]
    )

    assert tab._service_route_combos["openai"].value == "香港家宽 A"
    assert tab._service_route_combos["claude"].value == "香港家宽 A"
    assert tab._service_route_combos["youtube"].value == "机房线路 B"
    assert tab._service_route_profile_options[SERVICE_ROUTE_DEFAULT_LABEL] == ""


def test_service_route_combo_surfaces_deleted_binding_instead_of_silent_fallback():
    tab = object.__new__(LocalProxyTab)
    combo = _ComboStub()
    tab._subscription_profiles_snapshot = []
    tab._service_route_combos = {"openai": combo}
    tab._service_route_bindings = {"openai": "deleted-profile"}
    tab._service_route_combo_loading = False

    tab._refresh_service_route_profile_options([])

    assert combo.value == SERVICE_ROUTE_MISSING_LABEL
    assert SERVICE_ROUTE_MISSING_LABEL in combo.values


def test_subscription_refresh_keeps_bound_profiles_out_of_main_node_promotion():
    hot_update_source = inspect.getsource(LocalProxyTab._start_subscription_hot_update)
    fetch_source = inspect.getsource(LocalProxyTab._fetch_subscription)

    assert "local_proxy_service_bindings_for_profile" in hot_update_source
    assert "refresh_running_local_service_routes_from_subscription" in hot_update_source
    assert "refresh_running_local_ai_proxy_from_subscription" in hot_update_source
    assert "refresh_running_local_service_routes_from_subscription" in fetch_source
