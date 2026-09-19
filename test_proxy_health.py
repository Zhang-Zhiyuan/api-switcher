"""Pure controller fixtures: no user configuration, sockets or live proxy changes."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core import local_proxy, proxy_route_diagnostics, remote_proxy
from core.proxy_health import parse_proxy_health, proxy_health_summary


NOW = datetime(2026, 9, 20, 8, 30, tzinfo=timezone.utc)
TEST_URL = "https://target.example.test/health"
GROUP = {"testUrl": TEST_URL}


def observation(delay=27, *, alive=True, checked=NOW):
    return {"alive": alive, "history": [{"time": checked.isoformat(), "delay": delay}]}


def specific_node(record, generic=None):
    return {**(generic or {}), "extra": {TEST_URL: record}}


@pytest.mark.parametrize("delay,alive,expected", [(0, False, False), (31, True, True)])
def test_specific_result_wins_over_opposite_generic_result(delay, alive, expected):
    node = specific_node(observation(delay, alive=alive), observation(0 if alive else 10, alive=not alive))
    health = parse_proxy_health(GROUP, node, NOW)

    assert health.target_healthy is expected
    assert health.specific
    assert "此策略组探针" in proxy_health_summary(health)
    assert proxy_route_diagnostics.health_summary(GROUP, node, NOW) == proxy_health_summary(health)


@pytest.mark.parametrize("offset,state,expected", [
    (-181, "stale", None), (-180, "passed", True),
    (6, "stale", None), (5, "passed", True),
])
def test_health_time_bounds(offset, state, expected):
    node = specific_node(observation(checked=NOW + timedelta(seconds=offset)))
    health = parse_proxy_health(GROUP, node, NOW)
    assert health.state == state
    assert health.target_healthy is expected


@pytest.mark.parametrize("timestamp", [None, "", "bad", 123, "2026-09-20T08:30:00"])
def test_missing_malformed_or_timezone_unknown_time_is_not_healthy(timestamp):
    record = observation()
    record["history"][-1]["time"] = timestamp
    health = parse_proxy_health(GROUP, specific_node(record), NOW)
    assert health.target_healthy is None
    assert health.state == "unknown_time"
    assert "时间未知" in proxy_health_summary(health)


@pytest.mark.parametrize("delay", [None, True, False, "27", -1, 1.5, float("nan"), float("inf"), 2**31, 10**500])
def test_invalid_delays_never_report_healthy_or_failed(delay):
    health = parse_proxy_health(GROUP, specific_node(observation(delay)), NOW)
    assert health.target_healthy is None
    assert health.state == "invalid_delay"
    assert "延迟数据无效" in proxy_health_summary(health)


@pytest.mark.parametrize("alive", ["false", "true", 0, 1, [], {}])
def test_malformed_alive_does_not_coerce_strings_or_numbers_to_health(alive):
    health = parse_proxy_health(GROUP, specific_node(observation(alive=alive)), NOW)
    assert health.target_healthy is None
    assert "状态数据无效" in proxy_health_summary(health)


def test_valid_history_without_alive_is_supported():
    record = observation()
    del record["alive"]
    assert parse_proxy_health(GROUP, specific_node(record), NOW).target_healthy is True


@pytest.mark.parametrize("record", [{}, {"alive": True}, {"history": []}, {"history": [None]}])
def test_empty_specific_history_never_falls_back_to_generic_success(record):
    health = parse_proxy_health(GROUP, specific_node(record, observation()), NOW)
    assert health.specific
    assert health.target_healthy is None
    assert "未检测" in proxy_health_summary(health)


@pytest.mark.parametrize("delay,alive", [(27, True), (0, False)])
@pytest.mark.parametrize("on_group", [False, True])
def test_generic_fallback_is_explicit_but_not_target_health(delay, alive, on_group):
    generic = observation(delay, alive=alive)
    group = {**GROUP, **generic} if on_group else GROUP
    node = {} if on_group else generic
    health = parse_proxy_health(group, node, NOW)
    assert health.target_healthy is None
    assert health.state == ("passed" if alive else "failed")
    assert "通用探针（非此目标专测）" in proxy_health_summary(health)


@pytest.mark.parametrize("group,node", [(None, None), ([], []), ({}, {"history": "invalid"})])
def test_invalid_controller_containers_are_unknown(group, node):
    assert parse_proxy_health(group, node, NOW).target_healthy is None


def status_from_controller(monkeypatch, node, *, group_fields=None, active="backup"):
    config = remote_proxy.yaml.safe_dump({
        "proxy-groups": [{"name": "AI-PROXY", "type": "fallback", "url": TEST_URL,
                          "proxies": ["primary", "backup"]}],
    })
    config_path = SimpleNamespace(read_text=lambda **_: remote_proxy.AI_PROXY_CONFIG_MARKER + "\n" + config)
    group_state = {"now": active, **(group_fields or {})}

    def controller(_port, path):
        if path == "/proxies/AI-PROXY":
            return group_state
        assert path == "/proxies/" + active
        return node

    monkeypatch.setattr(local_proxy, "_read_local_mihomo_controller_json", controller)
    monkeypatch.setattr(local_proxy, "parse_proxy_health", lambda group, item: parse_proxy_health(group, item, NOW))
    return local_proxy._local_mihomo_failover_status(config_path, 17897)


def test_main_status_uses_config_test_url_when_controller_omits_it(monkeypatch):
    node = specific_node(observation(31), observation(0, alive=False))
    status = status_from_controller(monkeypatch, node)
    assert status.healthy is True
    assert status.active_fallback is True
    assert status.candidates == 2
    assert "31ms" in status.detail


def test_main_status_cannot_mask_target_failure_with_generic_success(monkeypatch):
    node = specific_node(observation(0, alive=False), observation(12))
    status = status_from_controller(monkeypatch, node)
    assert status.healthy is False
    assert "此策略组探针失败" in status.detail
    assert "健康延迟" not in status.detail


@pytest.mark.parametrize("record,fragment", [
    (observation(checked=NOW - timedelta(days=1)), "过期"),
    (observation(checked=NOW + timedelta(minutes=1)), "时钟不同步"),
    ({"alive": True, "history": [{"delay": 20}]}, "时间未知"),
    (observation(True), "延迟数据无效"),
    (observation(alive="false"), "状态数据无效"),
])
def test_main_status_matches_diagnostics_for_unknown_health(monkeypatch, record, fragment):
    node = specific_node(record, observation(12))
    status = status_from_controller(monkeypatch, node)
    assert status.healthy is None
    assert status.active_fallback is True
    assert fragment in status.detail
    assert "目标健康状态未确认" in status.detail
    assert proxy_route_diagnostics.health_summary(GROUP, node, NOW) in status.detail


@pytest.mark.parametrize("alive", [True, False])
@pytest.mark.parametrize("on_group", [True, False])
def test_main_older_controller_generic_fallback_is_not_target_failure(monkeypatch, alive, on_group):
    record = observation(27 if alive else 0, alive=alive)
    status = status_from_controller(monkeypatch, {} if on_group else record,
                                    group_fields=record if on_group else {})
    assert status.healthy is None
    assert "通用探针（非此目标专测）" in status.detail
    assert "目标健康状态未确认" in status.detail


def test_unknown_active_node_is_not_verified_using_another_nodes_health(monkeypatch):
    status = status_from_controller(monkeypatch, specific_node(observation()), active="unconfigured")
    assert status.healthy is None
    assert "当前节点未确定" in status.detail


def test_unknown_health_keeps_running_status_and_bounded_real_startup_retry(monkeypatch):
    status = local_proxy.LocalAIProxyStatus(
        installed=True, running=True, config_path="unused-config.yaml",
        proxy_url="http://127.0.0.1:17897", fallback_candidates=2, network_healthy=None,
    )
    assert "运行中" in status.summary()
    assert "上游健康检查失败" not in status.summary()
    monkeypatch.setattr(local_proxy, "inspect_local_ai_proxy", lambda: status)
    probes = []

    def probe(**_):
        probes.append(True)
        return "AI 连通性 0/4 可达" if len(probes) == 1 else "AI 连通性 4/4 可达"

    monkeypatch.setattr(local_proxy, "probe_local_ai_proxy", probe)
    health_reads = []

    def health(*_):
        health_reads.append(True)
        return local_proxy._LocalMihomoFailoverStatus(healthy=None, candidates=2)

    monkeypatch.setattr(local_proxy, "_local_mihomo_failover_status", health)
    elapsed = [0.0]
    sleeps = []

    def sleep(duration):
        sleeps.append(duration)
        elapsed[0] += duration

    monkeypatch.setattr(local_proxy, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep))
    result, retried = local_proxy._probe_local_ai_proxy_after_failover_warmup(warmup_seconds=0.3)
    assert retried
    assert result == "AI 连通性 4/4 可达"
    assert len(probes) == 2
    assert health_reads and sleeps
    assert elapsed[0] == pytest.approx(0.3)
