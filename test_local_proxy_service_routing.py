import json
from pathlib import Path

import pytest

from core import local_proxy, proxy_routing, remote_proxy


def _node(name: str, server: str) -> dict:
    return {
        "name": name,
        "type": "vless",
        "server": server,
        "port": 443,
    }


def _subscription_node(index: int, node: dict) -> remote_proxy.ProxySubscriptionNode:
    return remote_proxy.ProxySubscriptionNode(
        index=index,
        node=node,
        node_key=remote_proxy.proxy_node_key(node),
    )


def _patch_profiles(monkeypatch, profiles: dict[str, tuple[dict, ...]]) -> dict:
    state_profiles = {}
    cached_by_path = {}
    for index, (profile_id, nodes) in enumerate(profiles.items(), 1):
        items = tuple(
            _subscription_node(node_index, node)
            for node_index, node in enumerate(nodes, 1)
        )
        cache_path = f"cache-{index}.yaml"
        state_profiles[profile_id] = {
            "id": profile_id,
            "name": f"线路 {index}",
            "saved_path": cache_path,
            "selected_node_key": remote_proxy.proxy_subscription_node_key(items[0]),
        }
        cached_by_path[cache_path] = remote_proxy.ProxySubscriptionResult(
            nodes=items,
            saved_path=cache_path,
        )
    monkeypatch.setattr(
        remote_proxy,
        "load_proxy_subscription_state",
        lambda: {"profiles": state_profiles},
    )
    monkeypatch.setattr(
        remote_proxy,
        "load_cached_proxy_subscription",
        lambda profile=None: cached_by_path.get(str((profile or {}).get("saved_path") or "")),
    )
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda _profile=None: {})
    return state_profiles


def test_services_can_pin_different_nodes_inside_the_same_subscription(monkeypatch):
    first = _node("家宽节点 1", "one.example.com")
    second = _node("家宽节点 2", "two.example.com")
    _patch_profiles(monkeypatch, {"home": (first, second)})
    preferences = {
        "service_profile_bindings": {"openai": "home", "claude": "home"},
        "service_node_bindings": {
            "openai": remote_proxy.proxy_node_key(first),
            "claude": remote_proxy.proxy_node_key(second),
        },
    }
    parsed = remote_proxy.yaml.safe_load(local_proxy._build_local_mihomo_config(
        _node("默认节点", "default.example.com"), 17897, preferences=preferences,
    ))
    routes = {rule.split(",")[1]: rule.split(",")[2] for rule in parsed["rules"] if rule.startswith("DOMAIN-SUFFIX,")}
    assert routes["openai.com"] != routes["anthropic.com"]
    assert len(parsed["proxy-groups"]) == 3
    for group in parsed["proxy-groups"][1:]:
        assert len(group["proxies"]) == 1
    assert [node["server"] for node in parsed["proxies"]] == ["default.example.com", "one.example.com", "two.example.com"]


def test_missing_fixed_node_blocks_update_instead_of_falling_back(monkeypatch):
    _patch_profiles(monkeypatch, {"home": (_node("剩余节点", "other.example.com"),)})
    with pytest.raises(RuntimeError, match="固定节点已失效"):
        local_proxy._build_local_mihomo_config(
            _node("默认节点", "default.example.com"), 17897,
            preferences={"service_profile_bindings": {"claude": "home"},
                         "service_node_bindings": {"claude": "a" * 64}},
        )


def test_custom_target_has_independent_route_and_precedes_parent_domain(monkeypatch):
    _patch_profiles(monkeypatch, {"home": (_node("家宽", "home.example.com"),),
                                 "dc": (_node("机房", "dc.example.com"),)})
    preferences = {
        "custom_targets": [{"id": "api", "kind": "domain", "value": "api.openai.com", "enabled": True}],
        "service_profile_bindings": {"openai": "dc", "custom:api": "home"},
    }
    parsed = remote_proxy.yaml.safe_load(local_proxy._build_local_mihomo_config(
        _node("默认", "default.example.com"), 17897, preferences=preferences,
    ))
    specific = f"DOMAIN-SUFFIX,api.openai.com,{local_proxy._subscription_route_group_name('home', 'custom:api')}"
    parent = f"DOMAIN-SUFFIX,openai.com,{local_proxy._subscription_route_group_name('dc')}"
    assert parsed["rules"].index(specific) < parsed["rules"].index(parent)
    assert parsed["dns"]["nameserver-policy"]["+.api.openai.com"] != parsed["dns"]["nameserver-policy"]["+.openai.com"]


def test_specific_ip_route_precedes_broader_network():
    config = remote_proxy.build_mihomo_config(
        _node("默认", "default.example.com"),
        extra_proxy_ip_cidrs=("203.0.113.0/24", "203.0.113.9/32"),
    )
    rules = remote_proxy.yaml.safe_load(config)["rules"]
    assert rules.index("IP-CIDR,203.0.113.9/32,AI-PROXY,no-resolve") < rules.index("IP-CIDR,203.0.113.0/24,AI-PROXY,no-resolve")


def test_batch_route_apply_rolls_back_profiles_nodes_and_sites(monkeypatch, tmp_path):
    nodes = (_node("first", "first.example.com"), _node("second", "second.example.com"))
    _patch_profiles(monkeypatch, {"home": nodes})
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", tmp_path)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    original = {"service_profile_bindings": {"openai": "home"},
                "service_node_bindings": {"openai": remote_proxy.proxy_node_key(nodes[0])}}
    local_proxy.save_local_proxy_preferences(**original)
    snapshot = proxy_routing.route_snapshot(local_proxy.load_local_proxy_preferences())
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running",
                        lambda: (_ for _ in ()).throw(RuntimeError("controller failed")))
    with pytest.raises(RuntimeError, match="已恢复原绑定"):
        local_proxy.set_local_proxy_service_routes_and_apply(
            {"service_profile_bindings": {"youtube": "home"}, "builtin_sites": {"youtube": True},
             "service_node_bindings": {"youtube": remote_proxy.proxy_node_key(nodes[1])}}, expected=snapshot,
        )
    assert proxy_routing.route_snapshot(local_proxy.load_local_proxy_preferences()) == snapshot


def test_route_editor_detects_stale_draft_before_applying(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", tmp_path)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(builtin_sites={"youtube": True})
    with pytest.raises(RuntimeError, match="已被其他操作修改"):
        local_proxy.set_local_proxy_service_routes_and_apply({}, expected={})
    assert local_proxy.load_local_proxy_preferences()["builtin_sites"]["youtube"] is True


@pytest.mark.parametrize("value", [[], {"claude": 123}, {"claude": "a" * 64}])
def test_corrupt_or_orphan_node_binding_is_not_silently_downgraded(value):
    with pytest.raises(ValueError, match="节点"):
        proxy_routing.normalize_routes({"service_node_bindings": value})


def test_invalid_custom_target_binding_cannot_silently_disappear():
    with pytest.raises(ValueError, match="无效的自定义目标"):
        proxy_routing.normalize_routes({
            "custom_targets": [{"id": "api", "value": "not,a,domain"}],
            "service_profile_bindings": {"custom:api": "home"},
        })


def test_service_routing_uses_residential_for_ai_and_datacenter_for_youtube(monkeypatch):
    residential = _node("residential", "home.example.com")
    residential_backup = _node("residential backup", "home-backup.example.com")
    datacenter = _node("datacenter", "dc.example.com")
    datacenter_backup = _node("datacenter backup", "dc-backup.example.com")
    _patch_profiles(
        monkeypatch,
        {
            "profile-a": (residential, residential_backup),
            "profile-b": (datacenter, datacenter_backup),
        },
    )

    config = local_proxy._build_local_mihomo_config(
        residential,
        17897,
        preferences={
            "builtin_sites": {"youtube": True},
            "service_profile_bindings": {
                "openai": "profile-a",
                "claude": "profile-a",
                "youtube": "profile-b",
            },
            "strict_privacy": True,
        },
        fallback_nodes=(residential_backup,),
    )
    parsed = remote_proxy.yaml.safe_load(config)
    residential_group = local_proxy._subscription_route_group_name("profile-a")
    claude_group = local_proxy._subscription_route_group_name("profile-a", "claude")
    youtube_group = local_proxy._subscription_route_group_name("profile-b", "youtube")

    assert f"DOMAIN-SUFFIX,openai.com,{residential_group}" in parsed["rules"]
    assert f"DOMAIN-SUFFIX,anthropic.com,{claude_group}" in parsed["rules"]
    assert f"DOMAIN-SUFFIX,youtube.com,{youtube_group}" in parsed["rules"]
    assert f"DOMAIN-SUFFIX,googlevideo.com,{youtube_group}" in parsed["rules"]
    assert f"DOMAIN-SUFFIX,youtubei.googleapis.com,{youtube_group}" in parsed["rules"]
    assert parsed["dns"]["nameserver-policy"]["+.youtube.com"] == [
        f"https://1.1.1.1/dns-query#{youtube_group}",
        f"https://8.8.8.8/dns-query#{youtube_group}",
    ]
    assert len(parsed["proxy-groups"]) == 4
    assert parsed["proxy-groups"][1]["url"] == remote_proxy.AI_PROXY_HEALTH_CHECK_URL
    assert parsed["proxy-groups"][2]["url"] == "https://api.anthropic.com/v1/models"
    assert parsed["proxy-groups"][3]["url"] == "https://www.youtube.com/generate_204"
    assert [node["server"] for node in parsed["proxies"]] == [
        "home.example.com",
        "home-backup.example.com",
        "home.example.com",
        "home-backup.example.com",
        "home.example.com",
        "home-backup.example.com",
        "dc.example.com",
        "dc-backup.example.com",
    ]
    assert remote_proxy._managed_config_strict_privacy_enabled(config) is True


def test_service_routing_can_bind_openai_and_claude_to_non_primary_profile(monkeypatch):
    manual = _node("manual", "manual.example.com")
    residential = _node("residential", "home.example.com")
    _patch_profiles(monkeypatch, {"profile-a": (residential,)})

    config = local_proxy._build_local_mihomo_config(
        manual,
        17897,
        preferences={
            "service_profile_bindings": {
                "openai": "profile-a",
                "claude": "profile-a",
            }
        },
    )
    parsed = remote_proxy.yaml.safe_load(config)
    route_group = local_proxy._subscription_route_group_name("profile-a")

    assert f"DOMAIN-SUFFIX,openai.com,{route_group}" in parsed["rules"]
    claude_group = local_proxy._subscription_route_group_name("profile-a", "claude")
    assert f"DOMAIN-SUFFIX,anthropic.com,{claude_group}" in parsed["rules"]
    assert "DOMAIN-SUFFIX,generativelanguage.googleapis.com,AI-PROXY" in parsed["rules"]
    assert len(parsed["proxy-groups"]) == 3
    assert parsed["proxy-groups"][1]["url"] == remote_proxy.AI_PROXY_HEALTH_CHECK_URL


def test_explicit_ai_binding_does_not_reuse_foreign_primary_fallback(monkeypatch):
    residential = _node("residential", "home.example.com")
    datacenter_backup = _node("foreign fallback", "dc.example.com")
    _patch_profiles(monkeypatch, {"profile-a": (residential,)})

    config = local_proxy._build_local_mihomo_config(
        residential,
        17897,
        preferences={"service_profile_bindings": {"openai": "profile-a"}},
        fallback_nodes=(datacenter_backup,),
    )
    parsed = remote_proxy.yaml.safe_load(config)
    route_group = local_proxy._subscription_route_group_name("profile-a")

    assert f"DOMAIN-SUFFIX,openai.com,{route_group}" in parsed["rules"]
    assert len(parsed["proxy-groups"]) == 2
    assert parsed["proxy-groups"][0]["proxies"] == [
        remote_proxy.AI_PROXY_INTERNAL_NODE_NAME,
        f"{remote_proxy.AI_PROXY_FALLBACK_NODE_PREFIX}1",
    ]
    assert parsed["proxy-groups"][1]["proxies"] == [
        remote_proxy._managed_additional_node_names(route_group)[0]
    ]


def test_disabled_builtin_binding_does_not_require_its_profile(monkeypatch):
    monkeypatch.setattr(
        remote_proxy,
        "load_proxy_subscription_state",
        lambda: {"profiles": {}},
    )
    config = local_proxy._build_local_mihomo_config(
        _node("primary", "primary.example.com"),
        17897,
        preferences={
            "builtin_sites": {"youtube": False},
            "service_profile_bindings": {"youtube": "deleted-profile"},
        },
    )

    assert "youtube.com" not in "\n".join(remote_proxy.yaml.safe_load(config)["rules"])


def test_enabled_service_binding_fails_before_silent_route_fallback(monkeypatch):
    monkeypatch.setattr(
        remote_proxy,
        "load_proxy_subscription_state",
        lambda: {"profiles": {}},
    )

    with pytest.raises(RuntimeError, match="绑定的订阅已被删除"):
        local_proxy._build_local_mihomo_config(
            _node("primary", "primary.example.com"),
            17897,
            preferences={"service_profile_bindings": {"openai": "deleted-profile"}},
        )


def test_existing_primary_fallback_pool_does_not_absorb_secondary_route(monkeypatch, tmp_path):
    primary = _node("primary", "primary.example.com")
    primary_backup = _node("primary backup", "primary-backup.example.com")
    secondary = _node("secondary", "secondary.example.com")
    group_name = "SUB-0123456789AB-PROXY"
    config = remote_proxy.build_mihomo_config(
        primary,
        17897,
        fallback_proxy_nodes=(primary_backup,),
        additional_proxy_groups=(
            {
                "name": group_name,
                "proxy_node": secondary,
                "health_check_url": "https://www.youtube.com/generate_204",
                "health_check_expected_status": "204",
            },
        ),
        extra_proxy_domains=("youtube.com",),
        proxy_domain_routes={"youtube.com": group_name},
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config, encoding="utf-8")
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"config_path": str(config_path)})
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", Path(tmp_path))

    fallbacks = local_proxy._existing_local_proxy_fallback_nodes(primary)

    assert [node["server"] for node in fallbacks] == ["primary-backup.example.com"]


def test_additional_route_group_must_be_used_by_a_rule():
    group_name = "SUB-0123456789AB-PROXY"

    with pytest.raises(ValueError, match="未被任何服务使用"):
        remote_proxy.build_mihomo_config(
            _node("primary", "primary.example.com"),
            17897,
            additional_proxy_groups=(
                {
                    "name": group_name,
                    "proxy_node": _node("secondary", "secondary.example.com"),
                },
            ),
        )


def test_strict_route_validator_rejects_tampered_secondary_health_check(monkeypatch):
    residential = _node("residential", "home.example.com")
    datacenter = _node("datacenter", "dc.example.com")
    _patch_profiles(
        monkeypatch,
        {"profile-a": (residential,), "profile-b": (datacenter,)},
    )
    config = local_proxy._build_local_mihomo_config(
        residential,
        17897,
        preferences={
            "strict_privacy": True,
            "builtin_sites": {"youtube": True},
            "service_profile_bindings": {"youtube": "profile-b"},
        },
    )

    tampered = config.replace(
        "https://www.youtube.com/generate_204",
        "https://example.com/generate_204",
        1,
    )

    assert remote_proxy._managed_config_strict_privacy_enabled(config) is True
    assert remote_proxy._managed_config_strict_privacy_enabled(tampered) is False


def test_strict_route_validator_rejects_dns_route_mismatch(monkeypatch):
    primary = _node("primary", "primary.example.com")
    datacenter = _node("datacenter", "dc.example.com")
    _patch_profiles(monkeypatch, {"profile-b": (datacenter,)})
    config = local_proxy._build_local_mihomo_config(
        primary,
        17897,
        preferences={
            "strict_privacy": True,
            "builtin_sites": {"youtube": True},
            "service_profile_bindings": {"youtube": "profile-b"},
        },
    )
    parsed = remote_proxy.yaml.safe_load(config)
    parsed["dns"]["nameserver-policy"]["+.youtube.com"] = [
        "https://1.1.1.1/dns-query#AI-PROXY",
        "https://8.8.8.8/dns-query#AI-PROXY",
    ]
    tampered = (
        remote_proxy.AI_PROXY_CONFIG_MARKER
        + "\n"
        + remote_proxy._dump_yaml(parsed)
    )

    assert remote_proxy._managed_config_strict_privacy_enabled(tampered) is False


def test_preferences_preserve_only_supported_service_profile_bindings():
    preferences = local_proxy._normalize_local_proxy_preferences(
        {
            "service_profile_bindings": {
                "openai": "profile-a",
                "youtube": "profile-b",
                "unknown": "profile-c",
                "claude": 123,
            }
        }
    )

    assert preferences["service_profile_bindings"] == {
        "openai": "profile-a",
        "youtube": "profile-b",
    }


@pytest.mark.parametrize(
    "bindings",
    [[], {"openai": 123}, {"youtube": "x" * 65}],
)
def test_strict_routing_reader_rejects_corrupt_service_bindings(
    monkeypatch,
    tmp_path,
    bindings,
):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text(
        json.dumps(
            {"strict_privacy": False, "service_profile_bindings": bindings}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)
    local_proxy.clear_local_proxy_preferences_cache()

    with pytest.raises(RuntimeError, match="service_profile_bindings|订阅绑定"):
        local_proxy._load_local_proxy_routing_preferences_strict()


def test_binding_builtin_profile_enables_site_and_applies_transactionally(
    monkeypatch,
    tmp_path,
):
    _patch_profiles(monkeypatch, {"profile-b": (_node("dc", "dc.example.com"),)})
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", tmp_path)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    monkeypatch.setattr(
        local_proxy,
        "apply_local_proxy_routing_to_running",
        lambda: "运行配置已更新",
    )

    message = local_proxy.set_local_proxy_service_profile_binding_and_apply(
        "youtube",
        "profile-b",
    )
    preferences = local_proxy.load_local_proxy_preferences()

    assert preferences["service_profile_bindings"] == {"youtube": "profile-b"}
    assert preferences["builtin_sites"]["youtube"] is True
    assert "运行配置已更新" in message


def test_binding_apply_failure_restores_previous_preferences(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, {"profile-a": (_node("home", "home.example.com"),)})
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", tmp_path)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(
        builtin_sites={"youtube": False},
        service_profile_bindings={},
    )

    def fail_apply():
        raise RuntimeError("controller unavailable")

    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", fail_apply)

    with pytest.raises(RuntimeError, match="已恢复原绑定"):
        local_proxy.set_local_proxy_service_profile_binding_and_apply(
            "youtube",
            "profile-a",
        )

    preferences = local_proxy.load_local_proxy_preferences()
    assert preferences["service_profile_bindings"] == {}
    assert preferences["builtin_sites"]["youtube"] is False


def test_bound_subscription_refresh_rebuilds_route_without_replacing_main_node(
    monkeypatch,
    tmp_path,
):
    first = _subscription_node(1, _node("new dc", "new-dc.example.com"))
    calls = {"selected": [], "applied": 0, "main_refresh": 0}
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", tmp_path)
    monkeypatch.setattr(
        local_proxy,
        "local_proxy_service_bindings_for_profile",
        lambda _profile_id: ("youtube",),
    )
    monkeypatch.setattr(
        remote_proxy,
        "load_proxy_subscription_state",
        lambda: {
            "profiles": {
                "profile-b": {
                    "id": "profile-b",
                    "selected_node_key": "removed-node",
                }
            }
        },
    )
    monkeypatch.setattr(
        remote_proxy,
        "set_proxy_subscription_selected_node",
        lambda node, **kwargs: calls["selected"].append((node, kwargs)),
    )
    monkeypatch.setattr(
        local_proxy,
        "apply_local_proxy_routing_to_running",
        lambda: calls.__setitem__("applied", calls["applied"] + 1) or "已应用",
    )
    monkeypatch.setattr(
        local_proxy,
        "refresh_running_local_ai_proxy_from_subscription",
        lambda *_args, **_kwargs: calls.__setitem__(
            "main_refresh", calls["main_refresh"] + 1
        ),
    )

    message = local_proxy.refresh_running_local_service_routes_from_subscription(
        (first,),
        profile_id="profile-b",
    )

    assert calls["selected"] == [
        (first.node, {"profile_id": "profile-b"})
    ]
    assert calls["applied"] == 1
    assert calls["main_refresh"] == 0
    assert "YouTube" in message
    assert "首个独立节点" in message
