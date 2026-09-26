"""Synthetic remote config recovery: no live SSH, credentials or proxy changes."""
import base64
import json

import pytest

from core import proxy_routing as routes, remote_proxy
from test_ssh_service_routing import routed_ssh as _routed_ssh

routed_ssh = _routed_ssh


NODE = {"name": "synthetic", "type": "http", "server": "example.test", "port": 8080,
        "username": "synthetic-user", "password": "synthetic-password"}


def config(preferences, *, metadata=True):
    options = routes.config_options(routes.normalize_routes(preferences))
    if not metadata:
        options.pop("service_route_preferences")
    return remote_proxy.build_mihomo_config(NODE, **options)


@pytest.mark.parametrize("preferences", [
    {"service_route_modes": {"openai": "direct"}},
    {"builtin_sites": {"youtube": True}, "service_route_modes": {"youtube": "direct"}},
    {"builtin_sites": {"youtube": True}},
    {"custom_targets": [{"id": "custom-one", "value": "example.test"}]},
    {"custom_targets": [{"id": "custom-one", "value": "203.0.113.0/24"}],
     "service_route_modes": {"custom": "direct"}},
])
@pytest.mark.parametrize("metadata", [True, False])
def test_missing_authority_prevents_losing_direct_or_custom_routes(tmp_path, monkeypatch, preferences, metadata):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    old = config(preferences, metadata=metadata)
    with pytest.raises(RuntimeError, match="远端配置未覆盖"):
        routes.ssh_config_options("synthetic-host", old)
    assert not routes._host_path("synthetic-host").exists()


@pytest.mark.parametrize("mode", [{}, {"strict_privacy": True}, {"proxy_non_cn": True}])
def test_legacy_default_only_deployment_can_still_reload(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    old = remote_proxy.build_mihomo_config(NODE, **mode)
    assert routes.ssh_config_options("synthetic-host", old)["service_route_preferences"] == routes.normalize_routes({})


def test_new_default_only_deployment_without_local_record_can_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    old = config({})
    assert routes.ROUTE_SNAPSHOT_MARKER not in old
    assert routes.ssh_config_options("synthetic-host", old)["service_route_preferences"] == routes.normalize_routes({})
    # Empty snapshots from another compatible builder also have no lost intent.
    old += "\n" + routes.route_snapshot_marker({}, remote_proxy.yaml.safe_load(old)["rules"])
    assert routes.ssh_config_options("synthetic-host", old)["service_route_preferences"] == routes.normalize_routes({})


def test_metadata_only_manual_disabled_choice_requires_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    old = config({"builtin_sites": {"youtube": False}, "service_route_modes": {"youtube": "default"}})
    with pytest.raises(RuntimeError, match="远端配置未覆盖"):
        routes.ssh_config_options("synthetic-host", old)


def test_recovery_only_restores_current_host_local_authority(routed_ssh):
    configs, calls, first, second = routed_ssh
    preferences = routes.normalize_routes({
        "builtin_sites": {"youtube": True, "google": False},
        "service_profile_bindings": {"claude": "home", "google": "dc"},
        "service_node_pools": {"claude": [remote_proxy.proxy_node_key(first)]},
        "service_node_bindings": {"google": remote_proxy.proxy_node_key(second)},
        "service_route_modes": {"youtube": "direct", "openai": "default"},
        "custom_targets": [{"id": "offline", "value": "example.test", "enabled": False}],
    })
    configs["ssh-a"] = config(preferences)
    before = dict(configs)
    assert routes.load_ssh_route_editor_preferences("ssh-a")["_authority_missing"]
    recovered = routes.recover_ssh_routes("ssh-a")
    assert recovered == preferences == routes.load_ssh_routes("ssh-a")
    assert not routes.load_ssh_route_editor_preferences("ssh-a")["_authority_missing"]
    assert routes.load_ssh_route_editor_preferences("ssh-b")["_authority_missing"]
    assert configs == before and calls == []
    assert routes.ssh_config_options("ssh-a", configs["ssh-a"])["service_route_preferences"] == preferences
    snapshot = configs["ssh-a"].split(routes.ROUTE_SNAPSHOT_MARKER, 1)[1].splitlines()[0]
    decoded = base64.urlsafe_b64decode(snapshot).decode("utf-8")
    assert "synthetic-password" not in decoded and "synthetic-user" not in decoded and '"proxies"' not in decoded


def test_existing_local_record_never_overwritten_by_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    routes._save_ssh_routes("synthetic-host", {})
    monkeypatch.setattr(remote_proxy, "read_managed_ai_proxy_config", lambda *_: pytest.fail("must not connect"))
    before = routes._host_path("synthetic-host").read_bytes()
    with pytest.raises(ValueError, match="已有"):
        routes.recover_ssh_routes("synthetic-host")
    assert routes._host_path("synthetic-host").read_bytes() == before


@pytest.mark.parametrize("corruption", ["rules", "duplicate", "base64", "oversize", "extra", "schema",
                                       "payload", "duplicate-field", "legacy", "unmanaged"])
def test_invalid_recovery_never_writes_local_or_remote(routed_ssh, corruption):
    configs, calls, *_ = routed_ssh
    text = config({"service_route_modes": {"openai": "direct"}})
    marker = next(line for line in text.splitlines() if line.startswith(routes.ROUTE_SNAPSHOT_MARKER))
    if corruption == "rules":
        text = text.replace("DOMAIN-SUFFIX,openai.com,DIRECT", "DOMAIN-SUFFIX,openai.com,AI-PROXY")
    elif corruption == "duplicate":
        text += "\n" + marker
    elif corruption == "base64":
        text = text.replace(marker, routes.ROUTE_SNAPSHOT_MARKER + "!!!")
    elif corruption == "oversize":
        text = text.replace(marker, routes.ROUTE_SNAPSHOT_MARKER + "a" * 400000)
    elif corruption in ("extra", "schema", "payload", "duplicate-field"):
        payload = json.loads(base64.urlsafe_b64decode(marker[len(routes.ROUTE_SNAPSHOT_MARKER):]))
        if corruption == "extra":
            payload["routes"]["untrusted"] = "discarding unknown fields is not recovery"
        elif corruption == "schema":
            payload["routes"]["service_node_pools"] = []
        elif corruption == "payload":
            payload["routes"]["service_route_modes"]["openai"] = "default"
        raw = json.dumps(payload)
        if corruption == "duplicate-field":
            raw = raw.replace('"routes": {', '"routes": {}, "routes": {', 1)
        text = text.replace(marker, routes.ROUTE_SNAPSHOT_MARKER + base64.urlsafe_b64encode(raw.encode()).decode())
    elif corruption == "legacy":
        text = text.replace(marker, "")
    else:
        text = text.replace(remote_proxy.AI_PROXY_CONFIG_MARKER, "")
    configs["ssh-a"] = text
    with pytest.raises(ValueError):
        routes.recover_ssh_routes("ssh-a")
    assert not routes._host_path("ssh-a").exists()
    assert configs["ssh-a"] == text and not calls


def test_failed_first_apply_does_not_create_false_empty_authority(monkeypatch, routed_ssh):
    configs, calls, *_ = routed_ssh
    configs["ssh-a"] = config({"service_route_modes": {"openai": "direct"}}, metadata=False)
    before = configs["ssh-a"]
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic reload failure")
    monkeypatch.setattr(remote_proxy, "reload_ai_proxy", fail)
    with pytest.raises(RuntimeError, match="synthetic reload failure"):
        routes.apply_ssh_routes("ssh-a", {})
    assert not routes._host_path("ssh-a").exists()
    with pytest.raises(RuntimeError, match="远端配置未覆盖"):
        routes.ssh_config_options("ssh-a", before)
    assert configs["ssh-a"] == before and not calls


@pytest.mark.parametrize("fail_write", [False, True])
def test_metadata_only_change_never_reloads_kernel(monkeypatch, routed_ssh, fail_write):
    configs, calls, *_ = routed_ssh
    configs["ssh-a"] = remote_proxy.build_mihomo_config(NODE, health_checked_group=True,
                                                       resilient_transport=True, mainland_dns=True)
    before = configs["ssh-a"]
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status",
                        lambda *args, **kwargs: pytest.fail("metadata must not reload the live kernel"))
    if fail_write:
        original = remote_proxy.ssh_manager.write_remote_file
        attempts = []
        def fail_once(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError("synthetic write failure")
            return original(*args, **kwargs)
        monkeypatch.setattr(remote_proxy.ssh_manager, "write_remote_file", fail_once)
        with pytest.raises(RuntimeError, match="运行线路未改变"):
            routes.apply_ssh_routes("ssh-a", {"service_route_modes": {"openai": "default"}})
        assert configs["ssh-a"] == before
        assert not routes._host_path("ssh-a").exists()
    else:
        message = routes.apply_ssh_routes("ssh-a", {"service_route_modes": {"openai": "default"}})
        assert "无需热更新" in message and "恢复记录" in message
        assert routes.recover_routes_from_config(configs["ssh-a"])["service_route_modes"] == {"openai": "default"}
