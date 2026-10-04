"""No network/GUI: saved subscription scheduling and application authority."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from core import local_proxy, proxy_routing, remote_proxy, subscription_auto_refresh as worker
from core.proxy_update_result import update_result
from core.subscription_refresh_schedule import SubscriptionRefreshSchedule


@pytest.fixture
def scenario(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    schedule = SubscriptionRefreshSchedule(clock=lambda: clock.now)
    state = {"active_profile_id": "a", "profiles": {
        key: {"id": key, "url": f"https://{key}.example/sub", "name": key}
        for key in ("a", "b")
    }}
    routes = {"local": {"service_profile_bindings": {"claude": "a", "youtube": "b"}},
              "server": {"service_profile_bindings": {"claude": "a", "youtube": "b"}}}
    nodes = {key: (remote_proxy.ProxySubscriptionNode(1, {
        "name": key, "type": "http", "server": f"{key}.example", "port": 443,
    }),) for key in ("a", "b")}
    cache = {}
    events = []
    failures = set()
    outcomes = {}
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", lambda: routes["local"])
    monkeypatch.setattr(proxy_routing, "load_ssh_routes", lambda name: routes[name])
    monkeypatch.setattr(proxy_routing, "host_lock", lambda name: nullcontext())
    monkeypatch.setattr(worker, "_cached_node_keys", lambda profile: {"original"})
    monkeypatch.setattr(local_proxy, "local_proxy_subscription_direct_fallback_allowed", lambda: False)
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda profile: cache.get(profile["id"]))

    def fetch(url, **kwargs):
        key = kwargs["profile_id"]
        events.append(("fetch", key))
        if key in failures:
            raise OSError("synthetic timeout")
        cache[key] = remote_proxy.ProxySubscriptionResult(nodes[key], f"fake-{key}.yaml", url)
        return cache[key]

    def apply(name, nodes, **kwargs):
        events.append(("apply", name, kwargs["profile_id"]))
        return outcomes.get(name, update_result("configured", "applied"))

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fetch)
    monkeypatch.setattr(local_proxy, "refresh_running_local_service_routes_from_subscription",
                        lambda nodes, **kwargs: apply("local", nodes, **kwargs))
    monkeypatch.setattr(remote_proxy, "refresh_running_ai_proxy_from_subscription", apply)
    monkeypatch.setattr(local_proxy, "refresh_running_local_ai_proxy_from_subscription",
                        lambda nodes, **kwargs: apply("local", nodes, **kwargs))
    monkeypatch.setattr(local_proxy, "current_local_ai_proxy_node_key", lambda: "original")
    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *args: {"key": "original"})
    monkeypatch.setattr(remote_proxy, "proxy_node_key", lambda node: node.get("key", node["name"] if "name" in node else ""))

    return SimpleNamespace(clock=clock, schedule=schedule, state=state, routes=routes,
                           nodes=nodes, cache=cache, events=events, failures=failures,
                           outcomes=outcomes,
                           run=lambda scope="local", period=1800: worker.refresh_saved_subscriptions(
                               scope, server_names=("server",), interval_seconds=period, schedule=schedule))


def test_win_and_ssh_share_download_but_apply_independently(scenario):
    local = scenario.run()
    remote = scenario.run("ssh")
    assert local["downloaded_count"] == 2
    assert remote["downloaded_count"] == 0
    assert remote["reused_count"] == 2
    assert scenario.events == [("fetch", "a"), ("fetch", "b"),
                               ("apply", "local", "a"), ("apply", "server", "a")]
    assert remote["next_delay_seconds"] == 1800


def test_download_backoff_does_not_refetch_or_reapply_successful_sources(scenario):
    scenario.failures.add("b")
    first = scenario.run()
    assert first["retryable"] and first["next_delay_seconds"] == 60
    scenario.events.clear()
    scenario.clock.now = 60
    second = scenario.run()
    assert scenario.events == [("fetch", "b")]
    assert second["waiting_count"] == 1
    assert second["next_delay_seconds"] == 120
    scenario.failures.clear()
    scenario.clock.now = 180
    scenario.events.clear()
    third = scenario.run()
    assert scenario.events == [("fetch", "b"), ("apply", "local", "b")]
    assert third["downloaded_count"] == 1 and not third["errors"]


def test_retained_runtime_retries_cached_content_not_download(scenario):
    scenario.outcomes["local"] = update_result("候选暂不可用", "retained", warning=True, retryable=True)
    result = scenario.run()
    assert result["steps"][-1]["outcome"] == "retained"
    assert result["steps"][-1]["warning"]
    assert result["next_delay_seconds"] == 60
    scenario.clock.now = 60
    scenario.events.clear()
    result = scenario.run()
    assert scenario.events == [("apply", "local", "a")]
    assert result["reused_count"] == 2 and result["next_delay_seconds"] == 120


def test_failed_local_apply_does_not_force_successful_ssh_to_apply_again(scenario):
    scenario.outcomes["local"] = update_result("held", "retained", warning=True, retryable=True)
    scenario.run()
    scenario.run("ssh")
    scenario.clock.now = 60
    scenario.events.clear()
    result = scenario.run("ssh")
    assert scenario.events == [] and result["waiting_count"] == 2
    scenario.run()
    assert scenario.events == [("apply", "local", "a")]


def test_due_download_failure_does_not_starve_pending_cached_apply(scenario):
    scenario.outcomes["local"] = update_result("held", "retained", warning=True, retryable=True)
    scenario.run(period=300)
    scenario.failures.update(("a", "b"))
    scenario.outcomes.clear()
    scenario.clock.now = 300
    scenario.events.clear()
    result = scenario.run(period=300)
    assert scenario.events == [("fetch", "a"), ("fetch", "b"), ("apply", "local", "a")]
    assert result["errors"] and result["reused_count"] == 2
    assert result["next_delay_seconds"] == 60


def test_failed_due_download_does_not_use_changed_unverified_cache(scenario):
    scenario.outcomes["local"] = update_result("held", "retained", warning=True, retryable=True)
    scenario.run(period=300)
    scenario.failures.update(("a", "b"))
    scenario.cache["a"] = scenario.cache["b"]
    scenario.cache["b"] = scenario.cache["a"]
    scenario.routes["local"]["service_profile_bindings"] = {"claude": "a"}
    scenario.clock.now = 300
    scenario.events.clear()
    result = scenario.run(period=300)
    assert result["errors"] and not any(event[0] == "apply" for event in scenario.events)


def test_unchanged_subscription_still_checks_runtime_at_next_period(scenario):
    scenario.run(period=300)
    scenario.clock.now = 300
    scenario.events.clear()
    scenario.run(period=300)
    assert scenario.events == [("fetch", "a"), ("fetch", "b"), ("apply", "local", "a")]


def test_short_win_download_period_does_not_starve_ssh_health_check(scenario):
    scenario.run(period=300)
    scenario.run("ssh", period=900)
    for now in (300, 600, 900):
        scenario.clock.now = now
        scenario.run(period=300)
    scenario.events.clear()
    result = scenario.run("ssh", period=900)
    assert result["reused_count"] == 2
    assert scenario.events == [("apply", "server", "a")]


def test_editing_saved_routes_invalidates_only_that_consumer_ack(scenario):
    scenario.run()
    scenario.routes["local"]["service_node_bindings"] = {"claude": "another-node"}
    scenario.events.clear()
    result = scenario.run()
    assert result["reused_count"] == 2
    assert scenario.events == [("apply", "local", "a")]


def test_new_source_does_not_reuse_previous_source_cache(scenario):
    scenario.run()
    scenario.state["profiles"]["a"]["url"] = "https://new.example/sub"
    scenario.events.clear()
    scenario.run()
    assert scenario.events == [("fetch", "a"), ("apply", "local", "a")]


def test_direct_fallback_policy_changes_partition_download_reuse(scenario, monkeypatch):
    scenario.run()
    monkeypatch.setattr(local_proxy, "local_proxy_subscription_direct_fallback_allowed", lambda: True)
    scenario.events.clear()
    scenario.run("ssh")
    assert scenario.events[:2] == [("fetch", "a"), ("fetch", "b")]


def test_missing_shared_cache_never_counts_as_applied_and_recovers(scenario):
    scenario.run()
    scenario.cache.clear()
    scenario.events.clear()
    result = scenario.run("ssh")
    assert result["errors"] and scenario.events == []
    assert result["next_delay_seconds"] == 60
    scenario.clock.now = 60
    scenario.events.clear()
    result = scenario.run("ssh")
    assert not result["errors"] and result["downloaded_count"] == 2


def test_source_changes_after_download_not_applied(scenario, monkeypatch):
    fetch = remote_proxy.fetch_proxy_subscription

    def edited(url, **kwargs):
        result = fetch(url, **kwargs)
        scenario.state["profiles"][kwargs["profile_id"]]["source_revision"] = "changed"
        return result

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", edited)
    result = scenario.run()
    assert result["errors"] and not result["results"]
    assert not any(event[0] == "apply" for event in scenario.events)


def test_removed_binding_not_acknowledged_as_applied(scenario, monkeypatch):
    fetch = remote_proxy.fetch_proxy_subscription

    def edited(url, **kwargs):
        result = fetch(url, **kwargs)
        scenario.routes["local"]["service_profile_bindings"] = {}
        return result

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", edited)
    result = scenario.run()
    assert not any(event[0] == "apply" for event in scenario.events)
    assert result["steps"][-1]["outcome"] == "skipped"


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_new_binding_during_download_cannot_promote_subscription_to_default(scenario, monkeypatch, scope):
    name = "local" if scope == "local" else "server"
    scenario.routes[name]["service_profile_bindings"] = {}
    fetch = remote_proxy.fetch_proxy_subscription

    def edited(url, **kwargs):
        result = fetch(url, **kwargs)
        scenario.routes[name]["service_profile_bindings"] = {"claude": "a"}
        return result

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", edited)
    result = scenario.run(scope)
    assert not any(event[0] == "apply" for event in scenario.events)
    assert result["steps"][-1]["outcome"] == "skipped"


def test_cache_phase_does_not_leak_reservations_if_interval_elapses(scenario, monkeypatch):
    finish = scenario.schedule.finish_success

    def delayed(token, revision):
        version = finish(token, revision)
        scenario.clock.now += 100
        return version

    monkeypatch.setattr(scenario.schedule, "finish_success", delayed)
    scenario.run(period=1)
    assert all(state.reservation is None for state in scenario.schedule._sources.values())


def test_plain_legacy_result_not_inferred_success_from_text(scenario):
    scenario.outcomes["local"] = "成功，已应用"
    result = scenario.run()
    assert result["steps"][-1]["outcome"] == "unknown"
    assert result["steps"][-1]["warning"] and not result["retryable"]


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_same_content_retry_keeps_proven_origin_after_cache_replaces_old_node(scenario, monkeypatch, scope):
    original = "f" * 64
    for routes in scenario.routes.values():
        routes["service_profile_bindings"] = {}
    monkeypatch.setattr(worker, "_cached_node_keys", lambda profile: {original})
    monkeypatch.setattr(local_proxy, "current_local_ai_proxy_node_key", lambda: original)
    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *args: {"key": original})
    name = "local" if scope == "local" else "server"
    scenario.outcomes[name] = update_result("not yet", "retained", retryable=True, warning=True)
    scenario.run(scope)
    monkeypatch.setattr(worker, "_cached_node_keys", lambda profile: set())
    scenario.clock.now = 60
    scenario.events.clear()
    result = scenario.run(scope)
    assert result["reused_count"] == 1
    assert scenario.events == [("apply", name, "a")]


@pytest.mark.parametrize("change", ["node", "source", "routes"])
def test_remembered_origin_never_authorizes_changed_context(scenario, monkeypatch, change):
    original = "f" * 64
    scenario.routes["local"]["service_profile_bindings"] = {}
    monkeypatch.setattr(worker, "_cached_node_keys", lambda profile: {original})
    monkeypatch.setattr(local_proxy, "current_local_ai_proxy_node_key", lambda: original)
    scenario.outcomes["local"] = update_result("not yet", "retained", retryable=True, warning=True)
    scenario.run()
    monkeypatch.setattr(worker, "_cached_node_keys", lambda profile: set())
    if change == "node":
        monkeypatch.setattr(local_proxy, "current_local_ai_proxy_node_key", lambda: "e" * 64)
    elif change == "source":
        scenario.state["profiles"]["a"]["url"] = "https://changed.example/sub"
    else:
        scenario.routes["local"]["strict_privacy"] = True
    scenario.clock.now = 60
    scenario.events.clear()
    scenario.run()
    assert not any(event[0] == "apply" for event in scenario.events)


@pytest.mark.parametrize("period", [True, 0, -1, float("nan"), float("inf")])
def test_invalid_period_rejected_before_download(scenario, period):
    with pytest.raises(ValueError):
        scenario.run(period=period)
    assert not scenario.events


def test_unscheduled_calls_keep_manual_fresh_download_behavior(scenario):
    scenario.run()
    scenario.events.clear()
    result = worker.refresh_saved_subscriptions("local")
    assert scenario.events[:2] == [("fetch", "a"), ("fetch", "b")]
    assert "next_delay_seconds" not in result
