"""Independent scheduler integration review: synthetic clocks/state only."""
import pytest

from core import local_proxy, proxy_routing, remote_proxy, subscription_auto_refresh as worker
from test_subscription_refresh_integration import scenario as _scenario_fixture


scenario = _scenario_fixture


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_transient_apply_preflight_read_retries_cache_at_backoff_not_full_period(scenario, monkeypatch, scope):
    module = local_proxy if scope == "local" else proxy_routing
    attribute = "_load_local_proxy_routing_preferences_strict" if scope == "local" else "load_ssh_routes"
    read = getattr(module, attribute)
    reads = 0

    def transient_read(*args):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise OSError("synthetic transient apply preflight error")
        return read(*args)

    monkeypatch.setattr(module, attribute, transient_read)
    first = scenario.run(scope, period=1800)
    assert scenario.events == [("fetch", "a"), ("fetch", "b")]
    assert first["retryable"] and first["next_delay_seconds"] == 60

    scenario.clock.now = 60
    scenario.events.clear()
    second = scenario.run(scope, period=1800)
    target = "local" if scope == "local" else "server"
    assert scenario.events == [("apply", target, "a")]
    assert second["reused_count"] == 2 and not second["errors"]
    assert second["next_delay_seconds"] == 1740


def test_failed_ssh_scope_read_gets_retry_deadline_independent_of_healthy_hosts(scenario, monkeypatch):
    load = proxy_routing.load_ssh_routes

    def transient_read(name):
        if name == "temporary-unreadable":
            raise OSError("synthetic temporarily locked routes file")
        return load(name)

    monkeypatch.setattr(proxy_routing, "load_ssh_routes", transient_read)
    result = worker.refresh_saved_subscriptions(
        "ssh", server_names=("temporary-unreadable", "server"),
        interval_seconds=1800, schedule=scenario.schedule,
    )
    assert ("apply", "server", "a") in scenario.events
    assert result["errors"] and result["retryable"]
    assert 1 <= result["next_delay_seconds"] <= 60


def test_source_change_in_apply_preflight_does_not_defer_other_ready_sources_for_full_period(scenario, monkeypatch):
    fetch = remote_proxy.fetch_proxy_subscription

    def edit_completed_source(url, **kwargs):
        result = fetch(url, **kwargs)
        if kwargs["profile_id"] == "b":
            scenario.state["profiles"]["a"]["source_revision"] = "newly-confirmed-source"
        return result

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", edit_completed_source)
    result = scenario.run()
    assert not any(event[0] == "apply" for event in scenario.events)
    assert result["errors"] and result["retryable"]
    assert 1 <= result["next_delay_seconds"] <= 60
