"""Synthetic-only redirect safety and subscription diagnostic regressions."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import threading
import time
from types import SimpleNamespace
from urllib import request as urlrequest

import pytest

from core.subscription_transport import SubscriptionRedirectHandler, subscription_error_message


class RedirectResponse(BytesIO):
    def read(self, *args):
        pytest.fail("redirect response bodies must not be drained")


def _redirect(source, target, *, strict=True, deadline=None, request_headers=None):
    handler = SubscriptionRedirectHandler(deadline, strict=strict)
    calls = []
    handler.parent = SimpleNamespace(open=lambda request, **kwargs: calls.append((request, kwargs)) or "response")
    request = urlrequest.Request(source, headers=request_headers or {})
    request.timeout = 1.0
    response = RedirectResponse(b"unbounded redirect response")
    result = handler.http_error_302(request, response, 302, "Found", {"Location": target})
    return result, calls, response


@pytest.mark.parametrize("scheme", ["ftp", "file", "data", "gopher"])
@pytest.mark.parametrize("strict", [False, True])
def test_redirects_cannot_leave_http_proxy_transport(scheme, strict):
    with pytest.raises(ValueError, match="不支持"):
        _redirect("https://subscription.example.test/config", f"{scheme}://other.example.test/file", strict=strict)


def test_strict_https_downgrade_is_rejected():
    with pytest.raises(ValueError, match="降级"):
        _redirect("https://subscription.example.test/config", "http://subscription.example.test/config")


def test_explicit_compatibility_mode_can_allow_https_downgrade():
    assert _redirect("https://subscription.example.test/config", "http://subscription.example.test/config", strict=False)[0] == "response"


def test_relative_signed_redirect_is_preserved_without_draining_body():
    result, calls, response = _redirect("https://subscription.example.test/old", "/new?a=A%2FB%2bC&sig=synthetic")
    assert result == "response"
    assert calls[0][0].full_url == "https://subscription.example.test/new?a=A%2FB%2bC&sig=synthetic"
    assert response.closed


def test_cross_origin_does_not_forward_credentials():
    _result, calls, _response = _redirect(
        "https://subscription.example.test/old", "https://other.example.test/new",
        request_headers={"Authorization": "Bearer synthetic", "Cookie": "session=synthetic", "Proxy-Authorization": "Basic synthetic", "User-Agent": "clash.meta"},
    )
    request = calls[0][0]
    assert request.get_header("Authorization") is None
    assert request.get_header("Cookie") is None
    assert request.get_header("Proxy-authorization") is None
    assert request.get_header("User-agent") == "clash.meta"


def test_same_origin_preserves_origin_authentication_but_rebuilds_proxy_auth():
    _result, calls, _response = _redirect(
        "https://subscription.example.test/old", "/new",
        request_headers={"Authorization": "Bearer synthetic", "Proxy-Authorization": "Basic synthetic"},
    )
    assert calls[0][0].get_header("Authorization") == "Bearer synthetic"
    assert calls[0][0].get_header("Proxy-authorization") is None


def test_expired_redirect_deadline_closes_response_without_next_request():
    handler = SubscriptionRedirectHandler(time.monotonic() - 1)
    handler.parent = SimpleNamespace(open=lambda *_args, **_kwargs: pytest.fail("deadline expired"))
    request = urlrequest.Request("https://subscription.example.test/old")
    response = RedirectResponse()
    with pytest.raises(TimeoutError):
        handler.http_error_302(request, response, 302, "Found", {"Location": "/new"})
    assert response.closed


def test_each_hop_gets_the_remaining_timeout(monkeypatch):
    monkeypatch.setattr("core.subscription_transport.time.monotonic", lambda: 100.0)
    _result, calls, _response = _redirect("https://subscription.example.test/old", "/new", deadline=100.125)
    assert calls[0][1]["timeout"] == 0.125


def test_loop_detection_remains_bounded():
    handler = SubscriptionRedirectHandler()
    request = urlrequest.Request("https://subscription.example.test/old")
    request.redirect_dict = {"https://subscription.example.test/new": handler.max_repeats}
    response = RedirectResponse()
    with pytest.raises(ValueError, match="循环"):
        handler.http_error_302(request, response, 302, "Found", {"Location": "/new"})
    assert response.closed


@contextmanager
def _slow_redirect_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            time.sleep(0.08)
            number = int(self.path.strip("/"))
            self.send_response(302)
            self.send_header("Location", f"/{number + 1}")
            self.send_header("Content-Length", "0")
            try:
                self.end_headers()
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/0"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_redirect_chain_does_not_reset_the_total_deadline():
    with _slow_redirect_server() as url:
        start = time.monotonic()
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}), SubscriptionRedirectHandler(start + 0.25))
        with pytest.raises(TimeoutError):
            opener.open(url, timeout=1)
        assert time.monotonic() - start < 0.65


@pytest.mark.parametrize("url", [
    "https://subscription.example.test/sub/SYNTHETIC_PATH_CREDENTIAL",
    "https://subscription.example.test/config?unusual=SYNTHETIC_QUERY_CREDENTIAL",
    "https://synthetic-user:SYNTHETIC_PASSWORD@subscription.example.test/config",
])
def test_diagnostics_hide_original_url_and_unnamed_credentials(url):
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    selector = parsed.path + ("?" + parsed.query if parsed.query else "")
    error = RuntimeError(f"invalid URL {url!r}; invalid selector {selector!r}")
    message = subscription_error_message(error, url=url)
    assert "SYNTHETIC_" not in message
    assert url not in message


def test_diagnostics_hide_foreign_redirect_url_and_keep_error_type_text():
    message = subscription_error_message("redirect failed: ftp://other.example.test/arbitrary/SYNTHETIC_PATH?weird=SYNTHETIC_QUERY")
    assert "SYNTHETIC_" not in message
    assert "redirect failed" in message


def test_diagnostics_remain_bounded():
    assert len(subscription_error_message("x" * 5000, max_length=60)) == 60


def test_short_format_query_values_do_not_erase_network_error_details():
    message = subscription_error_message(
        "WinError 10061: subscription request using clash was refused",
        url="https://subscription.example.test/config?type=1&format=clash",
    )
    assert message == "WinError 10061: subscription request using clash was refused"


def test_short_authentication_query_values_remain_secret():
    message = subscription_error_message(
        "request rejected: short credential xy", url="https://subscription.example.test/config?token=xy",
    )
    assert "xy" not in message


def test_redirect_without_location_returns_control_without_reading_response():
    handler = SubscriptionRedirectHandler()
    response = RedirectResponse()
    assert handler.http_error_302(urlrequest.Request("https://subscription.example.test/config"), response, 302, "Found", {}) is None
    # Ownership remains with urllib's HTTPError and the caller's error cleanup.
    assert not response.closed
    response.close()
