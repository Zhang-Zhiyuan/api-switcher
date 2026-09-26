"""Explicit DIRECT authority, isolated from real subscriptions and settings."""
import copy

import pytest
import yaml

from core import local_proxy, proxy_route_diagnostics, proxy_routing, remote_proxy
from core.local_proxy_constants import LOCAL_PROXY_BUILTIN_SITES
from core.subscription_routing_policy import cleanup_legacy_social_routes, suggest_tagged_routes
from test_local_proxy_service_routing import _node, _patch_profiles
from test_service_route_modes import isolated_routes  # noqa: F401
from ui.dialogs import service_route_bulk_dialog as bulk
from ui.widgets.service_route_overview import route_changes, route_description


def _preferences(**updates):
    return proxy_routing.normalize_routes({
        "builtin_sites": {"youtube": True, "google": True},
        "service_route_modes": {"youtube": "direct", "google": "direct"},
        **updates,
    })


def _config(preferences, *, local=True, **kwargs):
    node = _node("synthetic", "node.example.test")
    if local:
        return yaml.safe_load(local_proxy._build_local_mihomo_config(node, 17897, preferences=preferences))
    return yaml.safe_load(remote_proxy.build_mihomo_config(
        node, mainland_dns=True, **proxy_routing.config_options(preferences), **kwargs,
    ))


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("broad_scope", [True, False])
def test_direct_websites_emit_rules_and_system_dns_without_subscription_reads(monkeypatch, local, broad_scope):
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state",
                        lambda: pytest.fail("direct-only rules must not need a subscription cache"))
    preferences = {**_preferences(), "proxy_non_cn": broad_scope}
    config = _config(preferences, local=local, proxy_non_cn=broad_scope)
    rules, dns = config["rules"], config["dns"]["nameserver-policy"]
    for site in LOCAL_PROXY_BUILTIN_SITES:
        if site["id"] not in {"youtube", "google"}:
            continue
        for domain in site["targets"]:
            assert f"DOMAIN-SUFFIX,{domain},DIRECT" in rules
            assert rules.index(f"DOMAIN-SUFFIX,{domain},DIRECT") < len(rules) - 1
            assert dns[f"+.{domain}"] == ["system"]
    # Google websites must not steal their more specific AI subdomains.
    assert "DOMAIN-SUFFIX,generativelanguage.googleapis.com,AI-PROXY" in rules
    assert rules.index("DOMAIN-SUFFIX,generativelanguage.googleapis.com,AI-PROXY") < rules.index(
        "DOMAIN-SUFFIX,googleapis.com,DIRECT")
    assert all(url.endswith("#AI-PROXY") for url in dns["+.generativelanguage.googleapis.com"])
    assert len(config["proxy-groups"]) == 1
    assert rules[-1] == ("MATCH,AI-PROXY" if broad_scope else "MATCH,DIRECT")


@pytest.mark.parametrize("kind,value", [("domain", "api.openai.com"), ("ip-cidr", "203.0.113.8/32"),
                                         ("ip-cidr", "2001:db8::8/128")])
def test_explicit_custom_direct_overrides_inherited_subscription(monkeypatch, kind, value):
    _patch_profiles(monkeypatch, {"home": (_node("home", "home.example.test"),)})
    preferences = _preferences(
        service_profile_bindings={"custom": "home", "openai": "home"},
        custom_targets=[{"id": "child", "kind": kind, "value": value, "enabled": True}],
        service_route_modes={"custom:child": "direct"},
    )
    config = _config(preferences)
    if kind == "domain":
        rule = f"DOMAIN-SUFFIX,{value},DIRECT"
        assert config["dns"]["nameserver-policy"]["+.api.openai.com"] == ["system"]
        assert all("#SUB-" in url for url in config["dns"]["nameserver-policy"]["+.openai.com"])
    else:
        rule = f"{'IP-CIDR6' if ':' in value else 'IP-CIDR'},{value},DIRECT,no-resolve"
    assert rule in config["rules"]
    plan = local_proxy._service_route_blueprint(preferences)
    assert plan["service_routes"]["custom:child"] == "DIRECT"
    assert "custom" not in plan["service_routes"]


def test_custom_direct_default_inherits_but_explicit_default_uses_proxy():
    preferences = _preferences(
        service_route_modes={"custom": "direct", "custom:proxy": "default"},
        custom_targets=[{"id": key, "value": f"{key}.example.test"} for key in ("direct", "proxy")],
    )
    config = _config(preferences)
    assert "DOMAIN-SUFFIX,direct.example.test,DIRECT" in config["rules"]
    assert "DOMAIN-SUFFIX,proxy.example.test,AI-PROXY" in config["rules"]
    descriptions = {row["id"]: route_description(row, preferences, []) for row in proxy_routing.route_rows(preferences)}
    assert descriptions["custom:direct"]["inherited"]
    assert "直连" in descriptions["custom:direct"]["profile"]
    assert not descriptions["custom:proxy"]["inherited"]
    assert "直连" not in descriptions["custom:proxy"]["profile"]


def test_disabled_direct_intent_is_saved_but_has_no_rules_even_in_strict_mode():
    preferences = _preferences(
        builtin_sites={"youtube": False, "google": False},
        custom_targets=[{"id": "disabled", "value": "example.test", "enabled": False}],
        service_route_modes={"youtube": "direct", "google": "direct", "custom:disabled": "direct"},
    )
    config = _config({**preferences, "strict_privacy": True})
    assert not any(rule.startswith("DOMAIN-SUFFIX,youtube.com,") for rule in config["rules"])
    assert not any(rule.startswith("DOMAIN-SUFFIX,example.test,") for rule in config["rules"])
    assert config["rules"][-1] == "MATCH,AI-PROXY"
    row = next(row for row in proxy_routing.route_rows(preferences) if row["id"] == "youtube")
    description = route_description(row, preferences, [])
    assert "未启用" in description["hint"] and "直连" in description["hint"]


@pytest.mark.parametrize("local", [True, False])
def test_strict_mode_rejects_active_direct_without_silently_downgrading(local):
    preferences = {**_preferences(), "strict_privacy": True}
    with pytest.raises(ValueError, match="直连目标与严格隐私模式冲突"):
        _config(preferences, local=local, strict_privacy=True)
    assert preferences["strict_privacy"] is True


@pytest.mark.parametrize("routes", [
    {"extra_proxy_domains": ["example.test"], "proxy_domain_routes": {"example.test": "DIRECT"}},
    {"extra_proxy_ip_cidrs": ["203.0.113.0/24"], "proxy_ip_cidr_routes": {"203.0.113.0/24": "DIRECT"}},
])
def test_builder_enforces_privacy_for_direct_domain_and_ip(routes):
    with pytest.raises(ValueError, match="严格隐私"):
        remote_proxy.build_mihomo_config(_node("test", "node.example.test"), strict_privacy=True, **routes)


@pytest.mark.parametrize("name", ["direct", "DIRECT ", "REJECT", "PASS", "COMPATIBLE"])
def test_only_exact_direct_rule_is_allowed_not_other_builtin_bypasses(name):
    with pytest.raises(ValueError):
        remote_proxy.build_mihomo_config(_node("test", "node.example.test"),
                                         proxy_domain_routes={"openai.com": name})


def test_direct_does_not_weaken_proxy_names_or_strict_dns_validation():
    for operation in (
        lambda: remote_proxy._managed_proxy_route_name("DIRECT"),
        lambda: remote_proxy._proxy_doh_nameservers("DIRECT"),
        lambda: remote_proxy._strict_privacy_dns_config(domain_routes={"openai.com": "DIRECT"}),
    ):
        with pytest.raises(ValueError):
            operation()


@pytest.mark.parametrize("field,value", [("service_profile_bindings", "dc"),
                                         ("service_node_bindings", "node"), ("service_node_pools", ["node"])])
def test_conflicting_direct_and_subscription_authority_rejected(field, value):
    with pytest.raises(ValueError, match="冲突"):
        proxy_routing.normalize_routes({"service_route_modes": {"youtube": "direct"},
                                       "service_profile_bindings": {"youtube": "dc"}, field: {"youtube": value}})


@pytest.mark.usefixtures("isolated_routes")
def test_direct_survives_local_and_ssh_save_reload_and_unrelated_changes():
    preferences = _preferences()
    local_proxy.save_local_proxy_preferences(**preferences)
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(keep_running_on_exit=False)
    assert proxy_routing.route_snapshot(local_proxy._load_local_proxy_routing_preferences_strict()) == preferences
    proxy_routing._save_ssh_routes("synthetic", preferences)
    assert proxy_routing.load_ssh_routes("synthetic") == preferences
    assert proxy_routing.ssh_probe_kwargs("synthetic") == {"routing_preferences": preferences}
    assert proxy_routing.load_ssh_routes("another-host")["service_route_modes"] == {}


@pytest.mark.parametrize("start_strict", [True, False])
@pytest.mark.usefixtures("isolated_routes")
def test_local_privacy_conflict_preserves_preferences_before_any_runtime_change(monkeypatch, start_strict):
    local_proxy.save_local_proxy_preferences(**({"strict_privacy": True} if start_strict else _preferences()))
    before = local_proxy.LOCAL_PROXY_PREFS_PATH.read_bytes()
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running",
                        lambda: pytest.fail("privacy conflict must be checked before applying"))
    with pytest.raises(ValueError, match="严格隐私"):
        if start_strict:
            local_proxy.set_local_proxy_service_routes_and_apply(_preferences())
        else:
            local_proxy.set_local_proxy_strict_privacy(True)
    assert local_proxy.LOCAL_PROXY_PREFS_PATH.read_bytes() == before


def test_direct_manual_choice_survives_tag_suggestions_and_legacy_cleanup():
    original = _preferences(service_route_modes={"youtube": "direct", "x_twitter": "direct"})
    catalog = [{"id": "dc", "name": "dc", "network_type": "datacenter", "nodes": [{"key": "one"}]}]
    suggested, _ = suggest_tagged_routes(original, catalog)
    cleaned, _ = cleanup_legacy_social_routes(suggested, catalog)
    assert "youtube" not in cleaned["service_profile_bindings"]
    assert "x_twitter" not in cleaned["service_profile_bindings"]
    assert cleaned["service_profile_bindings"]["google"] == "dc"
    assert cleaned["service_route_modes"] == original["service_route_modes"]


def test_direct_batch_clears_bindings_and_enables_only_selected_targets_without_subscriptions():
    source = proxy_routing.normalize_routes({
        "builtin_sites": {"youtube": False, "google": False},
        "custom_targets": [{"id": "test", "value": "example.test", "enabled": False}],
        "service_profile_bindings": {"youtube": "deleted", "google": "kept"},
        "service_node_bindings": {"youtube": "stale"},
        "service_node_pools": {"google": ["kept-node"]},
    })
    before = copy.deepcopy(source)
    result, _ = bulk.apply_route_batch(source, [], ["youtube", "custom:test"], bulk.DIRECT)
    assert source == before
    assert result["builtin_sites"] == {"youtube": True, "google": False}
    assert result["custom_targets"][0]["enabled"]
    assert result["service_route_modes"] == {"youtube": "direct", "custom:test": "direct"}
    assert result["service_profile_bindings"] == {"google": "kept"}
    assert result["service_node_bindings"] == {}
    assert result["service_node_pools"] == {"google": ["kept-node"]}
    changes = route_changes({"local": before}, {"local": result}, [])
    assert {change["service"] for change in changes} == {"youtube", "custom:test"}
    assert all("直连" in change["after"] for change in changes)


def test_runtime_diagnostics_shows_direct_instead_of_default_proxy():
    preferences = _preferences()
    saved = proxy_route_diagnostics.saved_rules(preferences)
    for host in ("www.youtube.com", "youtubei.googleapis.com", "accounts.google.com"):
        result = proxy_route_diagnostics.match_rules(host, saved)
        assert result.certain and result.route == "DIRECT"
    snapshot = proxy_route_diagnostics.RouteSnapshot("synthetic", preferences, [], saved=saved)
    assert snapshot.route_label("DIRECT") == "直连"
    assert snapshot.selected("DIRECT") == "直连（未测试连通性）"
