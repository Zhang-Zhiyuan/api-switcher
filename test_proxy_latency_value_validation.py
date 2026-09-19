"""Malformed cached measurements must not crash rendering or become evidence."""
from datetime import datetime, timezone

import pytest

from core import remote_proxy


def measurement(value=25, **kwargs):
    return {"ok": True, "latency_ms": value,
            "measured_at": datetime.now(timezone.utc).isoformat(), **kwargs}


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), -1, 0, True,
                                  False, "bad", "1.5", 1.5, [], {}, None,
                                  2**31, 10**100, 1e100,
                                  pytest.param(10**5000, id="oversized-integer")])
@pytest.mark.parametrize("as_object", [False, True])
def test_invalid_success_is_not_connectivity_evidence(value, as_object):
    result = measurement(value)
    if as_object:
        result = remote_proxy.ProxyNodeLatencyResult("synthetic", **result)
    assert remote_proxy.proxy_node_latency_ms(result) is None
    assert not remote_proxy.proxy_node_latency_ok(result)
    assert not remote_proxy.proxy_node_latency_explicitly_unreachable(result)
    assert remote_proxy.proxy_node_latency_label(result) == "结果无效"


@pytest.mark.parametrize("value", [1, 25, "25", 25.0, 2**31 - 1])
def test_valid_integer_latency_remains_compatible(value):
    result = measurement(value)
    assert remote_proxy.proxy_node_latency_ok(result)
    assert remote_proxy.proxy_node_latency_ms(result) == int(value)


@pytest.mark.parametrize("updates", [{"ok": "false"}, {"ok": 1}, {"cancelled": "false"}, {"cancelled": 1}])
def test_nonboolean_status_flags_do_not_create_evidence(updates):
    result = measurement(**updates)
    assert not remote_proxy.proxy_node_latency_ok(result)
    assert not remote_proxy.proxy_node_latency_explicitly_unreachable(result)
    assert remote_proxy.proxy_node_latency_label(result) == "结果无效"


@pytest.mark.parametrize("attempts", [float("inf"), float("nan"), -1, True, "bad", [], 2.5,
                                     2**31, 1e100, pytest.param(10**5000, id="oversized-integer")])
def test_bad_attempt_count_is_safely_defaulted(attempts):
    assert remote_proxy.proxy_node_latency_attempts(measurement(attempts=attempts)) == 0


def test_cache_roundtrip_skips_invalid_entries_and_preserves_valid_siblings(monkeypatch):
    values = {
        "bad-infinite": measurement(float("inf")),
        "bad-negative": measurement(-1),
        "bad-status": measurement(ok="false"),
        "ok": measurement(25, attempts=float("inf")),
        "failed": measurement(None, ok=False),
        "cancelled": measurement(None, ok=False, cancelled=True),
    }
    monkeypatch.setattr(remote_proxy, "_normalize_proxy_subscription_state", lambda state: state)
    restored = remote_proxy.load_proxy_subscription_latencies({"node_latencies": values})
    assert set(restored) == {"ok", "failed", "cancelled"}
    assert restored["ok"]["attempts"] == 0
    assert remote_proxy.proxy_node_latency_explicitly_unreachable(restored["failed"])
    assert not remote_proxy.proxy_node_latency_explicitly_unreachable(restored["cancelled"])
    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_profile_state", lambda _id, **kw: kw)
    saved = remote_proxy.save_proxy_subscription_latencies(values, profile_id="synthetic")
    assert set(saved["node_latencies"]) == set(restored)
    assert remote_proxy.load_proxy_subscription_latencies(saved) == restored


def test_sort_and_best_ignore_malformed_success_instead_of_crashing_or_selecting_it():
    nodes = [remote_proxy.ProxySubscriptionNode(index, {
        "name": f"美国-合成-{index}", "type": "trojan", "server": "127.0.0.1",
        "port": 18000 + index, "password": "synthetic-only",
    }) for index in range(3)]
    values = {remote_proxy.proxy_subscription_node_key(item): measurement(delay)
              for item, delay in zip(nodes, [float("inf"), -1, 25])}
    assert len(remote_proxy.sort_proxy_subscription_nodes(nodes, values)) == 3
    assert remote_proxy.best_proxy_subscription_node_by_latency(nodes, values) is nodes[2]
