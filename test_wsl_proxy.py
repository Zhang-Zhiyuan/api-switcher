from __future__ import annotations

import subprocess

import pytest

from core import local_proxy, remote_proxy, wsl_proxy


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["wsl.exe"], returncode, stdout, stderr)


def _target(*, mode="nat", version=2) -> wsl_proxy.WSLProxyTarget:
    return wsl_proxy.WSLProxyTarget(
        distro="Ubuntu-Test",
        version=version,
        network_mode=mode,
        home="/home/tester",
        shell="/bin/bash",
        gateway="172.31.112.1" if mode != "mirrored" and version == 2 else "",
        guest_cidr="172.31.112.0/20" if mode != "mirrored" and version == 2 else "",
    )


def test_discovers_nat_target_and_limits_listener_to_wsl_subnet(monkeypatch):
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(
            stdout=(
                "distro=Ubuntu-24.04\n"
                "version=2\n"
                "mode=nat\n"
                "kernel=6.6.87.2-microsoft-standard-WSL2\n"
                "home=/home/alice\n"
                "shell=/bin/bash\n"
                "gateway=172.31.112.1\n"
                "guest_cidr=172.31.119.165/20\n"
            )
        ),
    )

    target = wsl_proxy.discover_default_wsl_target()

    assert target.distro == "Ubuntu-24.04"
    assert target.proxy_host == "172.31.112.1"
    assert target.guest_cidr == "172.31.112.0/20"
    assert target.requires_lan_listener is True
    assert target.lan_allowed_ips == (
        "127.0.0.0/8",
        "::1/128",
        "172.31.112.0/20",
    )


def test_discovers_mirrored_wsl_and_keeps_windows_proxy_loopback_only(monkeypatch):
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(
            stdout=(
                "distro=Ubuntu\nversion=2\nmode=mirrored\n"
                "kernel=microsoft-standard-WSL2\nhome=/home/alice\n"
                "shell=/bin/zsh\ngateway=192.168.1.1\nguest_cidr=192.168.1.8/24\n"
            )
        ),
    )

    target = wsl_proxy.discover_default_wsl_target()

    assert target.proxy_host == "127.0.0.1"
    assert target.requires_lan_listener is False
    assert target.lan_allowed_ips == ()


@pytest.mark.parametrize(
    "stdout",
    [
        (
            "distro=Ubuntu\nversion=2\nmode=nat\nkernel=WSL2\n"
            "home=/home/alice\nshell=/bin/bash\n"
            "gateway=192.168.1.1\nguest_cidr=172.31.119.165/20\n"
        ),
        (
            "distro=Ubuntu\nversion=2\nmode=nat\nkernel=WSL2\n"
            "home=/home/alice\nshell=/bin/bash\n"
            "gateway=10.0.0.1\nguest_cidr=10.0.0.2/8\n"
        ),
        (
            "distro=Ubuntu\nversion=2\nmode=nat\nkernel=WSL2\n"
            "home=/home/alice\nshell=/bin/bash\n"
            "gateway=203.0.113.1\nguest_cidr=203.0.113.2/24\n"
        ),
        (
            "distro=Ubuntu\nversion=2\nmode=bridged\nkernel=WSL2\n"
            "home=/home/alice\nshell=/bin/bash\n"
            "gateway=192.168.1.1\nguest_cidr=192.168.1.20/24\n"
        ),
    ],
)
def test_discovery_rejects_uncertain_or_overbroad_nat_networks(monkeypatch, stdout):
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(stdout=stdout),
    )

    with pytest.raises(RuntimeError):
        wsl_proxy.discover_default_wsl_target()


def test_mihomo_wsl_listener_is_whitelisted_and_strict_contract_remains_valid():
    node = {
        "name": "test",
        "type": "vless",
        "server": "proxy.example.com",
        "port": 443,
    }
    config = remote_proxy.build_mihomo_config(
        node,
        17897,
        strict_privacy=True,
        lan_allowed_ips=_target().lan_allowed_ips,
    )
    parsed = remote_proxy.yaml.safe_load(config)

    assert parsed["allow-lan"] is True
    assert parsed["bind-address"] == "*"
    assert parsed["lan-allowed-ips"] == [
        "127.0.0.0/8",
        "::1/128",
        "172.31.112.0/20",
    ]
    assert remote_proxy.AI_PROXY_WSL_SHARE_MARKER in config
    assert remote_proxy._managed_config_strict_privacy_enabled(config) is True

    parsed["lan-allowed-ips"] = ["127.0.0.0/8", "::1/128", "0.0.0.0/0"]
    tampered = (
        remote_proxy.AI_PROXY_CONFIG_MARKER
        + "\n"
        + remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER
        + "\n"
        + remote_proxy.AI_PROXY_WSL_SHARE_MARKER
        + "\n"
        + remote_proxy._dump_yaml(parsed)
    )
    assert remote_proxy._managed_config_strict_privacy_enabled(tampered) is False


def test_default_mihomo_listener_contract_stays_loopback_only():
    config = remote_proxy.build_mihomo_config(
        {"name": "test", "type": "vless", "server": "proxy.example.com", "port": 443},
        17897,
    )
    parsed = remote_proxy.yaml.safe_load(config)

    assert parsed["allow-lan"] is False
    assert parsed["bind-address"] == "127.0.0.1"
    assert "lan-allowed-ips" not in parsed
    assert remote_proxy.AI_PROXY_WSL_SHARE_MARKER not in config


def test_local_config_enables_only_discovered_wsl_nat_subnet():
    config = local_proxy._build_local_mihomo_config(
        {"name": "test", "type": "vless", "server": "proxy.example.com", "port": 443},
        17897,
        preferences={"share_to_wsl": True},
        wsl_target=_target(),
    )
    parsed = remote_proxy.yaml.safe_load(config)

    assert parsed["allow-lan"] is True
    assert parsed["lan-allowed-ips"][-1] == "172.31.112.0/20"


def test_strict_preferences_reject_ambiguous_wsl_share_value(monkeypatch, tmp_path):
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text('{"share_to_wsl": "maybe"}', encoding="utf-8")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", preferences_path)
    local_proxy.clear_local_proxy_preferences_cache()

    with pytest.raises(RuntimeError, match="share_to_wsl"):
        local_proxy._load_local_proxy_routing_preferences_strict()


def test_local_install_persists_reversible_wsl_ownership(monkeypatch, tmp_path):
    config_dir = tmp_path / "mihomo"
    binary_dir = tmp_path / "bin"
    config_dir.mkdir()
    binary_dir.mkdir()
    binary = binary_dir / "mihomo.exe"
    binary.write_bytes(b"MZ")
    saved_states = []
    target = _target()
    integration = wsl_proxy.WSLProxyIntegrationResult(
        target,
        17897,
        configured_profiles=(".profile", ".bashrc"),
        created_profiles=(".profile",),
        firewall_rule="API-Switcher-WSL-Proxy-17897",
        tcp_reachable=True,
    )
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", config_dir)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_BIN_DIR", binary_dir)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(share_to_wsl=True)
    monkeypatch.setattr(local_proxy.wsl_proxy, "discover_default_wsl_target", lambda: target)
    monkeypatch.setattr(local_proxy, "_select_local_mixed_port", lambda _port: 17897)
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: binary)
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
    monkeypatch.setattr(
        local_proxy.wsl_proxy,
        "install_proxy_integration",
        lambda actual_target, port, actual_binary, **_kwargs: integration,
    )
    monkeypatch.setattr(local_proxy, "_save_last_proxy_node", lambda _node: None)
    monkeypatch.setattr(local_proxy, "_local_mihomo_core_status_detail", lambda: "")
    monkeypatch.setattr(local_proxy, "_schedule_mihomo_update_check", lambda: False)

    message = local_proxy.install_local_ai_proxy(
        remote_proxy.format_proxy_node(
            {"name": "test", "type": "vless", "server": "proxy.example.com", "port": 443}
        )
    )

    parsed = remote_proxy.yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
    assert parsed["lan-allowed-ips"][-1] == "172.31.112.0/20"
    assert saved_states[-1]["managed_wsl_proxy"]["owner"] == "api-switcher"
    assert saved_states[-1]["managed_wsl_proxy"]["created_profiles"] == [".profile"]
    assert "WSL 已接入" in message


def test_restore_managed_settings_cleans_wsl_before_windows_state(monkeypatch):
    actions = []
    state = {
        "managed_wsl_proxy": {
            "owner": "api-switcher",
            "distro": "Ubuntu",
            "firewall_rule": "API-Switcher-WSL-Proxy-17897",
        }
    }
    monkeypatch.setattr(
        local_proxy.wsl_proxy,
        "remove_proxy_integration",
        lambda _state: actions.append("wsl"),
    )
    monkeypatch.setattr(local_proxy, "_restore_local_env", lambda *_args: actions.append("env"))
    monkeypatch.setattr(
        local_proxy,
        "_restore_local_vscode_proxy",
        lambda *_args: actions.append("vscode"),
    )
    monkeypatch.setattr(
        local_proxy,
        "_restore_windows_system_proxy",
        lambda *_args: actions.append("wininet"),
    )

    assert local_proxy._restore_managed_settings(state, 17897) == []
    assert actions == ["wsl", "env", "vscode", "wininet"]


def test_install_repairs_nat_firewall_only_after_tcp_probe_fails(monkeypatch, tmp_path):
    target = _target()
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"MZ")
    probes = iter([False, True])
    firewall_calls = []
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(
            stdout="configured=1\nprofiles=.profile,.bashrc\ncreated=.profile\n"
        ),
    )
    monkeypatch.setattr(
        wsl_proxy,
        "probe_wsl_proxy_tcp",
        lambda *_args, **_kwargs: next(probes),
    )
    monkeypatch.setattr(
        wsl_proxy,
        "_ensure_scoped_firewall_rule",
        lambda *_args: firewall_calls.append(True) or "API-Switcher-WSL-Proxy-17897",
    )

    result = wsl_proxy.install_proxy_integration(target, 17897, binary)

    assert firewall_calls == [True]
    assert result.tcp_reachable is True
    assert result.configured_profiles == (".profile", ".bashrc")
    assert result.created_profiles == (".profile",)
    assert result.state()["firewall_rule"] == "API-Switcher-WSL-Proxy-17897"


def test_failed_install_removes_only_new_managed_hooks_and_rule(monkeypatch, tmp_path):
    target = _target()
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"MZ")
    removed = []
    deleted = []
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(
            stdout="configured=1\nprofiles=.profile\ncreated=.profile\n"
        ),
    )
    monkeypatch.setattr(wsl_proxy, "probe_wsl_proxy_tcp", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        wsl_proxy,
        "_ensure_scoped_firewall_rule",
        lambda *_args: "API-Switcher-WSL-Proxy-17897",
    )
    monkeypatch.setattr(
        wsl_proxy,
        "_remove_profile_hooks",
        lambda distro, **kwargs: removed.append((distro, kwargs)),
    )
    monkeypatch.setattr(
        wsl_proxy,
        "_delete_firewall_rule",
        lambda rule, **kwargs: deleted.append((rule, kwargs)),
    )

    with pytest.raises(RuntimeError, match="WSL NAT"):
        wsl_proxy.install_proxy_integration(target, 17897, binary)

    assert removed[0][0] == "Ubuntu-Test"
    assert removed[0][1]["created_profiles"] == (".profile",)
    assert deleted[0][0] == "API-Switcher-WSL-Proxy-17897"


def test_profile_write_failure_uses_reported_partial_ownership_for_cleanup(monkeypatch, tmp_path):
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"MZ")
    removed = []
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(
            returncode=1,
            stdout="profiles=.profile\ncreated=.profile\n",
            stderr="profile path is not writable: .bashrc",
        ),
    )
    monkeypatch.setattr(
        wsl_proxy,
        "_remove_profile_hooks",
        lambda distro, **kwargs: removed.append((distro, kwargs)),
    )

    with pytest.raises(RuntimeError, match="写入 WSL 代理环境入口失败"):
        wsl_proxy.install_proxy_integration(_target(), 17897, binary)

    assert removed == [
        (
            "Ubuntu-Test",
            {"created_profiles": (".profile",), "strict": False},
        )
    ]


def test_existing_compatible_hooks_survive_profile_rewrite_failure(monkeypatch, tmp_path):
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"MZ")
    removed = []
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(
            returncode=1,
            stdout="profiles=.profile\ncreated=\n",
            stderr="profile path is temporarily locked: .bashrc",
        ),
    )
    monkeypatch.setattr(
        wsl_proxy,
        "_remove_profile_hooks",
        lambda distro, **kwargs: removed.append((distro, kwargs)),
    )
    previous = {
        "owner": "api-switcher",
        "distro": "Ubuntu-Test",
        "mixed_port": 17897,
    }

    with pytest.raises(RuntimeError, match="写入 WSL 代理环境入口失败"):
        wsl_proxy.install_proxy_integration(
            _target(),
            17897,
            binary,
            previous_state=previous,
        )

    assert removed == []


def test_existing_owned_firewall_rule_is_retained_when_scope_is_unchanged(monkeypatch, tmp_path):
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"MZ")
    deleted = []
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(
            stdout="configured=1\nprofiles=.profile\ncreated=\n"
        ),
    )
    monkeypatch.setattr(wsl_proxy, "probe_wsl_proxy_tcp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        wsl_proxy,
        "_delete_firewall_rule",
        lambda rule, **kwargs: deleted.append((rule, kwargs)),
    )
    previous = {
        "owner": "api-switcher",
        "distro": "Ubuntu-Test",
        "guest_cidr": "172.31.112.0/20",
        "mixed_port": 17897,
        "firewall_rule": "API-Switcher-WSL-Proxy-17897",
    }

    result = wsl_proxy.install_proxy_integration(
        _target(),
        17897,
        binary,
        previous_state=previous,
    )

    assert result.firewall_rule == "API-Switcher-WSL-Proxy-17897"
    assert deleted == []


def test_switching_to_mirrored_mode_removes_old_nat_firewall_rule(monkeypatch, tmp_path):
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"MZ")
    deleted = []
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(
            stdout="configured=1\nprofiles=.profile\ncreated=\n"
        ),
    )
    monkeypatch.setattr(wsl_proxy, "probe_wsl_proxy_tcp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        wsl_proxy,
        "_delete_firewall_rule",
        lambda rule, **kwargs: deleted.append((rule, kwargs)),
    )
    previous = {
        "owner": "api-switcher",
        "distro": "Ubuntu-Test",
        "guest_cidr": "172.31.112.0/20",
        "mixed_port": 17897,
        "firewall_rule": "API-Switcher-WSL-Proxy-17897",
    }

    result = wsl_proxy.install_proxy_integration(
        _target(mode="mirrored"),
        17897,
        binary,
        previous_state=previous,
    )

    assert result.firewall_rule == ""
    assert deleted == [("API-Switcher-WSL-Proxy-17897", {"strict": True})]


def test_changed_subnet_stages_new_firewall_rule_before_retiring_old(monkeypatch, tmp_path):
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"MZ")
    actions = []
    new_rule = wsl_proxy._scoped_firewall_rule_name(_target(), 17897)
    old_rule = "API-Switcher-WSL-Proxy-17897-deadbeefcafe"
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(stdout="configured=1\nprofiles=.profile\ncreated=\n"),
    )
    monkeypatch.setattr(wsl_proxy, "probe_wsl_proxy_tcp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        wsl_proxy,
        "_ensure_scoped_firewall_rule",
        lambda *_args: actions.append(("add", new_rule)) or new_rule,
    )
    monkeypatch.setattr(
        wsl_proxy,
        "_delete_firewall_rule",
        lambda rule, **kwargs: actions.append(("delete", rule, kwargs)),
    )
    previous = {
        "owner": "api-switcher",
        "distro": "Ubuntu-Test",
        "guest_cidr": "172.30.0.0/20",
        "mixed_port": 17897,
        "firewall_rule": old_rule,
    }

    result = wsl_proxy.install_proxy_integration(
        _target(),
        17897,
        binary,
        previous_state=previous,
    )

    assert result.firewall_rule == new_rule
    assert actions == [
        ("add", new_rule),
        ("delete", old_rule, {"strict": True}),
    ]


def test_changed_subnet_failure_keeps_old_rule_and_hooks(monkeypatch, tmp_path):
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"MZ")
    probes = iter([True, False])
    deleted = []
    removed = []
    new_rule = wsl_proxy._scoped_firewall_rule_name(_target(), 17897)
    old_rule = "API-Switcher-WSL-Proxy-17897-deadbeefcafe"
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(stdout="configured=1\nprofiles=.profile\ncreated=\n"),
    )
    monkeypatch.setattr(
        wsl_proxy,
        "probe_wsl_proxy_tcp",
        lambda *_args, **_kwargs: next(probes),
    )
    monkeypatch.setattr(wsl_proxy, "_ensure_scoped_firewall_rule", lambda *_args: new_rule)
    monkeypatch.setattr(
        wsl_proxy,
        "_delete_firewall_rule",
        lambda rule, **kwargs: deleted.append((rule, kwargs)),
    )
    monkeypatch.setattr(
        wsl_proxy,
        "_remove_profile_hooks",
        lambda distro, **kwargs: removed.append((distro, kwargs)),
    )
    previous = {
        "owner": "api-switcher",
        "distro": "Ubuntu-Test",
        "guest_cidr": "172.30.0.0/20",
        "mixed_port": 17897,
        "firewall_rule": old_rule,
    }

    with pytest.raises(RuntimeError, match="WSL NAT"):
        wsl_proxy.install_proxy_integration(
            _target(),
            17897,
            binary,
            previous_state=previous,
        )

    assert deleted == [(new_rule, {"strict": False})]
    assert removed == []


def test_same_subnet_repair_uses_alternate_rule_slot_before_failure(monkeypatch, tmp_path):
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"MZ")
    target = _target()
    old_rule = wsl_proxy._scoped_firewall_rule_name(target, 17897, slot="a")
    staged_rule = wsl_proxy._scoped_firewall_rule_name(target, 17897, slot="b")
    staged = []
    deleted = []
    probes = iter([False, False])
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: _completed(stdout="configured=1\nprofiles=.profile\ncreated=\n"),
    )
    monkeypatch.setattr(
        wsl_proxy,
        "probe_wsl_proxy_tcp",
        lambda *_args, **_kwargs: next(probes),
    )
    monkeypatch.setattr(
        wsl_proxy,
        "_ensure_scoped_firewall_rule",
        lambda *_args: staged.append(_args[-1]) or _args[-1],
    )
    monkeypatch.setattr(
        wsl_proxy,
        "_delete_firewall_rule",
        lambda rule, **kwargs: deleted.append((rule, kwargs)),
    )
    previous = {
        "owner": "api-switcher",
        "distro": target.distro,
        "guest_cidr": target.guest_cidr,
        "mixed_port": 17897,
        "firewall_rule": old_rule,
    }

    with pytest.raises(RuntimeError, match="WSL NAT"):
        wsl_proxy.install_proxy_integration(
            target,
            17897,
            binary,
            previous_state=previous,
        )

    assert staged == [staged_rule]
    assert deleted == [(staged_rule, {"strict": False})]


def test_remove_refuses_unowned_state(monkeypatch):
    monkeypatch.setattr(
        wsl_proxy,
        "_remove_profile_hooks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not mutate")),
    )

    with pytest.raises(RuntimeError, match="不属于本工具"):
        wsl_proxy.remove_proxy_integration({"owner": "another-app", "distro": "Ubuntu"})


def test_shell_hook_is_dynamic_for_nat_and_contains_no_credentials():
    script = wsl_proxy._build_posix_proxy_script(17897)
    install = wsl_proxy._build_install_script(17897)
    remove = wsl_proxy._build_remove_script((".profile",))
    check = wsl_proxy._build_profile_check_script()

    assert "ip route show default" in script
    assert "wslinfo --networking-mode" in script
    assert "HTTP_PROXY" in script and "ANTHROPIC" not in script and "API_KEY" not in script
    assert wsl_proxy.WSL_SOURCE_BEGIN in install
    assert "ambiguous API Switcher markers" in install
    assert "existing file is not owned by API Switcher" in install
    assert wsl_proxy.WSL_MANAGED_FILE_HEADER in install
    assert "remove_owned_file" in remove and wsl_proxy.WSL_MANAGED_FILE_HEADER in remove
    assert wsl_proxy.WSL_MANAGED_FILE_HEADER in check


def test_wslconfig_merge_is_idempotent_and_preserves_unrelated_settings():
    original = (
        "# user comment\n"
        "[wsl2]\n"
        "memory=8GB\n"
        "networkingMode=nat ; keep this comment\n"
        "[experimental]\n"
        "autoMemoryReclaim=gradual\n"
    )

    updated = wsl_proxy._merge_wslconfig(original)

    assert "memory=8GB" in updated
    assert "networkingMode=mirrored ; keep this comment" in updated
    assert "dnsTunneling=true" in updated
    assert "autoProxy=true" in updated
    assert "autoMemoryReclaim=gradual" in updated
    assert wsl_proxy._merge_wslconfig(updated) == updated


def test_wslconfig_merge_refuses_ambiguous_sections_or_keys():
    with pytest.raises(RuntimeError, match="多个"):
        wsl_proxy._merge_wslconfig("[wsl2]\nautoProxy=true\n[wsl2]\nautoProxy=false\n")
    with pytest.raises(RuntimeError, match="重复"):
        wsl_proxy._merge_wslconfig("[wsl2]\nautoProxy=true\nautoproxy=false\n")


def test_wslconfig_reader_accepts_utf16_and_never_overwrites_old_backup(tmp_path):
    config = tmp_path / ".wslconfig"
    config.write_text("[wsl2]\nnetworkingMode=nat\n", encoding="utf-16")
    first_backup = tmp_path / ".wslconfig.api-switcher.bak"
    first_backup.write_text("original", encoding="utf-8")

    assert "networkingMode=nat" in wsl_proxy._read_wslconfig_text(config)
    assert wsl_proxy._next_wslconfig_backup_path(config).name == ".wslconfig.api-switcher.bak.1"


def test_wslconfig_concurrent_edit_is_detected_before_overwrite(tmp_path):
    config = tmp_path / ".wslconfig"
    config.write_text("[wsl2]\nnetworkingMode=nat\n", encoding="utf-8")
    snapshot = wsl_proxy._wslconfig_file_snapshot(config)
    config.write_text("[wsl2]\nnetworkingMode=mirrored\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="其他程序修改"):
        wsl_proxy._assert_wslconfig_unchanged(config, snapshot)

    missing = tmp_path / "new.wslconfig"
    missing.write_text("[wsl2]\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="其他程序创建"):
        wsl_proxy._assert_wslconfig_unchanged(missing, None)


def test_inspection_reports_both_credential_free_api_paths(monkeypatch):
    target = _target(mode="mirrored")
    responses = iter(
        [
            _completed(stdout="configured=1\n"),
            _completed(stdout="codex=0:401\nclaude=0:404\n"),
        ]
    )
    monkeypatch.setattr(wsl_proxy, "discover_default_wsl_target", lambda **_kwargs: target)
    monkeypatch.setattr(
        wsl_proxy,
        "_run_wsl_command",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr(wsl_proxy, "probe_wsl_proxy_tcp", lambda *_args, **_kwargs: True)

    inspection = wsl_proxy.inspect_proxy_integration(17897, full_probe=True)

    assert inspection.ready is True
    assert inspection.codex_reachable is True
    assert inspection.claude_reachable is True
    assert "Codex 路径可达" in inspection.summary()
    assert "Claude Code 路径可达" in inspection.summary()


def test_proxy_generated_5xx_is_not_reported_as_api_reachable():
    assert wsl_proxy._http_probe_ok(401) is True
    assert wsl_proxy._http_probe_ok(404) is True
    assert wsl_proxy._http_probe_ok(407) is False
    assert wsl_proxy._http_probe_ok(502) is False


def test_reconcile_preserves_created_profile_ownership_on_same_distro(monkeypatch):
    previous = {
        "owner": "api-switcher",
        "enabled": True,
        "distro": "Ubuntu-Test",
        "version": 2,
        "network_mode": "nat",
        "gateway": "172.31.112.1",
        "guest_cidr": "172.31.112.0/20",
        "mixed_port": 17897,
        "configured_profiles": [".profile"],
        "created_profiles": [".profile"],
        "firewall_rule": "",
    }
    result = wsl_proxy.WSLProxyIntegrationResult(
        _target(),
        17897,
        configured_profiles=(".profile", ".bashrc"),
        created_profiles=(),
        tcp_reachable=True,
    )
    state = {"managed_wsl_proxy": previous}
    monkeypatch.setattr(
        local_proxy.wsl_proxy,
        "install_proxy_integration",
        lambda *_args, **_kwargs: result,
    )

    detail, actual = local_proxy._reconcile_wsl_integration_state(
        state,
        _target(),
        17897,
        force=True,
        binary_path=local_proxy.LOCAL_PROXY_BIN_DIR / "mihomo.exe",
    )

    assert actual is result
    assert "WSL 已接入" in detail
    assert state["managed_wsl_proxy"]["created_profiles"] == [".profile"]


def test_reconcile_does_not_remove_old_distro_before_new_install_succeeds(monkeypatch):
    previous = {
        "owner": "api-switcher",
        "enabled": True,
        "distro": "Ubuntu-Old",
        "version": 2,
        "network_mode": "nat",
        "gateway": "172.30.0.1",
        "guest_cidr": "172.30.0.0/20",
        "mixed_port": 17897,
        "configured_profiles": [".profile"],
        "created_profiles": [],
        "firewall_rule": "API-Switcher-WSL-Proxy-17897-deadbeefcafe",
    }
    state = {"managed_wsl_proxy": previous}
    removed = []
    monkeypatch.setattr(
        local_proxy.wsl_proxy,
        "install_proxy_integration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("new distro failed")),
    )
    monkeypatch.setattr(
        local_proxy.wsl_proxy,
        "remove_proxy_integration",
        lambda value, **kwargs: removed.append((value, kwargs)),
    )

    with pytest.raises(RuntimeError, match="new distro failed"):
        local_proxy._reconcile_wsl_integration_state(
            state,
            _target(),
            17897,
            force=True,
            binary_path=local_proxy.LOCAL_PROXY_BIN_DIR / "mihomo.exe",
        )

    assert removed == []
    assert state["managed_wsl_proxy"] == previous


def test_firewall_rule_is_program_port_and_subnet_scoped(monkeypatch, tmp_path):
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"MZ")
    commands = []
    monkeypatch.setattr(wsl_proxy, "_is_windows_admin", lambda: True)
    monkeypatch.setattr(wsl_proxy, "_netsh_executable", lambda: "netsh.exe")
    monkeypatch.setattr(
        wsl_proxy,
        "_run_windows_command",
        lambda args, **_kwargs: commands.append(list(args)) or _completed(),
    )

    rule = wsl_proxy._ensure_scoped_firewall_rule(_target(), 17897, binary)

    assert rule == wsl_proxy._scoped_firewall_rule_name(_target(), 17897)
    add = commands[-1]
    assert "localport=17897" in add
    assert "remoteip=172.31.112.0/20" in add
    assert f"program={binary.resolve()}" in add
    assert "edge=no" in add


def test_firewall_rule_name_is_scoped_and_legacy_names_remain_removable():
    first = wsl_proxy._scoped_firewall_rule_name(_target(), 17897)
    second = wsl_proxy._scoped_firewall_rule_name(
        wsl_proxy.WSLProxyTarget(
            distro="Ubuntu-Test",
            version=2,
            network_mode="nat",
            home="/home/test",
            shell="/bin/bash",
            gateway="172.30.0.1",
            guest_cidr="172.30.0.0/20",
        ),
        17897,
    )

    assert first != second
    assert wsl_proxy._next_scoped_firewall_rule_name(_target(), 17897, first).endswith("-b")
    assert wsl_proxy._owned_firewall_rule_name(first) is True
    assert wsl_proxy._owned_firewall_rule_name("API-Switcher-WSL-Proxy-17897") is True
    assert wsl_proxy._owned_firewall_rule_name(first.rsplit("-", 1)[0]) is True
    assert wsl_proxy._owned_firewall_rule_name("API-Switcher-WSL-Proxy-17897-bad") is False


def test_cleanup_enumerates_recorded_and_both_scoped_rule_slots():
    target = _target()
    first = wsl_proxy._scoped_firewall_rule_name(target, 17897, slot="a")
    second = wsl_proxy._scoped_firewall_rule_name(target, 17897, slot="b")
    state = {
        "owner": "api-switcher",
        "distro": target.distro,
        "guest_cidr": target.guest_cidr,
        "mixed_port": 17897,
        "firewall_rule": first,
    }

    names = wsl_proxy._managed_firewall_rule_names(state)

    assert names[0] == first
    assert first in names and second in names
    assert first.rsplit("-", 1)[0] in names
