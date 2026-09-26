"""Real expected-status evaluation with disposable mihomo and loopback stubs."""
from contextlib import ExitStack
import threading
from urllib.parse import quote

from core import local_proxy
from core.local_proxy_constants import LOCAL_PROXY_AI_SERVICES, LOCAL_PROXY_BUILTIN_SITES
from test_mihomo_batch_latency_integration import LoopbackProxy, binary_path


def test_real_core_respects_explicit_target_status_sets_without_external_requests(monkeypatch):
    binary = binary_path()
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())
    statuses = (200, 204, 301, 400, 401, 403, 404, 429, 503)
    with ExitStack() as stack:
        upstreams = [LoopbackProxy(stack, status=status) for status in statuses]
        sentinel = LoopbackProxy(stack)
        with local_proxy._isolated_mihomo_batch_session(binary, [proxy.node for proxy in upstreams]) as session:
            for service in (*LOCAL_PROXY_AI_SERVICES, *LOCAL_PROXY_BUILTIN_SITES):
                expected = service["health_check_expected_status"]
                allowed = {int(value) for value in expected.split("/")}
                url = f"http://127.0.0.1:{sentinel.server.server_port}/{service['id']}"
                for status, name in zip(statuses, session.route_names):
                    path = (f"/proxies/{name}/delay?timeout=2000&expected=" + quote(expected, safe="")
                            + "&url=" + quote(url, safe=""))
                    try:
                        local_proxy._isolated_batch_controller_request(session, path, timeout=3)
                    except RuntimeError as exc:
                        assert status not in allowed and "HTTP 503" in str(exc)
                    state = local_proxy._isolated_batch_controller_request(session, "/proxies/" + name)
                    assert state["extra"][url]["alive"] is (status in allowed), (service["id"], status)
            assert all(proxy.hits for proxy in upstreams)
            assert not sentinel.hits  # No direct-route fallback, even on failure.
