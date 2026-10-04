"""Independent follow-up review using only synthetic workers and saved state."""
from types import SimpleNamespace

import pytest

from core import local_proxy, remote_proxy, subscription_auto_refresh as worker
from test_local_proxy_quick_ui import (
    flush,
    node as synthetic_node,
    quick_tab as _quick_tab_fixture,
    result as latency_result,
)
from test_subscription_refresh_integration import scenario as _scenario_fixture
from test_subscription_auto_refresh import InlineThread, timer as _timer_fixture


quick_tab = _quick_tab_fixture
scenario = _scenario_fixture
timer = _timer_fixture


@pytest.mark.parametrize("termination", ["raises", "omits_result"])
@pytest.mark.parametrize("transport", ["tcp", "https"])
def test_stability_probe_retains_callback_results_when_group_does_not_return_them(
    quick_tab, monkeypatch, termination, transport,
):
    tab, calls = quick_tab
    kind = "trojan" if transport == "tcp" else "hysteria2"
    tab._subscription_nodes = [synthetic_node(10, kind), synthetic_node(11, kind)]
    tab._quality_results = {}
    monkeypatch.setattr(local_proxy, "select_stable_local_proxy_node", lambda *_args, **_kwargs: (None, {}))

    def partial_probe(items, **kwargs):
        measured = latency_result(items[0])
        kwargs["progress_callback"](1, len(items), measured)
        if termination == "raises":
            raise TimeoutError("synthetic group teardown failure")
        return {}

    if transport == "tcp":
        monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", partial_probe)
    else:
        monkeypatch.setattr(local_proxy, "measure_proxy_node_data_plane_latencies", partial_probe)
    tab._verify_subscription_stability(all_nodes=True)
    flush(calls)
    completed_key = remote_proxy.proxy_subscription_node_key(tab._subscription_nodes[0])
    missing_key = remote_proxy.proxy_subscription_node_key(tab._subscription_nodes[1])
    assert remote_proxy.proxy_node_latency_ok(tab._latency_results[completed_key])
    assert remote_proxy.proxy_node_latency_incomplete(tab._latency_results[missing_key])
    assert remote_proxy.proxy_node_latency_ok(calls.saved[-1][0][completed_key])
    if termination == "raises":
        assert "synthetic group teardown failure" in calls.status[-1][0]


def test_stability_probe_does_not_adopt_out_of_scope_final_results(quick_tab, monkeypatch):
    tab, calls = quick_tab
    tab._quality_results = {}
    scope = tab._subscription_nodes[:2]
    outside = tab._subscription_nodes[2]
    monkeypatch.setattr(local_proxy, "select_stable_local_proxy_node", lambda *_args, **_kwargs: (None, {}))
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", lambda *_args, **_kwargs: {
        remote_proxy.proxy_subscription_node_key(outside): latency_result(outside),
    })
    tab._verify_subscription_stability()
    flush(calls)
    assert set(tab._latency_results) == {
        "older-profile-result", *(remote_proxy.proxy_subscription_node_key(item) for item in scope),
    }


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_source_changed_after_its_download_is_not_returned_as_displayable_cache(scenario, monkeypatch, scope):
    fetch = remote_proxy.fetch_proxy_subscription

    def edit_previous_source(url, **kwargs):
        result = fetch(url, **kwargs)
        if kwargs["profile_id"] == "b":
            scenario.state["profiles"]["a"]["url"] = "https://replacement.example/sub"
            scenario.state["profiles"]["a"]["source_revision"] = "replacement-version"
        return result

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", edit_previous_source)
    payload = scenario.run(scope)
    assert payload["errors"]
    assert not any(event[0] == "apply" for event in scenario.events)
    # The UI finish path consumes this mapping by profile ID alone, so stale
    # entries must not overwrite the node list of the same-ID replacement URL.
    assert "a" not in payload["results"]
    assert "b" in payload["results"]
    assert set(payload["result_sources"]) == {"b"}
    assert "https://" not in str(payload["result_sources"])


@pytest.mark.parametrize("change", ["url", "source_path", "source_revision", "deleted"])
@pytest.mark.parametrize("selected", ["a", "b"])
def test_queued_ui_completion_rechecks_source_and_keeps_healthy_siblings(timer, monkeypatch, change, selected):
    profiles = {
        "a": {"url": "https://a.example/old", "source_path": "", "source_revision": "one"},
        "b": {"url": "https://b.example/healthy", "source_path": "", "source_revision": "two"},
    }
    payload = {
        "results": {name: SimpleNamespace(nodes=(name + "-nodes",)) for name in profiles},
        "result_sources": {name: worker.subscription_source_identity(name, profile)
                           for name, profile in profiles.items()},
        "errors": [], "steps": [], "apply_messages": [], "retryable": False,
        "next_delay_seconds": 1800,
    }
    pending = []

    class DeferredThread(InlineThread):
        def start(self):
            pending.append(self.target)

    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {"profiles": profiles})
    monkeypatch.setattr(worker, "refresh_saved_subscriptions", lambda *_args, **_kwargs: payload)
    current = "_current_subscription_profile_id" if timer.kind == "local" else "_current_proxy_subscription_profile_id"
    setattr(timer.tab, current, lambda: selected)
    timer.start(DeferredThread)
    pending.pop()()  # The result is now queued, but Tk finish has not run yet.
    if change == "deleted":
        profiles.pop("a")
    else:
        profiles["a"][change] = "synthetic replacement"
    timer.calls.after.pop(next(iter(timer.calls.after)))()
    if selected == "a":
        assert not timer.calls.nodes
    else:
        assert timer.calls.nodes == [((("b-nodes",),), {"preserve_key": "selected"})]
    assert timer.calls.status[-1][1] == "warning"
    assert "旧链接节点" in timer.calls.status[-1][0]
    assert timer.calls.delays[-1] == 60000
    assert not getattr(timer.tab, timer.prefix + "busy")


def test_source_identity_ignores_unrelated_saved_metadata_changes(timer, monkeypatch):
    profile = {"url": "https://a.example/sub", "source_path": "", "source_revision": "one", "name": "original"}
    source_identity = worker.subscription_source_identity("a", profile)
    profile.update(name="renamed", selected_node_key="synthetic-new-choice", last_fetched_at="new timestamp")
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {"profiles": {"a": profile}})
    monkeypatch.setattr(worker, "refresh_saved_subscriptions", lambda *_args, **_kwargs: {
        "results": {"a": SimpleNamespace(nodes=("healthy-node",))}, "result_sources": {"a": source_identity},
        "errors": [], "apply_messages": [], "steps": [], "retryable": False,
    })
    timer.start()
    assert timer.calls.nodes == [((("healthy-node",),), {"preserve_key": "selected"})]
    assert timer.calls.status[-1][1] == "info"


@pytest.mark.parametrize("sources", [None, [], {}, {"a": "synthetic-wrong-source"}])
def test_explicit_missing_or_malformed_source_identity_never_displays_nodes(timer, monkeypatch, sources):
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: {
        "profiles": {"a": {"url": "https://a.example/current", "source_revision": "one"}},
    })
    monkeypatch.setattr(worker, "refresh_saved_subscriptions", lambda *_args, **_kwargs: {
        "results": {"a": SimpleNamespace(nodes=("untrusted-node",))}, "result_sources": sources,
        "errors": [], "apply_messages": [], "steps": [], "retryable": False,
    })
    timer.start()
    assert not timer.calls.nodes
    assert timer.calls.status[-1][1] == "warning"
    assert timer.calls.delays[-1] == 60000
