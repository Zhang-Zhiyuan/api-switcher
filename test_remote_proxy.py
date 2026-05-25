from __future__ import annotations

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
