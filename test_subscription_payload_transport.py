"""Exercise subscription HTTP payload integrity against an isolated loopback server."""

from contextlib import contextmanager
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import threading
from urllib import request as urlrequest
import zlib

import pytest

from core import remote_proxy


FIRST = b"proxies:\n - {name: first, type: http, server: first.example.test, port: 8080}\n"
SECOND = b" - {name: second, type: http, server: second.example.test, port: 8080}\n"


@contextmanager
def _subscription_server(payload, *, status=200, headers=None):
    response_headers = {"Content-Type": "application/yaml", "Content-Length": str(len(payload))}
    response_headers.update(headers or {})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(status)
            for key, value in response_headers.items():
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/subscription"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def _read_subscription(url, *, max_bytes=1024 * 1024):
    return remote_proxy._open_validated_proxy_subscription_request(
        urlrequest.Request(url), timeout=2, max_bytes=max_bytes, direct=True,
    )


def test_plain_subscription_payload_round_trip():
    with _subscription_server(FIRST + SECOND) as url:
        payload, content_type, charset = _read_subscription(url)
    assert payload == FIRST + SECOND
    assert content_type == "application/yaml"
    assert charset == "utf-8"


def test_subscription_rejects_incomplete_content_length_even_when_prefix_is_valid_yaml():
    with _subscription_server(FIRST, headers={"Content-Length": str(len(FIRST + SECOND))}) as url:
        with pytest.raises((remote_proxy._ProxySubscriptionPayloadError, OSError)):
            _read_subscription(url)


def test_subscription_rejects_unsolicited_partial_http_response():
    with _subscription_server(
        FIRST, status=206,
        headers={"Content-Range": f"bytes 0-{len(FIRST) - 1}/{len(FIRST + SECOND)}"},
    ) as url:
        with pytest.raises((remote_proxy._ProxySubscriptionPayloadError, OSError)):
            _read_subscription(url)


@pytest.mark.parametrize("missing_bytes", [1, 8])
def test_subscription_rejects_truncated_gzip_even_when_all_yaml_decodes(missing_bytes):
    encoded = gzip.compress(FIRST + SECOND)[:-missing_bytes]
    with _subscription_server(encoded, headers={"Content-Encoding": "gzip"}) as url:
        with pytest.raises((remote_proxy._ProxySubscriptionPayloadError, OSError, zlib.error)):
            _read_subscription(url)


def test_subscription_reads_all_concatenated_gzip_members():
    encoded = gzip.compress(FIRST) + gzip.compress(SECOND)
    with _subscription_server(encoded, headers={"Content-Encoding": "gzip"}) as url:
        payload, _, _ = _read_subscription(url)
    assert payload == FIRST + SECOND
    assert len(remote_proxy.parse_proxy_subscription_content(payload.decode("utf-8"))) == 2


@pytest.mark.parametrize("wrapper", [zlib.MAX_WBITS, -zlib.MAX_WBITS])
def test_subscription_rejects_truncated_deflate_even_when_prefix_is_valid_yaml(wrapper):
    compressor = zlib.compressobj(wbits=wrapper)
    encoded = compressor.compress(FIRST + SECOND) + compressor.flush()
    with _subscription_server(encoded[:-1], headers={"Content-Encoding": "deflate"}) as url:
        with pytest.raises((remote_proxy._ProxySubscriptionPayloadError, OSError, zlib.error)):
            _read_subscription(url)


@pytest.mark.parametrize("wrapper", [zlib.MAX_WBITS, -zlib.MAX_WBITS])
def test_subscription_accepts_both_deflate_wrappers(wrapper):
    compressor = zlib.compressobj(wbits=wrapper)
    encoded = compressor.compress(FIRST + SECOND) + compressor.flush()
    with _subscription_server(encoded, headers={"Content-Encoding": "deflate"}) as url:
        payload, _, _ = _read_subscription(url)
    assert payload == FIRST + SECOND


def test_concatenated_gzip_members_share_the_expanded_size_limit():
    encoded = gzip.compress(FIRST) + gzip.compress(SECOND * 200)
    with _subscription_server(encoded, headers={"Content-Encoding": "gzip"}) as url:
        with pytest.raises(ValueError, match="超过"):
            _read_subscription(url, max_bytes=1024)


@pytest.mark.parametrize("empty_member", [False, True])
@pytest.mark.parametrize("padding_length", [1, 64])
def test_gzip_accepts_trailing_zero_padding(empty_member, padding_length):
    encoded = gzip.compress(FIRST + SECOND)
    if empty_member:
        encoded += gzip.compress(b"")
    encoded += b"\x00" * padding_length
    with _subscription_server(encoded, headers={"Content-Encoding": "gzip"}) as url:
        payload, _, _ = _read_subscription(url)
    assert payload == FIRST + SECOND


@pytest.mark.parametrize("suffix", [b"garbage", b"\x1f\x8b", b"\x00garbage"])
def test_gzip_rejects_trailing_nonzero_garbage(suffix):
    with _subscription_server(gzip.compress(FIRST) + suffix, headers={"Content-Encoding": "gzip"}) as url:
        with pytest.raises(remote_proxy._ProxySubscriptionPayloadError):
            _read_subscription(url)


@pytest.mark.parametrize("damage", ["truncated", "crc"])
def test_gzip_rejects_damaged_later_member(damage):
    later = gzip.compress(SECOND)
    if damage == "truncated":
        later = later[:-1]
    else:
        later = later[:-8] + bytes([later[-8] ^ 1]) + later[-7:]
    with _subscription_server(gzip.compress(FIRST) + later, headers={"Content-Encoding": "gzip"}) as url:
        with pytest.raises(remote_proxy._ProxySubscriptionPayloadError):
            _read_subscription(url)


@pytest.mark.parametrize("slack", [0, 1])
def test_all_gzip_members_accept_exact_expanded_byte_limit(slack):
    padding = b"# padding for expanded-size boundary\n" * 20
    expected = FIRST + SECOND + padding
    encoded = gzip.compress(FIRST) + gzip.compress(SECOND + padding) + gzip.compress(b"")
    assert len(encoded) < len(expected)
    with _subscription_server(encoded, headers={"Content-Encoding": "gzip"}) as url:
        payload, _, _ = _read_subscription(url, max_bytes=len(expected) + slack)
    assert payload == expected


def test_all_gzip_members_reject_one_byte_over_expanded_limit():
    padding = b"# padding for expanded-size boundary\n" * 20
    expected = FIRST + SECOND + padding
    encoded = gzip.compress(FIRST) + gzip.compress(SECOND + padding)
    assert len(encoded) < len(expected) - 1
    with _subscription_server(encoded, headers={"Content-Encoding": "gzip"}) as url:
        with pytest.raises(ValueError, match="解压后超过"):
            _read_subscription(url, max_bytes=len(expected) - 1)


def test_empty_gzip_member_after_exact_limit_remains_valid():
    encoded = gzip.compress(FIRST) + gzip.compress(b"") + b"\x00"
    assert remote_proxy._decode_http_payload(encoded, "gzip", len(FIRST)) == FIRST


def test_nonempty_gzip_member_after_exact_limit_rejected():
    encoded = gzip.compress(FIRST) + gzip.compress(b"\n")
    with pytest.raises(ValueError, match="解压后超过"):
        remote_proxy._decode_http_payload(encoded, "gzip", len(FIRST))


@pytest.mark.parametrize("encodings", [
    ("gzip", "deflate"), ("deflate", "gzip"), ("gzip", "gzip"),
    ("identity", "gzip", "identity"), ("x-gzip", "deflate"),
])
def test_subscription_decodes_stacked_content_encodings_in_reverse_order(encodings):
    encoded = FIRST + SECOND
    for encoding in encodings:
        if encoding in {"gzip", "x-gzip"}:
            encoded = gzip.compress(encoded)
        elif encoding == "deflate":
            encoded = zlib.compress(encoded)
    with _subscription_server(encoded, headers={"Content-Encoding": ", ".join(encodings)}) as url:
        payload, _, _ = _read_subscription(url)
    assert payload == FIRST + SECOND


def test_stacked_content_encodings_reject_damaged_inner_payload():
    encoded = zlib.compress(gzip.compress(FIRST + SECOND)[:-1])
    with _subscription_server(encoded, headers={"Content-Encoding": "gzip, deflate"}) as url:
        with pytest.raises(remote_proxy._ProxySubscriptionPayloadError):
            _read_subscription(url)


def test_unsupported_content_encoding_is_retryable_payload_error():
    with _subscription_server(FIRST, headers={"Content-Encoding": "unknown"}) as url:
        with pytest.raises(remote_proxy._ProxySubscriptionPayloadError, match="不支持"):
            _read_subscription(url)


class _MemoryResponse(io.BytesIO):
    def __init__(self, payload, *, status=200, headers=None):
        super().__init__(payload)
        self.status = status
        self.headers = {"Content-Type": "application/yaml", "Content-Length": str(len(payload))}
        self.headers.update(headers or {})


@pytest.mark.parametrize("damage", ["content-length", "gzip-eof", "gzip-crc", "partial"])
@pytest.mark.parametrize("recovers", [False, True])
def test_fetch_retries_incomplete_subscription_without_publishing_partial_cache(monkeypatch, tmp_path, damage, recovers):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(remote_proxy, "_subscription_system_proxy_map", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(remote_proxy.urlrequest, "build_opener",
                        lambda *_args, **_kwargs: pytest.fail("cache regression must not open a live network route"))
    monkeypatch.setattr(remote_proxy, "_subscription_proxy_environment_diagnostic",
                        lambda _url: remote_proxy.ProxyEnvironmentDiagnostic())
    monkeypatch.setattr(remote_proxy, "_reconcile_subscription_proxy_environment",
                        lambda _url, diagnostic, **_kwargs: diagnostic)
    monkeypatch.setattr(remote_proxy, "_open_current_proxy_subscription_request",
                        lambda _request, **_kwargs: _MemoryResponse(FIRST))
    subscription_url = "https://subscription.example.test/config"
    previous = remote_proxy.fetch_proxy_subscription(subscription_url, retries=1)
    cache_path = Path(previous.saved_path)
    previous_state = remote_proxy.load_proxy_subscription_state()
    replacement = FIRST.replace(b"name: first", b"name: replacement") + SECOND
    calls = []

    def attempt(_request, **_kwargs):
        calls.append(_request.get_header("User-agent"))
        assert cache_path.read_bytes() == FIRST
        assert remote_proxy.load_proxy_subscription_state() == previous_state
        if recovers and len(calls) == 2:
            return _MemoryResponse(replacement)
        if damage == "content-length":
            return _MemoryResponse(FIRST, headers={"Content-Length": str(len(replacement))})
        if damage == "partial":
            return _MemoryResponse(FIRST, status=206)
        encoded = gzip.compress(replacement)
        if damage == "gzip-eof":
            encoded = encoded[:-1]
        else:
            encoded = encoded[:-8] + bytes([encoded[-8] ^ 1]) + encoded[-7:]
        return _MemoryResponse(encoded, headers={"Content-Encoding": "gzip"})

    monkeypatch.setattr(remote_proxy, "_open_current_proxy_subscription_request", attempt)
    if recovers:
        result = remote_proxy.fetch_proxy_subscription(subscription_url, retries=2, retry_base_delay=0)
        assert len(result.nodes) == 2
        assert result.nodes[0].node["name"] == "replacement"
        assert cache_path.read_bytes() == replacement
        assert remote_proxy.load_proxy_subscription_state() != previous_state
    else:
        with pytest.raises(ValueError):
            remote_proxy.fetch_proxy_subscription(subscription_url, retries=2, retry_base_delay=0)
        assert cache_path.read_bytes() == FIRST
        assert remote_proxy.load_proxy_subscription_state() == previous_state
    assert len(calls) == 2
