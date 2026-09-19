"""Offline recovery candidates must not depend on the currently edited profile."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import local_proxy, remote_proxy


def _node(label, *, server=None):
    return {"name": label, "type": "http", "server": server or f"{label}.example", "port": 443}


@pytest.fixture(autouse=True)
def _isolated_candidates(monkeypatch):
    monkeypatch.setattr(local_proxy, "_managed_local_subscription_recovery_nodes", lambda: ())
    monkeypatch.setattr(local_proxy, "_load_last_proxy_node", lambda: None)
    monkeypatch.setattr(local_proxy, "_cached_subscription_fallback_nodes", lambda _node: ())
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda: [])


def _cache(monkeypatch, tmp_path, pools, *, latencies=None):
    profiles = []
    for index, nodes in enumerate(pools):
        path = tmp_path / f"subscription-{index}.yaml"
        path.write_text(remote_proxy._dump_yaml({"proxies": nodes}), encoding="utf-8")
        profiles.append({
            "id": f"profile-{index}", "name": f"profile-{index}",
            "saved_path": str(path), "node_latencies": (latencies or {}).get(index, {}),
        })
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda: profiles)
    return profiles


def _measurement(node, *, ok=True, age=0):
    return remote_proxy.proxy_node_key(node), {
        "ok": ok, "latency_ms": 30 if ok else None, "attempts": 1,
        "measured_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
    }


def test_cached_nodes_work_without_any_local_deployment(monkeypatch, tmp_path):
    nodes = [_node("cached-a"), _node("cached-b")]
    _cache(monkeypatch, tmp_path, [nodes])

    assert local_proxy._available_local_subscription_recovery_nodes() == tuple(nodes)


def test_different_subscriptions_get_slots_before_one_pool_fills_them(monkeypatch, tmp_path):
    first = [_node(f"a-{index}") for index in range(10)]
    second = [_node(f"b-{index}") for index in range(10)]
    _cache(monkeypatch, tmp_path, [first, second])
    monkeypatch.setattr(local_proxy, "_managed_local_subscription_recovery_nodes", lambda: tuple(first[:5]))

    actual = local_proxy._available_local_subscription_recovery_nodes()

    assert len(actual) == remote_proxy.AI_PROXY_FALLBACK_MAX_NODES
    assert actual[:3] == (first[0], second[0], first[1])
    assert second[1] in actual


def test_recent_success_is_prioritized_across_subscriptions(monkeypatch, tmp_path):
    first, second = _node("untested"), _node("working")
    _cache(monkeypatch, tmp_path, [[first], [second]], latencies={1: dict([_measurement(second)])})
    monkeypatch.setattr(local_proxy, "_managed_local_subscription_recovery_nodes", lambda: (first,))

    assert local_proxy._available_local_subscription_recovery_nodes() == (second, first)


@pytest.mark.parametrize("result", [
    {"ok": "true", "latency_ms": 1},
    {"ok": True, "latency_ms": 1, "cancelled": True},
    {"ok": True, "latency_ms": 1, "measured_at": "2000-01-01T00:00:00Z"},
])
def test_invalid_cancelled_or_stale_success_does_not_override_primary(monkeypatch, tmp_path, result):
    first, second = _node("primary"), _node("unverified")
    result.setdefault("measured_at", datetime.now(timezone.utc).isoformat())
    _cache(monkeypatch, tmp_path, [[second]], latencies={0: {remote_proxy.proxy_node_key(second): result}})
    monkeypatch.setattr(local_proxy, "_managed_local_subscription_recovery_nodes", lambda: (first,))

    assert local_proxy._available_local_subscription_recovery_nodes() == (first, second)


def test_fresh_failures_are_last_but_not_permanently_discarded(monkeypatch, tmp_path):
    failed, unknown = _node("failed"), _node("unknown")
    _cache(monkeypatch, tmp_path, [[failed, unknown]], latencies={0: dict([_measurement(failed, ok=False)])})

    assert local_proxy._available_local_subscription_recovery_nodes() == (unknown, failed)


def test_internal_display_name_does_not_block_cache_quality_or_create_duplicate(monkeypatch, tmp_path):
    original = _node("original")
    deployed = {**original, "name": remote_proxy.AI_PROXY_INTERNAL_NODE_NAME}
    unknown = _node("unknown")
    _cache(monkeypatch, tmp_path, [[original, unknown]], latencies={0: dict([_measurement(original)])})
    monkeypatch.setattr(local_proxy, "_managed_local_subscription_recovery_nodes", lambda: (unknown, deployed))

    actual = local_proxy._available_local_subscription_recovery_nodes()

    assert actual[0]["server"] == original["server"]
    assert len(actual) == 2


def test_download_candidates_do_not_apply_ai_region_exclusion(monkeypatch, tmp_path):
    hong_kong = _node("香港", server="download-node.example")
    _cache(monkeypatch, tmp_path, [[hong_kong]])

    assert local_proxy._available_local_subscription_recovery_nodes() == (hong_kong,)


def test_candidate_pool_preserves_protocol_fields_and_excludes_unresolved_dependencies():
    good = {**_node("good"), "type": "vless", "uuid": "synthetic-uuid", "tls": True,
            "servername": "front.example", "reality-opts": {"public-key": "synthetic-key"}}
    chained = {**_node("chained"), "dialer-proxy": "omitted-parent"}
    bypass = {**_node("bypass"), "type": "direct"}

    actual = local_proxy._select_subscription_recovery_nodes(((None, {}, chained, bypass, good),))

    assert actual == (good,)
    assert actual[0] is not good


def test_distinct_endpoints_are_preferred_and_shared_endpoint_credentials_remain_fallbacks():
    same = [_node(f"alias-{index}", server="shared.example") | {"password": f"synthetic-{index}"}
            for index in range(5)]
    different = _node("different")

    actual = local_proxy._select_subscription_recovery_nodes((same, [different]))

    assert actual[:2] == (same[0], different)
    assert len(actual) == remote_proxy.AI_PROXY_FALLBACK_MAX_NODES


def test_deployed_service_groups_are_included_and_unmanaged_groups_are_ignored(monkeypatch, tmp_path):
    # Restore this particular function: the autouse fixture isolates normal cache tests.
    managed_reader = _REAL_MANAGED_READER
    first = [_node(f"ai-{index}") for index in range(5)]
    service = _node("youtube")
    config = remote_proxy.build_mihomo_config(
        first[0], fallback_proxy_nodes=first[1:],
        additional_proxy_groups=[{"name": "SUB-123456789ABC-PROXY", "proxy_node": service}],
        extra_proxy_domains=["youtube.com"],
        proxy_domain_routes={"youtube.com": "SUB-123456789ABC-PROXY"},
    )
    parsed = remote_proxy.yaml.safe_load(config)
    parsed["proxies"].append(_node("unmanaged"))
    parsed["proxy-groups"].append({"name": "foreign-group", "type": "select", "proxies": ["unmanaged"]})
    content = remote_proxy.AI_PROXY_CONFIG_MARKER + "\n" + remote_proxy._dump_yaml(parsed)
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_managed_local_config_path", lambda _state: path)

    actual = managed_reader()

    assert len(actual) == 5
    assert actual[1]["server"] == service["server"]
    assert "unmanaged.example" not in {node["server"] for node in actual}
    assert path.read_text(encoding="utf-8") == content


def test_missing_and_broken_cache_do_not_hide_another_subscription(monkeypatch, tmp_path):
    good = _node("good")
    profiles = _cache(monkeypatch, tmp_path, [[_node("broken")], [good]])
    Path(profiles[0]["saved_path"]).write_text("<html>not a subscription</html>", encoding="utf-8")
    profiles.insert(0, {"id": "missing", "saved_path": str(tmp_path / "missing.yaml")})

    assert local_proxy._available_local_subscription_recovery_nodes() == (good,)


def test_invalid_deployment_state_does_not_hide_saved_subscription(monkeypatch, tmp_path):
    good = _node("good")
    _cache(monkeypatch, tmp_path, [[good]])
    monkeypatch.setattr(local_proxy, "_managed_local_subscription_recovery_nodes", _REAL_MANAGED_READER)

    def invalid_state():
        raise ValueError("synthetic corrupted state")

    monkeypatch.setattr(local_proxy, "_load_state", invalid_state)

    assert local_proxy._available_local_subscription_recovery_nodes() == (good,)


def test_discovery_deadline_prevents_cache_read(monkeypatch, tmp_path):
    _cache(monkeypatch, tmp_path, [[_node("cached")]])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda *_: pytest.fail("cache read after deadline"))

    assert local_proxy._cached_local_subscription_recovery_pools(deadline=0.0) == ([], {})


def test_recovery_does_not_install_core_or_change_last_node(monkeypatch, tmp_path):
    _cache(monkeypatch, tmp_path, [[_node("cached")]])
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_BIN_DIR", tmp_path / "missing-bin")
    monkeypatch.setattr(local_proxy, "_available_local_subscription_recovery_nodes", lambda **_: pytest.fail("must not scan caches without core"))
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", lambda *_args, **_kwargs: pytest.fail("must not start without core"))
    monkeypatch.setattr(local_proxy, "_save_last_proxy_node", lambda *_: pytest.fail("must not change live node"))

    with pytest.raises(RuntimeError, match="已安装 mihomo"):
        with local_proxy.local_proxy_subscription_recovery_session(5.0):
            pytest.fail("missing core yielded a session")


_REAL_MANAGED_READER = local_proxy._managed_local_subscription_recovery_nodes


def test_bad_cached_node_is_filtered_without_losing_other_working_routes(monkeypatch, tmp_path):
    bad, good, other = _node("bad"), _node("good"), _node("other")
    calls, exits = [], []

    @contextmanager
    def session(_binary, node, *, fallback_proxy_nodes, startup_timeout_seconds):
        names = tuple(item["name"] for item in (node, *fallback_proxy_nodes))
        calls.append(names)
        assert 0 < startup_timeout_seconds <= 10
        if "bad" in names:
            raise local_proxy._IsolatedMihomoStartupError("synthetic unsupported transport")
        try:
            yield SimpleNamespace(route_count=len(names))
        finally:
            exits.append(names)

    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", session)

    with local_proxy._isolated_subscription_recovery_pool(
        tmp_path / "mihomo.exe", (bad, good, other), deadline=local_proxy.time.monotonic() + 10,
    ) as actual:
        assert actual.route_count == 2
        assert exits == [("good",), ("other",)]

    assert calls == [("bad", "good", "other"), ("bad",), ("good",), ("other",), ("good", "other")]
    assert exits[-1] == ("good", "other")


@pytest.mark.parametrize("failure_stage", ["body", "body_startup_error", "exit", "failed_startup_cleanup"])
def test_recovery_never_retries_download_body_or_cleanup_failure(monkeypatch, tmp_path, failure_stage):
    calls = []

    @contextmanager
    def session(_binary, node, **_kwargs):
        calls.append(node)
        try:
            if failure_stage == "failed_startup_cleanup":
                raise local_proxy._IsolatedMihomoStartupError("startup failed")
            yield SimpleNamespace(route_count=2)
        finally:
            if failure_stage in {"exit", "failed_startup_cleanup"}:
                raise RuntimeError("synthetic credentials cleanup failed")

    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", session)

    with pytest.raises(RuntimeError, match="synthetic"):
        with local_proxy._isolated_subscription_recovery_pool(
            tmp_path / "mihomo.exe", (_node("one"), _node("two")),
            deadline=local_proxy.time.monotonic() + 10,
        ):
            if failure_stage == "body":
                raise RuntimeError("synthetic download body failed")
            if failure_stage == "body_startup_error":
                raise local_proxy._IsolatedMihomoStartupError("synthetic body failure")

    assert len(calls) == 1


def test_validation_cleanup_failure_stops_before_another_node(monkeypatch, tmp_path):
    calls = []

    @contextmanager
    def session(_binary, node, *, fallback_proxy_nodes, **_kwargs):
        calls.append(node)
        if fallback_proxy_nodes:
            raise local_proxy._IsolatedMihomoStartupError("synthetic bad pool")
        try:
            yield SimpleNamespace()
        finally:
            raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", session)

    with pytest.raises(RuntimeError, match="cleanup failure"):
        with local_proxy._isolated_subscription_recovery_pool(
            tmp_path / "mihomo.exe", (_node("one"), _node("two")),
            deadline=local_proxy.time.monotonic() + 10,
        ):
            pytest.fail("cleanup error must stop recovery")

    assert len(calls) == 2


def test_recovery_validation_reserves_budget_for_rebuild_and_download(monkeypatch, tmp_path):
    clock = [100.0]
    calls = []

    @contextmanager
    def session(_binary, node, *, fallback_proxy_nodes, startup_timeout_seconds):
        calls.append((node["name"], startup_timeout_seconds))
        if len(calls) == 1:
            clock[0] += 1
            raise local_proxy._IsolatedMihomoStartupError("synthetic bad pool")
        if node["name"] == "bad":
            clock[0] += startup_timeout_seconds
            raise local_proxy._IsolatedMihomoStartupError("synthetic slow incompatible node")
        clock[0] += 0.1
        yield SimpleNamespace()

    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", session)
    monkeypatch.setattr(local_proxy.time, "monotonic", lambda: clock[0])

    with local_proxy._isolated_subscription_recovery_pool(
        tmp_path / "mihomo.exe", (_node("bad"), _node("good")), deadline=110,
    ):
        assert clock[0] < 106

    assert calls[1][1] == 2.25
    assert calls[-1][1] > 5


def test_expired_recovery_budget_never_starts_a_core(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", lambda *_args, **_kwargs: pytest.fail("expired recovery started core"))

    with pytest.raises(TimeoutError):
        with local_proxy._isolated_subscription_recovery_pool(
            tmp_path / "mihomo.exe", (_node("node"),), deadline=0,
        ):
            pytest.fail("expired recovery yielded a session")


def test_single_bad_node_is_not_validated_repeatedly(monkeypatch, tmp_path):
    calls = []

    @contextmanager
    def session(_binary, node, **_kwargs):
        calls.append(node)
        raise local_proxy._IsolatedMihomoStartupError("synthetic invalid single node")
        yield  # pragma: no cover

    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", session)

    with pytest.raises(local_proxy._IsolatedMihomoStartupError):
        with local_proxy._isolated_subscription_recovery_pool(
            tmp_path / "mihomo.exe", (_node("node"),), deadline=local_proxy.time.monotonic() + 10,
        ):
            pytest.fail("bad singleton yielded a session")

    assert len(calls) == 1


def test_real_core_recovers_from_one_incompatible_cached_node():
    binary = os.environ.get("API_SWITCHER_MIHOMO_TEST_CORE", "")
    if not binary or not Path(binary).is_file():
        pytest.skip("optional isolated mihomo integration requires API_SWITCHER_MIHOMO_TEST_CORE")
    bad = {**_node("incompatible", server="127.0.0.1"), "type": "synthetic-unsupported-transport"}
    good = _node("loopback-one", server="127.0.0.1") | {"port": 9}
    other = _node("loopback-two", server="127.0.0.1") | {"port": 19}
    directories_before = set(local_proxy._ISOLATED_MIHOMO_DIRECTORIES)
    processes_before = set(local_proxy._ISOLATED_MIHOMO_PROCESSES)

    # Startup/config compatibility only: no outbound request, DNS resolution,
    # live proxy integration, or developer subscription is involved.
    with local_proxy._isolated_subscription_recovery_pool(
        Path(binary), (bad, good, other), deadline=local_proxy.time.monotonic() + 10,
    ) as session:
        assert session.route_count == 2
        assert session.proxy_url.startswith("http://127.0.0.1:")

    assert local_proxy._ISOLATED_MIHOMO_DIRECTORIES == directories_before
    assert local_proxy._ISOLATED_MIHOMO_PROCESSES == processes_before
