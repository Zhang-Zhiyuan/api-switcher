from __future__ import annotations

import base64
import json

import pytest

from core import remote_proxy


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
