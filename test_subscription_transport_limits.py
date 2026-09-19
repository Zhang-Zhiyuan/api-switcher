"""Bounded readers, decompression work, and deterministic response cleanup."""

import gzip
import io
import traceback
from http.client import InvalidURL
from urllib.error import HTTPError

import pytest

from core import remote_proxy


URL = "https://subscription.example.test/sub"
PAYLOAD = b"proxies:\n - {name: test, type: http, server: example.test, port: 8080}\n"


class ReadOnlyResponse:
    status = 200

    def __init__(self, payload, *, chunk_size=4096, headers=None):
        self.body = io.BytesIO(payload)
        self.chunk_size = chunk_size
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.body.close()

    def read(self, size):
        return self.body.read(min(size, self.chunk_size))


@pytest.mark.parametrize("with_length", [False, True])
def test_read_only_wrappers_do_not_silently_publish_the_first_chunk(monkeypatch, with_length):
    content = PAYLOAD + b"# complete content\n" * 5000
    response = ReadOnlyResponse(content, headers={"Content-Length": str(len(content))} if with_length else {})
    monkeypatch.setattr(remote_proxy, "_open_current_proxy_subscription_request", lambda *_args, **_kwargs: response)
    result = remote_proxy._open_proxy_subscription_request(
        remote_proxy.urlrequest.Request(URL), timeout=2, max_bytes=len(content),
    )
    assert result[0] == content
    assert response.body.closed


def test_read_only_wrapper_enforces_total_size_across_short_reads(monkeypatch):
    response = ReadOnlyResponse(PAYLOAD + b"x" * 2048, chunk_size=23)
    monkeypatch.setattr(remote_proxy, "_open_current_proxy_subscription_request", lambda *_args, **_kwargs: response)
    with pytest.raises(ValueError, match="超过"):
        remote_proxy._open_proxy_subscription_request(remote_proxy.urlrequest.Request(URL), timeout=2, max_bytes=1024)
    assert response.body.closed


@pytest.mark.parametrize("status", [403, 429, 503])
def test_http_error_response_is_closed_but_retry_metadata_remains(monkeypatch, status):
    body = io.BytesIO(b"synthetic error")
    error = HTTPError(URL, status, "synthetic", {"Retry-After": "2"}, body)

    def open_response(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(remote_proxy, "_open_current_proxy_subscription_request", open_response)
    with pytest.raises(HTTPError) as captured:
        remote_proxy._open_proxy_subscription_request(remote_proxy.urlrequest.Request(URL), timeout=2, max_bytes=1024)
    assert body.closed
    assert captured.value.code == status
    assert remote_proxy._subscription_retry_after_seconds(captured.value) == 2


def test_gzip_member_count_is_bounded_even_when_output_is_empty():
    encoded = gzip.compress(b"") * 1025
    with pytest.raises(ValueError, match="压缩.*过多"):
        remote_proxy._decode_http_payload(encoded, "gzip", 1024 * 1024)


def test_content_encoding_nesting_is_bounded_even_for_identity_layers():
    with pytest.raises(ValueError, match="压缩.*过多"):
        remote_proxy._decode_http_payload(PAYLOAD, ",".join(["identity"] * 9), 1024)


@pytest.mark.parametrize("route", ["current", "direct", "explicit"])
def test_every_subscription_route_rejects_ftp_redirects(monkeypatch, route):
    from test_subscription_payload_transport import _subscription_server

    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(remote_proxy.urlrequest.FTPHandler, "ftp_open",
                        lambda *_args: pytest.fail("HTTP proxy route escaped through FTP"))
    with _subscription_server(b"", status=302, headers={"Location": "ftp://subscription.example.test/sub"}) as url:
        kwargs = {"timeout": 2, "max_bytes": 1024}
        if route == "direct":
            kwargs["direct"] = True
        elif route == "explicit":
            kwargs["proxy_map"] = {"http": url, "https": url}
        with pytest.raises(ValueError, match="不支持"):
            remote_proxy._open_proxy_subscription_request(remote_proxy.urlrequest.Request(url), **kwargs)


def test_subscription_wrapper_applies_deadline_to_real_redirect_chain():
    from test_subscription_transport_policy import _slow_redirect_server

    with _slow_redirect_server() as url:
        start = remote_proxy.time.monotonic()
        with pytest.raises(TimeoutError):
            remote_proxy._open_proxy_subscription_request(
                remote_proxy.urlrequest.Request(url), timeout=1, deadline=start + 0.25,
                max_bytes=1024, direct=True,
            )
        assert remote_proxy.time.monotonic() - start < 0.65


@pytest.mark.parametrize("error_type", [InvalidURL, ValueError, RuntimeError])
def test_published_download_error_and_traceback_hide_subscription_credentials(monkeypatch, error_type):
    credential = "SYNTHETIC_PRIVATE_SUBSCRIPTION_VALUE"
    url = f"https://subscription.example.test/sub/{credential}?custom={credential} invalid"
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(remote_proxy, "_subscription_proxy_environment_diagnostic",
                        lambda _url: remote_proxy.ProxyEnvironmentDiagnostic())
    monkeypatch.setattr(remote_proxy, "_subscription_system_proxy_map", lambda *_args, **_kwargs: None)

    def fail(request, **_kwargs):
        raise error_type(f"synthetic transport rejected selector {request.selector!r}")

    monkeypatch.setattr(remote_proxy, "_open_current_proxy_subscription_request", fail)
    with pytest.raises((ValueError, RuntimeError)) as captured:
        remote_proxy.fetch_proxy_subscription(url, retries=1, retry_base_delay=0, persist=False)
    rendered = "".join(traceback.format_exception(captured.value))
    assert credential not in str(captured.value)
    assert credential not in rendered
    assert "transport rejected selector" in str(captured.value)


def test_http_cleanup_failure_does_not_mask_status(monkeypatch):
    class BrokenClose(io.BytesIO):
        def close(self):
            super().close()
            raise OSError("synthetic cleanup failure")

    body = BrokenClose()
    error = HTTPError(URL, 503, "Unavailable", {}, body)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(remote_proxy, "_open_current_proxy_subscription_request", fail)
    with pytest.raises(HTTPError) as captured:
        remote_proxy._open_proxy_subscription_request(remote_proxy.urlrequest.Request(URL), timeout=1, max_bytes=1024)
    assert captured.value.code == 503
    assert body.closed
