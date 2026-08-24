from pathlib import Path

import pytest

from core import local_proxy, remote_proxy


def _node():
    return {
        "name": "privacy-test",
        "type": "vless",
        "server": "proxy.example.com",
        "port": 443,
    }


def _write_config(path: Path, *, strict: bool) -> None:
    path.write_text(
        remote_proxy.build_mihomo_config(_node(), 17897, strict_privacy=strict),
        encoding="utf-8",
    )


def _applied_state(config_path: Path, *, pid: int = 4321) -> dict:
    return {
        "mixed_port": 17897,
        "config_path": str(config_path),
        "applied_config_sha256": local_proxy._local_config_sha256(config_path),
        "applied_config_pid": pid,
        "applied_config_mixed_port": 17897,
        "applied_config_at": "2026-08-13T00:00:00Z",
    }


def _patch_running_status(
    monkeypatch,
    config_path: Path,
    *,
    state: dict | None = None,
) -> dict:
    state = dict(state or _applied_state(config_path))
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", config_path.parent)
    monkeypatch.setattr(
        local_proxy,
        "_load_state",
        lambda: dict(state),
    )
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 4321)
    monkeypatch.setattr(local_proxy, "_is_pid_running", lambda _pid: True)
    monkeypatch.setattr(
        local_proxy,
        "_is_managed_mihomo_pid",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_local_env_matches", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_windows_system_proxy_matches", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_local_vscode_proxy_match_detail", lambda _port: "")
    return state


def test_strict_privacy_contract_is_read_from_actual_managed_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, strict=True)

    assert local_proxy._managed_local_config_has_strict_privacy(config_path) is True

    parsed = remote_proxy.yaml.safe_load(config_path.read_text(encoding="utf-8"))
    parsed["rules"].insert(-1, "GEOIP,CN,DIRECT")
    config_path.write_text(
        remote_proxy.AI_PROXY_CONFIG_MARKER + "\n" + remote_proxy._dump_yaml(parsed),
        encoding="utf-8",
    )

    assert local_proxy._managed_local_config_has_strict_privacy(config_path) is False


def test_local_managed_node_restores_reserved_display_name_without_routing_to_it(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    original = {
        "name": "DIRECT",
        "type": "vless",
        "server": "proxy.example.com",
        "port": 443,
    }
    config_path.write_text(
        remote_proxy.build_mihomo_config(original, strict_privacy=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        local_proxy,
        "_load_state",
        lambda: {"config_path": str(config_path)},
    )
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", config_path.parent)

    restored = local_proxy._read_local_managed_proxy_node()
    parsed = remote_proxy.yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert restored is not None
    assert restored["name"] == "DIRECT"
    assert remote_proxy.proxy_node_key(restored) == remote_proxy.proxy_node_key(original)
    assert parsed["proxies"][0]["name"] == remote_proxy.AI_PROXY_INTERNAL_NODE_NAME
    assert parsed["proxy-groups"][0]["proxies"] == [
        remote_proxy.AI_PROXY_INTERNAL_NODE_NAME
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: config.update(ipv6=True),
        lambda config: config.update({"allow-lan": True}),
        lambda config: config.update({"bind-address": "0.0.0.0"}),
        lambda config: config.update(mode="global"),
        lambda config: config.update({"external-controller": "0.0.0.0:17898"}),
        lambda config: config["dns"].update(enable=False),
        lambda config: config["dns"].update(ipv6=True),
        lambda config: config["dns"].update({"enhanced-mode": "fake-ip"}),
        lambda config: config["dns"].update({"use-system-hosts": True}),
        lambda config: config["dns"].update({"respect-rules": False}),
        lambda config: config["dns"].pop("proxy-server-nameserver"),
        lambda config: config["dns"].update({"nameserver": ["8.8.8.8"]}),
        lambda config: config["dns"].update({"proxy-server-nameserver": ["udp://1.1.1.1"]}),
        lambda config: config["dns"].update({"nameserver": ["https://"]}),
        lambda config: config["dns"].update(
            {"nameserver": ["https://user:pass@resolver.example/dns-query"]}
        ),
        lambda config: config["dns"].update(
            {"nameserver": ["https://resolver.example/dns-query"]}
        ),
        lambda config: config["dns"].update(
            {"nameserver": ["https://1.1.1.1@evil.example/dns-query"]}
        ),
        lambda config: config["dns"].update(
            {"nameserver": ["https://1.1.1.1/dns-query#DIRECT"]}
        ),
        lambda config: config["dns"].update(
            {"proxy-server-nameserver": ["https://resolver.example/dns-query"]}
        ),
        lambda config: config["dns"].update(
            {"nameserver-policy": {"+.openai.com": "8.8.8.8"}}
        ),
        lambda config: config["dns"].update({"fallback": ["8.8.8.8"]}),
        lambda config: config["dns"].update({"listen": "0.0.0.0:1053"}),
        lambda config: config["dns"].update({"default-nameserver": ["resolver.example.com"]}),
        lambda config: config["dns"].update({"default-nameserver": ["127.0.0.1"]}),
        lambda config: config.update({"proxy-groups": []}),
        lambda config: config["proxy-groups"][0].update(type="fallback"),
        lambda config: config["proxy-groups"][0].update(type="SELECT"),
        lambda config: config["proxy-groups"][0].update(proxies=["DIRECT"]),
        lambda config: config["proxy-groups"].append(
            {
                "name": "AI-PROXY",
                "type": "select",
                "proxies": [remote_proxy.AI_PROXY_INTERNAL_NODE_NAME],
            }
        ),
        lambda config: config["proxies"].append(dict(config["proxies"][0])),
        lambda config: (
            config["proxies"][0].update(name="DIRECT"),
            config["proxy-groups"][0].update(proxies=["DIRECT"]),
        ),
        lambda config: (
            config["proxies"][0].update(name="reject"),
            config["proxy-groups"][0].update(proxies=["reject"]),
        ),
        lambda config: (
            config["proxies"][0].update(name="PaSs"),
            config["proxy-groups"][0].update(proxies=["PaSs"]),
        ),
        lambda config: (
            config["proxies"][0].update(name="AI-PROXY"),
            config["proxy-groups"][0].update(proxies=["AI-PROXY"]),
        ),
        lambda config: config["rules"].insert(-1, "DOMAIN,example.com,BYPASS"),
        lambda config: config["rules"].insert(-1, "DOMAIN,example.com,REJECT"),
        lambda config: config["rules"].append("MATCH,DIRECT"),
        lambda config: config.update({"external-controller": "localhost:17898"}),
        lambda config: config.update(
            {
                "rules": [
                    rule
                    for rule in config["rules"]
                    if rule not in remote_proxy.PRIVATE_DIRECT_IP_RULES
                ]
            }
        ),
    ],
)
def test_strict_privacy_contract_rejects_missing_fail_closed_guarantees(
    tmp_path,
    mutate,
):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, strict=True)
    parsed = remote_proxy.yaml.safe_load(config_path.read_text(encoding="utf-8"))
    mutate(parsed)
    config_path.write_text(
        remote_proxy.AI_PROXY_CONFIG_MARKER + "\n" + remote_proxy._dump_yaml(parsed),
        encoding="utf-8",
    )

    assert local_proxy._managed_local_config_has_strict_privacy(config_path) is False


def test_inspect_reports_desired_active_drift_from_running_config(
    monkeypatch,
    tmp_path,
):
    preferences_path = tmp_path / "preferences.json"
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=True)
    _write_config(config_path, strict=False)
    _patch_running_status(monkeypatch, config_path)

    status = local_proxy.inspect_local_ai_proxy()

    assert status.running is True
    assert status.strict_privacy_desired is True
    assert status.strict_privacy_active is False
    assert "隐私边界漂移" in status.detail
    assert "可能仍有 DIRECT" in status.detail


def test_inspect_reports_strict_active_only_when_running_contract_is_valid(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=True)
    _write_config(config_path, strict=True)
    _patch_running_status(monkeypatch, config_path)

    status = local_proxy.inspect_local_ai_proxy()

    assert status.strict_privacy_desired is True
    assert status.strict_privacy_active is True
    assert "当前运行配置已验证" in status.detail


def test_inspect_fails_closed_for_legacy_state_without_applied_fingerprint(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=True)
    _write_config(config_path, strict=True)
    _patch_running_status(
        monkeypatch,
        config_path,
        state={"mixed_port": 17897, "config_path": str(config_path)},
    )

    status = local_proxy.inspect_local_ai_proxy()

    assert status.running is True
    assert status.strict_privacy_active is False
    assert "缺少匹配的已应用" in status.detail
    assert "重新应用规则或重启" in status.detail


def test_inspect_fails_closed_in_atomic_write_before_reload_crash_window(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=True)

    _write_config(config_path, strict=False)
    old_applied_state = _applied_state(config_path)
    # Simulate atomic_write_text(new strict config) completing immediately
    # before the controller reload call/process state update.
    _write_config(config_path, strict=True)
    _patch_running_status(monkeypatch, config_path, state=old_applied_state)

    status = local_proxy.inspect_local_ai_proxy()

    assert status.running is True
    assert status.strict_privacy_active is False
    assert "隐私边界未确认" in status.detail


def test_inspect_rejects_applied_fingerprint_from_previous_process(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=True)
    _write_config(config_path, strict=True)
    state = _applied_state(config_path, pid=9999)
    _patch_running_status(monkeypatch, config_path, state=state)

    status = local_proxy.inspect_local_ai_proxy()

    assert status.strict_privacy_active is False
    assert "SHA-256/PID/端口指纹" in status.detail


def test_install_records_fingerprint_for_successfully_started_process(
    monkeypatch,
    tmp_path,
):
    config_dir = tmp_path / "mihomo"
    config_dir.mkdir()
    saved_states = []
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", config_dir)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=True)
    monkeypatch.setattr(local_proxy, "_select_local_mixed_port", lambda _port: 17897)
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: tmp_path / "mihomo.exe")
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_save_state", lambda state: saved_states.append(dict(state)))
    monkeypatch.setattr(local_proxy, "_capture_previous_env", lambda: {})
    monkeypatch.setattr(local_proxy, "_capture_vscode_proxy_state", lambda _settings: {})
    monkeypatch.setattr(local_proxy, "_capture_windows_system_proxy_state", lambda: {})
    monkeypatch.setattr(local_proxy.vscode_parser, "read_vscode_settings", lambda: {})
    monkeypatch.setattr(local_proxy, "_start_local_mihomo", lambda *_args: None)
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 4321)
    monkeypatch.setattr(local_proxy, "_apply_local_env", lambda _port: None)
    monkeypatch.setattr(local_proxy, "_apply_local_vscode_proxy", lambda _port: None)
    monkeypatch.setattr(local_proxy, "_apply_windows_system_proxy", lambda _port: None)
    monkeypatch.setattr(local_proxy, "_save_last_proxy_node", lambda _node: None)

    local_proxy.install_local_ai_proxy(remote_proxy.format_proxy_node(_node()))

    final_state = saved_states[-1]
    config_path = config_dir / "config.yaml"
    assert final_state["applied_config_sha256"] == local_proxy._local_config_sha256(
        config_path
    )
    assert final_state["applied_config_pid"] == 4321
    assert final_state["applied_config_mixed_port"] == 17897


def test_reload_success_records_new_applied_fingerprint(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, strict=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", config_path.parent)
    state = _applied_state(config_path)
    saved_states = []
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=True)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: dict(state))
    monkeypatch.setattr(local_proxy, "_save_state", lambda value: saved_states.append(dict(value)))
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 4321)
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda _port=17897: local_proxy.LocalAIProxyStatus(
            installed=True,
            running=True,
            config_path=str(config_path),
            proxy_url="http://127.0.0.1:17897",
        ),
    )
    monkeypatch.setattr(local_proxy, "_reload_local_mihomo_config", lambda *_args: None)
    monkeypatch.setattr(local_proxy, "_save_last_proxy_node", lambda _node: None)
    monkeypatch.setattr(local_proxy, "_remember_selected_subscription_node", lambda *_args, **_kwargs: None)

    local_proxy.reload_local_ai_proxy(
        remote_proxy.format_proxy_node({**_node(), "name": "new-node"})
    )

    final_state = saved_states[-1]
    assert final_state["applied_config_sha256"] == local_proxy._local_config_sha256(
        config_path
    )
    assert final_state["applied_config_sha256"] != state["applied_config_sha256"]
    assert final_state["applied_config_pid"] == 4321
    assert local_proxy._managed_local_config_has_strict_privacy(config_path) is True


def test_reload_failure_restores_old_config_and_applied_fingerprint(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, strict=True)
    original = config_path.read_text(encoding="utf-8")
    original_hash = local_proxy._local_config_sha256(config_path)
    state = _applied_state(config_path)
    saved_states = []
    reloads = []
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=False)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: dict(state))
    monkeypatch.setattr(local_proxy, "_save_state", lambda value: saved_states.append(dict(value)))
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 4321)
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda _port=17897: local_proxy.LocalAIProxyStatus(
            installed=True,
            running=True,
            config_path=str(config_path),
            proxy_url="http://127.0.0.1:17897",
        ),
    )

    def reload_config(path, _port):
        reloads.append(path.read_text(encoding="utf-8"))
        if len(reloads) == 1:
            raise TimeoutError("controller response lost")

    monkeypatch.setattr(local_proxy, "_reload_local_mihomo_config", reload_config)

    with pytest.raises(RuntimeError, match="已强制恢复旧配置"):
        local_proxy.reload_local_ai_proxy(remote_proxy.format_proxy_node(_node()))

    assert len(reloads) == 2
    assert config_path.read_text(encoding="utf-8") == original
    assert saved_states[-1]["applied_config_sha256"] == original_hash
    assert saved_states[-1]["applied_config_pid"] == 4321


def test_reload_failed_rollback_clears_applied_fingerprint(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, strict=True)
    state = _applied_state(config_path)
    saved_states = []
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=False)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: dict(state))
    monkeypatch.setattr(local_proxy, "_save_state", lambda value: saved_states.append(dict(value)))
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 4321)
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda _port=17897: local_proxy.LocalAIProxyStatus(
            installed=True,
            running=True,
            config_path=str(config_path),
            proxy_url="http://127.0.0.1:17897",
        ),
    )
    monkeypatch.setattr(
        local_proxy,
        "_reload_local_mihomo_config",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("controller unavailable")),
    )

    with pytest.raises(RuntimeError, match="强制恢复也失败"):
        local_proxy.reload_local_ai_proxy(remote_proxy.format_proxy_node(_node()))

    assert saved_states
    assert "applied_config_sha256" not in saved_states[-1]


def test_strict_privacy_transaction_keeps_pending_preference_when_stopped(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: False)

    def apply():
        raise AssertionError("must not hot apply")

    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", apply)

    message = local_proxy.set_local_proxy_strict_privacy_and_apply(True)

    assert local_proxy.load_local_proxy_preferences()["strict_privacy"] is True
    assert "下次启动时生效" in message


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"strict_privacy": True}, False),
        ({"strict_privacy": "on"}, False),
        ({"strict_privacy": False}, True),
        ({}, True),
    ],
)
def test_subscription_direct_fallback_uses_persisted_privacy_authority(
    monkeypatch,
    tmp_path,
    payload,
    expected,
):
    preferences_path = tmp_path / "preferences.json"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)
    preferences_path.write_text(remote_proxy.json.dumps(payload), encoding="utf-8")

    assert local_proxy.local_proxy_subscription_direct_fallback_allowed() is expected


def test_subscription_direct_fallback_fails_closed_on_unreadable_or_corrupt_preferences(
    monkeypatch,
    tmp_path,
):
    preferences_path = tmp_path / "preferences.json"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)
    preferences_path.write_text("{broken", encoding="utf-8")

    assert local_proxy.local_proxy_subscription_direct_fallback_allowed() is False

    preferences_path.unlink()
    preferences_path.mkdir()
    assert local_proxy.local_proxy_subscription_direct_fallback_allowed() is False


def test_subscription_direct_fallback_uses_compatibility_default_without_preferences(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "missing.json")

    assert local_proxy.local_proxy_subscription_direct_fallback_allowed() is True


def test_subscription_direct_fallback_stays_closed_after_ui_quarantines_corrupt_preferences(
    monkeypatch,
    tmp_path,
):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)
    local_proxy.clear_local_proxy_preferences_cache()

    # The permissive UI reader keeps rendering by quarantining the bad file.
    assert local_proxy.load_local_proxy_preferences()["strict_privacy"] is False
    assert not preferences_path.exists()
    assert len(tuple(tmp_path.glob("preferences.json.corrupt-*"))) == 1

    # The worker-side subscription policy must not reinterpret that absence as
    # a clean first-run compatibility state and leak through a direct fallback.
    assert local_proxy.local_proxy_subscription_direct_fallback_allowed() is False


def test_subscription_direct_fallback_fails_closed_when_quarantine_enumeration_fails(
    monkeypatch,
    tmp_path,
):
    preferences_path = tmp_path / "preferences.json"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)

    original_glob = Path.glob

    def guarded_glob(self, pattern):
        if self == tmp_path:
            raise PermissionError("access denied")
        return original_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", guarded_glob)

    assert local_proxy.local_proxy_subscription_direct_fallback_allowed() is False


@pytest.mark.parametrize(
    "invalid_value",
    ["maybe", {"enabled": True}, 2],
)
def test_subscription_direct_fallback_fails_closed_on_invalid_strict_privacy_value(
    monkeypatch,
    tmp_path,
    invalid_value,
):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text(
        remote_proxy.json.dumps({"strict_privacy": invalid_value}),
        encoding="utf-8",
    )
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)

    assert local_proxy.local_proxy_subscription_direct_fallback_allowed() is False


@pytest.mark.parametrize("content", ["{broken", "[]", "null", '"strict"'])
def test_routing_preferences_authority_rejects_corrupt_or_non_object_without_quarantine(
    monkeypatch,
    tmp_path,
    content,
):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)

    with pytest.raises(RuntimeError, match="路由偏好.*已中止配置变更"):
        local_proxy._load_local_proxy_routing_preferences_strict()

    assert preferences_path.read_text(encoding="utf-8") == content
    assert not tuple(tmp_path.glob("preferences.json.corrupt-*"))


def test_permissive_ui_read_cannot_hide_corruption_from_routing_authority(
    monkeypatch,
    tmp_path,
):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)
    local_proxy.clear_local_proxy_preferences_cache()

    assert local_proxy.load_local_proxy_preferences()["strict_privacy"] is False
    with pytest.raises(RuntimeError, match="路由偏好.*已中止配置变更"):
        local_proxy._load_local_proxy_routing_preferences_strict()

    assert not preferences_path.exists()
    quarantined = tuple(tmp_path.glob("preferences.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{broken"


def test_routing_preferences_authority_keeps_legacy_default_only_when_file_is_missing(
    monkeypatch,
    tmp_path,
):
    preferences_path = tmp_path / "missing.json"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)

    preferences = local_proxy._load_local_proxy_routing_preferences_strict()

    assert preferences["strict_privacy"] is False
    assert preferences["proxy_non_cn"] is False
    assert preferences["builtin_sites"] == {}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"strict_privacy": True}, True),
        ({"strict_privacy": "on"}, True),
        ({"strict_privacy": False}, False),
        ({}, False),
    ],
)
def test_browser_strict_privacy_authority_reads_persisted_intent(
    monkeypatch,
    tmp_path,
    payload,
    expected,
):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text(remote_proxy.json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)

    assert (
        local_proxy.local_proxy_strict_privacy_desired_authoritative() is expected
    )


def test_browser_strict_privacy_authority_missing_is_legacy_compatible(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        local_proxy,
        "LOCAL_PROXY_PREFS_PATH",
        tmp_path / "missing.json",
    )

    assert local_proxy.local_proxy_strict_privacy_desired_authoritative() is False


def test_browser_strict_privacy_authority_rejects_corrupt_state_without_quarantine(
    monkeypatch,
    tmp_path,
):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)

    with pytest.raises(RuntimeError, match="路由偏好.*已中止配置变更"):
        local_proxy.local_proxy_strict_privacy_desired_authoritative()

    assert preferences_path.read_text(encoding="utf-8") == "{broken"
    assert not tuple(tmp_path.glob("preferences.json.corrupt-*"))


@pytest.mark.parametrize("invalid_value", [None, [], {}, "maybe", 2])
def test_browser_strict_privacy_authority_rejects_invalid_intent_type(
    monkeypatch,
    tmp_path,
    invalid_value,
):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text(
        remote_proxy.json.dumps({"strict_privacy": invalid_value}),
        encoding="utf-8",
    )
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)

    with pytest.raises(RuntimeError, match="strict_privacy 值无效"):
        local_proxy.local_proxy_strict_privacy_desired_authoritative()


@pytest.mark.parametrize("operation", ["install", "reload"])
@pytest.mark.parametrize("preference_failure", ["corrupt", "permission"])
def test_install_and_reload_fail_before_config_pid_or_environment_mutation_when_routing_authority_is_invalid(
    monkeypatch,
    tmp_path,
    operation,
    preference_failure,
):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text(
        "{broken" if preference_failure == "corrupt" else '{"strict_privacy": true}',
        encoding="utf-8",
    )
    config_dir = tmp_path / "mihomo"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    original_config = "# existing running config\nmode: rule\n"
    config_path.write_text(original_config, encoding="utf-8")
    pid_path = tmp_path / "mihomo.pid"
    original_pid = "4321"
    pid_path.write_text(original_pid, encoding="utf-8")

    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", config_dir)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", pid_path)
    local_proxy.clear_local_proxy_preferences_cache()

    if preference_failure == "permission":
        original_read_text = Path.read_text

        def guarded_read_text(self, *args, **kwargs):
            if self == preferences_path:
                raise PermissionError("access denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded_read_text)

    mutations = []

    def mutation(name, result=None):
        def record(*_args, **_kwargs):
            mutations.append(name)
            return result

        return record

    monkeypatch.setattr(
        local_proxy,
        "_select_local_mixed_port",
        mutation("select-port", local_proxy.DEFAULT_LOCAL_MIXED_PORT),
    )
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", mutation("ensure-dirs"))
    monkeypatch.setattr(
        local_proxy,
        "_ensure_mihomo_binary",
        mutation("ensure-binary", tmp_path / "mihomo.exe"),
    )
    monkeypatch.setattr(local_proxy, "_save_state", mutation("save-state"))
    monkeypatch.setattr(local_proxy, "_start_local_mihomo", mutation("start-process"))
    monkeypatch.setattr(local_proxy, "_reload_local_mihomo_config", mutation("reload-process"))
    monkeypatch.setattr(local_proxy, "_apply_local_env", mutation("apply-env"))
    monkeypatch.setattr(local_proxy, "_apply_local_vscode_proxy", mutation("apply-vscode"))
    monkeypatch.setattr(local_proxy, "_apply_windows_system_proxy", mutation("apply-system"))

    action = (
        local_proxy.install_local_ai_proxy
        if operation == "install"
        else local_proxy.reload_local_ai_proxy
    )
    with pytest.raises(RuntimeError, match="路由偏好.*已中止配置变更"):
        action(remote_proxy.format_proxy_node(_node()))

    assert mutations == []
    assert config_path.read_text(encoding="utf-8") == original_config
    assert pid_path.read_text(encoding="utf-8") == original_pid
    assert preferences_path.exists()
    if preference_failure == "corrupt":
        assert preferences_path.read_text(encoding="utf-8") == "{broken"
        assert not tuple(tmp_path.glob("preferences.json.corrupt-*"))


@pytest.mark.parametrize("entrypoint", ["apply", "autostart"])
def test_routing_apply_and_autostart_fail_closed_before_delegating_on_corrupt_authority(
    monkeypatch,
    tmp_path,
    entrypoint,
):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)
    delegated = []
    monkeypatch.setattr(
        local_proxy,
        "reload_local_ai_proxy",
        lambda *_args, **_kwargs: delegated.append("reload"),
    )
    monkeypatch.setattr(
        local_proxy,
        "install_local_ai_proxy",
        lambda *_args, **_kwargs: delegated.append("install"),
    )

    action = (
        local_proxy.apply_local_proxy_routing_to_running
        if entrypoint == "apply"
        else local_proxy.auto_start_local_ai_proxy_if_enabled
    )
    with pytest.raises(RuntimeError, match="路由偏好.*已中止配置变更"):
        action()

    assert delegated == []
    assert preferences_path.read_text(encoding="utf-8") == "{broken"


def test_strict_privacy_transaction_restores_preference_after_reload_failure(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=False)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, strict=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", config_path.parent)
    state = _applied_state(config_path)
    monkeypatch.setattr(
        local_proxy,
        "_load_state",
        lambda: dict(state),
    )
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 4321)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(
        local_proxy,
        "apply_local_proxy_routing_to_running",
        lambda: (_ for _ in ()).throw(RuntimeError("controller reload failed")),
    )
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda _port=17897: local_proxy.LocalAIProxyStatus(
            installed=True,
            running=True,
            config_path="config.yaml",
            proxy_url="http://127.0.0.1:17897",
            strict_privacy_active=False,
            strict_privacy_desired=False,
        ),
    )

    with pytest.raises(RuntimeError, match="已恢复原偏好.*已恢复原运行配置"):
        local_proxy.set_local_proxy_strict_privacy_and_apply(True)

    assert local_proxy.load_local_proxy_preferences()["strict_privacy"] is False


def test_strict_privacy_transaction_fails_if_running_proxy_stops_after_apply(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, strict=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", config_path.parent)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=False)
    state = _applied_state(config_path)
    monkeypatch.setattr(
        local_proxy,
        "_load_state",
        lambda: dict(state),
    )
    managed_states = iter((True, False))
    monkeypatch.setattr(
        local_proxy,
        "_managed_local_proxy_is_running",
        lambda _state: next(managed_states),
    )
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 4321)
    saved_states = []
    monkeypatch.setattr(local_proxy, "_save_state", lambda value: saved_states.append(dict(value)))
    monkeypatch.setattr(
        local_proxy,
        "apply_local_proxy_routing_to_running",
        lambda: "controller accepted",
    )
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda _port=17897: local_proxy.LocalAIProxyStatus(
            installed=True,
            running=False,
            config_path=str(config_path),
            proxy_url="http://127.0.0.1:17897",
            strict_privacy_active=False,
            strict_privacy_desired=True,
        ),
    )

    with pytest.raises(RuntimeError, match="受管代理已停止") as exc_info:
        local_proxy.set_local_proxy_strict_privacy_and_apply(True)

    assert "代理已停止，需重新启动" in str(exc_info.value)
    assert local_proxy.load_local_proxy_preferences()["strict_privacy"] is False
    assert saved_states
    assert "applied_config_sha256" not in saved_states[-1]
