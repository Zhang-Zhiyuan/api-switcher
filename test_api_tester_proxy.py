"""Regression tests for API tester proxy diagnostics."""

import urllib.request

from core.api_tester import APITester


def test_invalid_loopback_proxy_is_detected_without_mutating_environment(monkeypatch):
    APITester._proxy_check_cache.clear()
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897", "all": "http://127.0.0.1:17897"},
    )

    def refused(*_args, **_kwargs):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr("core.api_tester.socket.create_connection", refused)

    warning = APITester._invalid_local_proxy_warning("https://gateway.example/v1/models")

    assert "127.0.0.1:17897" in warning
    assert "临时直连" in warning


def test_non_loopback_proxy_is_not_automatically_bypassed(monkeypatch):
    APITester._proxy_check_cache.clear()
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://proxy.example:8080"},
    )

    assert APITester._invalid_local_proxy_warning("https://gateway.example/v1/models") == ""


def test_urlopen_bypasses_refused_loopback_proxy(monkeypatch):
    APITester._proxy_check_cache.clear()
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://localhost:17897"},
    )
    def refused(*_args, **_kwargs):
        raise ConnectionRefusedError()

    monkeypatch.setattr("core.api_tester.socket.create_connection", refused)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    calls = []

    class DirectOpener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: DirectOpener())
    request = urllib.request.Request("https://gateway.example/v1/models")

    response, warning = APITester._urlopen(request, timeout=7)

    assert isinstance(response, Response)
    assert calls == [(request.full_url, 7)]
    assert "localhost:17897" in warning
