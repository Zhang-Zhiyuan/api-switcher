"""Task-level failure is incomplete evidence, not an unreachable upstream node."""

import pytest

from core import local_proxy, remote_proxy
from test_local_proxy_quick_ui import flush, quick_tab as quick_tab
from test_ssh_latency_quick_ui import complete, node, view
from ui.tabs import local_proxy_tab, ssh_tab


@pytest.mark.parametrize("failure", ["empty", "raise", "executor"])
def test_local_quick_task_failure_is_incomplete_and_not_unreachable(quick_tab, monkeypatch, failure):
    tab, calls = quick_tab

    def action(*_args, **_kwargs):
        if failure == "raise":
            raise RuntimeError("synthetic scheduler failure")
        return {}

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", action)
    monkeypatch.setattr(local_proxy, "measure_proxy_node_data_plane_latencies", action)
    if failure == "executor":
        def unavailable(**_kwargs):
            raise RuntimeError("synthetic executor failure")

        monkeypatch.setattr(local_proxy_tab, "ThreadPoolExecutor", unavailable)
    tab._measure_subscription_latencies(all_nodes=True)
    flush(calls)
    assert not tab._busy
    for item in tab._subscription_nodes:
        result = tab._latency_results[remote_proxy.proxy_subscription_node_key(item)]
        assert remote_proxy.proxy_node_latency_incomplete(result)
        assert not remote_proxy.proxy_node_latency_explicitly_unreachable(result)
    assert "可连 0，失败 0，取消 0，未完成 3" in calls.status[-1][0]
    assert calls.status[-1][1] == "warning"
    assert calls.saved[0][0]["older-profile-result"]["latency_ms"] == 30


@pytest.mark.parametrize("failure", ["empty", "raise"])
def test_ssh_quick_task_failure_reports_all_nodes_incomplete(monkeypatch, failure):
    tab = view(monkeypatch)

    def measure(*_args, **_kwargs):
        if failure == "raise":
            raise RuntimeError("synthetic SSH unavailable")
        return {}

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", measure)
    tab._measure_proxy_subscription_latencies()
    complete(tab)
    assert all(remote_proxy.proxy_node_latency_incomplete(result) for result in tab._proxy_latency_results.values())
    assert all(not remote_proxy.proxy_node_latency_explicitly_unreachable(result)
               for result in tab._proxy_latency_results.values())
    assert "可连 0，失败 0，取消 0，未完成 2" in tab.messages[-1]
    assert tab.severities[-1] == "warning"


@pytest.mark.parametrize("states,expected", [
    (("failed", "incomplete"), "incomplete"),
    (("ok", "incomplete"), "ok"),
    (("cancelled", "incomplete"), "cancelled"),
    (("failed", "missing"), "incomplete"),
    (("failed", "omitted"), "incomplete"),
    (("failed", "failed"), "failed"),
    (("incomplete", "omitted"), "incomplete"),
])
def test_ssh_aggregate_never_turns_missing_evidence_into_complete_failure(states, expected):
    item = node(1)
    key = remote_proxy.proxy_subscription_node_key(item)
    server_results = {}
    for index, state in enumerate(states):
        if state == "omitted":
            continue
        server_results[str(index)] = {}
        if state != "missing":
            server_results[str(index)][key] = remote_proxy.ProxyNodeLatencyResult(
                key, state == "ok", latency_ms=17 if state == "ok" else None,
                detail="synthetic result", cancelled=state == "cancelled", incomplete=state == "incomplete",
            )
    result = ssh_tab.SSHTab._aggregate_proxy_latency_results(None, server_results, len(states), [item])[key]
    assert remote_proxy.proxy_node_latency_ok(result) is (expected == "ok")
    assert remote_proxy.proxy_node_latency_incomplete(result) is (expected == "incomplete")
    assert remote_proxy.proxy_node_latency_cancelled(result) is (expected == "cancelled")
    assert remote_proxy.proxy_node_latency_explicitly_unreachable(result) is (expected == "failed")
    if expected == "ok":
        assert "1/2 可用" in result.detail and "1 台未完成" in result.detail
