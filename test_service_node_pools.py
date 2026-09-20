"""Explicit candidate authority stays bounded across save, refresh and rollback."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import local_proxy, proxy_routing, remote_proxy
from core.subscription_routing_policy import suggest_tagged_routes
from test_local_proxy_service_routing import _patch_profiles


def _node(index, **extra):
    return {"name": f"candidate-{index}", "type": "http", "server": f"node-{index}.example.test", "port": 8080, **extra}


def _key(node):
    return remote_proxy.proxy_node_key(node)


def _routes(keys, service="claude"):
    return {"service_profile_bindings": {service: "home"}, "service_node_pools": {service: list(keys)}}


@pytest.fixture
def isolated_routes(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "local-preferences.json")
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    local_proxy.clear_local_proxy_preferences_cache()
    yield tmp_path
    local_proxy.clear_local_proxy_preferences_cache()


def test_legacy_absence_and_deep_snapshot_copy():
    assert proxy_routing.normalize_routes({})["service_node_pools"] == {}
    source = _routes(["first", "second"])
    snapshot = proxy_routing.route_snapshot(source)
    snapshot["service_node_pools"]["claude"].reverse()
    assert source["service_node_pools"]["claude"] == ["first", "second"]


@pytest.mark.parametrize("raw", [None, [], "candidate", 1, True])
def test_non_object_pool_authority_rejected(raw):
    with pytest.raises(ValueError, match="候选节点池"):
        proxy_routing.normalize_routes({"service_node_pools": raw})


@pytest.mark.parametrize("keys", [None, [], (), {}, "one", [""], [None], [1], [True],
                                  ["x" * 129], ["line\nbreak"], ["one", "one"], ["one", " one "],
                                  [str(index) for index in range(17)]])
def test_invalid_candidate_lists_do_not_turn_into_full_auto(keys):
    source = {"service_profile_bindings": {"claude": "home"}, "service_node_pools": {"claude": keys}}
    before = copy.deepcopy(source)
    with pytest.raises(ValueError, match="候选节点"):
        proxy_routing.normalize_routes(source)
    assert source == before


def test_pool_requires_subscription_and_excludes_pin_and_default():
    with pytest.raises(ValueError, match="没有对应订阅"):
        proxy_routing.normalize_routes({"service_node_pools": {"claude": ["one"]}})
    for field, value in (("service_node_bindings", "pin"), ("service_route_modes", "default")):
        with pytest.raises(ValueError, match="冲突"):
            proxy_routing.normalize_routes({**_routes(["one"]), field: {"claude": value}})


def test_custom_pool_is_removed_only_when_target_is_deliberately_deleted():
    source = {"custom_targets": [{"id": "kept", "value": "example.test"}],
              "service_profile_bindings": {"custom:kept": "home", "custom:removed": "home"},
              "service_node_pools": {"custom:kept": ["one"], "custom:removed": ["two"]}}
    assert proxy_routing.normalize_routes(source)["service_node_pools"] == {"custom:kept": ["one"]}


def test_local_save_load_unrelated_write_keep_order_and_missing_keys(isolated_routes):
    source = _routes(["missing-cache-key", "first"])
    local_proxy.save_local_proxy_preferences(**source)
    local_proxy.clear_local_proxy_preferences_cache()
    assert local_proxy._load_local_proxy_routing_preferences_strict()["service_node_pools"] == source["service_node_pools"]
    local_proxy.save_local_proxy_preferences(keep_running_on_exit=False)
    saved = json.loads(local_proxy.LOCAL_PROXY_PREFS_PATH.read_text(encoding="utf-8"))
    assert saved["service_node_pools"] == source["service_node_pools"]


@pytest.mark.parametrize("raw", [None, [], {"claude": []}, {"claude": [None]}])
def test_invalid_update_preserves_valid_file(isolated_routes, raw):
    local_proxy.save_local_proxy_preferences(**_routes(["first"]))
    before = local_proxy.LOCAL_PROXY_PREFS_PATH.read_bytes()
    with pytest.raises(ValueError, match="候选节点"):
        local_proxy.save_local_proxy_preferences(service_node_pools=raw)
    assert local_proxy.LOCAL_PROXY_PREFS_PATH.read_bytes() == before


def test_corrupt_authority_cannot_be_erased_by_unrelated_save(isolated_routes):
    content = '{"service_profile_bindings":{"claude":"home"},"service_node_pools":{"claude":[]}}'
    local_proxy.LOCAL_PROXY_PREFS_PATH.write_text(content, encoding="utf-8")
    for operation in (local_proxy._load_local_proxy_routing_preferences_strict,
                      lambda: local_proxy.save_local_proxy_preferences(keep_running_on_exit=False)):
        with pytest.raises(ValueError, match="候选节点池"):
            operation()
    assert local_proxy.LOCAL_PROXY_PREFS_PATH.read_text(encoding="utf-8") == content


def test_ssh_metadata_roundtrip_and_host_isolation_without_network(isolated_routes, monkeypatch):
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: pytest.fail("metadata cannot connect"))
    proxy_routing._save_ssh_routes("host-a", _routes(["second", "first"]))
    proxy_routing._save_ssh_routes("host-b", _routes(["other"]))
    assert proxy_routing.load_ssh_routes("host-a")["service_node_pools"] == {"claude": ["second", "first"]}
    assert proxy_routing.load_ssh_routes("host-b")["service_node_pools"] == {"claude": ["other"]}


def test_candidates_use_only_selected_keys_in_priority_order_without_quality_filter(monkeypatch):
    nodes = [_node(index) for index in range(5)]
    profiles = _patch_profiles(monkeypatch, {"home": tuple(nodes), "other": (_node(100),)})
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda *_args: pytest.fail("manual candidates do not use stale quality filters"))
    primary, fallback = local_proxy._selected_subscription_route_pool(
        profiles["home"], ai_sensitive=True, node_keys=[_key(nodes[3]), _key(nodes[1])],
    )
    assert primary == nodes[3] and fallback == (nodes[1],)


def test_partial_missing_candidates_do_not_expand_and_warn(monkeypatch):
    nodes = [_node(index) for index in range(4)]
    _patch_profiles(monkeypatch, {"home": tuple(nodes)})
    routes = _routes(["missing", _key(nodes[2]), _key(nodes[1])])
    warnings = proxy_routing.node_pool_warnings(routes)
    assert len(warnings) == 1 and "1 个自选候选节点缺失" in warnings[0]
    group = proxy_routing.config_options(routes)["additional_proxy_groups"][0]
    assert group["proxy_node"] == nodes[2] and group["fallback_proxy_nodes"] == (nodes[1],)
    assert routes["service_node_pools"]["claude"][0] == "missing"


def test_alias_connections_deduplicate_without_dropping_other_selected_connections(monkeypatch):
    first, other = _node(1), _node(2)
    alias = {**first, "name": "renamed alias"}
    _patch_profiles(monkeypatch, {"home": (first, alias, other)})
    group = proxy_routing.config_options(_routes([_key(alias), _key(first), _key(other)]))["additional_proxy_groups"][0]
    assert group["proxy_node"] == alias
    assert group["fallback_proxy_nodes"] == (other,)
    assert "1 个自选候选节点实际为重复连接" in proxy_routing.node_pool_warnings(
        _routes([_key(alias), _key(first), _key(other)]),
    )[0]


def test_dependent_candidate_is_unavailable_not_permission_to_expand(monkeypatch):
    dependent, good, other = _node(1, **{"dialer-proxy": "omitted"}), _node(2), _node(3)
    _patch_profiles(monkeypatch, {"home": (dependent, good, other)})
    routes = _routes([_key(dependent), _key(good)])
    group = proxy_routing.config_options(routes)["additional_proxy_groups"][0]
    assert group["proxy_node"] == good and not group["fallback_proxy_nodes"]
    assert "1 个自选候选节点缺失或无法独立运行" in proxy_routing.node_pool_warnings(routes)[0]


@pytest.mark.parametrize("service", ["openai", "claude", "google_ai"])
def test_sixteen_manual_ai_candidates_keep_service_health_contract_and_strict_recognition(monkeypatch, service):
    nodes = [_node(index) for index in range(20)]
    _patch_profiles(monkeypatch, {"home": tuple(nodes)})
    keys = [_key(node) for node in nodes[17:1:-1]]
    options = proxy_routing.config_options(_routes(keys, service))
    config = remote_proxy.build_mihomo_config(_node(100), strict_privacy=True, **options)
    parsed = remote_proxy.yaml.safe_load(config)
    group = parsed["proxy-groups"][1]
    assert group["type"] == "fallback" and len(group["proxies"]) == 16
    health_url, expected_status = local_proxy._service_route_health_contract([service])
    assert (group["url"], group["expected-status"]) == (health_url, expected_status)
    by_name = {node["name"]: node for node in parsed["proxies"]}
    assert [by_name[name]["server"] for name in group["proxies"]] == [node["server"] for node in nodes[17:1:-1]]
    assert remote_proxy._managed_config_strict_privacy_enabled(config)


def test_pool_priority_changes_group_identity_but_old_full_auto_identity_is_preserved():
    assert local_proxy._subscription_route_group_name("home", "claude") == local_proxy._subscription_route_group_name("home", "claude", node_keys=[])
    first = local_proxy._subscription_route_group_name("home", "claude", node_keys=["first", "second"])
    second = local_proxy._subscription_route_group_name("home", "claude", node_keys=["second", "first"])
    assert first != second
    assert first != local_proxy._subscription_route_group_name("home", "claude", "first")


def test_pool_and_automatic_groups_load_a_single_subscription_snapshot(monkeypatch):
    nodes = [_node(index) for index in range(4)]
    profiles = _patch_profiles(monkeypatch, {"home": tuple(nodes)})
    old = remote_proxy.load_cached_proxy_subscription(profiles["home"])
    loader = Mock(side_effect=[old, AssertionError("one build must read one snapshot")])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", loader)
    source = {**_routes([_key(nodes[2]), _key(nodes[1])]), "service_profile_bindings": {"claude": "home", "openai": "home"}}
    assert len(proxy_routing.config_options(source)["additional_proxy_groups"]) == 2
    assert loader.call_count == 1


def test_disabled_service_keeps_pool_authority_without_emitting_live_group(monkeypatch):
    nodes = [_node(1), _node(2)]
    _patch_profiles(monkeypatch, {"home": tuple(nodes)})
    source = {**_routes([_key(nodes[1])], "youtube"), "builtin_sites": {"youtube": False}}
    assert proxy_routing.validate_routes(source)["service_node_pools"] == source["service_node_pools"]
    assert proxy_routing.config_options(source)["additional_proxy_groups"] == ()


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_missing_entire_pool_aborts_before_live_or_authority_mutation(isolated_routes, monkeypatch, scope):
    _patch_profiles(monkeypatch, {"home": (_node(1),)})
    source = _routes(["missing"])
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", lambda: pytest.fail("no live reload"))
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: pytest.fail("no SSH connection"))
    if scope == "local":
        local_proxy.save_local_proxy_preferences(service_route_modes={"claude": "default"})
        before = local_proxy.LOCAL_PROXY_PREFS_PATH.read_bytes()
        with pytest.raises(RuntimeError, match="全部缺失"):
            local_proxy.set_local_proxy_service_routes_and_apply(source)
        assert local_proxy.LOCAL_PROXY_PREFS_PATH.read_bytes() == before
    else:
        proxy_routing._save_ssh_routes("host-a", {"service_route_modes": {"claude": "default"}})
        before = proxy_routing._host_path("host-a").read_bytes()
        with pytest.raises(RuntimeError, match="全部缺失"):
            proxy_routing.apply_ssh_routes("host-a", source)
        assert proxy_routing._host_path("host-a").read_bytes() == before


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_apply_failure_restores_exact_candidate_order(isolated_routes, monkeypatch, scope):
    nodes = [_node(1), _node(2)]
    _patch_profiles(monkeypatch, {"home": tuple(nodes)})
    before = proxy_routing.normalize_routes(_routes([_key(nodes[1]), _key(nodes[0])]))
    after = _routes([_key(nodes[0])])
    fail = Mock(side_effect=OSError("synthetic reload failure"))
    if scope == "local":
        local_proxy.save_local_proxy_preferences(**before)
        monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", fail)
        with pytest.raises(RuntimeError, match="已恢复原绑定"):
            local_proxy.set_local_proxy_service_routes_and_apply(after, expected=before)
        assert proxy_routing.route_snapshot(local_proxy.load_local_proxy_preferences()) == before
    else:
        proxy_routing._save_ssh_routes("host-a", before)
        monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: SimpleNamespace(running=True))
        monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_args: _node(100))
        monkeypatch.setattr(remote_proxy, "reload_ai_proxy", fail)
        with pytest.raises(RuntimeError, match="已恢复原绑定"):
            proxy_routing.apply_ssh_routes("host-a", after, expected=before)
        assert proxy_routing.load_ssh_routes("host-a") == before


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_stale_pool_priority_is_part_of_compare_and_swap(isolated_routes, monkeypatch, scope):
    old, current = _routes(["one", "two"]), _routes(["two", "one"])
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: pytest.fail("stale editor cannot connect"))
    if scope == "local":
        local_proxy.save_local_proxy_preferences(**current)
        with pytest.raises(RuntimeError, match="其他操作修改"):
            local_proxy.set_local_proxy_service_routes_and_apply(old, expected=old)
    else:
        proxy_routing._save_ssh_routes("host-a", current)
        with pytest.raises(RuntimeError, match="其他操作修改"):
            proxy_routing.apply_ssh_routes("host-a", old, expected=old)


def test_subscription_removal_and_rollback_include_candidate_metadata(isolated_routes, monkeypatch):
    original = _routes(["one", "two"])
    local_proxy.save_local_proxy_preferences(**original)
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", Mock(side_effect=OSError("synthetic")))
    with pytest.raises(RuntimeError, match="已恢复原绑定"):
        local_proxy.clear_local_proxy_service_profile_bindings_and_apply("home")
    assert local_proxy.load_local_proxy_preferences()["service_node_pools"] == original["service_node_pools"]
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", lambda: "synthetic applied")
    local_proxy.clear_local_proxy_service_profile_bindings_and_apply("home")
    assert local_proxy.load_local_proxy_preferences()["service_node_pools"] == {}


def test_switch_to_default_removes_pool_without_destroying_it_on_failure(isolated_routes, monkeypatch):
    original = _routes(["one", "two"])
    local_proxy.save_local_proxy_preferences(**original)
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", Mock(side_effect=OSError("synthetic")))
    with pytest.raises(RuntimeError, match="已恢复原绑定"):
        local_proxy.set_local_proxy_service_profile_binding_and_apply("claude", "")
    assert local_proxy.load_local_proxy_preferences()["service_node_pools"] == original["service_node_pools"]
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", lambda: "synthetic applied")
    local_proxy.set_local_proxy_service_profile_binding_and_apply("claude", "")
    assert local_proxy.load_local_proxy_preferences()["service_node_pools"] == {}
    assert local_proxy.load_local_proxy_preferences()["service_route_modes"] == {"claude": "default"}


def test_local_partial_refresh_warns_and_never_changes_global_selection(isolated_routes, monkeypatch):
    nodes = [_node(1), _node(2)]
    _patch_profiles(monkeypatch, {"home": tuple(nodes)})
    local_proxy.save_local_proxy_preferences(**_routes(["missing", _key(nodes[1])]))
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda *_args, **_kwargs: pytest.fail("explicit pools cannot change global selection"))
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: False)
    message = local_proxy.refresh_running_local_service_routes_from_subscription(
        [remote_proxy.ProxySubscriptionNode(1, nodes[1])], profile_id="home",
    )
    assert "1 个自选候选节点缺失" in message and "不会加入未选节点" in message


def test_local_all_missing_refresh_does_not_change_saved_primary_or_live_config(isolated_routes, monkeypatch):
    nodes = [_node(1), _node(2)]
    _patch_profiles(monkeypatch, {"home": tuple(nodes)})
    local_proxy.save_local_proxy_preferences(**_routes(["missing"]))
    before = local_proxy.LOCAL_PROXY_PREFS_PATH.read_bytes()
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda *_args, **_kwargs: pytest.fail("cannot change global selection"))
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", lambda: pytest.fail("cannot reload live config"))
    with pytest.raises(RuntimeError, match="全部缺失"):
        local_proxy.refresh_running_local_service_routes_from_subscription(
            [remote_proxy.ProxySubscriptionNode(1, nodes[1])], profile_id="home",
        )
    assert local_proxy.LOCAL_PROXY_PREFS_PATH.read_bytes() == before


def test_draft_suggestions_do_not_overwrite_pool_even_when_malformed_or_stale():
    catalog = [{"id": "home", "network_type": "residential", "nodes": [{"key": "available"}]}]
    source = {"service_node_pools": {"claude": ["missing"]}}
    draft, _notices = suggest_tagged_routes(source, catalog)
    assert "claude" not in draft["service_profile_bindings"]
    assert draft["service_node_pools"] == source["service_node_pools"]


def test_reenabling_existing_binding_preserves_its_candidate_order(isolated_routes, monkeypatch):
    nodes = [_node(1), _node(2)]
    _patch_profiles(monkeypatch, {"home": tuple(nodes)})
    before = _routes([_key(nodes[1]), _key(nodes[0])], "youtube")
    local_proxy.save_local_proxy_preferences(**before, builtin_sites={"youtube": False})
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", lambda: "synthetic applied")
    local_proxy.set_local_proxy_service_profile_binding_and_apply("youtube", "home")
    assert local_proxy.load_local_proxy_preferences()["service_node_pools"] == before["service_node_pools"]


@pytest.fixture
def synthetic_ssh(isolated_routes, monkeypatch):
    nodes = [_node(1), _node(2), _node(3)]
    _patch_profiles(monkeypatch, {"home": tuple(nodes)})
    configs = {"host-a": remote_proxy.build_mihomo_config(_node(100), strict_privacy=True)}
    writes = []
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: SimpleNamespace(running=True))
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda host: (None, host))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/synthetic")
    monkeypatch.setattr(remote_proxy.ssh_manager, "read_remote_file", lambda client, _path: configs[client])

    def write(client, _path, content, **_kwargs):
        configs[client] = content
        writes.append(content)

    monkeypatch.setattr(remote_proxy.ssh_manager, "write_remote_file", write)
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", lambda *_args, **_kwargs: (0, "ok", ""))
    monkeypatch.setattr(remote_proxy, "_repair_remote_proxy_integrations", lambda *_args: pytest.fail("routing cannot rewrite shell integrations"))
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda *_args, **_kwargs: pytest.fail("routing cannot change global selection"))
    proxy_routing.apply_ssh_routes("host-a", _routes([_key(nodes[1]), _key(nodes[0])]))
    return configs, writes, nodes


def test_ssh_partial_refresh_keeps_selected_subset_and_reports_missing(monkeypatch, synthetic_ssh):
    configs, writes, nodes = synthetic_ssh
    _patch_profiles(monkeypatch, {"home": (nodes[0], nodes[2])})
    message = remote_proxy.refresh_running_ai_proxy_from_subscription(
        "host-a", [remote_proxy.ProxySubscriptionNode(1, nodes[0])], profile_id="home",
    )
    assert "1 个自选候选节点缺失" in message and "不会加入未选节点" in message
    parsed = remote_proxy.yaml.safe_load(configs["host-a"])
    assert len(writes) == 2
    assert [node["server"] for node in parsed["proxies"]] == [_node(100)["server"], nodes[0]["server"]]
    assert remote_proxy._managed_config_strict_privacy_enabled(configs["host-a"])
    assert proxy_routing.load_ssh_routes("host-a")["service_node_pools"]["claude"] == [_key(nodes[1]), _key(nodes[0])]


def test_ssh_all_missing_refresh_leaves_original_live_bytes(monkeypatch, synthetic_ssh):
    configs, writes, nodes = synthetic_ssh
    before = configs["host-a"]
    _patch_profiles(monkeypatch, {"home": (nodes[2],)})
    with pytest.raises(RuntimeError, match="全部缺失"):
        remote_proxy.refresh_running_ai_proxy_from_subscription(
            "host-a", [remote_proxy.ProxySubscriptionNode(1, nodes[2])], profile_id="home",
        )
    assert configs["host-a"] == before and len(writes) == 1


def test_ssh_failed_reload_restores_live_bytes_and_candidate_authority(monkeypatch, synthetic_ssh):
    configs, _writes, nodes = synthetic_ssh
    before = configs["host-a"]
    expected = proxy_routing.load_ssh_routes("host-a")
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", Mock(side_effect=[(1, "", "synthetic reload error"), (0, "ok", "")]))
    with pytest.raises(RuntimeError, match="已恢复原绑定"):
        proxy_routing.apply_ssh_routes("host-a", _routes([_key(nodes[2])]), expected=expected)
    assert configs["host-a"] == before
    assert proxy_routing.load_ssh_routes("host-a") == expected


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_diagnostics_distinguishes_selected_pool_from_whole_subscription(isolated_routes, monkeypatch, scope):
    from core import proxy_route_diagnostics

    source = proxy_routing.normalize_routes(_routes(["first", "second"]))
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda: [{"id": "home", "name": "synthetic source"}])
    if scope == "local":
        monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", lambda: source)
        monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
        monkeypatch.setattr(local_proxy, "_read_pid", lambda: None)
        snapshot = proxy_route_diagnostics.load_snapshot()
    else:
        proxy_routing._save_ssh_routes("host-a", source)
        monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: SimpleNamespace(running=False))
        snapshot = proxy_route_diagnostics.load_snapshot("host-a")
    assert list(snapshot.groups.values()) == ["synthetic source · 自选候选 2 个 · 自动切换"]


def test_ssh_batch_refresh_reports_partial_gap_in_other_bound_profile(monkeypatch, synthetic_ssh):
    _configs, _writes, nodes = synthetic_ssh
    _patch_profiles(monkeypatch, {"home": tuple(nodes), "other": (_node(5), _node(6))})
    routes = {"service_profile_bindings": {"claude": "home", "openai": "other"},
              "service_node_pools": {"claude": [_key(nodes[1])], "openai": [_key(_node(5)), _key(_node(6))]}}
    proxy_routing.apply_ssh_routes("host-a", routes)
    _patch_profiles(monkeypatch, {"home": tuple(nodes), "other": (_node(6),)})
    message = remote_proxy.refresh_running_ai_proxy_from_subscription(
        "host-a", [remote_proxy.ProxySubscriptionNode(1, nodes[0])], profile_id="home",
    )
    assert "OpenAI / Codex：1 个自选候选节点缺失" in message
