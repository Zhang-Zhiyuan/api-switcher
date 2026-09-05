import copy
import json
from types import SimpleNamespace

import pytest

from core import local_proxy, proxy_routing, remote_proxy
from test_local_proxy_service_routing import _node, _patch_profiles


@pytest.fixture
def routed_ssh(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    main = _node("默认", "main.example.com")
    first, second = _node("家宽", "home.example.com"), _node("机房", "dc.example.com")
    _patch_profiles(monkeypatch, {"home": (first,), "dc": (second,)})
    configs = {name: remote_proxy.build_mihomo_config(main) for name in ("ssh-a", "ssh-b")}
    calls = []
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda name: (None, name))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/test")
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: SimpleNamespace(running=True))
    monkeypatch.setattr(remote_proxy.ssh_manager, "read_remote_file", lambda client, _path: configs[client])
    def write(client, path, content, **_kwargs):
        configs[client] = content
        calls.append(("write", client, path))
    monkeypatch.setattr(remote_proxy.ssh_manager, "write_remote_file", write)
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", lambda *_args, **_kwargs: (0, "ok", ""))
    monkeypatch.setattr(remote_proxy, "_repair_remote_proxy_integrations", lambda *_args: "")
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda *_args, **_kwargs: calls.append(("select",)))
    return configs, calls, first, second


def test_ssh_servers_keep_independent_bindings_and_main_nodes(routed_ssh):
    configs, calls, first, second = routed_ssh
    before_b = configs["ssh-b"]
    routes = {"service_profile_bindings": {"openai": "home", "youtube": "dc"},
              "service_node_bindings": {"openai": remote_proxy.proxy_node_key(first),
                                        "youtube": remote_proxy.proxy_node_key(second)},
              "builtin_sites": {"youtube": True}}
    proxy_routing.apply_ssh_routes("ssh-a", routes, expected={})
    parsed = remote_proxy.yaml.safe_load(configs["ssh-a"])
    assert parsed["proxies"][0]["server"] == "main.example.com"
    assert len(parsed["proxy-groups"]) == 3
    assert configs["ssh-b"] == before_b
    assert proxy_routing.load_ssh_routes("ssh-b")["service_profile_bindings"] == {}
    assert proxy_routing.load_ssh_routes("ssh-a")["service_node_bindings"] == routes["service_node_bindings"]
    assert not any(call[0] == "select" for call in calls)


def test_normal_ssh_reload_preserves_persisted_routes(routed_ssh):
    configs, _calls, _first, _second = routed_ssh
    proxy_routing.apply_ssh_routes("ssh-a", {"service_profile_bindings": {"claude": "home"}})
    remote_proxy.reload_ai_proxy("ssh-a", remote_proxy.format_proxy_node(_node("新默认", "new-main.example.com")))
    parsed = remote_proxy.yaml.safe_load(configs["ssh-a"])
    assert parsed["proxies"][0]["server"] == "new-main.example.com"
    assert f"DOMAIN-SUFFIX,anthropic.com,{local_proxy._subscription_route_group_name('home', 'claude')}" in parsed["rules"]


def test_bound_subscription_refresh_never_promotes_it_to_ssh_default(routed_ssh):
    configs, _calls, first, _second = routed_ssh
    proxy_routing.apply_ssh_routes("ssh-a", {"service_profile_bindings": {"claude": "home"}})
    remote_proxy.refresh_running_ai_proxy_from_subscription(
        "ssh-a", [remote_proxy.ProxySubscriptionNode(index=1, node=first)], profile_id="home",
    )
    assert remote_proxy.yaml.safe_load(configs["ssh-a"])["proxies"][0]["server"] == "main.example.com"


def test_failed_ssh_reload_restores_config_and_does_not_save_bindings(monkeypatch, routed_ssh):
    configs, _calls, _first, _second = routed_ssh
    original = configs["ssh-a"]
    attempts = []
    def reload(*_args, **_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise TimeoutError("controller timed out")
        return (0, "ok", "")
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", reload)
    with pytest.raises(RuntimeError, match="已强制重载旧配置"):
        proxy_routing.apply_ssh_routes("ssh-a", {"service_profile_bindings": {"claude": "home"}})
    assert configs["ssh-a"] == original
    assert len(attempts) == 2
    assert proxy_routing.load_ssh_routes("ssh-a")["service_profile_bindings"] == {}


def test_failed_ssh_preferences_write_never_changes_running_routes(monkeypatch, routed_ssh):
    configs, calls, _first, _second = routed_ssh
    before = remote_proxy.yaml.safe_load(configs["ssh-a"])
    monkeypatch.setattr(proxy_routing, "_save_ssh_routes", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(RuntimeError, match="保存线路失败"):
        proxy_routing.apply_ssh_routes("ssh-a", {"service_profile_bindings": {"claude": "home"}})
    after = remote_proxy.yaml.safe_load(configs["ssh-a"])
    assert after["rules"] == before["rules"]
    assert len(after["proxy-groups"]) == 1
    assert not calls


def test_missing_fixed_node_rejected_before_any_ssh_write(routed_ssh):
    configs, calls, _first, _second = routed_ssh
    original = copy.deepcopy(configs)
    with pytest.raises(RuntimeError, match="固定节点已失效"):
        proxy_routing.apply_ssh_routes("ssh-a", {"service_profile_bindings": {"claude": "home"},
                                                "service_node_bindings": {"claude": "f" * 64}})
    assert configs == original
    assert not calls


def test_ssh_binding_blocks_profile_deletion_and_corruption_fails_closed(routed_ssh):
    proxy_routing.apply_ssh_routes("ssh-a", {"service_profile_bindings": {"claude": "home"}})
    with pytest.raises(ValueError, match="SSH 目标分流"):
        remote_proxy.delete_proxy_subscription_profile("home")
    path = proxy_routing._host_path("ssh-a")
    path.write_text(json.dumps({"ssh_name": "ssh-a", "routes": {"service_profile_bindings": []}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="无法确认订阅引用"):
        remote_proxy.delete_proxy_subscription_profile("home")


def test_offline_ssh_routes_saved_for_next_deployment(monkeypatch, routed_ssh):
    _configs, calls, _first, _second = routed_ssh
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: SimpleNamespace(running=False))
    message = proxy_routing.apply_ssh_routes("ssh-a", {"service_profile_bindings": {"claude": "home"}})
    assert "下次部署生效" in message
    assert proxy_routing.load_ssh_routes("ssh-a")["service_profile_bindings"] == {"claude": "home"}
    assert not calls


def test_ssh_deployment_preflight_uses_saved_service_routes(monkeypatch, routed_ssh):
    preferences = proxy_routing.normalize_routes({"service_profile_bindings": {"openai": "home", "claude": "home"}})
    proxy_routing._save_ssh_routes("ssh-a", preferences)
    captured = []
    monkeypatch.setattr(remote_proxy, "probe_ai_proxy_candidate_isolated",
                        lambda _name, _text, **kwargs: captured.append(kwargs) or "validated")
    remote_proxy._probe_ai_proxy_candidate_with_core_bootstrap("ssh-a", "default")
    assert captured[0]["routing_preferences"] == preferences


def test_isolated_preflight_template_contains_the_same_service_rules(routed_ssh):
    preferences = {"service_profile_bindings": {"openai": "home", "youtube": "dc"},
                   "builtin_sites": {"youtube": True}}
    template = remote_proxy._build_isolated_candidate_config(
        _node("默认机房", "main.example.com"), routing_preferences=preferences,
    )
    assert "mixed-port: __API_SWITCHER_CANDIDATE_PORT__" in template
    assert f"DOMAIN-SUFFIX,openai.com,{local_proxy._subscription_route_group_name('home')}" in template
    assert f"DOMAIN-SUFFIX,youtube.com,{local_proxy._subscription_route_group_name('dc', 'youtube')}" in template


def test_explicit_openai_route_allows_preflight_of_hk_default_used_for_other_targets(monkeypatch, routed_ssh):
    monkeypatch.setattr(remote_proxy, "_probe_ai_proxy_candidate_isolated", lambda *_args, **_kwargs: ())
    message = remote_proxy.probe_ai_proxy_candidate_isolated(
        "ssh-a", remote_proxy.format_proxy_node(_node("香港 · 默认流媒体", "hk.example.com")),
        routing_preferences={"service_profile_bindings": {"openai": "home"}},
    )
    assert "隔离候选" in message
    with pytest.raises(RuntimeError, match="香港节点仅允许手动选择"):
        remote_proxy.probe_ai_proxy_candidate_isolated(
            "ssh-a", remote_proxy.format_proxy_node(_node("香港 · 默认流媒体", "hk.example.com")),
        )
