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


def test_parse_proxy_node_rejects_missing_required_fields():
    with pytest.raises(ValueError, match="缺少字段"):
        remote_proxy.parse_proxy_node("{ name: bad, type: vless }")
