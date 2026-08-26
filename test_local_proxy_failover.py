from __future__ import annotations

import pytest

from core import local_proxy, remote_proxy


def _node(name: str, server: str, **extra) -> dict:
    return {
        "name": name,
        "type": "vless",
        "server": server,
        "port": 443,
        **extra,
    }


def _managed_state(port: int = 17897) -> dict:
    proxy_url = f"http://127.0.0.1:{port}"
    return {
        "mixed_port": port,
        "proxy_url": proxy_url,
        "managed_proxy_env": {
            "owner": "api-switcher",
            "proxy_url": proxy_url,
            "variables": list(remote_proxy.PROXY_ENV_KEYS),
        },
    }


def test_mihomo_fallback_pool_is_bounded_deduplicated_and_strict_safe():
    primary = _node("primary", "primary.example.com")
    duplicate = _node("same connection, different label", "primary.example.com")
    backup_one = _node("backup one", "backup-1.example.com")
    dependent = _node(
        "dependent",
        "dependent.example.com",
        **{"dialer-proxy": "subscription-only-parent"},
    )
    extras = [
        duplicate,
        backup_one,
        dependent,
        *(_node(f"backup {index}", f"backup-{index}.example.com") for index in range(2, 9)),
    ]

    config = remote_proxy.build_mihomo_config(
        primary,
        17897,
        fallback_proxy_nodes=extras,
        health_checked_group=True,
        strict_privacy=True,
    )
    parsed = remote_proxy.yaml.safe_load(config)

    assert len(parsed["proxies"]) == remote_proxy.AI_PROXY_FALLBACK_MAX_NODES
    assert [node["name"] for node in parsed["proxies"]] == [
        remote_proxy.AI_PROXY_INTERNAL_NODE_NAME,
        *(f"{remote_proxy.AI_PROXY_FALLBACK_NODE_PREFIX}{index}" for index in range(1, 5)),
    ]
    assert all(node["server"] != "dependent.example.com" for node in parsed["proxies"])
    assert [node["server"] for node in parsed["proxies"]].count("primary.example.com") == 1
    assert parsed["proxy-groups"] == [
        {
            "name": "AI-PROXY",
            "type": "fallback",
            "proxies": [node["name"] for node in parsed["proxies"]],
            "url": remote_proxy.AI_PROXY_HEALTH_CHECK_URL,
            "interval": remote_proxy.AI_PROXY_HEALTH_CHECK_INTERVAL_SECONDS,
            "lazy": False,
            "timeout": remote_proxy.AI_PROXY_HEALTH_CHECK_TIMEOUT_MS,
            "max-failed-times": remote_proxy.AI_PROXY_HEALTH_CHECK_MAX_FAILURES,
            "expected-status": remote_proxy.AI_PROXY_HEALTH_CHECK_EXPECTED_STATUS,
        }
    ]
    assert remote_proxy._managed_proxy_display_name(config) == "primary"
    assert remote_proxy._managed_config_strict_privacy_enabled(config) is True


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("url", "https://example.com/health"),
        ("interval", 1),
        ("lazy", True),
        ("timeout", 60_000),
        ("max-failed-times", 99),
        ("expected-status", 204),
    ],
)
def test_strict_validator_rejects_tampered_fallback_health_contract(key, value):
    config = remote_proxy.build_mihomo_config(
        _node("primary", "primary.example.com"),
        fallback_proxy_nodes=[_node("backup", "backup.example.com")],
        health_checked_group=True,
        strict_privacy=True,
    )
    parsed = remote_proxy.yaml.safe_load(config)
    parsed["proxy-groups"][0][key] = value
    drifted = (
        remote_proxy.AI_PROXY_CONFIG_MARKER
        + "\n"
        + remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER
        + "\n"
        + remote_proxy._dump_yaml(parsed)
    )

    assert remote_proxy._managed_config_strict_privacy_enabled(drifted) is False


def test_local_config_uses_silent_log_and_health_check_even_with_one_node():
    config = local_proxy._build_local_mihomo_config(
        _node("primary", "primary.example.com"),
        17897,
        preferences={},
    )
    parsed = remote_proxy.yaml.safe_load(config)

    assert parsed["log-level"] == "silent"
    assert parsed["tcp-concurrent"] is True
    assert parsed["keep-alive-interval"] == 15
    assert parsed["keep-alive-idle"] == 15
    assert parsed["disable-keep-alive"] is False
    assert parsed["dns"]["proxy-server-nameserver"] == [
        "https://doh.pub/dns-query#DIRECT",
        "https://dns.alidns.com/dns-query#DIRECT",
    ]
    assert parsed["dns"]["nameserver-policy"]["+.openai.com"] == [
        "https://1.1.1.1/dns-query#AI-PROXY",
        "https://8.8.8.8/dns-query#AI-PROXY",
    ]
    assert len(parsed["proxies"]) == 1
    assert parsed["proxy-groups"][0]["type"] == "fallback"
    assert parsed["proxy-groups"][0]["url"] == remote_proxy.AI_PROXY_HEALTH_CHECK_URL


def test_local_mainland_dns_routes_custom_proxy_domains_through_ai_proxy():
    config = local_proxy._build_local_mihomo_config(
        _node("primary", "primary.example.com"),
        17897,
        preferences={
            "custom_targets": [
                {"kind": "domain", "value": "example.org", "enabled": True}
            ]
        },
    )
    parsed = remote_proxy.yaml.safe_load(config)

    assert parsed["dns"]["nameserver-policy"]["+.example.org"] == [
        "https://1.1.1.1/dns-query#AI-PROXY",
        "https://8.8.8.8/dns-query#AI-PROXY",
    ]


def test_startup_probe_retries_once_after_fallback_health_initializes(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("proxy-groups: []", encoding="utf-8")
    probes = iter(["AI 连通性 0/4 可达", "AI 连通性 4/4 可达"])
    monkeypatch.setattr(local_proxy, "probe_local_ai_proxy", lambda **_kwargs: next(probes))
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: local_proxy.LocalAIProxyStatus(
            installed=True,
            running=True,
            config_path=str(config_path),
            proxy_url="http://127.0.0.1:17897",
            fallback_candidates=2,
        ),
    )
    monkeypatch.setattr(
        local_proxy,
        "_local_mihomo_failover_status",
        lambda *_args: local_proxy._LocalMihomoFailoverStatus(
            healthy=True,
            candidates=2,
        ),
    )

    summary, retried = local_proxy._probe_local_ai_proxy_after_failover_warmup()

    assert retried is True
    assert "4/4 可达" in summary


def test_startup_probe_does_not_wait_when_codex_paths_are_ready(monkeypatch):
    summary = (
        "AI 连通性 2/4 可达；"
        "OpenAI API: 可达 / HTTP 401；"
        "OpenAI/ChatGPT: 可达 / HTTP 200；"
        "Claude/Anthropic: 失败 / timeout；"
        "Gemini/Google AI: 失败 / timeout"
    )
    monkeypatch.setattr(local_proxy, "probe_local_ai_proxy", lambda **_kwargs: summary)
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: (_ for _ in ()).throw(AssertionError("must not wait for optional services")),
    )

    result, retried = local_proxy._probe_local_ai_proxy_after_failover_warmup()

    assert result == summary
    assert retried is False


def test_local_fallback_candidates_exclude_unsafe_and_cap_pool():
    primary = _node("primary", "primary.example.com")
    duplicate = remote_proxy.ProxySubscriptionNode(
        1,
        _node("duplicate", "primary.example.com"),
        region="日本",
    )
    hong_kong = remote_proxy.ProxySubscriptionNode(
        2,
        _node("hong kong", "hk.example.com"),
        region="香港",
    )
    bad = remote_proxy.ProxySubscriptionNode(
        3,
        _node("bad", "bad.example.com"),
        region="日本",
    )
    dependent = remote_proxy.ProxySubscriptionNode(
        4,
        _node(
            "dependent",
            "dependent.example.com",
            **{"dialer-proxy": "parent"},
        ),
        region="日本",
    )
    safe = [
        remote_proxy.ProxySubscriptionNode(
            5 + index,
            _node(f"safe-{index}", f"safe-{index}.example.com"),
            region="日本",
        )
        for index in range(7)
    ]
    bad_key = remote_proxy.proxy_subscription_node_key(bad)
    qualities = {
        bad_key: remote_proxy.ProxyNodeQualityResult(
            bad_key,
            True,
            ip_type="VPN 高风险",
            risk_score=95,
            quality_score=10,
            confidence="高",
            coverage_complete=True,
            classification_basis="信誉源网络/风险字段",
        )
    }

    selected = local_proxy._local_proxy_fallback_nodes(
        primary,
        [duplicate, hong_kong, bad, dependent, *safe],
        qualities,
    )
    servers = [node["server"] for node in selected]

    assert len(selected) == remote_proxy.AI_PROXY_FALLBACK_MAX_NODES - 1
    assert "primary.example.com" not in servers
    assert "hk.example.com" not in servers
    assert "bad.example.com" not in servers
    assert "dependent.example.com" not in servers
    assert all(server.startswith("safe-") for server in servers)


def test_existing_pool_is_preserved_for_routing_only_reload(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    primary = _node("primary", "primary.example.com")
    backups = [
        _node("backup one", "backup-1.example.com"),
        _node("backup two", "backup-2.example.com"),
    ]
    config_path.write_text(
        remote_proxy.build_mihomo_config(
            primary,
            17897,
            fallback_proxy_nodes=backups,
            health_checked_group=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"config_path": str(config_path)})

    preserved = local_proxy._existing_local_proxy_fallback_nodes(primary)

    assert [node["server"] for node in preserved] == [
        "backup-1.example.com",
        "backup-2.example.com",
    ]


def test_failover_status_reports_healthy_backup_without_exposing_node_name(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        remote_proxy.build_mihomo_config(
            _node("primary", "primary.example.com"),
            17897,
            fallback_proxy_nodes=[_node("secret backup label", "backup.example.com")],
            health_checked_group=True,
        ),
        encoding="utf-8",
    )
    backup_name = f"{remote_proxy.AI_PROXY_FALLBACK_NODE_PREFIX}1"

    def controller(_port, path):
        if path == "/proxies/AI-PROXY":
            return {"now": backup_name, "alive": True}
        assert path == "/proxies/" + backup_name
        return {"alive": True, "history": [{"delay": 47}]}

    monkeypatch.setattr(local_proxy, "_read_local_mihomo_controller_json", controller)

    status = local_proxy._local_mihomo_failover_status(config_path, 17897)

    assert status.healthy is True
    assert status.candidates == 2
    assert status.active_fallback is True
    assert "已切到备用节点" in status.detail
    assert "47ms" in status.detail
    assert "secret backup label" not in status.detail


def test_failover_status_uses_group_history_and_reports_failure(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        remote_proxy.build_mihomo_config(
            _node("primary", "primary.example.com"),
            17897,
            health_checked_group=True,
        ),
        encoding="utf-8",
    )

    def controller(_port, path):
        if path == "/proxies/AI-PROXY":
            return {
                "now": remote_proxy.AI_PROXY_INTERNAL_NODE_NAME,
                "alive": False,
                "history": [{"delay": 0}],
            }
        return {}

    monkeypatch.setattr(local_proxy, "_read_local_mihomo_controller_json", controller)

    status = local_proxy._local_mihomo_failover_status(config_path, 17897)

    assert status.healthy is False
    assert status.candidates == 1
    assert "健康检查失败" in status.detail


def test_legacy_select_status_does_not_query_controller(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        remote_proxy.build_mihomo_config(
            _node("primary", "primary.example.com"),
            17897,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        local_proxy,
        "_read_local_mihomo_controller_json",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not query legacy group")),
    )

    status = local_proxy._local_mihomo_failover_status(config_path, 17897)

    assert status.healthy is None
    assert status.candidates == 1
    assert "未启用" in status.detail


def test_status_summary_distinguishes_process_health_from_upstream_health():
    status = local_proxy.LocalAIProxyStatus(
        installed=True,
        running=True,
        config_path="config.yaml",
        proxy_url="http://127.0.0.1:17897",
        network_healthy=False,
    )

    assert "进程运行，但上游健康检查失败" in status.summary()


def test_reconcile_repairs_only_missing_owned_environment_values(monkeypatch):
    expected = local_proxy._local_proxy_env_values(17897)
    current = dict(expected)
    current["HTTPS_PROXY"] = None
    current["https_proxy"] = ""
    current["ALL_PROXY"] = "http://127.0.0.1:19999"
    writes = []
    process_env = {}
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy.os, "environ", process_env)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: _managed_state())
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(
        local_proxy.persistent_env,
        "_local_user_env_value_strict",
        lambda name: current[name],
    )
    monkeypatch.setattr(
        local_proxy.persistent_env,
        "set_local_user_env",
        lambda updates: writes.append(dict(updates)),
    )

    message = local_proxy.reconcile_running_local_ai_proxy_settings()

    assert writes == [
        {
            "HTTPS_PROXY": expected["HTTPS_PROXY"],
            "https_proxy": expected["https_proxy"],
        }
    ]
    assert process_env["HTTP_PROXY"] == expected["HTTP_PROXY"]
    assert "ALL_PROXY" not in process_env
    assert "HTTPS_PROXY" in message and "https_proxy" in message
    assert "ALL_PROXY" in message and "未覆盖" in message


def test_reconcile_never_writes_without_complete_ownership(monkeypatch):
    writes = []
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(
        local_proxy.persistent_env,
        "set_local_user_env",
        lambda updates: writes.append(dict(updates)),
    )

    message = local_proxy.reconcile_running_local_ai_proxy_settings()

    assert writes == []
    assert "所有权记录" in message


def test_startup_reconcile_restores_settings_owned_by_dead_proxy(monkeypatch, tmp_path):
    state = _managed_state()
    state["previous_env"] = {}
    state["previous_vscode"] = {}
    state["previous_system_proxy"] = {}
    pid_path = tmp_path / "mihomo.pid"
    pid_path.write_text("1234", encoding="utf-8")
    restores = []
    saved_states = []
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", pid_path)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: dict(state))
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: False)
    monkeypatch.setattr(
        local_proxy,
        "_restore_managed_settings",
        lambda value, port: restores.append((dict(value), port)) or [],
    )
    monkeypatch.setattr(local_proxy, "_save_state", lambda value: saved_states.append(dict(value)))

    message = local_proxy.reconcile_local_ai_proxy_startup_settings()

    assert restores == [(state, 17897)]
    assert saved_states == [{}]
    assert not pid_path.exists()
    assert "已自动恢复" in message
    assert "已打开的终端需重开" in message


@pytest.mark.parametrize(("managed", "listening"), [(True, False), (False, True)])
def test_startup_reconcile_preserves_settings_while_process_or_port_exists(
    monkeypatch,
    managed,
    listening,
):
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: _managed_state())
    monkeypatch.setattr(
        local_proxy,
        "_managed_local_proxy_is_running",
        lambda _state: managed,
    )
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: listening)
    monkeypatch.setattr(
        local_proxy,
        "_restore_managed_settings",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must preserve settings")),
    )

    message = local_proxy.reconcile_local_ai_proxy_startup_settings()

    assert "未自动改动" in message


def test_app_startup_reconciles_live_proxy_even_when_autostart_is_off(monkeypatch):
    from ui import app as app_module

    events = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    class DummyApp:
        _exit_requested = False

        def _run_on_ui_thread(self, callback):
            callback()

        def _set_app_status(self, message):
            events.append(("status", message))

        def _refresh_loaded_tab(self, name):
            events.append(("refresh", name))

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        local_proxy,
        "reconcile_local_ai_proxy_startup_settings",
        lambda: "repaired",
    )
    monkeypatch.setattr(local_proxy, "local_proxy_start_on_login_enabled", lambda: False)
    monkeypatch.setattr(
        local_proxy,
        "auto_start_local_ai_proxy_if_enabled",
        lambda: (_ for _ in ()).throw(AssertionError("autostart must remain off")),
    )

    app_module.App._auto_start_local_proxy(DummyApp())

    assert ("status", "repaired") in events
    assert ("refresh", "_local_proxy_tab") in events
