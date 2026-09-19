"""Actual disposable-core alias tests and startup benchmark, entirely loopback.

HTTP replaces only the synthetic target so no certificate, DNS, real service,
user subscription or live core is needed. This measures core-startup overhead,
not real UDP-provider throughput or service availability.
"""
from collections import Counter
from contextlib import ExitStack
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import threading
import time

import pytest

from core import local_proxy, remote_proxy


class LoopbackProxy:
    def __init__(self, stack, *, delay=0.015, status=204):
        self.hits = Counter()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                owner.hits[self.path] += 1
                time.sleep(delay)
                body = b"<html>synthetic intercepted error page</html>" if status == 200 else b""
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "http://127.0.0.1:1/must-not-follow")
                self.send_header("Content-Length", str(len(body)))
                if body:
                    self.send_header("Content-Type", "text/html")
                self.send_header("Connection", "close")
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)
                self.close_connection = True

            def do_CONNECT(self):
                self.connection.settimeout(2)
                self.send_response(200, "Connection established")
                self.end_headers()
                self.close_connection = True
                # Only parse/respond locally; never open the requested target.
                self.handle_one_request()

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()
        stack.callback(self.stop)
        self.node = {"name": "synthetic", "type": "http", "server": "127.0.0.1", "port": self.server.server_port}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def binary_path():
    binary = Path(os.environ.get("API_SWITCHER_MIHOMO_TEST_CORE") or local_proxy.LOCAL_PROXY_BIN_DIR / "mihomo.exe")
    if not binary.is_file():
        pytest.skip("Local mihomo core is not installed")
    return binary


def test_real_batch_alias_probes_are_authenticated_and_never_select_direct(monkeypatch):
    binary = binary_path()
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())
    before_processes = set(local_proxy._ISOLATED_MIHOMO_PROCESSES)
    before_directories = set(local_proxy._ISOLATED_MIHOMO_DIRECTORIES)
    with ExitStack() as stack:
        upstreams = [LoopbackProxy(stack) for _ in range(6)]
        sentinel = LoopbackProxy(stack)
        url = f"http://127.0.0.1:{sentinel.server.server_port}/health"
        closed_upstream = stack.enter_context(socket.socket())
        closed_upstream.bind(("127.0.0.1", 0))
        dead_node = {**upstreams[0].node, "port": closed_upstream.getsockname()[1]}
        with local_proxy._isolated_mihomo_batch_session(binary, [*(proxy.node for proxy in upstreams), dead_node]) as session:
            connection = http.client.HTTPConnection("127.0.0.1", session.controller_port, timeout=2)
            try:
                connection.request("GET", "/version")
                assert connection.getresponse().status == 401
            finally:
                connection.close()
            for index, name in enumerate(session.route_names[:-1]):
                before = [sum(proxy.hits.values()) for proxy in upstreams]
                assert local_proxy._probe_isolated_mihomo_batch_delay(session, name, 2, probe_url=url) > 0
                after = [sum(proxy.hits.values()) for proxy in upstreams]
                assert after[index] > before[index]
                assert all(after[other] == before[other] for other in range(len(upstreams)) if other != index)
            with pytest.raises(RuntimeError, match="HTTP 503"):
                local_proxy._probe_isolated_mihomo_batch_delay(session, session.route_names[-1], 2, probe_url=url)
            assert not sentinel.hits
    assert local_proxy._ISOLATED_MIHOMO_PROCESSES == before_processes
    assert local_proxy._ISOLATED_MIHOMO_DIRECTORIES == before_directories


@pytest.mark.parametrize("status", [503, 403, 302, 200, 204])
def test_real_batch_rejects_wrong_target_status_despite_successful_delay_api(monkeypatch, status):
    binary = binary_path()
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())
    with ExitStack() as stack:
        upstream = LoopbackProxy(stack, status=status)
        sentinel = LoopbackProxy(stack)
        url = f"http://127.0.0.1:{sentinel.server.server_port}/health"
        with local_proxy._isolated_mihomo_batch_session(binary, [upstream.node]) as session:
            name = session.route_names[0]
            if status == 204:
                assert local_proxy._probe_isolated_mihomo_batch_delay(session, name, 2, probe_url=url) > 0
            else:
                with pytest.raises(RuntimeError, match="预期 HTTP 204 验证"):
                    local_proxy._probe_isolated_mihomo_batch_delay(session, name, 2, probe_url=url)
                # The core's generic successful latency is not authoritative:
                # only its per-URL history records expected-status rejection.
                state = local_proxy._isolated_batch_controller_request(session, "/proxies/" + name)
                assert state["alive"] is True
                assert state["extra"][url]["alive"] is False
            assert upstream.hits
            assert not sentinel.hits


def test_real_batch_reduces_core_startups_for_full_node_list(monkeypatch):
    binary = binary_path()
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: binary)
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())
    original_delay = local_proxy._probe_isolated_mihomo_batch_delay
    original_popen = local_proxy.subprocess.Popen
    starts = []

    def popen(*args, **kwargs):
        starts.append(True)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(local_proxy.subprocess, "Popen", popen)
    with ExitStack() as stack:
        upstreams = [LoopbackProxy(stack) for _ in range(24)]
        sentinel = LoopbackProxy(stack)
        url = f"http://127.0.0.1:{sentinel.server.server_port}/health"
        nodes = [remote_proxy.ProxySubscriptionNode(i, {**proxy.node, "name": f"node-{i}"})
                 for i, proxy in enumerate(upstreams)]
        monkeypatch.setattr(local_proxy, "_probe_isolated_mihomo_batch_delay",
                            lambda session, name, timeout: original_delay(session, name, timeout, probe_url=url))

        def legacy_probe(proxy_url, label, _url, timeout):
            # Baseline still uses the old per-node isolated core and its real
            # mixed listener. Only its HTTPS Internet target is replaced.
            port = int(proxy_url.rsplit(":", 1)[1])
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            started = time.monotonic()
            try:
                connection.request("GET", url)
                response = connection.getresponse()
                assert response.status == 204
                return local_proxy.LocalAIProxyProbeResult(label, True, status=204,
                                                           elapsed_ms=max(1, int((time.monotonic() - started) * 1000)))
            finally:
                connection.close()

        monkeypatch.setattr(local_proxy, "_probe_ai_url_through_explicit_http_proxy", legacy_probe)
        started = time.monotonic()
        baseline = local_proxy.measure_proxy_node_data_plane_latencies(nodes, attempts=1, max_workers=4)
        old_seconds = time.monotonic() - started
        baseline_starts = len(starts)
        assert all(result.ok for result in baseline.values())
        assert baseline_starts == len(nodes)
        started = time.monotonic()
        quick = local_proxy.measure_proxy_node_data_plane_latencies(nodes, quick=True, attempts=1, max_workers=16)
        quick_seconds = time.monotonic() - started
        assert len(starts) - baseline_starts == 1, "quick mode must not silently use the per-node fallback"
        assert set(quick) == set(baseline)
        assert all(result.ok for result in quick.values())
        assert all(proxy.hits for proxy in upstreams)
        assert not sentinel.hits
        # Do not encode a flaky wall-time ratio as a correctness assertion.
        print(f"\nloopback 24-node probe: legacy={old_seconds:.3f}s/24 cores, "
              f"quick={quick_seconds:.3f}s/1 core, speedup={old_seconds / quick_seconds:.2f}x")
