"""Empty diagnostic errors produce terminal failures, never secondary IndexError."""

from contextlib import contextmanager
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from core import local_proxy, remote_proxy


@pytest.fixture(params=["", " \n\t "])
def fail_empty(request):
    def fail(*_args, **_kwargs):
        raise TimeoutError(request.param)

    return fail


def _node(index=1):
    return {
        "name": f"US-synthetic-{index}",
        "type": "vless",
        "server": f"synthetic-{index}.example.invalid",
        "port": 443,
        "uuid": "00000000-0000-0000-0000-000000000000",
    }


@pytest.mark.parametrize("operation", ["compact", "download", "upload"])
def test_local_deep_io_empty_error_returns_failure(monkeypatch, fail_empty, operation):
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())
    monkeypatch.setattr(local_proxy, "_open_explicit_https_proxy_connection", fail_empty)
    args = ("http://127.0.0.1:1234", "https://synthetic.example.invalid/probe")
    if operation == "compact":
        result = local_proxy._post_unauthenticated_json_through_explicit_http_proxy(*args, b"{}")
        assert not result.ok
        assert result.detail == "TimeoutError"
    else:
        probe = (
            local_proxy._download_exact_bytes_through_explicit_http_proxy
            if operation == "download"
            else local_proxy._upload_exact_bytes_through_explicit_http_proxy
        )
        assert probe(*args, 32) == (False, 0, "TimeoutError")


def test_local_parallel_probe_empty_errors_keep_all_targets(monkeypatch, fail_empty):
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: SimpleNamespace(running=True, proxy_url="http://127.0.0.1:1234", summary=lambda: "synthetic"),
    )
    monkeypatch.setattr(local_proxy, "LOCAL_AI_PROBE_TARGETS", (
        ("Target A", "https://a.example.invalid"), ("Target B", "https://b.example.invalid"),
    ))
    monkeypatch.setattr(local_proxy, "_probe_url_through_proxy", fail_empty)
    summary = local_proxy.probe_local_ai_proxy()
    assert "0/2" in summary
    assert "Target A" in summary and "Target B" in summary
    assert summary.count("TimeoutError") == 2


def test_short_stability_empty_errors_finish_all_rounds(monkeypatch, fail_empty):
    monkeypatch.setattr(local_proxy, "_probe_ai_url_through_explicit_http_proxy", fail_empty)
    result = local_proxy._probe_local_proxy_node_stability("http://127.0.0.1:1234", "synthetic-node")
    assert not result.stable and not result.short_stable
    assert result.total_attempts == 3 * len(local_proxy.LOCAL_AI_STABILITY_TARGETS)
    assert result.total_successes == 0
    assert "TimeoutError" in result.detail


@pytest.mark.parametrize("stage", ["transfer", "compact"])
def test_deep_stability_empty_errors_do_not_pass_gate(monkeypatch, fail_empty, stage):
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())

    def successful_transfer(*_args, **_kwargs):
        return local_proxy.LocalProxyTransferRoundResult(
            ok=True,
            downloaded_bytes=local_proxy.LOCAL_PROXY_DEEP_DOWNLOAD_BYTES,
            uploaded_bytes=local_proxy.LOCAL_PROXY_DEEP_UPLOAD_BYTES,
            elapsed_ms=20,
        )

    result = local_proxy._probe_local_proxy_node_deep_transport(
        "http://127.0.0.1:1234",
        transfer_probe=fail_empty if stage == "transfer" else successful_transfer,
        compact_probe=fail_empty,
    )
    assert not result.ok
    assert "TimeoutError" in result.detail
    assert not result.codex_compact_ok


@pytest.mark.parametrize("stage", ["short", "deep"])
def test_selection_session_empty_errors_remain_terminal(monkeypatch, fail_empty, stage):
    item = remote_proxy.ProxySubscriptionNode(1, _node())
    key = remote_proxy.proxy_subscription_node_key(item)
    sessions = []

    @contextmanager
    def isolated(_binary, _node):
        sessions.append(1)
        if stage == "short" or len(sessions) == 2:
            fail_empty()
        yield SimpleNamespace(proxy_url="http://127.0.0.1:1234")

    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("synthetic.exe"))
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", isolated)
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_proxy_node_stability",
        lambda *_args, **_kwargs: local_proxy.LocalProxyNodeStabilityResult(
            key, stable=False, short_stable=True,
        ),
    )
    chosen, results = local_proxy.select_stable_local_proxy_node(
        [item], {key: remote_proxy.ProxyNodeLatencyResult(key, True, 20)},
    )
    assert chosen is None
    assert not results[key].stable
    assert "TimeoutError" in results[key].detail
    assert len(sessions) == (1 if stage == "short" else 2)


@pytest.mark.parametrize("stage", ["resolve", "quality"])
def test_quality_single_empty_error_preserves_chinese_fallback(monkeypatch, fail_empty, stage):
    monkeypatch.setattr(remote_proxy, "proxy_quality_effective_services", lambda *_args: ["proxycheck"])
    monkeypatch.setattr(
        remote_proxy, "_resolve_proxy_node_ip",
        fail_empty if stage == "resolve" else lambda *_args, **_kwargs: "203.0.113.10",
    )
    monkeypatch.setattr(remote_proxy, "proxy_quality_settings_signature", fail_empty)
    result = remote_proxy.assess_proxy_node_quality(
        _node(), settings=SimpleNamespace(enabled_services=lambda: ["proxycheck"]), use_cache=False,
    )
    assert not result.ok
    assert result.detail == ("节点服务器解析失败" if stage == "resolve" else "节点服务器 IP 质量检测失败")


def test_quality_batch_resolution_empty_errors_keep_every_node(monkeypatch, fail_empty):
    monkeypatch.setattr(remote_proxy, "_resolve_proxy_node_ip", fail_empty)
    nodes = [_node(1), _node(2)]
    progress = []
    groups, results = remote_proxy._proxy_quality_resolve_groups(
        nodes, progress_callback=lambda *args: progress.append(args),
    )
    assert not groups
    assert len(results) == len(progress) == 2
    assert all(not result.ok and result.detail == "节点服务器解析失败" for result in results.values())


def test_quality_worker_exception_empty_text_has_fallback(fail_empty):
    try:
        fail_empty()
    except TimeoutError as exc:
        result = remote_proxy._proxy_quality_result_from_exception(_node(), exc)
    assert not result.ok
    assert result.detail == "节点服务器 IP 质量检测失败"
