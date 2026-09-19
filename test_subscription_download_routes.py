"""Subscription route fallback must be bounded, current, and read-only."""

from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from core import remote_proxy


URL = "https://subscription.example.test/config?token=synthetic"
PAYLOAD = b"proxies:\n - {name: test, type: http, server: proxy.example.test, port: 8080}\n"
RESULT = (PAYLOAD, "application/yaml", "utf-8")


@pytest.fixture(autouse=True)
def isolated_proxy_state(monkeypatch):
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies_registry", lambda: {}, raising=False)
    monkeypatch.setattr(remote_proxy.urlrequest, "proxy_bypass", lambda _host: False)
    monkeypatch.setattr(remote_proxy, "_subscription_proxy_environment_diagnostic",
                        lambda _url: remote_proxy.ProxyEnvironmentDiagnostic())


def _fetch(**kwargs):
    return remote_proxy.fetch_proxy_subscription(URL, persist=False, retry_base_delay=0, **kwargs)


def test_current_settings_replace_cached_default_opener_and_expand_all_proxy(monkeypatch):
    calls = []
    proxies = {"all": "http://127.0.0.1:19001"}
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: dict(proxies))
    monkeypatch.setattr(remote_proxy.urlrequest, "_opener", SimpleNamespace(
        open=lambda *_args, **_kwargs: pytest.fail("stale global opener must not be used")))

    def build(handler):
        calls.append(dict(handler.proxies))
        return SimpleNamespace(open=lambda _request, **_kwargs: "response")

    monkeypatch.setattr(remote_proxy.urlrequest, "build_opener", build)
    request = remote_proxy.urlrequest.Request(URL)
    assert remote_proxy._open_current_proxy_subscription_request(request, timeout=1) == "response"
    proxies.clear()
    remote_proxy._open_current_proxy_subscription_request(request, timeout=1)
    assert calls[0]["http"] == calls[0]["https"] == "http://127.0.0.1:19001"
    assert calls[1] == {}


def test_scheme_specific_proxy_wins_over_all_proxy(monkeypatch):
    values = {"https": "http://127.0.0.1:19002", "all": "http://127.0.0.1:19001", "no": "localhost"}
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: values)
    snapshot = remote_proxy._subscription_environment_proxy_map()
    assert snapshot["https"] == values["https"]
    assert snapshot["http"] == values["all"]
    assert snapshot["no"] == "localhost"
    assert "http" not in values


@pytest.mark.parametrize("error_kind", ["403", "html"])
def test_compatible_signature_is_tried_before_starting_disposable_proxy(monkeypatch, error_kind):
    signatures = []

    def download(request, **_kwargs):
        ua = request.get_header("User-agent")
        signatures.append(ua)
        if ua == "clash-verge/v2.5.2":
            return RESULT
        if error_kind == "403":
            raise HTTPError(URL, 403, "Forbidden", {}, None)
        raise remote_proxy._ProxySubscriptionPayloadError("网页或拦截页")

    monkeypatch.setattr(remote_proxy, "_open_validated_proxy_subscription_request", download)
    result = _fetch(recovery_proxy_provider=lambda _timeout: pytest.fail("unnecessary core startup"))
    assert len(result.nodes) == 1
    assert signatures == ["clash.meta", "clash-verge/v2.5.2"]


@pytest.mark.parametrize("primary_error", [403, 406])
def test_direct_route_gets_compatible_signatures_after_primary_exhausts_them(monkeypatch, primary_error):
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {"https": "http://127.0.0.1:19000"})
    direct_signatures = []

    def download(request, **kwargs):
        ua = request.get_header("User-agent")
        if kwargs.get("direct"):
            direct_signatures.append(ua)
            if ua == "clash-verge/v2.5.2":
                return RESULT
        raise HTTPError(URL, primary_error, "Forbidden", {}, None)

    monkeypatch.setattr(remote_proxy, "_open_validated_proxy_subscription_request", download)
    result = _fetch()
    assert "clash-verge/v2.5.2" in direct_signatures
    assert "临时直连" in result.proxy_warning


@pytest.mark.parametrize("strict", [False, True])
def test_system_proxy_survives_environment_override_without_mutating_settings(monkeypatch, strict):
    environment = {"https": "http://127.0.0.1:19000"}
    system = {"https": "http://127.0.0.1:19001"}
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: dict(environment))
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies_registry", lambda: dict(system))
    calls = []

    def download(_request, **kwargs):
        calls.append(kwargs)
        if kwargs.get("proxy_map", {}).get("https") == system["https"]:
            return RESULT
        raise TimeoutError("synthetic route timeout")

    monkeypatch.setattr(remote_proxy, "_open_validated_proxy_subscription_request", download)
    result = _fetch(allow_direct_fallback=not strict, retries=1)
    assert "Windows 系统代理" in result.proxy_warning
    if strict:
        assert all(not call.get("direct") for call in calls)
    assert environment == {"https": "http://127.0.0.1:19000"}
    assert system == {"https": "http://127.0.0.1:19001"}


@pytest.mark.parametrize("proxy", ["http://remote.example.test:8080", "socks5://127.0.0.1:19001"])
def test_strict_system_fallback_rejects_unverified_proxy(monkeypatch, proxy):
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies_registry", lambda: {"https": proxy})
    assert remote_proxy._subscription_system_proxy_map(remote_proxy.urlrequest.Request(URL), strict=True) is None


def test_duplicate_system_route_is_not_retried(monkeypatch):
    proxy = {"https": "http://127.0.0.1:19000"}
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies_registry", lambda: proxy)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: proxy)
    assert remote_proxy._subscription_system_proxy_map(remote_proxy.urlrequest.Request(URL), strict=False) is None


def test_no_proxy_bypass_does_not_hide_explicit_system_route(monkeypatch):
    proxy = {"https": "http://127.0.0.1:19000"}
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies_registry", lambda: proxy)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: proxy)
    monkeypatch.setattr(remote_proxy.urlrequest, "proxy_bypass", lambda _host: True)
    request = remote_proxy.urlrequest.Request(URL)
    assert remote_proxy._subscription_system_proxy_map(request, strict=False)["https"] == proxy["https"]
    assert remote_proxy._subscription_system_proxy_map(request, strict=True) is None


def test_system_timeout_does_not_skip_reserved_isolated_recovery(monkeypatch):
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies_registry", lambda: {"https": "http://127.0.0.1:19001"})

    def primary(*_args, **_kwargs):
        raise TimeoutError("synthetic primary timeout")

    routes = []

    def recover(**kwargs):
        route = kwargs["session"].proxy_map["https"]
        routes.append(route)
        if route.endswith(":19001"):
            raise TimeoutError("system route reached its deadline")
        return RESULT, None

    monkeypatch.setattr(remote_proxy, "_open_validated_proxy_subscription_request", primary)
    monkeypatch.setattr(remote_proxy, "_download_proxy_subscription_via_recovery", recover)
    result = _fetch(retries=1, recovery_proxy_provider=lambda _timeout: "http://127.0.0.1:19002")
    assert result.nodes
    assert routes == ["http://127.0.0.1:19001", "http://127.0.0.1:19002"]


def test_provider_only_document_reports_format_limitation_without_external_fetch(monkeypatch):
    monkeypatch.setattr(remote_proxy, "_open_proxy_subscription_request", lambda *_args, **_kwargs: (
        b"proxy-providers:\n  other:\n    type: http\n    url: https://nested.example.test/config\n",
        "application/yaml", "utf-8",
    ))
    with pytest.raises(ValueError, match="已下载.*proxy-providers"):
        _fetch(recovery_proxy_provider=lambda _timeout: pytest.fail("not a network failure"))


def test_provider_template_still_allows_an_expanded_client_signature(monkeypatch):
    calls = []

    def download(request, **_kwargs):
        calls.append(request.get_header("User-agent"))
        if len(calls) == 1:
            return b"proxy-providers:\n  example: {type: http, url: https://nested.example.test/sub}\n", "application/yaml", "utf-8"
        return RESULT

    monkeypatch.setattr(remote_proxy, "_open_proxy_subscription_request", download)
    assert _fetch().nodes
    assert calls == ["clash.meta", "clash-verge/v2.5.2"]


def test_unavailable_recovery_releases_its_reserved_budget(monkeypatch):
    clock = [100.0]
    deadlines = []
    monkeypatch.setattr(remote_proxy.time, "monotonic", lambda: clock[0])

    def download(_request, **kwargs):
        deadlines.append(kwargs["deadline"])
        if len(deadlines) == 1:
            clock[0] = kwargs["deadline"]
            raise TimeoutError("first route consumed its allowance")
        assert kwargs["deadline"] > clock[0]
        return RESULT

    monkeypatch.setattr(remote_proxy, "_open_validated_proxy_subscription_request", download)
    assert _fetch(timeout=12, retries=2, recovery_proxy_provider=lambda _timeout: None).nodes
    assert deadlines == [108.0, 112.0]


def test_subsecond_timeout_does_not_exceed_remaining_budget(monkeypatch):
    monkeypatch.setattr(remote_proxy.time, "monotonic", lambda: 100.0)
    assert 0 < remote_proxy._subscription_request_timeout(100.01) < 0.011


def test_download_requests_fresh_content_without_modifying_signed_url(monkeypatch):
    def download(request, **_kwargs):
        assert request.full_url == URL
        assert request.get_header("Cache-control") == "no-cache"
        assert request.get_header("Pragma") == "no-cache"
        return RESULT

    monkeypatch.setattr(remote_proxy, "_open_validated_proxy_subscription_request", download)
    assert _fetch().nodes


def test_rate_limit_is_not_misclassified_as_proxy_oserror(monkeypatch):
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {"https": "http://127.0.0.1:19000"})
    assert not remote_proxy._should_try_direct_subscription_download(HTTPError(URL, 429, "Too Many Requests", {}, None))
