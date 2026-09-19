"""Batch quick probes keep complete results without touching the managed proxy."""
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from core import local_proxy, remote_proxy


def node(index, **fields):
    return remote_proxy.ProxySubscriptionNode(index, {
        "name": f"node-{index}", "type": "hysteria2", "server": f"n{index}.example.test",
        "port": 443, "password": "synthetic-password", **fields,
    }, node_key=f"key-{index}")


def basics(monkeypatch):
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("synthetic-mihomo.exe"))
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())


def fake_batches(monkeypatch):
    basics(monkeypatch)
    batches = []

    @contextmanager
    def session(_binary, nodes, **_):
        batches.append(nodes)
        yield local_proxy._IsolatedMihomoBatchSession(1234, "synthetic", tuple(str(i) for i in range(len(nodes))))

    monkeypatch.setattr(local_proxy, "_isolated_mihomo_batch_session", session)
    return batches


def test_quick_all_nodes_are_chunked_without_candidate_truncation_and_report_once(monkeypatch):
    batches = fake_batches(monkeypatch)
    workers, reports, calls = [], [], []

    def executor(**kwargs):
        workers.append(kwargs["max_workers"])
        return ThreadPoolExecutor(**kwargs)

    monkeypatch.setattr(local_proxy, "ThreadPoolExecutor", executor)
    monkeypatch.setattr(local_proxy, "_probe_isolated_mihomo_batch_delay",
                        lambda *args: calls.append(args) or 23)
    nodes = [node(index) for index in range(130)]
    results = local_proxy.measure_proxy_node_data_plane_latencies(
        nodes, quick=True, attempts=1, max_workers=999, progress_callback=lambda *args: reports.append(args),
    )
    assert [len(batch) for batch in batches] == [64, 64, 2]
    assert workers == [16, 16, 2]
    assert set(results) == {item.node_key for item in nodes}
    assert len(calls) == len(nodes)
    assert [report[0] for report in reports] == list(range(1, 131))
    assert all(report[1] == 130 for report in reports)
    assert all(result.ok and result.attempts == 1 and result.latency_ms == 23 for result in results.values())


def test_quick_deduplicates_full_connection_not_password_sni_or_protocol(monkeypatch):
    batches = fake_batches(monkeypatch)
    calls = []
    monkeypatch.setattr(local_proxy, "_probe_isolated_mihomo_batch_delay", lambda *args: calls.append(args) or 31)
    original = node(1)
    duplicate = node(2, server=original.node["server"])
    different_password = node(3, server=original.node["server"], password="another-synthetic")
    different_sni = node(4, server=original.node["server"], sni="other.example.test")
    different_type = node(5, server=original.node["server"], type="tuic")
    results = local_proxy.measure_proxy_node_data_plane_latencies(
        [original, duplicate, different_password, different_sni, different_type], quick=True, attempts=1,
    )
    assert len(batches[0]) == 4
    assert len(calls) == 4
    assert len(results) == 5
    assert all(key == result.node_key and result.ok for key, result in results.items())
    assert results[original.node_key].measured_at == results[duplicate.node_key].measured_at


def test_quick_invalid_or_dependent_nodes_have_terminal_errors_without_blocking_peers(monkeypatch):
    batches = fake_batches(monkeypatch)
    monkeypatch.setattr(local_proxy, "_probe_isolated_mihomo_batch_delay", lambda *_: 19)
    results = local_proxy.measure_proxy_node_data_plane_latencies(
        [node(1, type="DIRECT"), node(2, **{"dialer-proxy": "private-label"}), node(3)], quick=True,
    )
    assert len(batches[0]) == 1
    assert set(results) == {"key-1", "key-2", "key-3"}
    assert not results["key-1"].ok and not results["key-2"].ok and results["key-3"].ok
    assert "private-label" not in results["key-2"].detail


def test_quick_empty_and_all_invalid_inputs_do_not_prepare_core(monkeypatch):
    basics(monkeypatch)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: pytest.fail("no core needed"))
    assert local_proxy.measure_proxy_node_data_plane_latencies([], quick=True) == {}
    assert not local_proxy.measure_proxy_node_data_plane_latencies([node(1, type="DIRECT")], quick=True)["key-1"].ok


@pytest.mark.parametrize("quick", [False, True])
def test_cancelled_before_start_does_not_prepare_core_or_report_unreachable(monkeypatch, quick):
    basics(monkeypatch)
    event = threading.Event()
    event.set()
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: pytest.fail("must not touch directories"))
    results = local_proxy.measure_proxy_node_data_plane_latencies([node(1), node(2)], quick=quick, cancel_event=event)
    assert len(results) == 2
    assert all(result.cancelled and result.attempts == 0 and result.label() == "已取消" for result in results.values())


def test_quick_cancellation_stops_queued_probes_and_later_cores_but_keeps_success(monkeypatch):
    batches = fake_batches(monkeypatch)
    event = threading.Event()
    calls, reports = [], []

    def probe(*_):
        calls.append(True)
        event.set()
        return 21

    monkeypatch.setattr(local_proxy, "_probe_isolated_mihomo_batch_delay", probe)
    results = local_proxy.measure_proxy_node_data_plane_latencies(
        [node(i) for i in range(130)], quick=True, attempts=1, max_workers=1, cancel_event=event,
        progress_callback=lambda *args: reports.append(args),
    )
    assert len(batches) == 1
    assert len(calls) == 1
    assert sum(result.ok for result in results.values()) == 1
    assert sum(result.cancelled for result in results.values()) == 129
    assert len(reports) == len(results) == 130


def test_quick_reports_fast_node_before_slow_peer_and_survives_callback_failure(monkeypatch):
    fake_batches(monkeypatch)
    reported = threading.Event()

    def probe(_session, name, _timeout):
        if name == "1":
            assert reported.wait(2), "progress must be emitted before all peers finish"
        return 19

    def progress(*_):
        reported.set()
        raise RuntimeError("synthetic destroyed UI")

    monkeypatch.setattr(local_proxy, "_probe_isolated_mihomo_batch_delay", probe)
    results = local_proxy.measure_proxy_node_data_plane_latencies([node(1), node(2)], quick=True,
                                                                 progress_callback=progress)
    assert all(result.ok for result in results.values())


def test_cancellation_during_inflight_failure_does_not_mark_node_unreachable(monkeypatch):
    fake_batches(monkeypatch)
    event = threading.Event()

    def probe(*_):
        event.set()
        raise TimeoutError("synthetic request interrupted")

    monkeypatch.setattr(local_proxy, "_probe_isolated_mihomo_batch_delay", probe)
    result = local_proxy.measure_proxy_node_data_plane_latencies([node(1)], quick=True, cancel_event=event)["key-1"]
    assert result.cancelled and not remote_proxy.proxy_node_latency_explicitly_unreachable(result)


def test_single_node_delay_failure_does_not_erase_peers(monkeypatch):
    fake_batches(monkeypatch)

    def probe(_session, name, _timeout):
        if name == "1":
            raise TimeoutError()
        return 17

    monkeypatch.setattr(local_proxy, "_probe_isolated_mihomo_batch_delay", probe)
    results = local_proxy.measure_proxy_node_data_plane_latencies([node(1), node(2), node(3)], quick=True)
    assert results["key-1"].ok and results["key-3"].ok
    assert not results["key-2"].ok and "TimeoutError" in results["key-2"].detail


@pytest.mark.parametrize("failure_stage", ["create", "submit", "iterate", "shutdown"])
def test_quick_executor_faults_preserve_completed_results(monkeypatch, failure_stage):
    fake_batches(monkeypatch)

    class Executor:
        def __init__(self, **_):
            if failure_stage == "create":
                raise RuntimeError("executor create")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            if failure_stage == "shutdown":
                raise RuntimeError("executor shutdown")

        def submit(self, _action, _session, name, key):
            if failure_stage == "submit" and name == "1":
                raise RuntimeError("executor submit")
            future = Future()
            future.set_result(remote_proxy.ProxyNodeLatencyResult(key, True, 19))
            return future

    def iteration(futures):
        yield next(iter(futures))
        raise RuntimeError("executor iteration")

    monkeypatch.setattr(local_proxy, "ThreadPoolExecutor", Executor)
    if failure_stage == "iterate":
        monkeypatch.setattr(local_proxy, "as_completed", iteration)
    results = local_proxy.measure_proxy_node_data_plane_latencies([node(1), node(2), node(3)], quick=True)
    assert len(results) == 3
    for key, result in results.items():
        failed = failure_stage == "create" or (failure_stage == "submit" and key == "key-2")
        assert result.ok is not failed


def test_failed_batch_startup_falls_back_without_repeating_failed_batch_mode(monkeypatch):
    basics(monkeypatch)
    starts, legacy, reported = [], [], []

    @contextmanager
    def broken(*_args, **_kwargs):
        starts.append(True)
        raise RuntimeError("synthetic unsupported batch")
        yield  # pragma: no cover

    @contextmanager
    def old_session(_binary, item):
        legacy.append(item)
        yield SimpleNamespace(proxy_url="http://127.0.0.1:1234")

    monkeypatch.setattr(local_proxy, "_isolated_mihomo_batch_session", broken)
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", old_session)
    monkeypatch.setattr(local_proxy, "_probe_ai_url_through_explicit_http_proxy",
                        lambda *_: local_proxy.LocalAIProxyProbeResult("synthetic", True, elapsed_ms=17))
    results = local_proxy.measure_proxy_node_data_plane_latencies(
        [node(i) for i in range(65)], quick=True, attempts=1, progress_callback=lambda *args: reported.append(args),
    )
    assert len(starts) == 1 and len(legacy) == 65
    assert len(reported) == len(results) == 65
    assert all(result.ok for result in results.values())


def test_cleanup_failure_is_not_swallowed_after_successful_batch_probes(monkeypatch):
    basics(monkeypatch)

    @contextmanager
    def session(*_args, **_kwargs):
        yield local_proxy._IsolatedMihomoBatchSession(1234, "synthetic", ("one",))
        raise local_proxy._IsolatedMihomoBatchCleanupError("synthetic cleanup failed")

    monkeypatch.setattr(local_proxy, "_isolated_mihomo_batch_session", session)
    monkeypatch.setattr(local_proxy, "_probe_isolated_mihomo_batch_delay", lambda *_: 21)
    with pytest.raises(local_proxy._IsolatedMihomoBatchCleanupError, match="cleanup failed"):
        local_proxy.measure_proxy_node_data_plane_latencies([node(1)], quick=True)


@pytest.mark.parametrize("delay", [None, True, 0, -1, "17", 3.5, 65536])
def test_batch_delay_rejects_malformed_controller_values(monkeypatch, delay):
    session = local_proxy._IsolatedMihomoBatchSession(1234, "synthetic-secret", ("node",))
    monkeypatch.setattr(local_proxy, "_isolated_batch_controller_request", lambda *_args, **_kwargs: {"delay": delay})
    with pytest.raises(RuntimeError, match="无效延迟"):
        local_proxy._probe_isolated_mihomo_batch_delay(session, "node", 5)


def test_batch_controller_is_loopback_authenticated_bounded_and_never_redirected(monkeypatch):
    requests, closed = [], []
    response_status = [200]

    class Connection:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1" and port == 1234 and timeout == 6

        def request(self, method, path, headers):
            requests.append((method, path, headers))

        def getresponse(self):
            return SimpleNamespace(status=response_status[0], read=lambda _size: json.dumps({"delay": 17}).encode(),
                                   headers={})

        def close(self):
            closed.append(True)

    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    session = local_proxy._IsolatedMihomoBatchSession(1234, "synthetic-secret", ("node",))
    assert "synthetic-secret" not in repr(session)
    assert local_proxy._probe_isolated_mihomo_batch_delay(session, "node", 5) == 17
    method, path, headers = requests[0]
    assert method == "GET" and headers == {"Authorization": "Bearer synthetic-secret"}
    assert parse_qs(urlsplit(path).query) == {
        "expected": ["204"], "timeout": ["5000"], "url": ["https://www.gstatic.com/generate_204"],
    }
    response_status[0] = 302
    with pytest.raises(RuntimeError, match="HTTP 302"):
        local_proxy._probe_isolated_mihomo_batch_delay(session, "node", 5)
    assert len(requests) == len(closed) == 2
    with pytest.raises(ValueError, match="不属于"):
        local_proxy._probe_isolated_mihomo_batch_delay(session, "DIRECT", 5)
    assert len(requests) == 2


@pytest.mark.parametrize("stage", ["body", "startup", "cancel"])
def test_batch_process_and_secret_directory_cleanup(monkeypatch, tmp_path, stage):
    basics(monkeypatch)
    created = {}
    event = threading.Event()
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_DIRECTORIES", set())
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_PROCESSES", set())
    monkeypatch.setattr(local_proxy, "_select_isolated_mihomo_ports", lambda **_: (1234, 1235))
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _: True)

    class Process:
        stopped = False

        def __init__(self, _args, **kwargs):
            created["directory"] = Path(kwargs["cwd"])
            created["config"] = remote_proxy.yaml.safe_load((created["directory"] / "config.yaml").read_text(encoding="utf-8"))
            created["process"] = self
            if stage == "cancel":
                event.set()

        def poll(self):
            return 1 if stage == "startup" or self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, **_):
            return 0

    monkeypatch.setattr(local_proxy.subprocess, "Popen", Process)
    monkeypatch.setattr(local_proxy, "_isolated_batch_controller_request", lambda *_args, **_kwargs: {"version": "synthetic"})
    with pytest.raises(RuntimeError):
        with local_proxy._isolated_mihomo_batch_session(tmp_path / "fake.exe", [node(1, name="DIRECT").node],
                                                       cancel_event=event):
            raise RuntimeError("synthetic body failure")
    config = created["config"]
    assert config["bind-address"] == "127.0.0.1" and config["allow-lan"] is False
    assert config["external-controller"] == "127.0.0.1:1235" and len(config["secret"]) >= 32
    assert config["proxies"][0]["name"] == "API-SWITCHER-BATCH-0"
    assert config["rules"] == ["MATCH,REJECT"] and not config.get("proxy-groups")
    assert not created["directory"].exists()
    assert not local_proxy._ISOLATED_MIHOMO_DIRECTORIES and not local_proxy._ISOLATED_MIHOMO_PROCESSES
