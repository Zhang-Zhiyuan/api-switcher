"""Timer provenance is checked again under the existing short commit lock."""
from unittest.mock import Mock

import pytest

from core import local_proxy, remote_proxy


def _node(index):
    return {"name": f"synthetic-{index}", "type": "http", "server": f"node-{index}.example.test", "port": 8080}


@pytest.fixture
def isolated_refresh(monkeypatch):
    original, replacement, candidate = _node(1), _node(2), _node(3)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: original)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {"active_profile_id": "home"})
    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy", lambda *_args, **_kwargs: pytest.fail("stale timer cannot write live config"))
    monkeypatch.setattr(local_proxy, "_select_stable_automatic_local_candidate", lambda *_args, **_kwargs: pytest.fail("stale origin must not launch probes"))
    return original, replacement, candidate


@pytest.mark.parametrize("changed", ["node", "profile"])
def test_origin_changed_before_refresh_is_not_accepted_as_new_baseline(monkeypatch, isolated_refresh, changed):
    original, replacement, _candidate = isolated_refresh
    if changed == "node":
        monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: replacement)
    else:
        monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {"active_profile_id": "other"})
    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy_verified", lambda *_args, **_kwargs: pytest.fail("must stop before commit"))
    result = local_proxy.refresh_running_local_ai_proxy_from_subscription(
        [remote_proxy.ProxySubscriptionNode(1, original)], profile_id="home",
        expected_current_key=remote_proxy.proxy_node_key(original),
    )
    assert "默认节点或订阅分组已变化" in result


@pytest.mark.parametrize("changed", ["node", "profile"])
def test_exact_current_path_rechecks_provenance_inside_serialized_commit(monkeypatch, isolated_refresh, changed):
    original, replacement, _candidate = isolated_refresh
    if changed == "node":
        monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", Mock(side_effect=[original, replacement]))
    else:
        monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", Mock(side_effect=[
            {"active_profile_id": "home"}, {"active_profile_id": "other"},
        ]))
    monkeypatch.setattr(local_proxy, "_local_proxy_fallback_nodes", lambda *_args, **_kwargs: pytest.fail("stale commit must stop before pool work"))
    result = local_proxy.refresh_running_local_ai_proxy_from_subscription(
        [remote_proxy.ProxySubscriptionNode(1, original)], profile_id="home",
        expected_current_key=remote_proxy.proxy_node_key(original),
    )
    assert "默认节点或订阅分组已变化" in result


@pytest.mark.parametrize("changed", ["node", "profile"])
def test_after_isolated_probe_commit_retains_original_timer_origin(monkeypatch, isolated_refresh, changed):
    original, replacement, candidate = isolated_refresh
    monkeypatch.setattr(local_proxy, "_select_stable_automatic_local_candidate", lambda *_args, **_kwargs: (
        remote_proxy.ProxySubscriptionNode(1, candidate), "synthetic proof", (), {},
    ))
    monkeypatch.setattr(local_proxy, "_prevalidated_local_candidate_matches", lambda *_args: True)
    if changed == "node":
        monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", Mock(side_effect=[original, original, replacement]))
    else:
        monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", Mock(side_effect=[
            {"active_profile_id": "home"}, {"active_profile_id": "other"},
        ]))
    monkeypatch.setattr(local_proxy, "_local_proxy_fallback_nodes", lambda *_args, **_kwargs: pytest.fail("stale commit must stop before pool work"))
    result = local_proxy.refresh_running_local_ai_proxy_from_subscription(
        [remote_proxy.ProxySubscriptionNode(1, candidate)], profile_id="home",
        expected_current_key=remote_proxy.proxy_node_key(original),
    )
    assert "默认节点或订阅分组已变化" in result


@pytest.mark.parametrize("guarded", [False, True])
def test_exact_current_forwards_optional_guard_without_forcing_deep_probe(monkeypatch, isolated_refresh, guarded):
    original, _replacement, _candidate = isolated_refresh
    commit = Mock(return_value="synthetic committed")
    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy_verified", commit)
    kwargs = {"expected_current_key": remote_proxy.proxy_node_key(original)} if guarded else {}
    result = local_proxy.refresh_running_local_ai_proxy_from_subscription(
        [remote_proxy.ProxySubscriptionNode(1, original)], profile_id="home", **kwargs,
    )
    assert result == "synthetic committed"
    assert "automatic_update" not in commit.call_args.kwargs
    assert ("_expected_current_key" in commit.call_args.kwargs) is guarded


def test_missing_expected_origin_fails_closed_without_reading_real_preferences(isolated_refresh):
    original, _replacement, _candidate = isolated_refresh
    result = local_proxy.reload_local_ai_proxy_verified(
        remote_proxy.format_proxy_node(original), _expected_current_key="", profile_id="home",
    )
    assert "已保留当前运行节点" in result
