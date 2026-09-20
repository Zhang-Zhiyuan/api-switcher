"""Explicit default overrides and inactive candidate pools keep their authority."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import local_proxy, proxy_route_diagnostics, proxy_routing, remote_proxy
from test_local_proxy_service_routing import _patch_profiles


def _node(index):
    return {"name": f"synthetic-{index}", "type": "http", "server": f"node-{index}.example", "port": 8080}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "routing.json")
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    local_proxy.clear_local_proxy_preferences_cache()
    yield
    local_proxy.clear_local_proxy_preferences_cache()


@pytest.mark.parametrize("target,kind,host", [
    ("override.example", "domain", "api.override.example"),
    ("203.0.113.0/24", "ip-cidr", "203.0.113.10"),
])
def test_explicit_custom_default_overrides_shared_custom_subscription(monkeypatch, target, kind, host):
    _patch_profiles(monkeypatch, {"shared": (_node(1),)})
    prefs = proxy_routing.normalize_routes({
        "service_profile_bindings": {"custom": "shared"},
        "service_route_modes": {"custom:own": "default"},
        "custom_targets": [
            {"id": "own", "kind": kind, "value": target},
            {"id": "inherited", "kind": "domain", "value": "inherited.example"},
        ],
    })
    options = proxy_routing.config_options(prefs)
    route_field = "proxy_domain_routes" if kind == "domain" else "proxy_ip_cidr_routes"
    assert options[route_field][target] == "AI-PROXY"
    assert options["proxy_domain_routes"]["inherited.example"] != "AI-PROXY"
    assert proxy_route_diagnostics.match_rules(host, proxy_route_diagnostics.saved_rules(prefs)).route == "AI-PROXY"
    parsed = remote_proxy.yaml.safe_load(remote_proxy.build_mihomo_config(_node(99), **options))
    prefix = "DOMAIN-SUFFIX" if kind == "domain" else "IP-CIDR"
    assert any(rule.startswith(f"{prefix},{target},AI-PROXY") for rule in parsed["rules"])


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_inactive_missing_candidates_do_not_block_active_route_refresh(isolated, monkeypatch, scope):
    _patch_profiles(monkeypatch, {"live": (_node(1),), "disabled": (_node(2),)})
    prefs = proxy_routing.normalize_routes({
        "service_profile_bindings": {"claude": "live", "youtube": "disabled"},
        "service_node_pools": {"claude": [remote_proxy.proxy_node_key(_node(1))], "youtube": ["expired"]},
        "builtin_sites": {"youtube": False},
    })
    reload = Mock(return_value="synthetic applied")
    if scope == "local":
        local_proxy.save_local_proxy_preferences(**prefs)
        monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
        monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda state: True)
        monkeypatch.setattr(local_proxy, "_is_port_listening", lambda port: True)
        monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: _node(99))
        monkeypatch.setattr(local_proxy, "reload_local_ai_proxy", reload)
        message = local_proxy.apply_local_proxy_routing_to_running()
        assert local_proxy.load_local_proxy_preferences()["service_node_pools"]["youtube"] == ["expired"]
    else:
        proxy_routing._save_ssh_routes("synthetic-host", prefs)
        monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *args: SimpleNamespace(running=True))
        monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *args: _node(99))
        monkeypatch.setattr(remote_proxy, "reload_ai_proxy", reload)
        message = remote_proxy.refresh_running_ai_proxy_from_subscription(
            "synthetic-host", [remote_proxy.ProxySubscriptionNode(1, _node(1))], profile_id="live",
        )
        assert proxy_routing.load_ssh_routes("synthetic-host")["service_node_pools"]["youtube"] == ["expired"]
    assert message == "synthetic applied"
    assert reload.call_count == 1
    # Editing/reenabling still validates this retained authority; an inactive
    # pool is not permission to widen the pool or silently replace missing keys.
    with pytest.raises(RuntimeError, match="全部缺失"):
        proxy_routing.node_pool_warnings(prefs)


@pytest.mark.parametrize("concurrent_selection", [False, True])
def test_failed_refresh_rolls_back_only_its_selection_and_preserves_newer_user_choice(isolated, monkeypatch, concurrent_selection):
    profile = remote_proxy.save_proxy_subscription_profile(name="Original", url="https://home.example/sub", activate=False)
    other = remote_proxy.save_proxy_subscription_profile(name="Other", url="https://other.example/sub", activate=True)
    profile_id = profile["id"]
    remote_proxy.save_proxy_subscription_profile_state(
        profile_id, selected_node_key="previous-expired-key", selected_node_display="Previous selection",
    )
    local_proxy.save_local_proxy_preferences(service_profile_bindings={"claude": profile_id})

    def fail_apply():
        assert remote_proxy.load_proxy_subscription_state()["profiles"][profile_id]["selected_node_key"] == remote_proxy.proxy_node_key(_node(1))
        remote_proxy.save_proxy_subscription_profile_state(profile_id, node_qualities={"fresh": {"quality_score": 93}})
        remote_proxy.rename_proxy_subscription_profile(profile_id, "Concurrent rename")
        if concurrent_selection:
            remote_proxy.set_proxy_subscription_selected_node(_node(3), profile_id=profile_id)
        raise OSError("synthetic reload failure")

    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", fail_apply)
    with pytest.raises((OSError, RuntimeError), match="synthetic reload failure"):
        local_proxy.refresh_running_local_service_routes_from_subscription(
            [remote_proxy.ProxySubscriptionNode(1, _node(1))], profile_id=profile_id,
        )
    state = remote_proxy.load_proxy_subscription_state()
    saved = state["profiles"][profile_id]
    assert state["active_profile_id"] == other["id"]
    assert saved["name"] == "Concurrent rename"
    assert saved["node_qualities"] == {"fresh": {"quality_score": 93}}
    if concurrent_selection:
        assert saved["selected_node_key"] == remote_proxy.proxy_node_key(_node(3))
    else:
        assert saved["selected_node_key"] == "previous-expired-key"
        assert saved["selected_node_display"] == "Previous selection"


def test_selection_rollback_error_does_not_hide_original_reload_failure(isolated, monkeypatch):
    profile = remote_proxy.save_proxy_subscription_profile(name="Original", url="https://home.example/sub")
    local_proxy.save_local_proxy_preferences(service_profile_bindings={"claude": profile["id"]})
    original = OSError("synthetic reload failure")
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", Mock(side_effect=original))
    monkeypatch.setattr(remote_proxy, "restore_proxy_subscription_selected_node", Mock(side_effect=OSError("synthetic disk failure")))
    with pytest.raises(RuntimeError) as failure:
        local_proxy.refresh_running_local_service_routes_from_subscription(
            [remote_proxy.ProxySubscriptionNode(1, _node(1))], profile_id=profile["id"],
        )
    assert "synthetic reload failure" in str(failure.value)
    assert "synthetic disk failure" in str(failure.value)
    assert failure.value.__cause__ is original


def test_entirely_overridden_custom_pool_does_not_read_unneeded_subscription(monkeypatch):
    prefs = proxy_routing.normalize_routes({
        "service_profile_bindings": {"custom": "expired-source"},
        "service_node_pools": {"custom": ["expired-node"]},
        "service_route_modes": {"custom:own": "default"},
        "custom_targets": [{"id": "own", "value": "only.example"}],
    })
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: pytest.fail("unused group must not read subscriptions"))
    assert proxy_routing.node_pool_warnings(prefs, active_only=True) == ()
    assert proxy_routing.config_options(prefs)["additional_proxy_groups"] == ()
