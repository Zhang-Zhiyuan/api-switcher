from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import local_proxy, remote_proxy


def _node(
    index: int,
    name: str,
    server: str,
    node_type: str = "vless",
) -> remote_proxy.ProxySubscriptionNode:
    return remote_proxy.ProxySubscriptionNode(
        index,
        remote_proxy.parse_proxy_node(
            f"{{ name: {name}, type: {node_type}, server: {server}, port: 443 }}"
        ),
    )


def _latency(item: remote_proxy.ProxySubscriptionNode, value: int):
    key = remote_proxy.proxy_subscription_node_key(item)
    return remote_proxy.ProxyNodeLatencyResult(key, True, latency_ms=value, attempts=3)


def _stability(item, stable: bool, detail: str = "", application_latency_ms: int = 100):
    key = remote_proxy.proxy_subscription_node_key(item)
    return local_proxy.LocalProxyNodeStabilityResult(
        node_key=key,
        stable=False,
        short_stable=stable,
        total_successes=12 if stable else 3,
        total_attempts=12,
        openai_successes=3 if stable else 1,
        application_latency_ms=application_latency_ms,
        detail=detail,
    )


def _deep_transport(ok: bool = True, detail: str = ""):
    successes = local_proxy.LOCAL_PROXY_DEEP_PROBE_ROUNDS if ok else 1
    return local_proxy.LocalProxyDeepTransportProbeResult(
        ok=ok,
        transfer_ok=ok,
        transfer_successes=successes,
        transfer_attempts=local_proxy.LOCAL_PROXY_DEEP_PROBE_ROUNDS,
        downloaded_bytes=successes * local_proxy.LOCAL_PROXY_DEEP_DOWNLOAD_BYTES,
        uploaded_bytes=successes * local_proxy.LOCAL_PROXY_DEEP_UPLOAD_BYTES,
        median_ms=500,
        codex_compact_ok=ok,
        codex_compact_elapsed_ms=120,
        codex_compact_detail="compact path 401" if ok else "compact path failed",
        detail=detail or ("deep passed" if ok else "deep failed"),
    )


def test_select_stable_node_tries_next_tcp_ranked_candidate(monkeypatch):
    first = _node(1, "美国-fast", "fast.example.com")
    second = _node(2, "美国-stable", "stable.example.com")
    latencies = {
        remote_proxy.proxy_subscription_node_key(first): _latency(first, 10),
        remote_proxy.proxy_subscription_node_key(second): _latency(second, 30),
    }
    probed = []

    @contextmanager
    def fake_session(_binary, node):
        yield SimpleNamespace(proxy_url=f"http://127.0.0.1:{17800 + len(probed)}")

    def fake_probe(_proxy_url, key, **_kwargs):
        probed.append(key)
        return _stability(
            first if key == remote_proxy.proxy_subscription_node_key(first) else second,
            len(probed) == 2,
        )

    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("mihomo.exe"))
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", fake_session)
    monkeypatch.setattr(local_proxy, "_probe_local_proxy_node_stability", fake_probe)
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_proxy_node_deep_transport",
        lambda *_args, **_kwargs: _deep_transport(True),
    )

    selected, results = local_proxy.select_stable_local_proxy_node(
        [second, first],
        latencies,
    )

    assert selected is second
    assert probed == [
        remote_proxy.proxy_subscription_node_key(first),
        remote_proxy.proxy_subscription_node_key(second),
    ]
    assert results[probed[0]].stable is False
    assert results[probed[1]].stable is True


def test_select_stable_node_returns_none_when_all_candidates_fail(monkeypatch):
    first = _node(1, "美国-one", "one.example.com")
    second = _node(2, "美国-two", "two.example.com")
    latencies = {
        remote_proxy.proxy_subscription_node_key(first): _latency(first, 20),
        remote_proxy.proxy_subscription_node_key(second): _latency(second, 30),
    }

    @contextmanager
    def fake_session(_binary, _node_value):
        yield SimpleNamespace(proxy_url="http://127.0.0.1:18000")

    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("mihomo.exe"))
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", fake_session)
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_proxy_node_stability",
        lambda _url, key, **_kwargs: local_proxy.LocalProxyNodeStabilityResult(
            key,
            False,
            total_attempts=12,
            detail="AI 转发不稳定",
        ),
    )

    selected, results = local_proxy.select_stable_local_proxy_node([first, second], latencies)

    assert selected is None
    assert len(results) == 2
    assert all(not result.stable for result in results.values())


def test_udp_native_node_skips_tcp_prefilter_and_can_be_selected(monkeypatch):
    udp_node = _node(1, "美国-hy2", "hy2.example.com", "hysteria2")
    key = remote_proxy.proxy_subscription_node_key(udp_node)
    probed = []

    @contextmanager
    def fake_session(_binary, node):
        probed.append(node["type"])
        yield SimpleNamespace(proxy_url="http://127.0.0.1:18000")

    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("mihomo.exe"))
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", fake_session)
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_proxy_node_stability",
        lambda *_args, **_kwargs: _stability(udp_node, True, application_latency_ms=88),
    )
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_proxy_node_deep_transport",
        lambda *_args, **_kwargs: _deep_transport(True),
    )

    selected, results = local_proxy.select_stable_local_proxy_node(
        [udp_node],
        {key: remote_proxy.ProxyNodeLatencyResult(key, False, detail="TCP 不适用")},
    )

    assert selected is udp_node
    assert results[key].stable is True
    assert probed == ["hysteria2", "hysteria2"]


def test_stable_selection_uses_ai_application_latency_across_protocols(monkeypatch):
    tcp_node = _node(1, "美国-tcp", "tcp.example.com")
    udp_node = _node(2, "日本-tuic", "tuic.example.com", "tuic")
    tcp_key = remote_proxy.proxy_subscription_node_key(tcp_node)

    @contextmanager
    def fake_session(_binary, node):
        yield SimpleNamespace(proxy_url=node["server"])

    def fake_probe(proxy_url, _key, **_kwargs):
        item = tcp_node if proxy_url == "tcp.example.com" else udp_node
        elapsed = 240 if item is tcp_node else 90
        return _stability(item, True, application_latency_ms=elapsed)

    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("mihomo.exe"))
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", fake_session)
    monkeypatch.setattr(local_proxy, "_probe_local_proxy_node_stability", fake_probe)
    deep_probed = []
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_proxy_node_deep_transport",
        lambda proxy_url, **_kwargs: deep_probed.append(proxy_url) or _deep_transport(True),
    )

    selected, _results = local_proxy.select_stable_local_proxy_node(
        [tcp_node, udp_node],
        {tcp_key: _latency(tcp_node, 20)},
    )

    assert selected is udp_node
    assert deep_probed == ["tuic.example.com"]


def test_stability_probe_rejects_hong_kong_chatgpt_exit(monkeypatch):
    def fake_probe(_proxy_url, label, _url, _timeout):
        return local_proxy.LocalAIProxyProbeResult(
            label,
            True,
            status=200 if label == "ChatGPT 出口" else 401,
            exit_country="HK" if label == "ChatGPT 出口" else "",
        )

    monkeypatch.setattr(local_proxy, "_probe_ai_url_through_explicit_http_proxy", fake_probe)

    result = local_proxy._probe_local_proxy_node_stability(
        "http://127.0.0.1:18000",
        "node-key",
        rounds=3,
    )

    assert result.total_successes == 12
    assert result.openai_successes == 3
    assert result.exit_country == "HK"
    assert result.stable is False
    assert "香港" in result.detail


def test_stability_requires_openai_api_every_round_even_with_eleven_of_twelve(monkeypatch):
    counts = {}

    def fake_probe(_proxy_url, label, _url, _timeout):
        counts[label] = counts.get(label, 0) + 1
        ok = not (label == "OpenAI API" and counts[label] == 3)
        return local_proxy.LocalAIProxyProbeResult(
            label,
            ok,
            status=200 if ok else 503,
            exit_country="US" if label == "ChatGPT 出口" and ok else "",
        )

    monkeypatch.setattr(local_proxy, "_probe_ai_url_through_explicit_http_proxy", fake_probe)

    result = local_proxy._probe_local_proxy_node_stability(
        "http://127.0.0.1:18000",
        "node-key",
        rounds=3,
    )

    assert result.total_successes == 11
    assert result.openai_successes == 2
    assert result.stable is False


def test_stability_accepts_exact_eleven_of_twelve_when_openai_api_is_three_of_three(monkeypatch):
    counts = {}

    def fake_probe(_proxy_url, label, _url, _timeout):
        counts[label] = counts.get(label, 0) + 1
        ok = not (label == "Claude/Anthropic" and counts[label] == 3)
        return local_proxy.LocalAIProxyProbeResult(
            label,
            ok,
            status=200 if label == "ChatGPT 出口" else (401 if ok else 503),
            exit_country="US" if label == "ChatGPT 出口" else "",
        )

    monkeypatch.setattr(local_proxy, "_probe_ai_url_through_explicit_http_proxy", fake_probe)

    result = local_proxy._probe_local_proxy_node_stability(
        "http://127.0.0.1:18000",
        "node-key",
        rounds=3,
    )

    assert result.total_successes == 11
    assert result.service_successes["Claude/Anthropic"] == 2
    assert result.short_stable is True
    assert result.stable is False


def test_download_exact_bytes_rejects_midstream_truncation(monkeypatch):
    class FakeResponse:
        status = 200

        def __init__(self):
            self._chunks = [b"x" * 700, b"x" * 200, b""]

        def read(self, _size):
            return self._chunks.pop(0)

        @staticmethod
        def getheader(name):
            return "1024" if name == "Content-Length" else None

    class FakeConnection:
        def request(self, *_args, **_kwargs):
            return None

        @staticmethod
        def getresponse():
            return FakeResponse()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(
        local_proxy,
        "_open_explicit_https_proxy_connection",
        lambda *_args, **_kwargs: (
            FakeConnection(),
            SimpleNamespace(),
            "/__down?bytes=1024",
            "speed.cloudflare.com",
        ),
    )

    ok, received, detail = local_proxy._download_exact_bytes_through_explicit_http_proxy(
        "http://127.0.0.1:18000",
        "https://speed.cloudflare.com/__down?bytes=1024",
        1024,
    )

    assert ok is False
    assert received == 900
    assert "实收 900" in detail


def test_download_trickle_exceeding_overall_deadline_fails_and_closes(monkeypatch):
    clock = [100.0]

    class FakeResponse:
        status = 200

        @staticmethod
        def getheader(name):
            return "1024" if name == "Content-Length" else None

        @staticmethod
        def read(_size):
            clock[0] += 0.02
            return b"x" * 64

    class FakeConnection:
        closed = False

        def request(self, *_args, **_kwargs):
            return None

        @staticmethod
        def getresponse():
            return FakeResponse()

        def close(self):
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(
        local_proxy,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    monkeypatch.setattr(
        local_proxy,
        "LOCAL_PROXY_DEEP_OPERATION_DEADLINE_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        local_proxy,
        "_open_explicit_https_proxy_connection",
        lambda *_args, **_kwargs: (
            connection,
            SimpleNamespace(),
            "/__down?bytes=1024",
            "speed.cloudflare.com",
        ),
    )

    ok, received, detail = local_proxy._download_exact_bytes_through_explicit_http_proxy(
        "http://127.0.0.1:18000",
        "https://speed.cloudflare.com/__down?bytes=1024",
        1024,
    )

    assert ok is False
    assert received == 0
    assert "截止时间" in detail
    assert connection.closed is True


@pytest.mark.parametrize(
    ("response_headers", "expected_ok", "detail_fragment"),
    [
        ([('Cf-MeTa-UpLoAd-ByTeS', "131073")], True, "服务端已核对"),
        ([], False, "缺少 cf-meta-upload-bytes"),
        ([('CF-META-UPLOAD-BYTES', "not-a-number")], False, "合法非负整数"),
        ([('cf-meta-upload-bytes', "131072")], False, "服务端回传 131072"),
    ],
)
def test_upload_requires_exact_case_insensitive_cloudflare_receipt(
    monkeypatch,
    response_headers,
    expected_ok,
    detail_fragment,
):
    class FakeResponse:
        status = 200

        @staticmethod
        def getheaders():
            return response_headers

    class FakeConnection:
        def __init__(self):
            self.sent = []
            self.closed = False

        def putrequest(self, *_args, **_kwargs):
            return None

        def putheader(self, *_args, **_kwargs):
            return None

        def endheaders(self):
            return None

        def send(self, chunk):
            self.sent.append(bytes(chunk))

        @staticmethod
        def getresponse():
            return FakeResponse()

        def close(self):
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(
        local_proxy,
        "_open_explicit_https_proxy_connection",
        lambda *_args, **_kwargs: (
            connection,
            SimpleNamespace(),
            "/__up",
            "speed.cloudflare.com",
        ),
    )

    ok, uploaded, detail = local_proxy._upload_exact_bytes_through_explicit_http_proxy(
        "http://127.0.0.1:18000",
        "https://speed.cloudflare.com/__up",
        131_073,
    )

    assert ok is expected_ok
    assert uploaded == (131_073 if expected_ok else 0)
    assert detail_fragment in detail
    assert [len(chunk) for chunk in connection.sent] == [65_536, 65_536, 1]
    assert connection.closed is True


def test_upload_cancellation_between_chunks_fails_and_closes(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.send_calls = 0
            self.closed = False

        def putrequest(self, *_args, **_kwargs):
            return None

        def putheader(self, *_args, **_kwargs):
            return None

        def endheaders(self):
            return None

        def send(self, _chunk):
            self.send_calls += 1
            local_proxy._ISOLATED_MIHOMO_SHUTTING_DOWN.set()

        @staticmethod
        def getresponse():
            raise AssertionError("取消后不应等待上传响应")

        def close(self):
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(
        local_proxy,
        "_open_explicit_https_proxy_connection",
        lambda *_args, **_kwargs: (
            connection,
            SimpleNamespace(),
            "/__up",
            "speed.cloudflare.com",
        ),
    )

    local_proxy._ISOLATED_MIHOMO_SHUTTING_DOWN.clear()
    try:
        ok, uploaded, detail = local_proxy._upload_exact_bytes_through_explicit_http_proxy(
            "http://127.0.0.1:18000",
            "https://speed.cloudflare.com/__up",
            128 * 1024,
        )
    finally:
        local_proxy._ISOLATED_MIHOMO_SHUTTING_DOWN.clear()

    assert ok is False
    assert uploaded == 0
    assert "取消上传请求体" in detail
    assert connection.send_calls == 1
    assert connection.closed is True


def test_compact_trickle_exceeding_overall_deadline_fails_and_closes(monkeypatch):
    clock = [200.0]

    class FakeResponse:
        status = 401

        @staticmethod
        def getheader(name):
            return "64" if name == "Content-Length" else None

        @staticmethod
        def read(_size):
            clock[0] += 0.02
            return b"x" * 64

    class FakeConnection:
        closed = False

        def putrequest(self, *_args, **_kwargs):
            return None

        def putheader(self, *_args, **_kwargs):
            return None

        def endheaders(self):
            return None

        def send(self, _chunk):
            return None

        @staticmethod
        def getresponse():
            return FakeResponse()

        def close(self):
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(
        local_proxy,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    monkeypatch.setattr(
        local_proxy,
        "LOCAL_PROXY_DEEP_OPERATION_DEADLINE_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        local_proxy,
        "_open_explicit_https_proxy_connection",
        lambda *_args, **_kwargs: (
            connection,
            SimpleNamespace(),
            "/v1/responses/compact",
            "api.openai.com",
        ),
    )

    result = local_proxy._post_unauthenticated_json_through_explicit_http_proxy(
        "http://127.0.0.1:18000",
        "https://api.openai.com/v1/responses/compact",
        b"{}",
    )

    assert result.ok is False
    assert "截止时间" in result.detail
    assert connection.closed is True


def test_cloudflare_transfer_round_rejects_upload_failure(monkeypatch):
    monkeypatch.setattr(
        local_proxy,
        "_download_exact_bytes_through_explicit_http_proxy",
        lambda *_args, **_kwargs: (
            True,
            local_proxy.LOCAL_PROXY_DEEP_DOWNLOAD_BYTES,
            "download complete",
        ),
    )
    monkeypatch.setattr(
        local_proxy,
        "_upload_exact_bytes_through_explicit_http_proxy",
        lambda *_args, **_kwargs: (False, 0, "connection reset"),
    )

    result = local_proxy._probe_cloudflare_transfer_round("http://127.0.0.1:18000")

    assert result.ok is False
    assert result.downloaded_bytes == local_proxy.LOCAL_PROXY_DEEP_DOWNLOAD_BYTES
    assert result.uploaded_bytes == 0
    assert "connection reset" in result.detail


@pytest.mark.parametrize("status", [403, 429])
def test_compact_path_preflight_rejects_policy_and_rate_limit(status):
    ok, detail = local_proxy._classify_codex_compact_probe_response(
        status,
        '{"error":{"message":"request rejected","type":"access_denied"}}',
    )

    assert ok is False
    assert f"HTTP {status}" in detail


def test_compact_path_preflight_uses_fixed_harmless_payload_without_real_token(monkeypatch):
    calls = []

    def fake_post(proxy_url, url, payload, **kwargs):
        calls.append((proxy_url, url, payload, kwargs))
        return local_proxy.LocalAIProxyProbeResult(
            "Codex 大请求网络近似",
            True,
            status=401,
            elapsed_ms=321,
            detail="structured 401",
        )

    monkeypatch.setattr(
        local_proxy,
        "_post_unauthenticated_json_through_explicit_http_proxy",
        fake_post,
    )

    result = local_proxy._probe_codex_compact_transport_quality(
        "http://127.0.0.1:18000"
    )

    assert result.ok is True
    assert result.successes == result.attempts == 1
    assert calls[0][1] == "https://api.openai.com/v1/responses/compact"
    assert len(calls[0][2]) == 128 * 1024
    assert b"api-switcher-network-probe-no-model" in calls[0][2]
    assert "未使用账号" in result.detail


def test_deep_transport_requires_two_complete_rounds_and_compact_path():
    rounds = iter(
        [
            local_proxy.LocalProxyTransferRoundResult(
                True,
                local_proxy.LOCAL_PROXY_DEEP_DOWNLOAD_BYTES,
                local_proxy.LOCAL_PROXY_DEEP_UPLOAD_BYTES,
                elapsed_ms=10_000,
            ),
            local_proxy.LocalProxyTransferRoundResult(
                True,
                local_proxy.LOCAL_PROXY_DEEP_DOWNLOAD_BYTES,
                local_proxy.LOCAL_PROXY_DEEP_UPLOAD_BYTES,
                elapsed_ms=20_000,
            ),
        ]
    )

    result = local_proxy._probe_local_proxy_node_deep_transport(
        "http://127.0.0.1:18000",
        transfer_probe=lambda *_args, **_kwargs: next(rounds),
        compact_probe=lambda *_args, **_kwargs: local_proxy.CodexCompactTransportProbeResult(
            True,
            successes=1,
            attempts=1,
            median_ms=500,
            detail="structured 401",
        ),
    )

    assert result.ok is True
    assert result.transfer_ok is True
    assert result.transfer_successes == result.transfer_attempts == 2
    assert result.codex_compact_ok is True
    # Slow links are allowed: byte completeness, not bandwidth, is the gate.
    assert result.median_ms == 20_000


def test_deep_transport_does_not_preflight_compact_after_incomplete_transfer():
    compact_calls = []
    rounds = iter(
        [
            local_proxy.LocalProxyTransferRoundResult(
                True,
                local_proxy.LOCAL_PROXY_DEEP_DOWNLOAD_BYTES,
                local_proxy.LOCAL_PROXY_DEEP_UPLOAD_BYTES,
            ),
            local_proxy.LocalProxyTransferRoundResult(
                False,
                local_proxy.LOCAL_PROXY_DEEP_DOWNLOAD_BYTES - 1,
                0,
                detail="truncated",
            ),
        ]
    )

    result = local_proxy._probe_local_proxy_node_deep_transport(
        "http://127.0.0.1:18000",
        transfer_probe=lambda *_args, **_kwargs: next(rounds),
        compact_probe=lambda *_args, **_kwargs: compact_calls.append(True),
    )

    assert result.ok is False
    assert result.transfer_ok is False
    assert compact_calls == []


def test_selection_tries_next_application_ranked_candidate_after_deep_failure(monkeypatch):
    first = _node(1, "美国-short-fast", "first.example.com")
    second = _node(2, "日本-deep-stable", "second.example.com")
    keys = {
        remote_proxy.proxy_subscription_node_key(first): first,
        remote_proxy.proxy_subscription_node_key(second): second,
    }
    deep_order = []

    @contextmanager
    def fake_session(_binary, node):
        yield SimpleNamespace(proxy_url=node["server"])

    def fake_short(_proxy_url, key, **_kwargs):
        item = keys[key]
        latency = 40 if item is first else 80
        return _stability(item, True, application_latency_ms=latency)

    def fake_deep(proxy_url, **_kwargs):
        deep_order.append(proxy_url)
        return _deep_transport(proxy_url == "second.example.com")

    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("mihomo.exe"))
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", fake_session)
    monkeypatch.setattr(local_proxy, "_probe_local_proxy_node_stability", fake_short)
    monkeypatch.setattr(local_proxy, "_probe_local_proxy_node_deep_transport", fake_deep)

    selected, results = local_proxy.select_stable_local_proxy_node(
        [first, second],
        {
            remote_proxy.proxy_subscription_node_key(first): _latency(first, 10),
            remote_proxy.proxy_subscription_node_key(second): _latency(second, 20),
        },
    )

    assert selected is second
    assert deep_order == ["first.example.com", "second.example.com"]
    assert results[remote_proxy.proxy_subscription_node_key(first)].stable is False
    assert results[remote_proxy.proxy_subscription_node_key(second)].stable is True


def test_selection_preserves_none_when_every_deep_candidate_fails(monkeypatch):
    first = _node(1, "美国-one", "one.example.com")
    second = _node(2, "日本-two", "two.example.com")

    @contextmanager
    def fake_session(_binary, node):
        yield SimpleNamespace(proxy_url=node["server"])

    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("mihomo.exe"))
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", fake_session)
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_proxy_node_stability",
        lambda _url, key, **_kwargs: _stability(
            first
            if key == remote_proxy.proxy_subscription_node_key(first)
            else second,
            True,
        ),
    )
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_proxy_node_deep_transport",
        lambda *_args, **_kwargs: _deep_transport(False),
    )

    selected, results = local_proxy.select_stable_local_proxy_node(
        [first, second],
        {
            remote_proxy.proxy_subscription_node_key(first): _latency(first, 10),
            remote_proxy.proxy_subscription_node_key(second): _latency(second, 20),
        },
    )

    assert selected is None
    assert len(results) == 2
    assert all(result.short_stable and not result.stable for result in results.values())


def test_explicit_connect_probe_never_obeys_no_proxy(monkeypatch):
    calls = []

    class FakeResponse:
        status = 200

        @staticmethod
        def read(_size):
            return b"fl=1\nip=203.0.113.9\nloc=US\ncolo=SJC\n"

        @staticmethod
        def getheaders():
            return []

    class FakeConnection:
        def __init__(self, host, port, **kwargs):
            calls.append(("connect", host, port, kwargs["timeout"]))

        def set_tunnel(self, host, port):
            calls.append(("tunnel", host, port))

        def request(self, method, path, headers):
            calls.append(("request", method, path, headers["Host"], headers["User-Agent"]))

        @staticmethod
        def getresponse():
            return FakeResponse()

        @staticmethod
        def close():
            return None

    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(local_proxy.http.client, "HTTPSConnection", FakeConnection)

    result = local_proxy._probe_ai_url_through_explicit_http_proxy(
        "http://127.0.0.1:19001",
        "ChatGPT 出口",
        "https://chatgpt.com/cdn-cgi/trace",
    )

    assert result.ok is True
    assert result.exit_country == "US"
    assert calls[0][:3] == ("connect", "127.0.0.1", 19001)
    assert ("tunnel", "chatgpt.com", 443) in calls
    assert ("request", "GET", "/cdn-cgi/trace", "chatgpt.com", "API-Switcher/1.0") in calls


@pytest.mark.parametrize("status", [429, 500, 503])
def test_ai_probe_rate_limit_and_server_errors_are_failures(status):
    ok, _country, _detail = local_proxy._classify_ai_probe_response(
        "ChatGPT 出口",
        status,
        "ip=203.0.113.1\nloc=US\ncolo=SJC\n",
    )

    assert ok is False


def test_regular_local_chatgpt_probe_label_uses_trace_classifier():
    ok, country, detail = local_proxy._classify_ai_probe_response(
        "OpenAI/ChatGPT",
        200,
        "ip=203.0.113.1\nloc=US\ncolo=SJC\n",
    )

    assert ok is True
    assert country == "US"
    assert "ChatGPT 出口 loc=US" in detail


@pytest.mark.parametrize("label", ["OpenAI API", "Claude/Anthropic"])
def test_ai_probe_rejects_policy_or_region_403(label):
    ok, _country, _detail = local_proxy._classify_ai_probe_response(
        label,
        403,
        '{"error":{"message":"unsupported country region policy","type":"access_denied"}}',
        {"request-id": "req_123"},
    )

    assert ok is False


def test_gemini_probe_rejects_noncredential_policy_403():
    ok, _country, _detail = local_proxy._classify_ai_probe_response(
        "Gemini/Google AI",
        403,
        '{"error":{"code":403,"message":"API disabled by organization policy","status":"PERMISSION_DENIED"}}',
    )

    assert ok is False


@pytest.mark.parametrize(
    ("label", "status", "body", "headers"),
    [
        (
            "OpenAI API",
            401,
            '{"error":{"message":"Incorrect API key provided","type":"invalid_request_error","code":"invalid_api_key"}}',
            {},
        ),
        (
            "Claude/Anthropic",
            401,
            '{"type":"error","error":{"type":"authentication_error","message":"x-api-key header is required"}}',
            {},
        ),
        (
            "Gemini/Google AI",
            403,
            '{"error":{"code":403,"message":"Please use API Key for Google Generative Language"}}',
            {},
        ),
    ],
)
def test_ai_probe_accepts_identifiable_credential_free_service_responses(
    label,
    status,
    body,
    headers,
):
    ok, _country, _detail = local_proxy._classify_ai_probe_response(
        label,
        status,
        body,
        headers,
    )

    assert ok is True


def test_isolated_mihomo_always_stops_and_removes_secret_config(monkeypatch, tmp_path):
    created = {}

    class FakeProcess:
        def __init__(self, args, **kwargs):
            self.args = args
            self.cwd = Path(kwargs["cwd"])
            self.terminated = False
            self.killed = False
            created["process"] = self
            created["directory"] = self.cwd
            created["config"] = (self.cwd / "config.yaml").read_text(encoding="utf-8")

        def poll(self):
            return 0 if self.terminated or self.killed else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        @staticmethod
        def wait(timeout):
            return 0

    node = remote_proxy.parse_proxy_node(
        "{ name: secret-node, type: vless, server: node.example.com, port: 443, uuid: top-secret-token }"
    )
    monkeypatch.setattr(local_proxy.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)

    with pytest.raises(RuntimeError, match="probe body failed"):
        with local_proxy._isolated_mihomo_session(tmp_path / "mihomo.exe", node) as session:
            assert session.config_path.is_file()
            assert "top-secret-token" in session.config_path.read_text(encoding="utf-8")
            raise RuntimeError("probe body failed")

    assert created["process"].terminated is True
    assert created["process"].killed is False
    assert not created["directory"].exists()
    assert "top-secret-token" in created["config"]


@pytest.mark.parametrize("reserved_name", ["DIRECT", "REJECT", "PASS", "AI-PROXY"])
def test_isolated_probe_config_never_routes_to_reserved_display_name(reserved_name):
    node = remote_proxy.parse_proxy_node(
        f"{{ name: {reserved_name}, type: vless, server: node.example.com, port: 443 }}"
    )

    config = local_proxy._build_isolated_mihomo_probe_config(node, 18000)
    parsed = remote_proxy.yaml.safe_load(config)

    assert parsed["proxies"][0]["name"] == "API-SWITCHER-PROBE-NODE"
    assert parsed["proxy-groups"] == [
        {
            "name": "API-SWITCHER-PROBE-GROUP",
            "type": "select",
            "proxies": ["API-SWITCHER-PROBE-NODE"],
        }
    ]
    assert parsed["rules"] == ["MATCH,API-SWITCHER-PROBE-GROUP"]


def test_exit_cleanup_stops_registered_process_and_removes_secret_directory(
    monkeypatch,
    tmp_path,
):
    class FakeProcess:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            return 0

    process = FakeProcess()
    secret_dir = tmp_path / "probe-secret"
    secret_dir.mkdir()
    (secret_dir / "config.yaml").write_text("uuid: secret", encoding="utf-8")
    with local_proxy._ISOLATED_MIHOMO_LOCK:
        local_proxy._ISOLATED_MIHOMO_PROCESSES.add(process)
        local_proxy._ISOLATED_MIHOMO_DIRECTORIES.add(secret_dir)

    local_proxy.cleanup_isolated_mihomo_sessions_for_shutdown()

    assert process.stopped is True
    assert not secret_dir.exists()
    assert process not in local_proxy._ISOLATED_MIHOMO_PROCESSES
    assert secret_dir not in local_proxy._ISOLATED_MIHOMO_DIRECTORIES
    local_proxy._ISOLATED_MIHOMO_SHUTTING_DOWN.clear()


def test_stable_selection_never_calls_managed_proxy_mutators(monkeypatch):
    item = _node(1, "美国-safe", "safe.example.com")
    key = remote_proxy.proxy_subscription_node_key(item)
    forbidden = (
        "install_local_ai_proxy",
        "reload_local_ai_proxy",
        "_start_local_mihomo",
        "_save_state",
        "_apply_local_env",
        "_apply_local_vscode_proxy",
        "_apply_windows_system_proxy",
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("managed proxy state must not be mutated")

    for name in forbidden:
        monkeypatch.setattr(local_proxy, name, unexpected)

    @contextmanager
    def fake_session(_binary, _node_value):
        yield SimpleNamespace(proxy_url="http://127.0.0.1:18000")

    setup_order = []
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: setup_order.append("dirs"))
    monkeypatch.setattr(
        local_proxy,
        "_ensure_mihomo_binary",
        lambda: setup_order.append("binary") or Path("mihomo.exe"),
    )
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", fake_session)
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_proxy_node_stability",
        lambda _url, node_key, **_kwargs: local_proxy.LocalProxyNodeStabilityResult(
            node_key,
            True,
            total_successes=12,
            total_attempts=12,
            openai_successes=3,
        ),
    )
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_proxy_node_deep_transport",
        lambda *_args, **_kwargs: _deep_transport(True),
    )

    selected, results = local_proxy.select_stable_local_proxy_node(
        [item],
        {key: _latency(item, 25)},
    )

    assert selected is item
    assert results[key].stable is True
    assert setup_order == ["dirs", "binary"]


def test_reload_refuses_to_apply_without_recoverable_managed_config(monkeypatch, tmp_path):
    missing_config = tmp_path / "missing-config.yaml"
    monkeypatch.setattr(local_proxy.os, "name", "nt")
    monkeypatch.setattr(
        local_proxy,
        "_load_state",
        lambda: {"mixed_port": 17897, "config_path": str(missing_config)},
    )
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda *_args, **_kwargs: local_proxy.LocalAIProxyStatus(
            installed=True,
            running=True,
            config_path=str(missing_config),
            proxy_url="http://127.0.0.1:17897",
        ),
    )
    monkeypatch.setattr(
        local_proxy,
        "_reload_local_mihomo_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("controller must not receive an unrecoverable update")
        ),
    )

    with pytest.raises(RuntimeError, match="无法保证失败回滚"):
        local_proxy.reload_local_ai_proxy(
            "{ name: new, type: vless, server: new.example.com, port: 443 }"
        )

    assert not missing_config.exists()
