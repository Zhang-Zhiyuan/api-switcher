"""Synthetic connection failures must not replace measurements with IndexError."""
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from core import remote_proxy


NODE = {"name": "synthetic", "type": "http", "server": "node.example.test", "port": 8080}


@pytest.mark.parametrize("error", [TimeoutError(), OSError(""), ConnectionResetError(" \n ")])
@pytest.mark.parametrize("require_all", [False, True])
def test_empty_connection_error_preserves_prior_success(monkeypatch, error, require_all):
    connect = Mock(side_effect=[nullcontext(), error])
    monkeypatch.setattr(remote_proxy.socket, "create_connection", connect)
    result = remote_proxy.measure_proxy_node_latency(NODE, attempts=2, require_all=require_all)
    assert connect.call_count == 2
    assert result.ok is (not require_all)
    assert result.attempts == 2
    if require_all:
        assert "1/2" in result.detail and type(error).__name__ in result.detail
    else:
        assert result.latency_ms >= 1


def test_empty_failure_does_not_skip_later_attempts_or_other_nodes(monkeypatch):
    connect = Mock(side_effect=TimeoutError())
    monkeypatch.setattr(remote_proxy.socket, "create_connection", connect)
    items = remote_proxy.parse_proxy_subscription_content(
        'proxies:\n- {name: one, type: http, server: one.example.test, port: 8080}\n'
        '- {name: two, type: http, server: two.example.test, port: 8080}\n'
    )
    results = remote_proxy.measure_proxy_node_latencies(items, attempts=3)
    assert len(results) == 2 and connect.call_count == 6
    assert all(not item.ok and item.attempts == 3 for item in results.values())
    assert all(item.detail == "TimeoutError" for item in results.values())
