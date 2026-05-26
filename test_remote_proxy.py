from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

import pytest

from core import local_proxy, remote_proxy


def test_parse_proxy_node_accepts_clash_inline_map():
    node = remote_proxy.parse_proxy_node(
        "- { name: 优秀|台湾, type: vless, server: example.com, port: 30021, "
        "uuid: 0a85ff2d-cf8b-4a25-a675-b1ec138b8d35, udp: true, tls: false, network: tcp }"
    )

    assert node["name"] == "优秀|台湾"
    assert node["type"] == "vless"
    assert node["server"] == "example.com"
    assert node["port"] == 30021
    assert node["udp"] is True
    assert node["tls"] is False


def test_build_mihomo_config_routes_only_ai_domains_to_proxy():
    config = remote_proxy.build_mihomo_config(
        {
            "name": "node-a",
            "type": "vless",
            "server": "example.com",
            "port": 30021,
            "uuid": "token",
            "udp": True,
        }
    )

    assert 'name: "node-a"' in config
    assert 'server: "example.com"' in config
    assert 'DOMAIN-SUFFIX,chatgpt.com,AI-PROXY' in config
    assert 'DOMAIN-SUFFIX,anthropic.com,AI-PROXY' in config
    assert 'DOMAIN-SUFFIX,generativelanguage.googleapis.com,AI-PROXY' in config
    assert 'DOMAIN-SUFFIX,oauth2.googleapis.com,AI-PROXY' in config
    assert 'MATCH,DIRECT' in config


def test_parse_proxy_node_supports_nested_inline_options_from_full_config():
    node = remote_proxy.parse_proxy_node(
        """
proxies:
  - { name: reality, type: vless, server: example.com, port: 443,
      uuid: token, reality-opts: { public-key: abc, short-id: "01" },
      alpn: [h2, http/1.1] }
rules:
  - MATCH,DIRECT
"""
    )

    assert node["name"] == "reality"
    assert node["reality-opts"]["public-key"] == "abc"
    assert node["reality-opts"]["short-id"] == "01"
    assert node["alpn"] == ["h2", "http/1.1"]


def test_parse_proxy_node_prefers_first_proxy_block_in_full_yaml():
    node = remote_proxy.parse_proxy_node(
        """
mixed-port: 7890
proxies:
  - name: first
    type: vless
    server: first.example.com
    port: "443"
    uuid: token
  - name: second
    type: ss
    server: second.example.com
    port: 8388
rules:
  - MATCH,DIRECT
"""
    )

    assert node["name"] == "first"
    assert node["server"] == "first.example.com"
    assert node["port"] == 443


def test_parse_proxy_node_supports_inline_proxy_list_in_full_yaml():
    node = remote_proxy.parse_proxy_node(
        """
mixed-port: 7890
proxies: [{ name: inline, type: vless, server: inline.example.com, port: "8443", uuid: token }]
rules:
  - MATCH,DIRECT
"""
    )

    assert node["name"] == "inline"
    assert node["server"] == "inline.example.com"
    assert node["port"] == 8443


def test_parse_proxy_subscription_content_lists_yaml_nodes():
    nodes = remote_proxy.parse_proxy_subscription_content(
        """
proxies:
  - name: first
    type: vless
    server: first.example.com
    port: 443
    uuid: token
  - { name: second, type: ss, server: second.example.com, port: 8388, cipher: aes-128-gcm, password: pass }
rules:
  - MATCH,DIRECT
"""
    )

    assert [item.node["name"] for item in nodes] == ["first", "second"]
    assert nodes[0].display_name().startswith("1. first")


def test_parse_proxy_subscription_content_decodes_base64_uri_subscription():
    raw = "\n".join([
        "vless://token@example.com:443?encryption=none&security=tls&type=ws&path=%2Fchat#VLESS%20Node",
        "trojan://secret@trojan.example.com:8443?security=tls&sni=t.example.com#Trojan",
    ])
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")

    nodes = remote_proxy.parse_proxy_subscription_content(encoded)

    assert [item.node["name"] for item in nodes] == ["VLESS Node", "Trojan"]
    assert nodes[0].node["ws-opts"]["path"] == "/chat"
    assert nodes[1].node["servername"] == "t.example.com"


def test_parse_proxy_subscription_content_accepts_vmess_uri():
    vmess = {
        "ps": "vmess-node",
        "add": "vmess.example.com",
        "port": "443",
        "id": "00000000-0000-0000-0000-000000000000",
        "aid": "0",
        "net": "ws",
        "path": "/ws",
        "host": "host.example.com",
        "tls": "tls",
    }
    encoded = base64.b64encode(json.dumps(vmess).encode("utf-8")).decode("ascii")

    nodes = remote_proxy.parse_proxy_subscription_content(f"vmess://{encoded}")

    assert nodes[0].node["name"] == "vmess-node"
    assert nodes[0].node["ws-opts"]["headers"]["Host"] == "host.example.com"


def test_parse_proxy_subscription_content_accepts_ss_sip002_variants():
    userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:pass").decode("ascii").rstrip("=")
    full = base64.urlsafe_b64encode(b"chacha20-ietf-poly1305:secret@full.example.com:8389").decode("ascii").rstrip("=")
    nodes = remote_proxy.parse_proxy_subscription_content(
        "\n".join([
            f"ss://{userinfo}@ss.example.com:8388#SS%20Userinfo",
            f"ss://{full}#SS%20Full",
        ])
    )

    assert nodes[0].node["cipher"] == "aes-256-gcm"
    assert nodes[0].node["server"] == "ss.example.com"
    assert nodes[1].node["server"] == "full.example.com"
    assert nodes[1].node["password"] == "secret"


def test_parse_proxy_subscription_content_accepts_ssr_uri():
    def b64u(value: str) -> str:
        return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")

    body = (
        f"ssr.example.com:443:origin:aes-256-gcm:plain:{b64u('pass')}"
        f"/?remarks={b64u('SSR Node')}&obfsparam={b64u('obfs.example')}&protoparam={b64u('proto')}"
    )
    nodes = remote_proxy.parse_proxy_subscription_content(f"ssr://{b64u(body)}")

    assert nodes[0].node["name"] == "SSR Node"
    assert nodes[0].node["type"] == "ssr"
    assert nodes[0].node["obfs-param"] == "obfs.example"
    assert nodes[0].node["protocol-param"] == "proto"


def test_parse_proxy_subscription_content_accepts_reality_grpc_and_hysteria2():
    vless = (
        "vless://token@reality.example.com:443?"
        "encryption=none&security=reality&sni=target.example.com&fp=chrome"
        "&pbk=public-key&sid=abcd&spx=%2F&type=grpc&serviceName=svc#Reality"
    )
    hy2 = "hy2://secret@hy.example.com:8443?sni=hy.example.com&insecure=1&alpn=h3#HY2"
    nodes = remote_proxy.parse_proxy_subscription_content(f"prefix {vless}, then {hy2}")

    assert nodes[0].node["reality-opts"]["public-key"] == "public-key"
    assert nodes[0].node["reality-opts"]["spider-x"] == "/"
    assert nodes[0].node["grpc-opts"]["grpc-service-name"] == "svc"
    assert nodes[1].node["type"] == "hysteria2"
    assert nodes[1].node["skip-cert-verify"] is True


def test_parse_proxy_subscription_content_accepts_tuic_and_proxy_mapping_aliases():
    tuic = "tuic://uuid:pass@tuic.example.com:443?sni=tuic.example.com&alpn=h3&congestion_control=bbr#TUIC"
    yaml_text = """
proxies:
  mapped:
    type: hy2
    address: mapped.example.com
    server_port: 9443
    password: mapped-pass
"""
    nodes = remote_proxy.parse_proxy_subscription_content(tuic + "\n" + yaml_text)
    by_name = {item.node["name"]: item.node for item in nodes}

    assert by_name["TUIC"]["type"] == "tuic"
    assert by_name["TUIC"]["congestion-controller"] == "bbr"
    assert by_name["mapped"]["type"] == "hysteria2"
    assert by_name["mapped"]["port"] == 9443


def test_parse_proxy_subscription_content_filters_provider_metadata_nodes():
    nodes = remote_proxy.parse_proxy_subscription_content(
        """
proxies:
  - { name: 剩余流量：351.98+GB, type: vless, server: cloudflare.example.com, port: 8443 }
  - { name: 套餐到期：2026-05-30, type: vless, server: cloudflare.example.com, port: 8443 }
  - { name: 官网地址防失联发布页：example.com, type: vless, server: cloudflare.example.com, port: 8443 }
  - { name: real-node, type: vless, server: real.example.com, port: 443 }
"""
    )

    assert [item.node["name"] for item in nodes] == ["real-node"]
    assert nodes[0].index == 1


def test_format_proxy_node_round_trips_selected_subscription_node():
    node = remote_proxy.parse_proxy_subscription_content(
        "vless://token@example.com:443?encryption=none&type=ws&path=%2Fchat#picked"
    )[0].node

    text = remote_proxy.format_proxy_node(node)
    parsed = remote_proxy.parse_proxy_node(text)

    assert parsed["name"] == "picked"
    assert parsed["ws-opts"]["path"] == "/chat"


def test_fetch_proxy_subscription_saves_content_and_returns_nodes(monkeypatch, tmp_path):
    class Headers:
        def get_content_type(self):
            return "application/yaml"

        def get_content_charset(self):
            return "utf-8"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"proxies:\n  - { name: fetched, type: vless, server: example.com, port: 443 }\n"

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", lambda *_args, **_kwargs: Response())

    result = remote_proxy.fetch_proxy_subscription("https://example.com/sub")

    assert result.nodes[0].node["name"] == "fetched"
    assert (tmp_path / "proxy_subscriptions").exists()
    assert result.saved_path.endswith(".yaml")
    state = remote_proxy.load_proxy_subscription_state()
    assert state["url"] == "https://example.com/sub"
    assert state["node_count"] == 1
    assert state["saved_path"] == result.saved_path
    assert state["content_type"] == "application/yaml"
    assert state["charset"] == "utf-8"


def test_fetch_proxy_subscription_retries_transient_download(monkeypatch, tmp_path):
    class Headers:
        def get_content_type(self):
            return "application/yaml"

        def get_content_charset(self):
            return "utf-8"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"proxies:\n  - { name: retry, type: vless, server: example.com, port: 443 }\n"

    calls = 0

    def fake_urlopen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("temporary disconnect")
        return Response()

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", fake_urlopen)

    result = remote_proxy.fetch_proxy_subscription("https://example.com/sub", retry_base_delay=0)

    assert calls == 3
    assert result.nodes[0].node["name"] == "retry"


def test_fetch_proxy_subscription_decodes_gzip_response(monkeypatch, tmp_path):
    class Headers:
        def get(self, key, default=None):
            return {"Content-Encoding": "gzip"}.get(key, default)

        def get_content_type(self):
            return "application/yaml"

        def get_content_charset(self):
            return "utf-8"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return gzip.compress(
                b"proxies:\n  - { name: zipped, type: vless, server: example.com, port: 443 }\n"
            )

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", lambda *_args, **_kwargs: Response())

    result = remote_proxy.fetch_proxy_subscription("https://example.com/sub", retry_base_delay=0)

    assert result.nodes[0].node["name"] == "zipped"
    assert b"proxies:" in Path(result.saved_path).read_bytes()


def test_load_cached_proxy_subscription_reads_saved_content(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    cache_dir = tmp_path / "proxy_subscriptions"
    cache_dir.mkdir()
    content_path = cache_dir / "subscription-test.yaml"
    content_path.write_text(
        "proxies:\n  - { name: cached, type: vless, server: example.com, port: 443 }\n",
        encoding="utf-8",
    )
    remote_proxy.save_proxy_subscription_state(
        url="https://example.com/sub",
        saved_path=str(content_path),
        last_fetched_at="2026-05-26T00:00:00+00:00",
        node_count=1,
    )

    cached = remote_proxy.load_cached_proxy_subscription()

    assert cached is not None
    assert cached.url == "https://example.com/sub"
    assert cached.nodes[0].node["name"] == "cached"


def test_load_cached_proxy_subscription_respects_saved_charset(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    cache_dir = tmp_path / "proxy_subscriptions"
    cache_dir.mkdir()
    content_path = cache_dir / "subscription-gbk.yaml"
    content_path.write_bytes(
        "proxies:\n  - { name: 缓存节点, type: vless, server: example.com, port: 443 }\n".encode(
            "gb18030"
        )
    )
    remote_proxy.save_proxy_subscription_state(
        url="https://example.com/sub",
        saved_path=str(content_path),
        last_fetched_at="2026-05-26T00:00:00+00:00",
        node_count=1,
        charset="gb18030",
    )

    cached = remote_proxy.load_cached_proxy_subscription()

    assert cached is not None
    assert cached.nodes[0].node["name"] == "缓存节点"


def test_proxy_subscription_state_persists_auto_refresh_and_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    node = remote_proxy.parse_proxy_node("{ name: picked, type: vless, server: example.com, port: 443 }")

    remote_proxy.set_proxy_subscription_auto_refresh(True)
    remote_proxy.set_proxy_subscription_selected_node(node)

    state = remote_proxy.load_proxy_subscription_state()
    assert state["auto_refresh"] is True
    assert state["selected_node_display"] == "picked (vless://example.com:443)"
    assert state["selected_node_key"] == remote_proxy.proxy_node_key(node)


def test_describe_proxy_node_uses_normalized_endpoint():
    node = remote_proxy.parse_proxy_node("{ name: node, type: vless, server: example.com, port: '443' }")

    assert remote_proxy.describe_proxy_node(node) == "node (vless://example.com:443)"


def test_parse_proxy_node_rejects_missing_required_fields():
    with pytest.raises(ValueError, match="缺少字段"):
        remote_proxy.parse_proxy_node("{ name: bad, type: vless }")


def test_parse_proxy_node_rejects_invalid_port():
    with pytest.raises(ValueError, match="端口"):
        remote_proxy.parse_proxy_node("{ name: bad, type: vless, server: example.com, port: 70000 }")


def test_inspect_status_summary_mentions_partial_running_state():
    status = remote_proxy.RemoteAIProxyStatus(
        installed=True,
        running=True,
        config_path="/home/me/.config/mihomo/config.yaml",
        proxy_url="http://127.0.0.1:7890",
        detail="进程存在，但端口未监听",
    )

    assert "端口未监听" in status.summary()


def test_start_script_checks_port_with_netstat_when_ss_is_missing():
    script = remote_proxy._build_start_script("/home/me/.config/mihomo", "/home/me/.config/api-switcher", "/home/me/bin", 7890)

    assert "command -v netstat" in script


def test_proxy_env_entrypoints_cover_vscode_shells_and_terminals():
    env_file = remote_proxy._build_env_file(7890)
    shell_paths = remote_proxy._shell_proxy_profile_paths("/home/me")
    profile_block = remote_proxy._build_shell_profile_block(
        "/home/me/.config/api-switcher/ai-proxy.env",
        "/home/me/.config/api-switcher/start-ai-proxy.sh",
    )
    vscode_setup = remote_proxy._build_vscode_server_env_setup(
        "/home/me/.config/api-switcher/ai-proxy.env",
        "/home/me/.config/api-switcher/start-ai-proxy.sh",
    )
    fish_config = remote_proxy._build_fish_proxy_config(
        "/home/me/.config/api-switcher/start-ai-proxy.sh",
        7890,
    )

    assert "export HTTP_PROXY=http://127.0.0.1:7890" in env_file
    assert "/home/me/.bash_profile" in shell_paths
    assert "/home/me/.bash_login" in shell_paths
    assert ". /home/me/.config/api-switcher/ai-proxy.env" in profile_block
    assert vscode_setup.startswith("#!/bin/sh")
    assert "Loaded by VS Code Remote Server" in vscode_setup
    assert remote_proxy.VSCODE_ENV_BLOCK_START in vscode_setup
    assert "set -gx HTTP_PROXY http://127.0.0.1:7890" in fish_config


def test_vscode_server_env_setup_preserves_custom_content_and_replaces_managed_block():
    existing = """#!/bin/sh
export KEEP_ME=1

# >>> API切换器 AI proxy VS Code >>>
old managed content
# <<< API切换器 AI proxy VS Code <<<

export AFTER=2
"""

    merged = remote_proxy._merge_vscode_server_env_setup(
        existing,
        "/home/me/.config/api-switcher/ai-proxy.env",
        "/home/me/.config/api-switcher/start-ai-proxy.sh",
    )

    assert merged.startswith("#!/bin/sh\n")
    assert "export KEEP_ME=1" in merged
    assert "export AFTER=2" in merged
    assert "old managed content" not in merged
    assert merged.count(remote_proxy.VSCODE_ENV_BLOCK_START) == 1
    assert ". /home/me/.config/api-switcher/ai-proxy.env" in merged


def test_apply_vscode_proxy_settings_preserves_existing_terminal_env():
    settings = {
        "editor.fontSize": 14,
        "terminal.integrated.env.linux": {"EXISTING": "1"},
    }

    updated, changed = remote_proxy._apply_vscode_proxy_settings(settings, 7890)

    assert changed is True
    assert updated["editor.fontSize"] == 14
    assert updated["http.proxy"] == "http://127.0.0.1:7890"
    assert updated["http.proxySupport"] == "override"
    assert updated["terminal.integrated.env.linux"]["EXISTING"] == "1"
    assert updated["terminal.integrated.env.linux"]["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert updated["terminal.integrated.env.linux"]["NO_PROXY"] == "127.0.0.1,localhost,::1,*.local"
    assert settings["terminal.integrated.env.linux"] == {"EXISTING": "1"}


def test_parse_vscode_settings_for_proxy_skips_invalid_json():
    assert remote_proxy._parse_vscode_settings_for_proxy("{bad json") is None
    assert remote_proxy._parse_vscode_settings_for_proxy("") == {}


def test_local_vscode_proxy_settings_preserve_existing_windows_env():
    settings = {
        "http.proxy": "http://old.proxy:8080",
        "terminal.integrated.env.windows": {"EXISTING": "1", "HTTP_PROXY": "http://old.proxy:8080"},
    }

    previous = local_proxy._capture_vscode_proxy_state(settings)
    updated, changed = local_proxy._apply_local_vscode_proxy_settings(settings, 17897)
    restored, restored_changed = local_proxy._restore_vscode_proxy_settings(updated, previous, 17897)

    assert changed is True
    assert updated["http.proxy"] == "http://127.0.0.1:17897"
    assert updated["http.proxySupport"] == "override"
    assert updated["terminal.integrated.env.windows"]["HTTPS_PROXY"] == "http://127.0.0.1:17897"
    assert restored_changed is True
    assert restored["http.proxy"] == "http://old.proxy:8080"
    assert "http.proxySupport" not in restored
    assert restored["terminal.integrated.env.windows"]["HTTP_PROXY"] == "http://old.proxy:8080"
    assert restored["terminal.integrated.env.windows"]["EXISTING"] == "1"


def test_pick_mihomo_windows_asset_prefers_non_compatible_archive():
    assets = [
        {"name": "mihomo-windows-amd64-compatible.zip", "browser_download_url": "compatible"},
        {"name": "mihomo-windows-amd64.zip", "browser_download_url": "regular"},
        {"name": "mihomo-linux-amd64.gz", "browser_download_url": "linux"},
    ]

    picked = local_proxy._pick_mihomo_asset(assets, "windows-amd64")

    assert picked["browser_download_url"] == "regular"


def test_select_local_mixed_port_skips_busy_default(monkeypatch):
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: None)
    monkeypatch.setattr(local_proxy, "_is_pid_running", lambda _pid: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda port: port == 17897)

    assert local_proxy._select_local_mixed_port(17897) == 17898


def test_select_local_mixed_port_ignores_unmanaged_pid(monkeypatch):
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 12345)
    monkeypatch.setattr(local_proxy, "_is_pid_running", lambda _pid: True)
    monkeypatch.setattr(local_proxy, "_is_managed_mihomo_pid", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: False)

    assert local_proxy._select_local_mixed_port(17897) == 17897


def test_inspect_local_proxy_reports_setting_drift(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mixed-port: 17897", encoding="utf-8")

    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897, "config_path": str(config_path)})
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 12345)
    monkeypatch.setattr(local_proxy, "_is_pid_running", lambda _pid: True)
    monkeypatch.setattr(local_proxy, "_is_managed_mihomo_pid", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_local_env_matches", lambda _port: False)
    monkeypatch.setattr(local_proxy, "_windows_system_proxy_matches", lambda _port: False)
    monkeypatch.setattr(local_proxy, "_local_vscode_proxy_match_detail", lambda _port: "VS Code 本机设置未完全指向本机代理")

    status = local_proxy.inspect_local_ai_proxy()

    assert status.running is True
    assert "pid 文件指向非本工具代理进程" in status.detail
    assert "Windows 环境变量未完全指向本机代理" in status.detail
    assert "Windows 系统代理未指向本机代理" in status.detail
    assert "VS Code 本机设置未完全指向本机代理" in status.detail


def test_windows_system_proxy_expected_values_match_managed_proxy():
    values = local_proxy._windows_system_proxy_expected_values(17897)

    assert values["ProxyEnable"] == 1
    assert values["ProxyServer"] == "127.0.0.1:17897"
    assert local_proxy._windows_system_proxy_matches_values(values, 17897) is True
    assert local_proxy._windows_system_proxy_matches_values({**values, "ProxyServer": "127.0.0.1:18000"}, 17897) is False


def test_probe_local_ai_proxy_reports_each_target(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mixed-port: 17897", encoding="utf-8")
    probes = [
        local_proxy.LocalAIProxyProbeResult("OpenAI/ChatGPT", True, status=403, elapsed_ms=11),
        local_proxy.LocalAIProxyProbeResult("Claude/Anthropic", True, status=405, elapsed_ms=12),
        local_proxy.LocalAIProxyProbeResult("Gemini/Google AI", False, detail="timeout", elapsed_ms=13),
    ]

    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: local_proxy.LocalAIProxyStatus(
            installed=True,
            running=True,
            config_path=str(config_path),
            proxy_url="http://127.0.0.1:17897",
        ),
    )
    monkeypatch.setattr(local_proxy, "_probe_url_through_proxy", lambda *_args, **_kwargs: probes.pop(0))

    summary = local_proxy.probe_local_ai_proxy()

    assert "AI 连通性 2/3 可达" in summary
    assert "OpenAI/ChatGPT: 可达 / HTTP 403 / 11ms" in summary
    assert "Gemini/Google AI: 失败 / timeout / 13ms" in summary


def test_read_url_with_retries_retries_transient_failure(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("temporary")
        return Response()

    monkeypatch.setattr(local_proxy.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(local_proxy.time, "sleep", lambda _seconds: None)

    payload = local_proxy._read_url_with_retries(
        local_proxy.urllib.request.Request("https://example.com/file"),
        timeout=1,
        label="下载测试",
        retries=2,
    )

    assert payload == b"ok"
    assert len(calls) == 2


def test_install_local_proxy_failure_reports_restore_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", tmp_path / "mihomo")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", tmp_path / "mihomo.pid")
    monkeypatch.setattr(local_proxy, "_select_local_mixed_port", lambda _port: 17897)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: tmp_path / "mihomo.exe")
    monkeypatch.setattr(local_proxy, "_capture_previous_env", lambda: {})
    monkeypatch.setattr(local_proxy, "_capture_vscode_proxy_state", lambda _settings: {})
    monkeypatch.setattr(local_proxy, "_capture_windows_system_proxy_state", lambda: {})
    monkeypatch.setattr(local_proxy.vscode_parser, "read_vscode_settings", lambda: {})
    monkeypatch.setattr(local_proxy, "_start_local_mihomo", lambda *_args: (_ for _ in ()).throw(RuntimeError("start failed")))
    monkeypatch.setattr(local_proxy, "_restore_local_env", lambda *_args: (_ for _ in ()).throw(RuntimeError("env restore failed")))
    monkeypatch.setattr(local_proxy, "_restore_local_vscode_proxy", lambda *_args: None)
    monkeypatch.setattr(local_proxy, "_restore_windows_system_proxy", lambda *_args: None)
    monkeypatch.setattr(local_proxy, "_cleanup_managed_process", lambda *_args: None)

    with pytest.raises(RuntimeError) as excinfo:
        local_proxy.install_local_ai_proxy("{ name: node, type: vless, server: example.com, port: 443 }")

    message = str(excinfo.value)
    assert "start failed" in message
    assert "env restore failed" in message


def test_stop_local_proxy_does_not_terminate_unmanaged_pid(monkeypatch, tmp_path):
    pid_path = tmp_path / "mihomo.pid"
    pid_path.write_text("12345", encoding="utf-8")
    terminated = []
    saved_states = []

    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", pid_path)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 12345 if pid_path.exists() else None)
    monkeypatch.setattr(local_proxy, "_is_pid_running", lambda pid: pid == 12345)
    monkeypatch.setattr(local_proxy, "_is_managed_mihomo_pid", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(local_proxy, "_terminate_pid", lambda pid: terminated.append(pid))
    monkeypatch.setattr(local_proxy, "_restore_local_env", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(local_proxy, "_restore_local_vscode_proxy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(local_proxy, "_save_state", lambda state: saved_states.append(state))

    message = local_proxy.stop_local_ai_proxy()

    assert "不是本工具启动" in message
    assert terminated == []
    assert not pid_path.exists()
    assert saved_states[-1] == {}
