from __future__ import annotations

import base64
import gzip
import io
import json
import os
from pathlib import Path
import sys
import threading

import pytest

from core import local_proxy, network_diagnostic_settings, network_diagnostics, remote_proxy


class _ConnectedProbeSocket:
    def close(self):
        return None


def _stable_local_prevalidation(node) -> local_proxy.LocalProxyNodeStabilityResult:
    node_dict = node.node if isinstance(node, remote_proxy.ProxySubscriptionNode) else node
    return local_proxy.LocalProxyNodeStabilityResult(
        node_key=remote_proxy.proxy_node_key(node_dict),
        stable=True,
        short_stable=True,
        deep_transport_ok=True,
        deep_transport_successes=local_proxy.LOCAL_PROXY_DEEP_PROBE_ROUNDS,
        deep_transport_attempts=local_proxy.LOCAL_PROXY_DEEP_PROBE_ROUNDS,
        codex_compact_ok=True,
    )


def _remote_proxy_status(*, running: bool) -> remote_proxy.RemoteAIProxyStatus:
    return remote_proxy.RemoteAIProxyStatus(
        installed=running,
        running=running,
        config_path="/home/me/.config/mihomo/config.yaml",
        proxy_url="http://127.0.0.1:7890",
    )


def _remote_stability_summary(prefix: str = "server: 隔离候选") -> str:
    expected = remote_proxy.REMOTE_AI_STABILITY_EXPECTED_PROBES
    return f"{prefix} AI 稳定性 {expected}/{expected} 可达"


def _remote_stability_output(*, compact_ok: bool = True, compact_detail: str = "ok") -> str:
    lines = [
        f"probe\t{label}\t1\t第{round_index}/3轮 ok\t10"
        for round_index in range(1, 4)
        for label, _url in remote_proxy.REMOTE_AI_STABILITY_TARGETS
    ]
    lines.append(
        f"probe\t{remote_proxy.REMOTE_CODEX_COMPACT_PROBE_LABEL}\t"
        f"{1 if compact_ok else 0}\t{compact_detail}\t10"
    )
    return "\n".join(lines)


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


def test_parse_proxy_node_accepts_single_proxy_uri():
    node = remote_proxy.parse_proxy_node(
        "vless://token@example.com:443?encryption=none&security=tls&type=ws&path=%2Fchat#URI%20Node"
    )

    assert node["name"] == "URI Node"
    assert node["type"] == "vless"
    assert node["server"] == "example.com"
    assert node["ws-opts"]["path"] == "/chat"


@pytest.mark.parametrize("node_type", ["DIRECT", "pass", "reject", "reject-drop", "dns"])
def test_parse_proxy_node_rejects_routing_primitive_as_transport(node_type):
    with pytest.raises(ValueError, match="路由内置类型"):
        remote_proxy.parse_proxy_node(
            f"{{ name: fake, type: {node_type}, server: example.com, port: 443 }}"
        )


def test_ping0_detail_url_for_proxy_node_supports_ip_domain_and_ipv6():
    assert remote_proxy.ping0_detail_url_for_proxy_node({
        "name": "ipv4",
        "type": "vless",
        "server": "8.8.8.8",
        "port": 443,
    }) == "https://ping0.cc/ip/8.8.8.8"
    assert remote_proxy.ping0_detail_url_for_proxy_node({
        "name": "domain",
        "type": "vless",
        "server": "node.example.com",
        "port": 443,
    }) == "https://ping0.cc/ip/node.example.com"
    assert remote_proxy.ping0_detail_url_for_proxy_node({
        "name": "ipv6",
        "type": "vless",
        "server": "[2001:4860:4860::8888]",
        "port": 443,
    }) == "https://ping0.cc/ip/2001:4860:4860::8888"


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

    assert config.startswith(remote_proxy.AI_PROXY_CONFIG_MARKER)
    assert f'name: "{remote_proxy.AI_PROXY_INTERNAL_NODE_NAME}"' in config
    assert remote_proxy._managed_proxy_display_name(config) == "node-a"
    assert 'external-controller: "127.0.0.1:8890"' in config
    assert 'server: "example.com"' in config
    assert 'DOMAIN-SUFFIX,chatgpt.com,AI-PROXY' in config
    assert 'DOMAIN-SUFFIX,anthropic.com,AI-PROXY' in config
    assert 'DOMAIN-SUFFIX,generativelanguage.googleapis.com,AI-PROXY' in config
    assert 'DOMAIN-SUFFIX,oauth2.googleapis.com,AI-PROXY' in config
    assert 'MATCH,DIRECT' in config


def test_build_mihomo_config_supports_extra_targets_and_non_cn_mode():
    config = remote_proxy.build_mihomo_config(
        {"name": "node-a", "type": "vless", "server": "example.com", "port": 443},
        17897,
        extra_proxy_domains=("youtube.com", "github.com"),
        extra_proxy_ip_cidrs=("8.8.8.8/32", "2001:4860:4860::8888/128"),
        proxy_non_cn=True,
    )

    assert "DOMAIN-SUFFIX,youtube.com,AI-PROXY" in config
    assert "DOMAIN-SUFFIX,github.com,AI-PROXY" in config
    assert "IP-CIDR,8.8.8.8/32,AI-PROXY,no-resolve" in config
    assert "IP-CIDR6,2001:4860:4860::8888/128,AI-PROXY,no-resolve" in config
    assert "GEOIP,CN,DIRECT" in config
    assert "MATCH,AI-PROXY" in config
    assert "MATCH,DIRECT" not in config


def test_build_mihomo_config_strict_privacy_is_fail_closed_and_uses_encrypted_dns():
    config = remote_proxy.build_mihomo_config(
        {"name": "node-a", "type": "vless", "server": "example.com", "port": 443},
        17897,
        proxy_non_cn=True,
        strict_privacy=True,
    )
    parsed = remote_proxy.yaml.safe_load(config.split("\n", 1)[1])

    assert remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER in config
    assert remote_proxy._managed_config_strict_privacy_enabled(config) is True
    assert "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve" in parsed["rules"]
    assert "IP-CIDR6,::1/128,DIRECT,no-resolve" in parsed["rules"]
    assert "GEOIP,CN,DIRECT" not in parsed["rules"]
    assert "MATCH,DIRECT" not in parsed["rules"]
    assert parsed["rules"][-1] == "MATCH,AI-PROXY"
    assert parsed["proxies"][0]["name"] == remote_proxy.AI_PROXY_INTERNAL_NODE_NAME
    assert parsed["proxy-groups"][0]["proxies"] == [
        remote_proxy.AI_PROXY_INTERNAL_NODE_NAME
    ]
    assert parsed["ipv6"] is False
    assert parsed["dns"] == {
        "enable": True,
        "ipv6": False,
        "enhanced-mode": "redir-host",
        "use-hosts": True,
        "use-system-hosts": False,
        "respect-rules": True,
        "default-nameserver": ["223.5.5.5", "119.29.29.29"],
        "proxy-server-nameserver": [
            "https://doh.pub/dns-query#DIRECT",
            "https://dns.alidns.com/dns-query#DIRECT",
        ],
        "nameserver": [
            "https://1.1.1.1/dns-query#AI-PROXY",
            "https://8.8.8.8/dns-query#AI-PROXY",
        ],
    }


def test_build_mihomo_config_default_keeps_compatible_split_routing():
    config = remote_proxy.build_mihomo_config(
        {"name": "node-a", "type": "vless", "server": "example.com", "port": 443}
    )

    assert "MATCH,DIRECT" in config
    assert "ipv6: true" in config
    assert "dns:" not in config
    assert remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER not in config
    assert remote_proxy._managed_config_strict_privacy_enabled(config) is False


@pytest.mark.parametrize("display_name", ["DIRECT", "REJECT", "PASS", "AI-PROXY"])
def test_build_mihomo_config_never_uses_reserved_display_name_as_outbound(display_name):
    config = remote_proxy.build_mihomo_config(
        {"name": display_name, "type": "vless", "server": "example.com", "port": 443},
        strict_privacy=True,
    )
    parsed = remote_proxy.yaml.safe_load(config)

    assert parsed["proxies"][0]["name"] == remote_proxy.AI_PROXY_INTERNAL_NODE_NAME
    assert parsed["proxy-groups"][0]["proxies"] == [
        remote_proxy.AI_PROXY_INTERNAL_NODE_NAME
    ]
    assert remote_proxy._managed_proxy_display_name(config) == display_name
    assert remote_proxy._managed_config_strict_privacy_enabled(config) is True


@pytest.mark.parametrize("reserved_name", ["DIRECT", "REJECT", "PASS", "AI-PROXY"])
def test_strict_validator_rejects_legacy_reserved_outbound_name(reserved_name):
    config = remote_proxy.build_mihomo_config(
        {"name": "safe", "type": "vless", "server": "example.com", "port": 443},
        strict_privacy=True,
    )
    parsed = remote_proxy.yaml.safe_load(config)
    parsed["proxies"][0]["name"] = reserved_name
    parsed["proxy-groups"][0]["proxies"] = [reserved_name]
    drifted = (
        remote_proxy.AI_PROXY_CONFIG_MARKER
        + "\n"
        + remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER
        + "\n"
        + remote_proxy._dump_yaml(parsed)
    )

    assert remote_proxy._managed_config_strict_privacy_enabled(drifted) is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda parsed: parsed["rules"].append("MATCH,DIRECT"),
        lambda parsed: parsed.__setitem__("ipv6", True),
        lambda parsed: parsed["dns"].__setitem__("enable", False),
        lambda parsed: parsed["dns"].__setitem__("ipv6", True),
        lambda parsed: parsed["dns"].__setitem__("respect-rules", False),
        lambda parsed: parsed["dns"].__setitem__("use-system-hosts", True),
        lambda parsed: parsed["dns"].__setitem__("nameserver", ["1.1.1.1"]),
        lambda parsed: parsed["dns"].__setitem__("proxy-server-nameserver", []),
        lambda parsed: parsed["dns"].__setitem__("nameserver", ["https://"]),
        lambda parsed: parsed["dns"].__setitem__(
            "nameserver", ["https://user:pass@resolver.example/dns-query"]
        ),
        lambda parsed: parsed["dns"].__setitem__(
            "nameserver", ["https://resolver.example/dns-query"]
        ),
        lambda parsed: parsed["dns"].__setitem__(
            "nameserver", ["https://1.1.1.1@evil.example/dns-query"]
        ),
        lambda parsed: parsed["dns"].__setitem__(
            "nameserver", ["https://1.1.1.1/dns-query#DIRECT"]
        ),
        lambda parsed: parsed["dns"].__setitem__(
            "proxy-server-nameserver", ["https://resolver.example/dns-query"]
        ),
        lambda parsed: parsed["dns"].__setitem__(
            "nameserver-policy", {"+.openai.com": "8.8.8.8"}
        ),
        lambda parsed: parsed["dns"].__setitem__("fallback", ["8.8.8.8"]),
        lambda parsed: parsed["dns"].__setitem__("listen", "0.0.0.0:1053"),
        lambda parsed: parsed["rules"].insert(-1, "DOMAIN,example.com,DIRECT"),
        lambda parsed: parsed.__setitem__("allow-lan", True),
        lambda parsed: parsed.__setitem__("bind-address", "0.0.0.0"),
        lambda parsed: parsed.__setitem__("mode", "global"),
        lambda parsed: parsed.__setitem__("external-controller", "0.0.0.0:8890"),
        lambda parsed: parsed.__setitem__("external-controller", "localhost:8890"),
        lambda parsed: parsed["dns"].__setitem__("enhanced-mode", "fake-ip"),
        lambda parsed: parsed["dns"].__setitem__("default-nameserver", ["dns.example.com"]),
        lambda parsed: parsed["dns"].__setitem__("default-nameserver", ["127.0.0.1"]),
        lambda parsed: parsed["proxy-groups"][0].__setitem__("proxies", ["DIRECT"]),
        lambda parsed: parsed["proxy-groups"][0].__setitem__("proxies", ["REJECT"]),
        lambda parsed: parsed["proxy-groups"][0].__setitem__("proxies", ["other-group"]),
        lambda parsed: parsed["proxy-groups"][0].__setitem__("proxies", []),
        lambda parsed: parsed["proxy-groups"][0].__setitem__("type", "fallback"),
        lambda parsed: parsed["proxy-groups"][0].__setitem__("type", "SELECT"),
        lambda parsed: parsed["proxy-groups"].append(
            {"name": "AI-PROXY", "type": "select", "proxies": ["node-a"]}
        ),
        lambda parsed: parsed.__setitem__("proxies", []),
        lambda parsed: parsed["proxies"].append(dict(parsed["proxies"][0])),
        lambda parsed: parsed["proxies"][0].__setitem__("type", "direct"),
        lambda parsed: parsed["proxies"].append(
            {"name": "node-b", "type": "vless", "server": "b.example.com", "port": 443}
        ),
        lambda parsed: (
            parsed["proxy-groups"].append(
                {"name": "BYPASS", "type": "select", "proxies": ["DIRECT"]}
            ),
            parsed["rules"].insert(-1, "DOMAIN,example.com,BYPASS"),
        ),
        lambda parsed: parsed["rules"].insert(-1, "DOMAIN,example.com,REJECT"),
        lambda parsed: parsed.__setitem__(
            "rules",
            [
                rule
                for rule in parsed["rules"]
                if rule not in remote_proxy.PRIVATE_DIRECT_IP_RULES
            ],
        ),
    ],
)
def test_managed_strict_privacy_contract_fails_closed_on_drift(mutation):
    config = remote_proxy.build_mihomo_config(
        {"name": "node-a", "type": "vless", "server": "example.com", "port": 443},
        strict_privacy=True,
    )
    parsed = remote_proxy.yaml.safe_load(config)
    mutation(parsed)
    drifted = (
        remote_proxy.AI_PROXY_CONFIG_MARKER
        + "\n"
        + remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER
        + "\n"
        + remote_proxy._dump_yaml(parsed)
    )

    assert remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER in drifted
    assert remote_proxy._managed_config_strict_privacy_enabled(drifted) is False


def test_managed_strict_privacy_contract_rejects_marker_with_invalid_yaml():
    invalid = (
        remote_proxy.AI_PROXY_CONFIG_MARKER
        + "\n"
        + remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER
        + "\nrules: ["
    )

    assert remote_proxy._managed_config_strict_privacy_enabled(invalid) is False


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


def test_proxy_node_region_and_latency_sorting():
    nodes = remote_proxy.parse_proxy_subscription_content(
        """
proxies:
  - { name: 台湾 2, type: vless, server: tw2.example.com, port: 443 }
  - { name: 日本, type: vless, server: jp.example.com, port: 443 }
  - { name: 台湾 1, type: vless, server: tw1.example.com, port: 443 }
  - { name: cf加速|越南动态家宽🇻🇳, type: vless, server: vn.example.com, port: 443 }
"""
    )
    latencies = {
        remote_proxy.proxy_node_key(nodes[0].node): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_node_key(nodes[0].node),
            True,
            latency_ms=80,
        ),
        remote_proxy.proxy_node_key(nodes[2].node): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_node_key(nodes[2].node),
            True,
            latency_ms=20,
        ),
    }

    sorted_nodes = remote_proxy.sort_proxy_subscription_nodes(nodes, latencies)

    assert remote_proxy.proxy_node_region(nodes[0].node) == "台湾"
    assert remote_proxy.proxy_node_region(nodes[3].node) == "越南"
    assert [item.node["name"] for item in sorted_nodes if remote_proxy.proxy_node_region(item.node) == "台湾"] == [
        "台湾 1",
        "台湾 2",
    ]


@pytest.mark.parametrize(
    ("name", "server"),
    [
        ("香港家宽", "node.example.com"),
        ("HK home", "node.example.com"),
        ("Hong Kong home", "node.example.com"),
        ("家宽 🇭🇰", "node.example.com"),
        ("HK01 home", "node.example.com"),
        ("HKG 01 home", "node.example.com"),
        ("Hong-Kong home", "node.example.com"),
        ("Hong_Kong home", "node.example.com"),
        ("H.K. home", "node.example.com"),
        ("neutral", "node.hk"),
    ],
)
def test_hong_kong_nodes_are_manual_only_for_automatic_selection(name, server):
    item = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            f"{{ name: '{name}', type: vless, server: {server}, port: 443 }}"
        ),
    )

    assert remote_proxy.proxy_subscription_node_is_hong_kong(item) is True
    assert remote_proxy.proxy_subscription_node_auto_selectable(item) is False


def test_automatic_selection_policy_keeps_hong_kong_visible_but_never_picks_it():
    nodes = remote_proxy.parse_proxy_subscription_content(
        """
proxies:
  - { name: 香港 最快家宽, type: vless, server: hk.example.com, port: 443 }
  - { name: 美国 稳定家宽, type: vless, server: us.example.com, port: 443 }
"""
    )
    hong_kong, safe = nodes
    hong_kong_key = remote_proxy.proxy_subscription_node_key(hong_kong)
    safe_key = remote_proxy.proxy_subscription_node_key(safe)
    latencies = {
        hong_kong_key: remote_proxy.ProxyNodeLatencyResult(hong_kong_key, True, latency_ms=5),
        safe_key: remote_proxy.ProxyNodeLatencyResult(safe_key, True, latency_ms=60),
    }
    qualities = {
        hong_kong_key: remote_proxy.ProxyNodeQualityResult(
            hong_kong_key,
            True,
            ip_type="家庭宽带/住宅 IP",
            risk_score=1,
            quality_score=100,
            quality_label="家宽高质",
            confidence="高",
            coverage_complete=True,
            classification_basis="信誉源网络/风险字段",
        ),
        safe_key: remote_proxy.ProxyNodeQualityResult(
            safe_key,
            True,
            ip_type="家庭宽带/住宅 IP",
            risk_score=10,
            quality_score=90,
            quality_label="家宽高质",
            confidence="高",
            coverage_complete=True,
            classification_basis="信誉源网络/风险字段",
        ),
    }

    assert hong_kong in remote_proxy.sort_proxy_subscription_nodes(nodes, latencies)
    assert remote_proxy.automatic_proxy_subscription_nodes(nodes) == (safe,)
    assert remote_proxy.best_proxy_subscription_node_by_latency(nodes, latencies) is safe
    assert remote_proxy.best_proxy_subscription_node_by_latency([hong_kong], latencies) is None
    assert remote_proxy.best_proxy_subscription_node_for_ai_proxy(nodes, qualities, latencies) is safe
    assert remote_proxy.best_proxy_subscription_node_for_ai_proxy([hong_kong], qualities, latencies) is None

    chosen, reason = remote_proxy.best_proxy_subscription_node_for_hot_update(
        nodes,
        None,
        latencies,
    )
    assert chosen is safe
    assert reason == "latency"
    assert remote_proxy.ranked_proxy_subscription_nodes_for_ai_probe(
        nodes,
        qualities,
        latencies,
    ) == (safe,)

    chosen, reason = remote_proxy.best_proxy_subscription_node_for_hot_update(
        [hong_kong],
        None,
        latencies,
    )
    assert chosen is None
    assert reason == "policy_excluded"


def test_automatic_selection_rejects_quality_provider_hong_kong_region():
    neutral = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: neutral, type: vless, server: edge.example.com, port: 443 }"
        ),
    )
    key = remote_proxy.proxy_subscription_node_key(neutral)
    quality = remote_proxy.ProxyNodeQualityResult(
        key,
        True,
        region="Hong Kong",
        ip_type="家庭宽带/住宅 IP",
        risk_score=1,
        quality_score=100,
        quality_label="家宽高质",
        confidence="高",
        coverage_complete=True,
        classification_basis="信誉源网络/风险字段",
    )
    latency = remote_proxy.ProxyNodeLatencyResult(key, True, latency_ms=5)

    assert remote_proxy.proxy_subscription_node_auto_selectable(neutral, quality) is False
    assert remote_proxy.automatic_proxy_subscription_nodes([neutral], {key: quality}) == ()
    assert remote_proxy.best_proxy_subscription_node_by_latency(
        [neutral],
        {key: latency},
        {key: quality},
    ) is None
    assert remote_proxy.best_proxy_subscription_node_for_ai_proxy(
        [neutral],
        {key: quality},
        {key: latency},
    ) is None


def test_name_only_subscription_match_cannot_migrate_current_node_to_hong_kong():
    current = remote_proxy.parse_proxy_node(
        "{ name: shared, type: vless, server: old-us.example.com, port: 443 }"
    )
    hong_kong = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: shared, type: vless, server: hk.example.com, port: 443 }"
        ),
    )

    assert remote_proxy._find_matching_subscription_node([hong_kong], current) is None
    assert remote_proxy._find_matching_subscription_node([hong_kong], hong_kong.node) is hong_kong

    disguised = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node(
            "{ name: shared, type: vless, server: edge.example.com, port: 443 }"
        ),
    )
    disguised_key = remote_proxy.proxy_subscription_node_key(disguised)
    quality = remote_proxy.ProxyNodeQualityResult(disguised_key, True, region="HK")
    assert remote_proxy._find_matching_subscription_node(
        [disguised],
        current,
        {disguised_key: quality},
    ) is None


def test_proxy_node_sorting_keeps_failed_nodes_after_unmeasured_within_region():
    nodes = remote_proxy.parse_proxy_subscription_content(
        """
proxies:
  - { name: 香港 failed, type: vless, server: hk-failed.example.com, port: 443 }
  - { name: 香港 ok, type: vless, server: hk-ok.example.com, port: 443 }
  - { name: 香港 unmeasured, type: vless, server: hk-new.example.com, port: 443 }
"""
    )
    latencies = {
        remote_proxy.proxy_node_key(nodes[0].node): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_node_key(nodes[0].node),
            False,
            detail="timed out",
        ),
        remote_proxy.proxy_node_key(nodes[1].node): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_node_key(nodes[1].node),
            True,
            latency_ms=20,
        ),
    }

    sorted_nodes = remote_proxy.sort_proxy_subscription_nodes(nodes, latencies)

    assert [item.node["name"] for item in sorted_nodes] == [
        "香港 ok",
        "香港 unmeasured",
        "香港 failed",
    ]


def test_assess_proxy_node_quality_classifies_proxycheck_residential():
    node = remote_proxy.parse_proxy_node(
        "{ name: AI代理家宽, type: vless, server: node.example.com, port: 443 }"
    )

    def resolver(host, *_args, **_kwargs):
        assert host == "node.example.com"
        return [(None, None, None, "", ("8.8.4.77", 0))]

    def http_get(url, _timeout):
        if "proxycheck.io/v3/8.8.4.77" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        "8.8.4.77": {
                            "network": {
                                "type": "Residential",
                                "provider": "Example Fiber",
                                "asn": "64500",
                            },
                            "detections": {
                                "proxy": False,
                                "vpn": False,
                                "tor": False,
                                "relay": False,
                                "hosting": False,
                                "risk": 8,
                            },
                        },
                    }
                ),
            )
        if "ipwho.is/8.8.4.77" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "success": True,
                        "country": "United States",
                        "city": "San Jose",
                        "connection": {
                            "asn": 64500,
                            "org": "Example Fiber",
                            "isp": "Example Fiber Broadband",
                        },
                    }
                ),
            )
        raise AssertionError(f"unexpected URL: {url}")

    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_PROXYCHECK},
        {},
    )

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=resolver,
        settings=settings,
    )

    assert result.ok is True
    assert result.ip == "8.8.4.77"
    assert result.quality_label == "家宽高质"
    assert result.quality_score >= 90
    assert result.risk_score == 8
    assert result.confidence == "中"
    assert result.sources == (network_diagnostic_settings.SERVICE_PROXYCHECK,)
    assert result.attempted_sources == (network_diagnostic_settings.SERVICE_PROXYCHECK,)
    assert result.coverage_complete is True
    assert result.assessment_scope == remote_proxy.PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER
    assert remote_proxy.proxy_node_quality_source_label(result) == "ProxyCheck"
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(result) is True


def test_assess_quality_blocks_neutral_node_when_provider_reports_hong_kong():
    node = remote_proxy.parse_proxy_node(
        "{ name: neutral-edge, type: vless, server: edge.example.com, port: 443 }"
    )
    node_key = remote_proxy.proxy_node_key(node)
    seen_urls = []

    def http_get(url, _timeout):
        seen_urls.append(url)
        if "proxycheck.io" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        "8.8.4.101": {
                            "network": {"type": "Residential", "provider": "Example Fiber"},
                            "detections": {"proxy": False, "vpn": False, "risk": 5},
                            "location": {
                                "country_code": "HK",
                                "country_name": "Hong Kong",
                                "region_name": "Hong Kong",
                                "city_name": "Hong Kong",
                            },
                        },
                    }
                ),
            )
        raise AssertionError(f"unexpected URL: {url}")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("8.8.4.101", 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_PROXYCHECK},
            {},
        ),
    )
    item = remote_proxy.ProxySubscriptionNode(1, node)

    assert result.ok is True
    assert result.region == "香港"
    assert not any("ipwho.is" in url for url in seen_urls)
    assert remote_proxy.proxy_subscription_node_auto_selectable(item, result) is False
    assert remote_proxy.automatic_proxy_subscription_nodes([item], {node_key: result}) == ()


def test_assess_quality_uses_geo_to_block_disguised_hong_kong_when_provider_has_no_region():
    node = remote_proxy.parse_proxy_node(
        "{ name: neutral-edge, type: vless, server: edge.example.com, port: 443 }"
    )
    seen_urls = []

    def http_get(url, _timeout):
        seen_urls.append(url)
        if "proxycheck.io" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        "8.8.4.102": {
                            "network": {"type": "Residential", "provider": "Example Fiber"},
                            "detections": {"proxy": False, "vpn": False, "risk": 5},
                        },
                    }
                ),
            )
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "success": True,
                        "country_code": "HK",
                        "country": "Hong Kong",
                        "region": "Hong Kong",
                        "city": "Hong Kong",
                        "connection": {"org": "Example Fiber", "isp": "Example Fiber"},
                    }
                ),
            )
        raise AssertionError(f"unexpected URL: {url}")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("8.8.4.102", 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_PROXYCHECK},
            {},
        ),
    )

    assert result.ok is True
    assert result.region == "香港"
    assert any("ipwho.is" in url for url in seen_urls)
    assert remote_proxy.proxy_subscription_node_auto_selectable(
        remote_proxy.ProxySubscriptionNode(1, node),
        result,
    ) is False


def test_quality_signature_invalidates_cache_when_model_semantics_change(monkeypatch):
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_PROXYCHECK},
        {},
    )
    services = [network_diagnostic_settings.SERVICE_PROXYCHECK]
    original = remote_proxy.proxy_quality_settings_signature(settings, services)

    monkeypatch.setattr(
        remote_proxy,
        "PROXY_QUALITY_CACHE_SCHEMA_VERSION",
        remote_proxy.PROXY_QUALITY_CACHE_SCHEMA_VERSION + 1,
    )

    assert remote_proxy.proxy_quality_settings_signature(settings, services) != original


def test_assess_proxy_node_quality_reuses_fresh_matching_cache(monkeypatch, tmp_path):
    node = remote_proxy.parse_proxy_node(
        "{ name: AI代理缓存, type: vless, server: cached.example.com, port: 443 }"
    )
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_NETCOFFEE},
        {},
    )
    signature = remote_proxy.proxy_quality_settings_signature(
        settings,
        [network_diagnostic_settings.SERVICE_NETCOFFEE],
    )
    node_key = remote_proxy.proxy_node_key(node)

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    remote_proxy.clear_proxy_subscription_state_cache()
    remote_proxy.save_proxy_subscription_qualities({
        node_key: remote_proxy.ProxyNodeQualityResult(
            node_key=node_key,
            ok=True,
            host="cached.example.com",
            ip="8.8.4.80",
            region="其他",
            ip_type="家庭宽带/住宅 IP",
            risk_score=6,
            risk_label="极低",
            quality_score=100,
            quality_label="家宽高质",
            detail="Net.Coffee trust_score=94",
            checked_at=remote_proxy._now_iso(),
            sources=(network_diagnostic_settings.SERVICE_NETCOFFEE,),
            attempted_sources=(network_diagnostic_settings.SERVICE_NETCOFFEE,),
            coverage_complete=True,
            confidence="高",
            classification_basis="信誉源网络/风险字段",
            quality_signature=signature,
        )
    })

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network should be skipped")),
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("8.8.4.80", 0))],
        settings=settings,
        enabled_services=[network_diagnostic_settings.SERVICE_NETCOFFEE],
    )

    assert result.ok is True
    assert result.cached is True
    assert result.quality_score == 100
    assert result.quality_signature == signature
    assert "缓存命中" in result.detail


def test_assess_proxy_node_quality_reuses_cache_by_same_ip_when_node_key_changes(monkeypatch, tmp_path):
    old_node = remote_proxy.parse_proxy_node(
        "{ name: AI代理缓存旧名, type: vless, server: old.example.com, port: 443 }"
    )
    new_node = remote_proxy.parse_proxy_node(
        "{ name: AI代理缓存新名, type: vless, server: new.example.com, port: 443 }"
    )
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_NETCOFFEE},
        {},
    )
    signature = remote_proxy.proxy_quality_settings_signature(
        settings,
        [network_diagnostic_settings.SERVICE_NETCOFFEE],
    )
    old_key = remote_proxy.proxy_node_key(old_node)
    new_key = remote_proxy.proxy_node_key(new_node)

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    remote_proxy.clear_proxy_subscription_state_cache()
    remote_proxy.save_proxy_subscription_qualities({
        old_key: remote_proxy.ProxyNodeQualityResult(
            node_key=old_key,
            ok=True,
            host="old.example.com",
            ip="8.8.4.83",
            region="其他",
            ip_type="家庭宽带/住宅 IP",
            risk_score=5,
            risk_label="极低",
            quality_score=100,
            quality_label="家宽高质",
            detail="Net.Coffee trust_score=95",
            checked_at=remote_proxy._now_iso(),
            sources=(network_diagnostic_settings.SERVICE_NETCOFFEE,),
            attempted_sources=(network_diagnostic_settings.SERVICE_NETCOFFEE,),
            coverage_complete=True,
            confidence="高",
            classification_basis="信誉源网络/风险字段",
            quality_signature=signature,
        )
    })

    result = remote_proxy.assess_proxy_node_quality(
        new_node,
        http_get=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network should be skipped")),
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("8.8.4.83", 0))],
        settings=settings,
        enabled_services=[network_diagnostic_settings.SERVICE_NETCOFFEE],
    )

    assert result.ok is True
    assert result.cached is True
    assert result.node_key == new_key
    assert result.host == "new.example.com"
    assert result.ip == "8.8.4.83"
    assert result.quality_score == 100


def test_quality_cache_and_batch_rebind_preserve_detected_hong_kong_region():
    old_node = remote_proxy.parse_proxy_node(
        "{ name: old-neutral, type: vless, server: old.example.com, port: 443 }"
    )
    new_node = remote_proxy.parse_proxy_node(
        "{ name: new-neutral, type: vless, server: new.example.com, port: 443 }"
    )
    old_key = remote_proxy.proxy_node_key(old_node)
    detected = remote_proxy.ProxyNodeQualityResult(
        node_key=old_key,
        ok=True,
        host="old.example.com",
        ip="8.8.4.103",
        region="香港",
        checked_at=remote_proxy._now_iso(),
        sources=(network_diagnostic_settings.SERVICE_NETCOFFEE,),
        attempted_sources=(network_diagnostic_settings.SERVICE_NETCOFFEE,),
        coverage_complete=True,
        classification_basis="信誉源网络/风险字段",
        quality_signature="region-policy-v1",
    )

    cached = remote_proxy._cached_proxy_node_quality_result(
        remote_proxy.proxy_node_key(new_node),
        "new.example.com",
        "其他",
        "8.8.4.103",
        "region-policy-v1",
        {old_key: detected},
        3600,
    )
    shared = remote_proxy._proxy_node_quality_result_for_node(detected, new_node)
    item = remote_proxy.ProxySubscriptionNode(1, new_node)

    assert cached is not None
    assert cached.region == "香港"
    assert shared.region == "香港"
    assert remote_proxy.proxy_subscription_node_auto_selectable(item, cached) is False
    assert remote_proxy.proxy_subscription_node_auto_selectable(item, shared) is False


def test_assess_proxy_node_quality_bypasses_cache_when_ip_changes(monkeypatch, tmp_path):
    node = remote_proxy.parse_proxy_node(
        "{ name: AI代理缓存IP变更, type: vless, server: changed.example.com, port: 443 }"
    )
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_NETCOFFEE},
        {},
    )
    signature = remote_proxy.proxy_quality_settings_signature(
        settings,
        [network_diagnostic_settings.SERVICE_NETCOFFEE],
    )
    node_key = remote_proxy.proxy_node_key(node)

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    remote_proxy.clear_proxy_subscription_state_cache()
    remote_proxy.save_proxy_subscription_qualities({
        node_key: remote_proxy.ProxyNodeQualityResult(
            node_key=node_key,
            ok=True,
            host="changed.example.com",
            ip="8.8.4.81",
            ip_type="家庭宽带/住宅 IP",
            quality_score=100,
            quality_label="家宽高质",
            checked_at=remote_proxy._now_iso(),
            sources=(network_diagnostic_settings.SERVICE_NETCOFFEE,),
            attempted_sources=(network_diagnostic_settings.SERVICE_NETCOFFEE,),
            coverage_complete=True,
            confidence="高",
            classification_basis="信誉源网络/风险字段",
            quality_signature=signature,
        )
    })

    new_ip = "8.8.4.82"
    seen_urls = []

    def http_get(url, _timeout):
        seen_urls.append(url)
        if "ip.net.coffee/api/ip/lookup" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({
                    "ip": new_ip,
                    "is_datacenter": False,
                    "isResidential": True,
                    "is_vpn": False,
                    "is_proxy": False,
                    "is_tor": False,
                    "company_type": "isp",
                    "trust_score": 95,
                }),
            )
        if "ip.net.coffee/api/iprisk" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"ip": new_ip, "trust_score": 95, "company_type": "isp"}),
            )
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"success": True, "connection": {"org": "Example Fiber", "isp": "Example Fiber"}}),
            )
        raise AssertionError(f"unexpected URL: {url}")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", (new_ip, 0))],
        settings=settings,
        enabled_services=[network_diagnostic_settings.SERVICE_NETCOFFEE],
    )

    assert result.ok is True
    assert result.cached is False
    assert result.ip == new_ip
    assert any("ip.net.coffee" in url for url in seen_urls)


@pytest.mark.parametrize(
    ("sources", "attempted_sources", "expected"),
    [
        ((), (), False),
        ((network_diagnostic_settings.SERVICE_NETCOFFEE,), (), False),
        ((), (network_diagnostic_settings.SERVICE_NETCOFFEE,), False),
        (
            (network_diagnostic_settings.SERVICE_NETCOFFEE,),
            (
                network_diagnostic_settings.SERVICE_NETCOFFEE,
                network_diagnostic_settings.SERVICE_PROXYCHECK,
            ),
            False,
        ),
        (
            (network_diagnostic_settings.SERVICE_NETCOFFEE,),
            (network_diagnostic_settings.SERVICE_NETCOFFEE,),
            True,
        ),
    ],
)
def test_quality_cache_requires_nonempty_exact_source_coverage(
    sources,
    attempted_sources,
    expected,
):
    result = remote_proxy.ProxyNodeQualityResult(
        node_key="cache-sources",
        ok=True,
        ip="8.8.8.8",
        checked_at=remote_proxy._now_iso(),
        sources=sources,
        attempted_sources=attempted_sources,
        coverage_complete=True,
        classification_basis="信誉源网络/风险字段",
        quality_signature="policy",
    )

    assert remote_proxy.proxy_node_quality_cacheable(result) is expected


def test_assess_proxy_node_quality_rejects_residential_business_conflict_for_ai_proxy():
    node = remote_proxy.parse_proxy_node(
        "{ name: AI代理冲突, type: vless, server: mixed.example.com, port: 443 }"
    )

    def resolver(host, *_args, **_kwargs):
        assert host == "mixed.example.com"
        return [(None, None, None, "", ("8.8.4.78", 0))]

    def http_get(url, _timeout):
        if "proxycheck.io/v3/8.8.4.78" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        "8.8.4.78": {
                            "network": {"type": "Residential", "provider": "Example Fiber"},
                            "detections": {"anonymous": False, "risk": 7},
                        },
                    }
                ),
            )
        if "ipqualityscore.com/api/json/ip/ipqs-key/8.8.4.78" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "success": True,
                        "connection_type": "Business",
                        "fraud_score": 22,
                        "proxy": False,
                        "vpn": False,
                        "tor": False,
                    }
                ),
            )
        if "ipwho.is/8.8.4.78" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "success": True,
                        "country": "United States",
                        "connection": {"asn": 64500, "org": "Example Fiber", "isp": "Example ISP"},
                    }
                ),
            )
        raise AssertionError(f"unexpected URL: {url}")

    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_PROXYCHECK, network_diagnostic_settings.SERVICE_IPQS},
        {network_diagnostic_settings.SERVICE_IPQS: "ipqs-key"},
    )

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=resolver,
        settings=settings,
        enabled_services=[network_diagnostic_settings.SERVICE_PROXYCHECK, network_diagnostic_settings.SERVICE_IPQS],
    )

    assert result.ok is True
    assert result.ip_type == "家宽/商宽冲突"
    assert result.risk_score and result.risk_score > 35
    assert result.quality_label == "来源冲突"
    assert "多源冲突" in result.detail
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(result) is False


def test_assess_proxy_node_quality_labels_high_risk_residential_as_high_risk():
    node = remote_proxy.parse_proxy_node(
        "{ name: AI代理高风险住宅, type: vless, server: risky-home.example.com, port: 443 }"
    )

    def resolver(host, *_args, **_kwargs):
        assert host == "risky-home.example.com"
        return [(None, None, None, "", ("8.8.4.79", 0))]

    def http_get(url, _timeout):
        if "proxycheck.io/v3/8.8.4.79" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        "8.8.4.79": {
                            "network": {"type": "Residential", "provider": "Example Fiber"},
                            "detections": {
                                "proxy": False,
                                "vpn": False,
                                "tor": False,
                                "relay": False,
                                "anonymous": False,
                                "risk": 82,
                            },
                        },
                    }
                ),
            )
        if "ipwho.is/8.8.4.79" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "success": True,
                        "country": "United States",
                        "connection": {"asn": 64500, "org": "Example Fiber", "isp": "Example ISP"},
                    }
                ),
            )
        raise AssertionError(f"unexpected URL: {url}")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=resolver,
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_PROXYCHECK},
            {},
        ),
    )

    assert result.ok is True
    assert result.ip_type == "住宅 IP 高风险"
    assert result.risk_score == 82
    assert result.quality_label == "高风险"
    assert result.quality_score <= 40
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(result) is False


def test_assess_proxy_node_quality_returns_failure_when_provider_raises():
    node = remote_proxy.parse_proxy_node(
        "{ name: 检测失败节点, type: vless, server: node.example.com, port: 443 }"
    )

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("quota exploded")),
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("8.8.4.88", 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_PROXYCHECK},
            {},
        ),
    )

    assert result.ok is False
    assert result.ip == "8.8.4.88"
    assert result.quality_label == "检测失败"
    assert result.sources == ()
    assert result.attempted_sources == (network_diagnostic_settings.SERVICE_PROXYCHECK,)
    assert "quota exploded" in result.detail


def test_low_confidence_geo_keyword_cannot_become_high_quality_home_broadband():
    node = remote_proxy.parse_proxy_node(
        "{ name: 低证据线路, type: vless, server: level3.example.com, port: 443 }"
    )

    def http_get(url, _timeout):
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "success": True,
                        "connection": {
                            "org": "Level 3 Communications",
                            "isp": "Level 3 Communications",
                        },
                    }
                ),
            )
        return network_diagnostics.HttpResult(url=url, ok=False, status_code=503, error="HTTP 503")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("8.8.4.91", 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_NETCOFFEE},
            {},
        ),
    )

    assert result.ok is True
    assert result.ip_type == "运营商/宽带"
    assert result.confidence == "低"
    assert result.quality_label == "家宽待核验"
    assert result.quality_score < 80
    assert result.sources == ()
    assert result.coverage_complete is False
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(result) is False
    assert remote_proxy.proxy_node_quality_cacheable(result) is False


def test_neutral_vpnapi_success_still_fetches_geo_network_classification():
    node = remote_proxy.parse_proxy_node(
        "{ name: 中性信誉源, type: vless, server: neutral.example.com, port: 443 }"
    )
    ip = "8.8.4.95"
    seen_urls = []

    def http_get(url, _timeout):
        seen_urls.append(url)
        if "vpnapi.io" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "ip": ip,
                        "security": {"vpn": False, "proxy": False, "tor": False, "relay": False},
                        "network": {"autonomous_system_organization": "Example Network"},
                    }
                ),
            )
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "success": True,
                        "connection": {"org": "Example Fiber", "isp": "Example Broadband"},
                    }
                ),
            )
        raise AssertionError(f"unexpected URL: {url}")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", (ip, 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_VPNAPI},
            {network_diagnostic_settings.SERVICE_VPNAPI: ["vpn-key"]},
        ),
    )

    assert result.ok is True
    assert any("ipwho.is" in url for url in seen_urls)
    assert result.sources == (network_diagnostic_settings.SERVICE_VPNAPI,)
    assert result.ip_type == "运营商/宽带"
    assert result.confidence == "中"
    assert result.quality_label == "家宽待核验"
    assert result.classification_basis == "Geo/ASN 辅助"
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(result) is False


def test_required_geo_failure_keeps_neutral_source_result_incomplete_and_uncached():
    node = remote_proxy.parse_proxy_node(
        "{ name: Geo失败, type: vless, server: geo-failed.example.com, port: 443 }"
    )
    ip = "8.8.4.97"

    def http_get(url, _timeout):
        if "vpnapi.io" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "ip": ip,
                        "security": {"vpn": False, "proxy": False, "tor": False, "relay": False},
                    }
                ),
            )
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(url=url, ok=False, status_code=503, error="HTTP 503")
        raise AssertionError(f"unexpected URL: {url}")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", (ip, 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_VPNAPI},
            {network_diagnostic_settings.SERVICE_VPNAPI: ["vpn-key"]},
        ),
    )

    assert result.ok is True
    assert result.sources == (network_diagnostic_settings.SERVICE_VPNAPI,)
    assert result.coverage_complete is False
    assert "Geo/ASN" in result.detail
    assert remote_proxy.proxy_node_quality_cacheable(result) is False


def test_server_ip_quality_rejects_ping0_without_key_before_dns_or_http():
    node = remote_proxy.parse_proxy_node(
        "{ name: 无Ping0Key, type: vless, server: ping0less.example.com, port: 443 }"
    )
    seen_urls = []

    def http_get(url, _timeout):
        seen_urls.append(url)
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"success": True, "connection": {"org": "Example Broadband"}}),
            )
        raise AssertionError(f"unexpected URL: {url}")

    def resolver(*_args, **_kwargs):
        raise AssertionError("DNS should not run without an executable source")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=resolver,
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_PING0},
            {},
        ),
    )

    assert result.ok is False
    assert result.quality_label == "检测源不可用"
    assert seen_urls == []
    assert result.sources == ()
    assert result.attempted_sources == ()
    assert result.coverage_complete is False


def test_ping0_non_idc_only_uses_geo_basis_and_is_never_auto_selected():
    node = remote_proxy.parse_proxy_node(
        "{ name: Ping0非IDC, type: vless, server: non-idc.example.com, port: 443 }"
    )
    ip = "8.8.4.98"
    seen_urls = []

    def http_get(url, _timeout):
        seen_urls.append(url)
        if "ping0.cc/apiloc/apikey(ping-key)" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "ip": ip,
                        "isidc": False,
                        "iprisk": 5,
                        "isnative": True,
                        "asntype": "isp",
                        "orgtype": "isp",
                    }
                ),
            )
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "success": True,
                        "connection": {
                            "org": "Example Broadband",
                            "isp": "Example Broadband",
                        },
                    }
                ),
            )
        raise AssertionError(f"unexpected URL: {url}")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", (ip, 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_PING0},
            {network_diagnostic_settings.SERVICE_PING0: ["ping-key"]},
        ),
    )

    assert result.ok is True
    assert result.sources == (network_diagnostic_settings.SERVICE_PING0,)
    assert result.coverage_complete is True
    assert result.classification_basis == "Geo/ASN 辅助"
    assert any("ipwho.is" in url for url in seen_urls)
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(result) is False


def test_ping0_only_high_risk_is_attributed_to_ping0_and_still_resolves_region():
    node = remote_proxy.parse_proxy_node(
        "{ name: Ping0高风险, type: vless, server: ping-risk.example.com, port: 443 }"
    )
    ip = "8.8.4.99"
    seen_urls = []

    def http_get(url, _timeout):
        seen_urls.append(url)
        if "ping0.cc/apiloc/apikey(ping-key)" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"ip": ip, "iprisk": 90}),
            )
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"success": True, "country_code": "US", "country": "United States"}),
            )
        raise AssertionError(f"unexpected URL: {url}")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", (ip, 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_PING0},
            {network_diagnostic_settings.SERVICE_PING0: ["ping-key"]},
        ),
    )

    assert result.ok is True
    assert result.classification_basis == "Ping0 指定 IP"
    assert result.quality_label == "高风险"
    assert result.region == "美国"
    assert any("ipwho.is" in url for url in seen_urls)
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(result) is False


def test_all_soft_failed_quality_sources_and_geo_return_failure():
    node = remote_proxy.parse_proxy_node(
        "{ name: 全部失败, type: vless, server: failed.example.com, port: 443 }"
    )

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=lambda url, _timeout: network_diagnostics.HttpResult(
            url=url,
            ok=False,
            status_code=503,
            error="HTTP 503",
        ),
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("8.8.4.92", 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {
                network_diagnostic_settings.SERVICE_NETCOFFEE,
                network_diagnostic_settings.SERVICE_PROXYCHECK,
            },
            {},
        ),
    )

    assert result.ok is False
    assert result.quality_label == "检测失败"
    assert result.sources == ()
    assert set(result.attempted_sources) == {
        network_diagnostic_settings.SERVICE_NETCOFFEE,
        network_diagnostic_settings.SERVICE_PROXYCHECK,
    }
    assert remote_proxy.proxy_node_quality_cacheable(result) is False


def test_empty_provider_payload_is_not_counted_as_usable_quality_evidence():
    node = remote_proxy.parse_proxy_node(
        "{ name: 空响应, type: vless, server: empty.example.com, port: 443 }"
    )

    def http_get(url, _timeout):
        if "ip.net.coffee" in url:
            return network_diagnostics.HttpResult(url=url, ok=True, text="{}")
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"success": True, "connection": {"org": "Unknown Network"}}),
            )
        raise AssertionError(f"unexpected URL: {url}")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("8.8.4.96", 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_NETCOFFEE},
            {},
        ),
    )

    assert result.ok is True
    assert result.sources == ()
    assert result.attempted_sources == (network_diagnostic_settings.SERVICE_NETCOFFEE,)
    assert result.coverage_complete is False
    assert result.classification_basis == "Geo/ASN 辅助"
    assert remote_proxy.proxy_node_quality_cacheable(result) is False


def test_partial_source_success_is_visible_but_not_reused_as_complete_cache(monkeypatch, tmp_path):
    node = remote_proxy.parse_proxy_node(
        "{ name: 部分成功, type: vless, server: partial.example.com, port: 443 }"
    )
    ip = "8.8.4.93"
    settings = network_diagnostic_settings.settings_from_values(
        {
            network_diagnostic_settings.SERVICE_PROXYCHECK,
            network_diagnostic_settings.SERVICE_VPNAPI,
        },
        {network_diagnostic_settings.SERVICE_VPNAPI: ["vpn-key"]},
    )
    calls = {"proxycheck": 0, "vpnapi": 0}

    def http_get(url, _timeout):
        if "proxycheck.io" in url:
            calls["proxycheck"] += 1
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        ip: {
                            "network": {"type": "Residential"},
                            "detections": {"proxy": False, "vpn": False, "tor": False, "risk": 8},
                        },
                    }
                ),
            )
        if "vpnapi.io" in url:
            calls["vpnapi"] += 1
            return network_diagnostics.HttpResult(url=url, ok=False, status_code=503, error="HTTP 503")
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"success": True, "country_code": "US", "country": "United States"}),
            )
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    remote_proxy.clear_proxy_subscription_state_cache()
    first = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", (ip, 0))],
        settings=settings,
    )
    remote_proxy.save_proxy_subscription_qualities({first.node_key: first})
    second = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", (ip, 0))],
        settings=settings,
    )

    assert first.ok is True
    assert first.sources == (network_diagnostic_settings.SERVICE_PROXYCHECK,)
    assert set(first.attempted_sources) == {
        network_diagnostic_settings.SERVICE_PROXYCHECK,
        network_diagnostic_settings.SERVICE_VPNAPI,
    }
    assert first.coverage_complete is False
    assert first.quality_label == "家宽待复核"
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(first) is False
    assert second.cached is False
    assert calls == {"proxycheck": 2, "vpnapi": 2}


def test_assess_proxy_node_quality_does_not_mislabel_unrelated_interruption_as_cancelled():
    node = remote_proxy.parse_proxy_node(
        "{ name: 网络中断节点, type: vless, server: node.example.com, port: 443 }"
    )

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=lambda *_args, **_kwargs: (_ for _ in ()).throw(InterruptedError("socket interrupted")),
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("8.8.4.89", 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_PROXYCHECK},
            {},
        ),
        cancel_event=threading.Event(),
    )

    assert result.ok is False
    assert result.quality_label == "检测失败"
    assert remote_proxy.proxy_node_quality_cancelled(result) is False


def test_assess_proxy_node_qualities_isolates_single_node_failure(monkeypatch):
    nodes = remote_proxy.parse_proxy_subscription_content(
        """
proxies:
  - { name: good, type: vless, server: good.example.com, port: 443 }
  - { name: bad, type: vless, server: bad.example.com, port: 443 }
"""
    )

    def fake_assess(node, *_args, **_kwargs):
        if node["name"] == "bad":
            raise RuntimeError("bad node boom")
        node_key = remote_proxy.proxy_node_key(node)
        return remote_proxy.ProxyNodeQualityResult(
            node_key,
            True,
            host=node["server"],
            ip="8.8.4.90",
            ip_type="家庭宽带/住宅 IP",
            risk_score=9,
            quality_score=95,
            quality_label="家宽高质",
        )

    monkeypatch.setattr(remote_proxy, "assess_proxy_node_quality", fake_assess)

    results = remote_proxy.assess_proxy_node_qualities(
        nodes,
        settings=network_diagnostic_settings.settings_from_values(set(), {}),
    )

    good_key = remote_proxy.proxy_node_key(nodes[0].node)
    bad_key = remote_proxy.proxy_node_key(nodes[1].node)
    assert results[good_key].ok is True
    assert results[bad_key].ok is False
    assert results[bad_key].quality_label == "检测失败"
    assert "bad node boom" in results[bad_key].detail


def test_assess_proxy_node_qualities_single_flights_same_ip_and_reports_progress(monkeypatch, tmp_path):
    nodes = remote_proxy.parse_proxy_subscription_content(
        "proxies:\n"
        + "\n".join(
            f"  - {{ name: 香港-{index}, type: vless, server: node-{index}.example.com, port: 443 }}"
            for index in range(12)
        )
    )
    shared_ip = "8.8.4.77"
    provider_calls = []
    progress = []
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_PROXYCHECK},
        {},
    )

    def http_get(url, _timeout):
        provider_calls.append(url)
        if "proxycheck.io" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        shared_ip: {
                            "network": {"type": "Residential", "provider": "Example Fiber", "asn": "64500"},
                            "detections": {"proxy": False, "vpn": False, "tor": False, "hosting": False, "risk": 7},
                        },
                    }
                ),
            )
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"success": True, "country_code": "US", "country": "United States"}),
            )
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    remote_proxy.clear_proxy_subscription_state_cache()
    results = remote_proxy.assess_proxy_node_qualities(
        nodes,
        http_get=http_get,
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", (shared_ip, 0))],
        settings=settings,
        enabled_services=[network_diagnostic_settings.SERVICE_PROXYCHECK],
        progress_callback=lambda completed, total, result: progress.append((completed, total, result.node_key)),
    )

    assert len(results) == 12
    assert sum("proxycheck.io" in url for url in provider_calls) == 1
    assert sum("ipwho.is" in url for url in provider_calls) == 1
    assert [item[0] for item in progress] == list(range(1, 13))
    assert all(item[1] == 12 for item in progress)
    assert {result.host for result in results.values()} == {f"node-{index}.example.com" for index in range(12)}
    assert all(result.ip == shared_ip and result.ok for result in results.values())
    assert sum(1 for result in results.values() if result.cached) == 0
    assert sum("同一 IP 批次复用" in result.detail for result in results.values()) == 11
    assert {result.confidence for result in results.values()} == {"中"}
    assert {result.sources for result in results.values()} == {
        (network_diagnostic_settings.SERVICE_PROXYCHECK,)
    }
    assert {result.attempted_sources for result in results.values()} == {
        (network_diagnostic_settings.SERVICE_PROXYCHECK,)
    }
    assert all(result.coverage_complete for result in results.values())
    assert {result.assessment_scope for result in results.values()} == {
        remote_proxy.PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER
    }
    assert len({result.quality_signature for result in results.values()}) == 1
    assert all(result.quality_signature for result in results.values())


def test_assess_proxy_node_qualities_groups_by_ip_before_provider_calls(monkeypatch, tmp_path):
    nodes = remote_proxy.parse_proxy_subscription_content(
        "proxies:\n"
        "  - { name: same-a, type: vless, server: a.example.com, port: 443 }\n"
        "  - { name: same-b, type: vless, server: b.example.com, port: 443 }\n"
        "  - { name: other, type: vless, server: c.example.com, port: 443 }\n"
    )
    host_ips = {
        "a.example.com": "8.8.4.101",
        "b.example.com": "8.8.4.101",
        "c.example.com": "8.8.4.102",
    }
    provider_ips = []
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_PROXYCHECK},
        {},
    )

    def resolver(host, *_args, **_kwargs):
        return [(None, None, None, "", (host_ips[host], 0))]

    def http_get(url, _timeout):
        if "proxycheck.io" in url:
            ip = url.rsplit("/", 1)[-1].split("?", 1)[0]
            provider_ips.append(ip)
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        ip: {
                            "network": {"type": "Residential", "provider": "Example Fiber", "asn": "64500"},
                            "detections": {"proxy": False, "vpn": False, "tor": False, "hosting": False, "risk": 6},
                        },
                    }
                ),
            )
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"success": True, "country_code": "US", "country": "United States"}),
            )
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    remote_proxy.clear_proxy_subscription_state_cache()
    results = remote_proxy.assess_proxy_node_qualities(
        nodes,
        http_get=http_get,
        resolver=resolver,
        settings=settings,
        enabled_services=[network_diagnostic_settings.SERVICE_PROXYCHECK],
        max_workers=3,
    )

    assert sorted(provider_ips) == ["8.8.4.101", "8.8.4.102"]
    same_results = [result for result in results.values() if result.ip == "8.8.4.101"]
    assert len(same_results) == 2
    assert sum("同一 IP 批次复用" in result.detail for result in same_results) == 1
    assert all(result.ok and result.coverage_complete for result in results.values())


def test_assess_proxy_node_qualities_incremental_cancel_stops_new_quality_work(monkeypatch, tmp_path):
    nodes = remote_proxy.parse_proxy_subscription_content(
        "proxies:\n"
        + "\n".join(
            f"  - {{ name: node-{index}, type: vless, server: node-{index}.example.com, port: 443 }}"
            for index in range(5)
        )
    )
    cancel_event = threading.Event()
    provider_calls = []
    progress = []
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_PROXYCHECK},
        {},
    )

    def resolver(host, *_args, **_kwargs):
        index = host.split("-", 1)[1].split(".", 1)[0]
        return [(None, None, None, "", (f"8.8.4.{110 + int(index)}", 0))]

    def fake_assess(node, *_args, **_kwargs):
        provider_calls.append(node["name"])
        if len(provider_calls) == 1:
            cancel_event.set()
        return remote_proxy.ProxyNodeQualityResult(
            remote_proxy.proxy_node_key(node),
            True,
            host=node["server"],
            ip="8.8.4.110",
            ip_type="家庭宽带/住宅 IP",
            risk_score=8,
            risk_label="低风险",
            quality_score=92,
            quality_label="家宽高质",
            confidence="中",
            sources=(network_diagnostic_settings.SERVICE_PROXYCHECK,),
            attempted_sources=(network_diagnostic_settings.SERVICE_PROXYCHECK,),
            coverage_complete=True,
            assessment_scope=remote_proxy.PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER,
            classification_basis="信誉源网络/风险字段",
            quality_signature="sig",
        )

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy, "assess_proxy_node_quality", fake_assess)
    remote_proxy.clear_proxy_subscription_state_cache()
    results = remote_proxy.assess_proxy_node_qualities(
        nodes,
        resolver=resolver,
        settings=settings,
        enabled_services=[network_diagnostic_settings.SERVICE_PROXYCHECK],
        max_workers=1,
        cancel_event=cancel_event,
        progress_callback=lambda completed, total, result: progress.append((completed, total, result.quality_label)),
    )

    assert provider_calls == ["node-0"]
    assert len(results) == 5
    assert sum(remote_proxy.proxy_node_quality_cancelled(result) for result in results.values()) == 4
    assert progress[-1][:2] == (5, 5)


def test_assess_proxy_node_qualities_parse_failure_does_not_block_grouped_results(monkeypatch, tmp_path):
    nodes = remote_proxy.parse_proxy_subscription_content(
        "proxies:\n"
        "  - { name: bad, type: vless, server: bad.example.com, port: 443 }\n"
        "  - { name: good-a, type: vless, server: good-a.example.com, port: 443 }\n"
        "  - { name: good-b, type: vless, server: good-b.example.com, port: 443 }\n"
    )
    provider_calls = []
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_PROXYCHECK},
        {},
    )

    def resolver(host, *_args, **_kwargs):
        if host == "bad.example.com":
            raise OSError("dns boom")
        return [(None, None, None, "", ("8.8.4.121", 0))]

    def http_get(url, _timeout):
        if "proxycheck.io" in url:
            provider_calls.append(url)
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        "8.8.4.121": {
                            "network": {"type": "Residential", "provider": "Example Fiber", "asn": "64500"},
                            "detections": {"proxy": False, "vpn": False, "tor": False, "hosting": False, "risk": 5},
                        },
                    }
                ),
            )
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"success": True, "country_code": "US", "country": "United States"}),
            )
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    remote_proxy.clear_proxy_subscription_state_cache()
    results = remote_proxy.assess_proxy_node_qualities(
        nodes,
        http_get=http_get,
        resolver=resolver,
        settings=settings,
        enabled_services=[network_diagnostic_settings.SERVICE_PROXYCHECK],
    )

    bad_key = remote_proxy.proxy_subscription_node_key(nodes[0])
    assert results[bad_key].quality_label == "解析失败"
    assert len(provider_calls) == 1
    assert sum(result.ok for result in results.values()) == 2
    assert sum("同一 IP 批次复用" in result.detail for result in results.values()) == 1


def test_assess_proxy_node_qualities_honors_pre_cancelled_batch(monkeypatch, tmp_path):
    nodes = remote_proxy.parse_proxy_subscription_content(
        """
proxies:
  - { name: 香港-1, type: vless, server: first.example.com, port: 443 }
  - { name: 香港-2, type: vless, server: second.example.com, port: 443 }
"""
    )
    cancel_event = threading.Event()
    cancel_event.set()
    progress = []
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    remote_proxy.clear_proxy_subscription_state_cache()

    results = remote_proxy.assess_proxy_node_qualities(
        nodes,
        http_get=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network should not run")),
        resolver=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("DNS should not run")),
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_NETCOFFEE},
            {},
        ),
        cancel_event=cancel_event,
        progress_callback=lambda completed, total, _result: progress.append((completed, total)),
    )

    assert len(results) == 2
    assert all(remote_proxy.proxy_node_quality_label(result) == "已取消" for result in results.values())
    assert all(remote_proxy.proxy_node_quality_cancelled(result) for result in results.values())
    assert progress == [(1, 2), (2, 2)]


def test_proxy_node_quality_cancelled_only_matches_cancel_placeholders():
    cancelled = remote_proxy.ProxyNodeQualityResult(
        node_key="cancelled",
        ok=False,
        quality_label="已取消",
    )
    failed = remote_proxy.ProxyNodeQualityResult(
        node_key="failed",
        ok=False,
        quality_label="检测失败",
    )
    measured = remote_proxy.ProxyNodeQualityResult(
        node_key="measured",
        ok=True,
        quality_label="已取消",
    )

    assert remote_proxy.proxy_node_quality_cancelled(cancelled) is True
    assert remote_proxy.proxy_node_quality_cancelled(failed) is False
    assert remote_proxy.proxy_node_quality_cancelled(measured) is False


def test_quality_batch_resolver_reuses_dns_result_for_same_call_shape():
    calls = []

    def resolver(*args, **kwargs):
        calls.append((args, kwargs))
        return [(None, None, None, "", ("8.8.4.41", 0))]

    shared = remote_proxy._proxy_quality_batch_resolver(resolver)

    assert shared("same.example.com", None, type=1) == shared("same.example.com", None, type=1)
    assert len(calls) == 1


def test_proxy_quality_dns_prefers_public_ipv6_over_private_ipv4():
    node = remote_proxy.parse_proxy_node(
        "{ name: 双栈节点, type: vless, server: dual.example.com, port: 443 }"
    )

    resolved = remote_proxy._resolve_proxy_node_ip(
        node,
        resolver=lambda *_args, **_kwargs: [
            (None, None, None, "", ("192.168.1.20", 0)),
            (None, None, None, "", ("2001:4860:4860:0:0:0:0:8888", 0, 0, 0)),
        ],
    )

    assert resolved == "2001:4860:4860::8888"


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "100.64.0.1",
        "192.0.2.1",
        "203.0.113.1",
        "240.0.0.1",
        "fc00::1",
        "2001:db8::1",
    ],
)
def test_proxy_quality_external_lookup_accepts_only_globally_routable_ips(address):
    assert remote_proxy._proxy_quality_ip_usable(address) is False


def test_effective_server_quality_sources_skip_key_required_services_without_keys():
    settings = network_diagnostic_settings.settings_from_values(
        {
            network_diagnostic_settings.SERVICE_NETCOFFEE,
            network_diagnostic_settings.SERVICE_PING0,
            network_diagnostic_settings.SERVICE_IPQS,
            network_diagnostic_settings.SERVICE_VPNAPI,
        },
        {},
    )

    assert remote_proxy.proxy_quality_effective_services(
        settings,
        settings.enabled_services(include_hidden=True),
    ) == [network_diagnostic_settings.SERVICE_NETCOFFEE]


def test_equivalent_ipv6_text_matches_provider_response():
    node = remote_proxy.parse_proxy_node(
        "{ name: IPv6节点, type: vless, server: '2001:4860:4860::8888', port: 443 }"
    )
    expanded_ip = "2001:4860:4860:0:0:0:0:8888"

    def http_get(url, _timeout):
        if "/api/ip/lookup/" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "ip": expanded_ip,
                        "isResidential": True,
                        "is_datacenter": False,
                        "is_vpn": False,
                        "is_proxy": False,
                        "is_tor": False,
                        "company_type": "isp",
                        "trust_score": 96,
                        "ai_verdict": {"label": "Clean residential", "confidence": 94},
                    }
                ),
            )
        if "/api/iprisk/" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"ip": expanded_ip, "trust_score": 96}),
            )
        if "ipwho.is" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({"success": True, "country_code": "US", "country": "United States"}),
            )
        raise AssertionError(f"unexpected URL: {url}")

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=http_get,
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_NETCOFFEE},
            {},
        ),
    )

    assert result.ok is True
    assert result.ip == "2001:4860:4860::8888"
    assert result.confidence == "中"
    assert result.classification_basis == "信誉源网络/风险字段"


def test_far_future_quality_timestamp_is_not_fresh():
    assert remote_proxy._quality_checked_at_fresh("2999-01-01T00:00:00+00:00", 6 * 60 * 60) is False


def test_quality_batch_worker_count_caps_nested_provider_concurrency():
    services = [
        network_diagnostic_settings.SERVICE_NETCOFFEE,
        network_diagnostic_settings.SERVICE_PROXYCHECK,
        network_diagnostic_settings.SERVICE_IPAPI,
        network_diagnostic_settings.SERVICE_IPQS,
        network_diagnostic_settings.SERVICE_VPNAPI,
    ]

    assert remote_proxy._proxy_quality_batch_worker_count(8, 100, services) == 2
    assert remote_proxy._proxy_quality_batch_worker_count(8, 3, [services[0]]) == 3


def test_merge_proxy_subscription_qualities_targets_captured_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    remote_proxy.clear_proxy_subscription_state_cache()
    first = remote_proxy.save_proxy_subscription_profile("香港组", "https://example.com/hk")
    second = remote_proxy.save_proxy_subscription_profile("美国组", "https://example.com/us")
    node = remote_proxy.parse_proxy_node(
        "{ name: 香港家宽, type: vless, server: hk.example.com, port: 443 }"
    )
    node_key = remote_proxy.proxy_node_key(node)
    result = remote_proxy.ProxyNodeQualityResult(
        node_key=node_key,
        ok=True,
        host="hk.example.com",
        ip="8.8.4.22",
        quality_label="家宽高质",
        quality_score=95,
        checked_at=remote_proxy._now_iso(),
    )

    remote_proxy.merge_proxy_subscription_qualities({node_key: result}, profile_id=first["id"])
    state = remote_proxy.load_proxy_subscription_state()

    assert state["active_profile_id"] == second["id"]
    assert node_key in state["profiles"][first["id"]]["node_qualities"]
    assert node_key not in state["profiles"][second["id"]]["node_qualities"]


def test_partial_refresh_does_not_replace_same_policy_complete_evidence():
    complete = remote_proxy.ProxyNodeQualityResult(
        node_key="node",
        ok=True,
        quality_label="家宽高质",
        coverage_complete=True,
        classification_basis="信誉源网络/风险字段",
        quality_signature="same-policy",
        checked_at=remote_proxy._now_iso(),
    )
    partial = remote_proxy.ProxyNodeQualityResult(
        node_key="node",
        ok=True,
        quality_label="家宽待复核",
        coverage_complete=False,
        classification_basis="信誉源网络/风险字段",
        quality_signature="same-policy",
    )

    merged, persisted = remote_proxy.merge_proxy_quality_refresh_results(
        {"node": complete},
        {"node": partial},
    )

    assert merged["node"] is complete
    assert persisted == {}


def test_partial_refresh_replaces_expired_complete_evidence():
    expired = remote_proxy.ProxyNodeQualityResult(
        node_key="node",
        ok=True,
        quality_label="家宽高质",
        coverage_complete=True,
        classification_basis="信誉源网络/风险字段",
        quality_signature="same-policy",
        checked_at="2000-01-01T00:00:00+00:00",
    )
    partial = remote_proxy.ProxyNodeQualityResult(
        node_key="node",
        ok=True,
        quality_label="家宽待复核",
        coverage_complete=False,
        classification_basis="信誉源网络/风险字段",
        quality_signature="same-policy",
        checked_at=remote_proxy._now_iso(),
    )

    merged, persisted = remote_proxy.merge_proxy_quality_refresh_results(
        {"node": expired},
        {"node": partial},
    )

    assert merged["node"] is partial
    assert persisted == {"node": partial}


def test_partial_refresh_for_a_new_policy_replaces_old_policy_result():
    old = remote_proxy.ProxyNodeQualityResult(
        node_key="node",
        ok=True,
        coverage_complete=True,
        quality_signature="old-policy",
    )
    current = remote_proxy.ProxyNodeQualityResult(
        node_key="node",
        ok=True,
        coverage_complete=False,
        quality_signature="new-policy",
    )

    merged, persisted = remote_proxy.merge_proxy_quality_refresh_results(
        {"node": old},
        {"node": current},
    )

    assert merged["node"] is current
    assert persisted == {"node": current}


def test_quality_preferred_sorting_selects_ai_proxy_residential_over_faster_idc():
    nodes = remote_proxy.parse_proxy_subscription_content(
        """
proxies:
  - { name: 日本 机房, type: vless, server: jp.example.com, port: 443 }
  - { name: 美国 家宽, type: vless, server: us.example.com, port: 443 }
"""
    )
    idc_key = remote_proxy.proxy_node_key(nodes[0].node)
    home_key = remote_proxy.proxy_node_key(nodes[1].node)
    latencies = {
        idc_key: remote_proxy.ProxyNodeLatencyResult(idc_key, True, latency_ms=10),
        home_key: remote_proxy.ProxyNodeLatencyResult(home_key, True, latency_ms=70),
    }
    qualities = {
        idc_key: remote_proxy.ProxyNodeQualityResult(
            idc_key,
            True,
            ip_type="IDC/云机房",
            risk_score=62,
            quality_score=28,
            quality_label="机房风险",
        ),
        home_key: remote_proxy.ProxyNodeQualityResult(
            home_key,
            True,
            ip_type="家庭宽带/住宅 IP",
            risk_score=12,
            quality_score=96,
            quality_label="家宽高质",
            confidence="高",
            coverage_complete=True,
            classification_basis="信誉源网络/风险字段",
        ),
    }

    sorted_nodes = remote_proxy.sort_proxy_subscription_nodes(nodes, latencies, qualities, prefer_quality=True)
    best = remote_proxy.best_proxy_subscription_node_for_ai_proxy(nodes, qualities, latencies)

    assert sorted_nodes[0].node["name"] == "美国 家宽"
    assert best is not None
    assert best.node["name"] == "美国 家宽"
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(qualities[home_key]) is True


def test_residential_anonymous_exit_is_never_labeled_or_selected_as_home_broadband():
    classification = network_diagnostics.IpClassification(
        ip_type="住宅代理/匿名出口可疑",
        risk_score=78,
        risk_label="高",
        confidence="中",
    )

    score = remote_proxy._proxy_quality_score(classification)
    label = remote_proxy._proxy_quality_label(classification, score)
    suspicious = remote_proxy.ProxyNodeQualityResult(
        node_key="anonymous-home",
        ok=True,
        ip_type=classification.ip_type,
        risk_score=5,
        quality_score=100,
        quality_label="家宽高质",
        confidence="高",
    )

    assert score <= 20
    assert label == "代理风险"
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(suspicious) is False


def test_non_native_broadcast_home_ip_is_never_auto_selected():
    result = remote_proxy.ProxyNodeQualityResult(
        node_key="broadcast-home",
        ok=True,
        ip_type="家庭/非IDC宽带（非原生/广播）",
        risk_score=10,
        quality_score=100,
        quality_label="家宽高质",
        confidence="高",
        coverage_complete=True,
        classification_basis="Ping0 指定 IP",
    )

    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(result) is False


@pytest.mark.parametrize(
    ("candidate_quality",),
    [
        (
            {
                "ip_type": "家庭宽带/住宅 IP",
                "risk_score": 5,
                "quality_score": 100,
                "quality_label": "家宽待复核",
                "confidence": "中",
                "coverage_complete": False,
                "classification_basis": "信誉源网络/风险字段",
            },
        ),
        (
            {
                "ip_type": "运营商/宽带",
                "risk_score": 18,
                "quality_score": 95,
                "quality_label": "家宽待核验",
                "confidence": "低",
                "coverage_complete": True,
                "classification_basis": "Geo/ASN 辅助",
            },
        ),
        (
            {
                "ip_type": "家庭/非IDC宽带（非原生/广播）",
                "risk_score": 45,
                "quality_score": 100,
                "quality_label": "非原生待复核",
                "confidence": "高",
                "coverage_complete": True,
                "classification_basis": "Ping0 指定 IP",
            },
        ),
    ],
)
def test_best_quality_candidate_never_falls_back_to_ai_ineligible_result(candidate_quality):
    nodes = remote_proxy.parse_proxy_subscription_content(
        "proxies:\n  - { name: 待复核候选, type: vless, server: candidate.example.com, port: 443 }"
    )
    key = remote_proxy.proxy_subscription_node_key(nodes[0])
    quality = remote_proxy.ProxyNodeQualityResult(node_key=key, ok=True, **candidate_quality)

    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(quality) is False
    assert remote_proxy.best_proxy_subscription_node_for_ai_proxy(nodes, {key: quality}) is None


@pytest.mark.parametrize(
    ("base_type", "augmented_type"),
    [
        ("代理/VPN/Tor 可疑", "代理/VPN/Tor 高风险可疑"),
        ("IDC/云机房", "家庭宽带/住宅 IP + IDC/云机房"),
        ("企业/商宽 IP", "家庭宽带/住宅 IP + 企业/商宽 IP"),
        ("家庭宽带/住宅 IP", "家庭宽带/住宅 IP（非原生/广播）"),
    ],
)
def test_proxy_quality_score_cannot_improve_when_adverse_markers_are_added(
    base_type,
    augmented_type,
):
    base = network_diagnostics.IpClassification(
        ip_type=base_type,
        risk_score=0,
        risk_label="极低",
        confidence="高",
    )
    augmented = network_diagnostics.IpClassification(
        ip_type=augmented_type,
        risk_score=0,
        risk_label="极低",
        confidence="高",
    )

    assert remote_proxy._proxy_quality_score(augmented) < remote_proxy._proxy_quality_score(base)


@pytest.mark.parametrize(
    ("confidence", "score", "risk", "expected"),
    [
        ("中", 80, 35, True),
        ("低", 100, 0, False),
        ("中", 79, 0, False),
        ("中", 100, 36, False),
        ("中", 100, None, False),
    ],
)
def test_ai_proxy_quality_gate_boundaries(confidence, score, risk, expected):
    result = remote_proxy.ProxyNodeQualityResult(
        node_key="gate",
        ok=True,
        ip_type="家庭宽带/住宅 IP",
        risk_score=risk,
        quality_score=score,
        quality_label="家宽高质",
        confidence=confidence,
        coverage_complete=True,
        classification_basis="信誉源网络/风险字段",
    )

    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(result) is expected


def test_quality_ranking_never_selects_explicitly_unreachable_high_score_node():
    nodes = remote_proxy.parse_proxy_subscription_content(
        """
proxies:
  - { name: 不可连满分, type: vless, server: dead.example.com, port: 443 }
  - { name: 可连高分, type: vless, server: live.example.com, port: 443 }
"""
    )
    dead_key = remote_proxy.proxy_node_key(nodes[0].node)
    live_key = remote_proxy.proxy_node_key(nodes[1].node)
    latencies = {
        dead_key: remote_proxy.ProxyNodeLatencyResult(dead_key, False, detail="timeout"),
        live_key: remote_proxy.ProxyNodeLatencyResult(live_key, True, latency_ms=80),
    }
    qualities = {
        dead_key: remote_proxy.ProxyNodeQualityResult(
            dead_key,
            True,
            ip_type="家庭宽带/住宅 IP",
            risk_score=2,
            quality_score=100,
            quality_label="家宽高质",
            confidence="高",
            coverage_complete=True,
            classification_basis="信誉源网络/风险字段",
        ),
        live_key: remote_proxy.ProxyNodeQualityResult(
            live_key,
            True,
            ip_type="家庭宽带/住宅 IP",
            risk_score=10,
            quality_score=90,
            quality_label="家宽高质",
            confidence="高",
            coverage_complete=True,
            classification_basis="信誉源网络/风险字段",
        ),
    }

    sorted_nodes = remote_proxy.sort_proxy_subscription_nodes(nodes, latencies, qualities, prefer_quality=True)
    best = remote_proxy.best_proxy_subscription_node_for_ai_proxy(nodes, qualities, latencies)

    assert sorted_nodes[0].node["name"] == "可连高分"
    assert best is not None
    assert best.node["name"] == "可连高分"


def test_stale_unreachable_latency_does_not_permanently_disqualify_best_quality_node():
    nodes = remote_proxy.parse_proxy_subscription_content(
        """
proxies:
  - { name: 旧失败家宽, type: vless, server: home.example.com, port: 443 }
  - { name: 未测速机房, type: vless, server: idc.example.com, port: 443 }
"""
    )
    home_key = remote_proxy.proxy_node_key(nodes[0].node)
    idc_key = remote_proxy.proxy_node_key(nodes[1].node)
    latencies = {
        home_key: {
            "ok": False,
            "latency_ms": None,
            "measured_at": "2000-01-01T00:00:00+00:00",
        }
    }
    qualities = {
        home_key: remote_proxy.ProxyNodeQualityResult(
            home_key,
            True,
            ip_type="家庭宽带/住宅 IP",
            risk_score=8,
            quality_score=96,
            quality_label="家宽高质",
            confidence="高",
            coverage_complete=True,
            classification_basis="信誉源网络/风险字段",
        ),
        idc_key: remote_proxy.ProxyNodeQualityResult(
            idc_key,
            True,
            ip_type="IDC/云机房",
            risk_score=55,
            quality_score=20,
            quality_label="机房风险",
            confidence="中",
            coverage_complete=True,
            classification_basis="信誉源网络/风险字段",
        ),
    }

    best = remote_proxy.best_proxy_subscription_node_for_ai_proxy(nodes, qualities, latencies)

    assert remote_proxy.proxy_node_latency_label(latencies[home_key]) == "已过期"
    assert best is not None
    assert best.node["name"] == "旧失败家宽"


def test_stale_in_memory_latency_result_expires_by_its_measurement_time():
    result = remote_proxy.ProxyNodeLatencyResult(
        node_key="old-failure",
        ok=False,
        detail="timeout",
        measured_at="2000-01-01T00:00:00+00:00",
    )

    assert remote_proxy.proxy_node_latency_fresh(result) is False
    assert remote_proxy.proxy_node_latency_explicitly_unreachable(result) is False
    assert remote_proxy.proxy_node_latency_label(result) == "已过期"


def test_measure_proxy_node_latency_success(monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    calls = []
    times = iter([10.0, 10.05])

    def fake_connect(endpoint, timeout):
        calls.append((endpoint, timeout))
        return FakeSocket()

    monkeypatch.setattr(remote_proxy.socket, "create_connection", fake_connect)
    monkeypatch.setattr(remote_proxy.time, "perf_counter", lambda: next(times))

    result = remote_proxy.measure_proxy_node_latency(
        {"name": "香港", "type": "vless", "server": "hk.example.com", "port": "443"},
        timeout=2.5,
        attempts=1,
    )

    assert result.ok is True
    assert result.latency_ms == 50
    assert calls == [(("hk.example.com", 443), 2.5)]


def test_measure_proxy_node_latency_failure(monkeypatch):
    monkeypatch.setattr(
        remote_proxy.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("timed out")),
    )

    result = remote_proxy.measure_proxy_node_latency(
        {"name": "bad", "type": "vless", "server": "bad.example.com", "port": 443},
        timeout=0.2,
        attempts=1,
    )

    assert result.ok is False
    assert result.latency_ms is None
    assert "timed out" in result.detail


def test_measure_proxy_node_latency_strict_mode_requires_every_attempt(monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    outcomes = iter((FakeSocket(), OSError("connection reset"), FakeSocket()))
    times = iter((0.0, 0.04, 1.0, 2.0, 2.06))

    def fake_connect(*_args, **_kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(remote_proxy.socket, "create_connection", fake_connect)
    monkeypatch.setattr(remote_proxy.time, "perf_counter", lambda: next(times))

    result = remote_proxy.measure_proxy_node_latency(
        {"name": "unstable", "type": "vless", "server": "unstable.example.com", "port": 443},
        timeout=0.2,
        attempts=3,
        require_all=True,
    )

    assert result.ok is False
    assert result.latency_ms is None
    assert "2/3" in result.detail


def test_measure_proxy_node_latency_uses_median_instead_of_lucky_minimum(monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    times = iter((0.0, 0.09, 1.0, 1.03, 2.0, 2.06))
    monkeypatch.setattr(remote_proxy.socket, "create_connection", lambda *_args, **_kwargs: FakeSocket())
    monkeypatch.setattr(remote_proxy.time, "perf_counter", lambda: next(times))

    result = remote_proxy.measure_proxy_node_latency(
        {"name": "stable", "type": "vless", "server": "stable.example.com", "port": 443},
        attempts=3,
        require_all=True,
    )

    assert result.ok is True
    assert result.latency_ms == 60


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


def test_fetch_proxy_subscription_without_persist_does_not_touch_managed_cache(
    monkeypatch,
    tmp_path,
):
    payload = b"proxies:\n  - { name: check, type: vless, server: example.com, port: 443 }\n"
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(
        remote_proxy,
        "_download_proxy_subscription",
        lambda **_kwargs: (payload, "application/yaml", "utf-8"),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        persist=False,
    )

    assert result.saved_path == ""
    assert result.nodes[0].node["name"] == "check"
    assert not (tmp_path / "proxy_subscriptions").exists()


def test_fetch_proxy_subscription_preserves_saved_profile_name(monkeypatch, tmp_path):
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

    remote_proxy.save_proxy_subscription_profile("香港家宽", "https://example.com/sub")
    remote_proxy.fetch_proxy_subscription("https://example.com/sub", retry_base_delay=0)

    profile = remote_proxy.active_proxy_subscription_profile()
    assert profile["name"] == "香港家宽"


def test_fetch_proxy_subscription_can_update_profile_without_activating(monkeypatch, tmp_path):
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
            return b"proxies:\n  - { name: refreshed, type: vless, server: one.example.com, port: 443 }\n"

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", lambda *_args, **_kwargs: Response())

    first = remote_proxy.save_proxy_subscription_profile("主力", "https://one.example/sub")
    second = remote_proxy.save_proxy_subscription_profile("备用", "https://two.example/sub")

    remote_proxy.fetch_proxy_subscription(
        "https://one.example/sub",
        retry_base_delay=0,
        profile_id=first["id"],
        activate=False,
    )

    state = remote_proxy.load_proxy_subscription_state()
    assert state["active_profile_id"] == second["id"]
    assert state["url"] == "https://two.example/sub"
    assert state["profiles"][first["id"]]["node_count"] == 1
    assert state["profiles"][first["id"]]["name"] == "主力"


def test_switching_subscription_profile_clears_optional_top_level_timestamps(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    first = remote_proxy.save_proxy_subscription_profile("主力", "https://one.example/sub")
    remote_proxy.save_proxy_subscription_profile_state(
        first["id"],
        node_latencies_updated_at="2026-01-01T00:00:00+00:00",
        node_qualities_updated_at="2026-01-01T00:00:00+00:00",
    )
    second = remote_proxy.save_proxy_subscription_profile("备用", "https://two.example/sub")

    remote_proxy.set_active_proxy_subscription_profile(second["id"])
    state = remote_proxy.load_proxy_subscription_state()

    assert state["active_profile_id"] == second["id"]
    assert "node_latencies_updated_at" not in state
    assert "node_qualities_updated_at" not in state
    assert remote_proxy.load_cached_proxy_subscription(state) is None


def test_fetch_proxy_subscription_restores_cache_when_state_save_fails(monkeypatch, tmp_path):
    payload = {
        "value": b"proxies:\n  - { name: old, type: vless, server: old.example.com, port: 443 }\n"
    }

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(
        remote_proxy,
        "_download_proxy_subscription",
        lambda **_kwargs: (payload["value"], "application/yaml", "utf-8"),
    )
    original = remote_proxy.fetch_proxy_subscription("https://example.com/sub")
    original_bytes = Path(original.saved_path).read_bytes()
    original_state = remote_proxy.load_proxy_subscription_state()
    payload["value"] = (
        b"proxies:\n  - { name: new, type: vless, server: new.example.com, port: 443 }\n"
    )
    monkeypatch.setattr(
        remote_proxy,
        "_persist_proxy_subscription_state",
        lambda _state: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        remote_proxy.fetch_proxy_subscription("https://example.com/sub")

    assert Path(original.saved_path).read_bytes() == original_bytes
    assert remote_proxy.load_proxy_subscription_state() == original_state


def test_import_proxy_subscription_restores_cache_when_state_save_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path / "storage")
    source = tmp_path / "fallback.yaml"
    source.write_text(
        "proxies:\n  - { name: old, type: vless, server: old.example.com, port: 443 }\n",
        encoding="utf-8",
    )
    original = remote_proxy.import_proxy_subscription_file(source)
    original_bytes = Path(original.saved_path).read_bytes()
    original_state = remote_proxy.load_proxy_subscription_state()
    source.write_text(
        "proxies:\n  - { name: new, type: vless, server: new.example.com, port: 443 }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        remote_proxy,
        "_persist_proxy_subscription_state",
        lambda _state: (_ for _ in ()).throw(PermissionError("state locked")),
    )

    with pytest.raises(PermissionError, match="state locked"):
        remote_proxy.import_proxy_subscription_file(source)

    assert Path(original.saved_path).read_bytes() == original_bytes
    assert remote_proxy.load_proxy_subscription_state() == original_state


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


def test_fetch_proxy_subscription_rotates_clash_client_signature_after_403(monkeypatch, tmp_path):
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
            return b"proxies:\n  - { name: compatible, type: vless, server: example.com, port: 443 }\n"

    user_agents = []

    def fake_urlopen(request, **_kwargs):
        user_agents.append(request.get_header("User-agent"))
        if len(user_agents) == 1:
            raise remote_proxy.HTTPError(request.full_url, 403, "Forbidden", {}, None)
        return Response()

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", fake_urlopen)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retry_base_delay=0,
    )

    assert result.nodes[0].node["name"] == "compatible"
    assert user_agents[:2] == list(remote_proxy.PROXY_SUBSCRIPTION_USER_AGENTS[:2])


def test_fetch_proxy_subscription_bypasses_failed_configured_proxy_without_changing_it(
    monkeypatch,
    tmp_path,
):
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
            return b"proxies:\n  - { name: direct, type: vless, server: example.com, port: 443 }\n"

    calls = []

    class DirectOpener:
        def open(self, _request, **_kwargs):
            calls.append("direct")
            return Response()

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: calls.append("configured-proxy")
        or (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr(
        remote_proxy.socket,
        "create_connection",
        lambda *_args, **_kwargs: _ConnectedProbeSocket(),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda handler: calls.append(type(handler).__name__) or DirectOpener(),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retry_base_delay=0,
    )

    assert result.nodes[0].node["name"] == "direct"
    assert calls == ["configured-proxy", "ProxyHandler", "direct"]


def test_fetch_proxy_subscription_detects_stale_loopback_environment_proxy(
    monkeypatch,
    tmp_path,
):
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
            return b"proxies:\n  - { name: direct, type: vless, server: example.com, port: 443 }\n"

    calls = []

    class DirectOpener:
        def open(self, _request, **_kwargs):
            calls.append("direct")
            return Response()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897"},
    )
    monkeypatch.setattr(
        remote_proxy.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError(10061, "refused")),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale proxy must be bypassed before urlopen")
        ),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda handler: calls.append(type(handler).__name__) or DirectOpener(),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retry_base_delay=0,
    )

    assert result.nodes[0].node["name"] == "direct"
    assert "HTTPS_PROXY" in result.proxy_warning
    assert "临时绕过" in result.proxy_warning
    assert calls == ["ProxyHandler", "direct"]
    assert os.environ.get("HTTPS_PROXY") == "http://127.0.0.1:17897"


def test_fetch_proxy_subscription_immediately_bypasses_stale_wininet_proxy(
    monkeypatch,
    tmp_path,
):
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
            return b"proxies:\n  - { name: direct, type: vless, server: example.com, port: 443 }\n"

    calls = []

    class DirectOpener:
        def open(self, _request, **_kwargs):
            calls.append("direct")
            return Response()

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7897"},
    )
    monkeypatch.setattr(
        "core.persistent_env._local_user_env_value_strict",
        lambda _name: None,
    )
    monkeypatch.setattr(
        remote_proxy.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale WinINET proxy must be bypassed before urlopen")
        ),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda handler: calls.append(type(handler).__name__) or DirectOpener(),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retry_base_delay=0,
    )

    assert result.nodes[0].node["name"] == "direct"
    assert "Windows 系统代理" in result.proxy_warning
    assert "临时绕过" in result.proxy_warning
    assert calls == ["ProxyHandler", "direct"]


def test_fetch_proxy_subscription_auto_cleans_program_owned_stale_proxy_before_download(
    monkeypatch,
    tmp_path,
):
    from core.api_tester import APITester

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
            return b"proxies:\n  - { name: recovered, type: vless, server: example.com, port: 443 }\n"

    calls = []
    reconciliation = {}

    class DirectOpener:
        def open(self, _request, **_kwargs):
            calls.append("direct")
            return Response()

    def reconcile(_cls, url, *, request_action):
        reconciliation["url"] = url
        reconciliation["action"] = request_action
        os.environ.pop("HTTPS_PROXY", None)
        return f"已自动清理变量（HTTPS_PROXY）；{request_action}"

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897"},
    )
    monkeypatch.setattr(
        remote_proxy.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError(10061, "refused")),
    )
    monkeypatch.setattr(
        APITester,
        "reconcile_invalid_local_proxy_for_request",
        classmethod(reconcile),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cleaned stale proxy must remain bypassed for this request")
        ),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda handler: calls.append(type(handler).__name__) or DirectOpener(),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retry_base_delay=0,
    )

    assert result.nodes[0].node["name"] == "recovered"
    assert "已自动清理变量" in result.proxy_warning
    assert "临时绕过" in result.proxy_warning
    assert reconciliation == {
        "url": "https://example.com/sub",
        "action": "本次订阅请求已临时绕过该代理并直连",
    }
    assert "HTTPS_PROXY" not in os.environ
    assert calls == ["ProxyHandler", "direct"]


def test_fetch_proxy_subscription_uses_real_owned_proxy_cleanup_contract(
    monkeypatch,
    tmp_path,
):
    from contextlib import nullcontext

    from core import api_tester
    from core.api_tester import APITester

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
            return b"proxies:\n  - { name: cleaned, type: vless, server: example.com, port: 443 }\n"

    class DirectOpener:
        def open(self, _request, **_kwargs):
            return Response()

    APITester._proxy_check_cache.clear()
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897"},
    )
    monkeypatch.setattr(
        remote_proxy.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError(10061, "refused")),
    )
    monkeypatch.setattr(
        "core.local_proxy._load_state",
        lambda: {
            "proxy_url": "http://127.0.0.1:17897",
            "managed_proxy_env": {
                "owner": "api-switcher",
                "proxy_url": "http://127.0.0.1:17897",
                "variables": ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"],
            },
        },
    )
    monkeypatch.setattr(
        "core.local_proxy._local_proxy_operation_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        "core.persistent_env._local_user_env_value_strict",
        lambda _name: None,
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("owned stale proxy must be cleaned and bypassed")
        ),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda _handler: DirectOpener(),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retry_base_delay=0,
    )

    assert result.nodes[0].node["name"] == "cleaned"
    assert "已自动清理变量" in result.proxy_warning
    assert "HTTPS_PROXY" not in os.environ


@pytest.mark.parametrize("status", [502, 503, 504])
def test_fetch_proxy_subscription_bypasses_configured_proxy_gateway_errors(
    monkeypatch,
    tmp_path,
    status,
):
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
            return b"proxies:\n  - { name: direct, type: vless, server: example.com, port: 443 }\n"

    calls = []

    class DirectOpener:
        def open(self, _request, **_kwargs):
            calls.append("direct")
            return Response()

    def configured_open(request, **_kwargs):
        calls.append("configured-proxy")
        raise remote_proxy.HTTPError(request.full_url, status, "Bad Gateway", {}, None)

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", configured_open)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr(
        remote_proxy.socket,
        "create_connection",
        lambda *_args, **_kwargs: _ConnectedProbeSocket(),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda handler: calls.append(type(handler).__name__) or DirectOpener(),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retries=1,
        retry_base_delay=0,
    )

    assert result.nodes[0].node["name"] == "direct"
    assert calls == ["configured-proxy", "ProxyHandler", "direct"]


def test_fetch_proxy_subscription_does_not_bypass_origin_gateway_error_without_proxy(
    monkeypatch,
    tmp_path,
):
    calls = []

    def configured_open(request, **_kwargs):
        calls.append("origin")
        raise remote_proxy.HTTPError(request.full_url, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", configured_open)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct opener must not be created without a configured proxy")
        ),
    )

    with pytest.raises(RuntimeError, match="HTTP Error 502"):
        remote_proxy.fetch_proxy_subscription(
            "https://example.com/sub",
            retries=1,
            retry_base_delay=0,
        )

    assert calls == ["origin"]


def test_fetch_proxy_subscription_uses_isolated_managed_pool_after_direct_failure(
    monkeypatch,
    tmp_path,
):
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
            return b"proxies:\n  - { name: recovered, type: vless, server: example.com, port: 443 }\n"

    calls = []

    class RecoveryOpener:
        def open(self, _request, **_kwargs):
            calls.append("managed-proxy")
            return Response()

    class RecoverySession:
        def __enter__(self):
            calls.append("start-isolated-session")
            return "http://127.0.0.1:17897"

        def __exit__(self, *_args):
            calls.append("stop-isolated-session")
            return False

    def build_opener(handler):
        assert isinstance(handler, remote_proxy._NoBypassProxyHandler)
        calls.append(dict(handler.proxies))
        return RecoveryOpener()

    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: calls.append("direct")
        or (_ for _ in ()).throw(TimeoutError("direct route timed out")),
    )
    monkeypatch.setattr(remote_proxy.urlrequest, "build_opener", build_opener)

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retries=1,
        retry_base_delay=0,
        recovery_proxy_provider=lambda _timeout: calls.append("create-isolated-session")
        or RecoverySession(),
    )

    assert result.nodes[0].node["name"] == "recovered"
    assert calls == [
        "direct",
        "create-isolated-session",
        "start-isolated-session",
        {"http": "http://127.0.0.1:17897", "https": "http://127.0.0.1:17897"},
        "managed-proxy",
        "stop-isolated-session",
    ]
    assert "现有受管节点的隔离代理" in result.proxy_warning
    assert "一次性代理已退出" in result.proxy_warning


def test_fetch_proxy_subscription_recovery_rotates_existing_nodes(monkeypatch, tmp_path):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"proxies:\n  - { name: route-2, type: vless, server: example.com, port: 443 }\n"

    route = [0]
    selections = []
    requests = []

    class RecoverySession:
        proxy_url = "http://127.0.0.1:19001"
        route_count = 3

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def select_route(self, index, timeout_seconds):
            selections.append((index, timeout_seconds))
            route[0] = index

    class RecoveryOpener:
        def open(self, request, **_kwargs):
            requests.append((route[0], request.get_header("User-agent")))
            if route[0] == 0:
                raise remote_proxy.HTTPError(
                    request.full_url,
                    451,
                    "Unavailable For Legal Reasons",
                    {},
                    None,
                )
            return Response()

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("direct timeout")),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda handler: RecoveryOpener()
        if isinstance(handler, remote_proxy._NoBypassProxyHandler)
        else (_ for _ in ()).throw(AssertionError("unexpected direct opener")),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retries=1,
        retry_base_delay=0,
        recovery_proxy_provider=lambda _timeout: RecoverySession(),
    )

    assert result.nodes[0].node["name"] == "route-2"
    assert [item[0] for item in requests] == [0, 1]
    assert selections and selections[0][0] == 1
    assert 0 < selections[0][1] <= 1.5
    assert "尝试 2 个已有节点" in result.proxy_warning


def test_fetch_proxy_subscription_recovery_rotates_client_signatures_after_403(
    monkeypatch,
    tmp_path,
):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"proxies:\n  - { name: ua-compatible, type: vless, server: example.com, port: 443 }\n"

    user_agents = []

    class RecoveryOpener:
        def open(self, request, **_kwargs):
            user_agents.append(request.get_header("User-agent"))
            if len(user_agents) < 3:
                raise remote_proxy.HTTPError(request.full_url, 403, "Forbidden", {}, None)
            return Response()

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("direct timeout")),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda handler: RecoveryOpener()
        if isinstance(handler, remote_proxy._NoBypassProxyHandler)
        else (_ for _ in ()).throw(AssertionError("unexpected direct opener")),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retries=1,
        retry_base_delay=0,
        recovery_proxy_provider=lambda _timeout: "http://127.0.0.1:19001",
    )

    assert result.nodes[0].node["name"] == "ua-compatible"
    assert user_agents == list(remote_proxy.PROXY_SUBSCRIPTION_USER_AGENTS[:3])


def test_fetch_proxy_subscription_retries_http_200_block_page_through_recovery(
    monkeypatch,
    tmp_path,
):
    class Response:
        def __init__(self, payload, content_type):
            self.payload = payload
            self.headers = {"Content-Type": content_type}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return self.payload

    class RecoveryOpener:
        def open(self, _request, **_kwargs):
            return Response(
                b"proxies:\n  - { name: recovered-html, type: vless, server: example.com, port: 443 }\n",
                "application/yaml",
            )

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: Response(
            b"<!doctype html><html><title>Access denied</title></html>",
            "text/html",
        ),
    )
    monkeypatch.setattr(remote_proxy.urlrequest, "build_opener", lambda _handler: RecoveryOpener())

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retries=1,
        retry_base_delay=0,
        recovery_proxy_provider=lambda _timeout: "http://127.0.0.1:19001",
    )

    assert result.nodes[0].node["name"] == "recovered-html"
    assert "隔离代理完成更新" in result.proxy_warning


def test_fetch_proxy_subscription_retries_direct_recovery_with_next_signature(
    monkeypatch,
    tmp_path,
):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"proxies:\n  - { name: direct-ua-2, type: vless, server: example.com, port: 443 }\n"

    direct_user_agents = []

    class DirectOpener:
        def open(self, request, **_kwargs):
            direct_user_agents.append(request.get_header("User-agent"))
            if len(direct_user_agents) == 1:
                raise remote_proxy.HTTPError(request.full_url, 403, "Forbidden", {}, None)
            return Response()

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("proxy disconnected")),
    )
    monkeypatch.setattr(remote_proxy.urlrequest, "build_opener", lambda _handler: DirectOpener())

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retries=2,
        retry_base_delay=0,
    )

    assert result.nodes[0].node["name"] == "direct-ua-2"
    assert direct_user_agents == list(remote_proxy.PROXY_SUBSCRIPTION_USER_AGENTS[:2])


def test_fetch_proxy_subscription_does_not_inspect_recovery_proxy_when_primary_succeeds(
    monkeypatch,
    tmp_path,
):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"proxies:\n  - { name: direct, type: vless, server: example.com, port: 443 }\n"

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", lambda *_args, **_kwargs: Response())

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retries=1,
        recovery_proxy_provider=lambda _timeout: (_ for _ in ()).throw(
            AssertionError("successful primary route must stay lazy")
        ),
    )

    assert result.nodes[0].node["name"] == "direct"
    assert result.proxy_warning == ""


def test_fetch_proxy_subscription_strict_can_use_verified_managed_recovery_proxy(
    monkeypatch,
    tmp_path,
):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"proxies:\n  - { name: strict-recovery, type: vless, server: example.com, port: 443 }\n"

    calls = []

    class RecoveryOpener:
        def open(self, _request, **_kwargs):
            calls.append("managed-proxy")
            return Response()

    def build_opener(handler):
        assert isinstance(handler, remote_proxy._NoBypassProxyHandler)
        calls.append(dict(handler.proxies))
        return RecoveryOpener()

    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict mode must not connect directly")
        ),
    )
    monkeypatch.setattr(remote_proxy.urlrequest, "build_opener", build_opener)

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retries=1,
        allow_direct_fallback=False,
        recovery_proxy_provider=lambda _timeout: "http://localhost:17897",
    )

    assert result.nodes[0].node["name"] == "strict-recovery"
    assert calls == [
        {"http": "http://localhost:17897", "https": "http://localhost:17897"},
        "managed-proxy",
    ]
    assert "现有受管节点的隔离代理" in result.proxy_warning


@pytest.mark.parametrize(
    "recovery_url",
    [
        "https://127.0.0.1:17897",
        "http://proxy.example.com:17897",
        "http://user:secret@127.0.0.1:17897",
        "http://127.0.0.1:17897/path",
    ],
)
def test_fetch_proxy_subscription_rejects_untrusted_recovery_proxy_urls(
    monkeypatch,
    tmp_path,
    recovery_url,
):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("direct failed")),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("untrusted recovery URL must not be used")
        ),
    )

    with pytest.raises(RuntimeError, match="direct failed"):
        remote_proxy.fetch_proxy_subscription(
            "https://example.com/sub",
            retries=1,
            retry_base_delay=0,
            recovery_proxy_provider=lambda _timeout: recovery_url,
        )


def test_fetch_proxy_subscription_can_forbid_direct_fallback(monkeypatch, tmp_path):
    calls = []

    class StrictProxyOpener:
        def open(self, _request, **_kwargs):
            calls.append("configured-proxy")
            raise OSError("proxy connection refused")

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict privacy must not use environment urlopen")
        ),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr(remote_proxy.urlrequest, "build_opener", lambda _handler: StrictProxyOpener())

    with pytest.raises(RuntimeError, match="已禁止绕过代理直连回退"):
        remote_proxy.fetch_proxy_subscription(
            "https://example.com/sub",
            retries=1,
            retry_base_delay=0,
            allow_direct_fallback=False,
        )

    assert calls == ["configured-proxy"]


def test_fetch_proxy_subscription_strict_ignores_no_proxy_star(monkeypatch, tmp_path):
    calls = []

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
            return b"proxies:\n  - { name: strict, type: vless, server: example.com, port: 443 }\n"

    class StrictProxyOpener:
        def open(self, _request, **_kwargs):
            calls.append("explicit-proxy")
            return Response()

    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {
            "https": "http://127.0.0.1:7890",
            "no": "*",
        },
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("NO_PROXY=* must never reach environment urlopen")
        ),
    )

    def build_opener(handler):
        assert isinstance(handler, remote_proxy._NoBypassProxyHandler)
        calls.append(dict(handler.proxies))
        return StrictProxyOpener()

    monkeypatch.setattr(remote_proxy.urlrequest, "build_opener", build_opener)

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        retries=1,
        allow_direct_fallback=False,
    )

    assert result.nodes[0].node["name"] == "strict"
    assert calls == [
        {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        "explicit-proxy",
    ]


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_no_bypass_proxy_handler_uses_proxy_even_when_no_proxy_is_star(
    monkeypatch,
    scheme,
):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    request = remote_proxy.urlrequest.Request(f"{scheme}://example.com/sub")
    request.timeout = 7
    handler = remote_proxy._NoBypassProxyHandler(
        {scheme: "http://127.0.0.1:7890"}
    )

    # Exercise ProxyHandler's actual dynamically installed per-scheme entry
    # point, not merely a mocked opener or a direct helper call.
    result = getattr(handler, f"{scheme}_open")(request)

    assert result is None
    assert request.host == "127.0.0.1:7890"
    if scheme == "http":
        assert request.has_proxy() is True
        assert request.selector == "http://example.com/sub"
        assert request._tunnel_host is None
    else:
        assert request.selector == "/sub"
        # For an HTTPS target behind an HTTP proxy urllib's HTTPSHandler uses
        # Request._tunnel_host to issue CONNECT to the original destination.
        assert request._tunnel_host == "example.com"


def test_fetch_proxy_subscription_strict_without_loopback_proxy_fails_closed(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict mode must not connect directly")
        ),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict mode must not build a direct opener")
        ),
    )

    with pytest.raises(RuntimeError, match="未配置代理.*拒绝直连"):
        remote_proxy.fetch_proxy_subscription(
            "https://example.com/sub",
            retries=1,
            allow_direct_fallback=False,
        )


def test_gateway_error_direct_recovery_stays_inside_total_deadline(
    monkeypatch,
    tmp_path,
):
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
            return b"proxies:\n  - { name: direct, type: vless, server: example.com, port: 443 }\n"

    clock = [100.0]
    calls = []

    def configured_open(request, *, timeout):
        calls.append(("configured-proxy", timeout))
        clock[0] += timeout
        raise remote_proxy.HTTPError(request.full_url, 502, "Bad Gateway", {}, None)

    class DirectOpener:
        def open(self, _request, *, timeout):
            calls.append(("direct", timeout))
            return Response()

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", configured_open)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr(
        remote_proxy.socket,
        "create_connection",
        lambda *_args, **_kwargs: _ConnectedProbeSocket(),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda _handler: DirectOpener(),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        timeout=10,
        retries=1,
        retry_base_delay=0,
    )

    assert result.nodes[0].node["name"] == "direct"
    assert [name for name, _timeout in calls] == ["configured-proxy", "direct"]
    assert 0 < calls[0][1] < 10
    assert 0 < calls[1][1] <= 10 - calls[0][1]


def test_fetch_proxy_subscription_reserves_deadline_for_direct_timeout_recovery(
    monkeypatch,
    tmp_path,
):
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
            return b"proxies:\n  - { name: direct, type: vless, server: example.com, port: 443 }\n"

    clock = [100.0]
    calls = []

    def configured_open(_request, *, timeout):
        calls.append(("configured-proxy", timeout))
        clock[0] += timeout
        raise TimeoutError("timed out after consuming configured-proxy budget")

    class DirectOpener:
        def open(self, _request, *, timeout):
            calls.append(("direct", timeout))
            return Response()

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", configured_open)
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr(
        remote_proxy.socket,
        "create_connection",
        lambda *_args, **_kwargs: _ConnectedProbeSocket(),
    )
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda _handler: DirectOpener(),
    )

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        timeout=10,
        retries=1,
        retry_base_delay=0,
    )

    assert result.nodes[0].node["name"] == "direct"
    assert [name for name, _timeout in calls] == ["configured-proxy", "direct"]
    assert 0 < calls[0][1] < 10
    assert 0 < calls[1][1] <= 10 - calls[0][1]


def test_proxy_subscription_chunked_body_read_enforces_monotonic_deadline(monkeypatch):
    clock = [100.0]
    closed = []

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.append(True)
            return False

        def read1(self, _size):
            clock[0] += 0.4
            return b"x"

    monkeypatch.setattr(remote_proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    request = remote_proxy.urlrequest.Request("https://example.com/sub")

    with pytest.raises(TimeoutError, match="总等待时间"):
        remote_proxy._open_proxy_subscription_request(
            request,
            timeout=1,
            deadline=101.0,
            max_bytes=1024,
        )

    assert closed == [True]
    assert clock[0] >= 101.0


def test_import_proxy_subscription_file_persists_all_nodes_in_managed_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path / "storage")
    source = tmp_path / "clash.yaml"
    source.write_text(
        "proxies:\n"
        "  - { name: US-1, type: vless, server: us.example.com, port: 443 }\n"
        "  - { name: HK-1, type: vless, server: hk.example.com, port: 443 }\n",
        encoding="utf-8",
    )

    result = remote_proxy.import_proxy_subscription_file(source)
    state = remote_proxy.load_proxy_subscription_state()

    assert [item.node["name"] for item in result.nodes] == ["US-1", "HK-1"]
    assert state["url"] == ""
    assert state["source_path"] == str(source.resolve())
    assert state["node_count"] == 2
    assert Path(state["saved_path"]).is_file()
    assert Path(state["saved_path"]) != source
    assert [item.node["name"] for item in remote_proxy.automatic_proxy_subscription_nodes(result.nodes)] == ["US-1"]

    source.unlink()
    remote_proxy.clear_proxy_subscription_state_cache()
    cached = remote_proxy.load_cached_proxy_subscription()

    assert cached is not None
    assert [item.node["name"] for item in cached.nodes] == ["US-1", "HK-1"]


def test_import_proxy_subscription_file_rejects_oversized_input(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path / "storage")
    source = tmp_path / "too-large.yaml"
    source.write_bytes(b"proxies:\n" + b"x" * 128)

    with pytest.raises(ValueError, match="超过"):
        remote_proxy.import_proxy_subscription_file(source, max_bytes=32)


def test_read_proxy_node_text_file_is_bounded_and_decodes_utf8_bom(tmp_path):
    source = tmp_path / "node.yaml"
    payload = b"\xef\xbb\xbf{name: test, type: vless, server: example.com, port: 443}"
    source.write_bytes(payload)

    text = remote_proxy.read_proxy_node_text_file(source, max_bytes=len(payload))

    assert text.startswith("{name: test")
    with pytest.raises(ValueError, match="超过"):
        remote_proxy.read_proxy_node_text_file(source, max_bytes=len(payload) - 1)


def test_local_file_profile_can_be_renamed_switched_and_keeps_empty_url(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path / "storage")
    source = tmp_path / "offline.yaml"
    source.write_text(
        "proxies:\n  - { name: offline, type: vless, server: us.example.com, port: 443 }\n",
        encoding="utf-8",
    )
    remote_proxy.import_proxy_subscription_file(source)
    local_profile = remote_proxy.active_proxy_subscription_profile()

    renamed = remote_proxy.rename_proxy_subscription_profile(local_profile["id"], "离线备用")
    remote_profile = remote_proxy.save_proxy_subscription_profile("线上", "https://example.com/sub")
    switched = remote_proxy.set_active_proxy_subscription_profile(local_profile["id"])
    state = remote_proxy.load_proxy_subscription_state()

    assert renamed["name"] == "离线备用"
    assert switched["name"] == "离线备用"
    assert state["url"] == ""
    assert state["source_path"] == str(source.resolve())
    assert remote_proxy.load_cached_proxy_subscription(state).nodes[0].node["name"] == "offline"

    remote_proxy.delete_proxy_subscription_profile(remote_profile["id"])
    state = remote_proxy.load_proxy_subscription_state()
    assert state["active_profile_id"] == local_profile["id"]
    assert state["url"] == ""


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


def test_fetch_proxy_subscription_rejects_oversized_gzip_after_limited_decode(monkeypatch, tmp_path):
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
            return gzip.compress(b"proxies:\n" + b"a" * 2048)

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(ValueError, match="解压后超过"):
        remote_proxy.fetch_proxy_subscription("https://example.com/sub", max_bytes=1024, retry_base_delay=0)


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


def test_load_cached_proxy_subscription_rejects_oversized_tampered_path(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy, "PROXY_SUBSCRIPTION_MAX_BYTES", 64)
    remote_proxy.clear_proxy_subscription_state_cache()
    content_path = tmp_path / "oversized-subscription.yaml"
    content_path.write_bytes(b"proxies:\n" + b"x" * 128)
    state = {
        "url": "https://example.com/sub",
        "saved_path": str(content_path),
        "last_fetched_at": "2026-05-26T00:00:00+00:00",
    }

    assert remote_proxy.load_cached_proxy_subscription(state) is None


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


def test_load_cached_proxy_subscription_reuses_parsed_nodes(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    remote_proxy.clear_proxy_subscription_state_cache()
    cache_dir = tmp_path / "proxy_subscriptions"
    cache_dir.mkdir()
    content_path = cache_dir / "subscription-cache.yaml"
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
    calls = {"count": 0}
    original_parse = remote_proxy.parse_proxy_subscription_content

    def counting_parse(text):
        calls["count"] += 1
        return original_parse(text)

    monkeypatch.setattr(remote_proxy, "parse_proxy_subscription_content", counting_parse)

    first = remote_proxy.load_cached_proxy_subscription()
    second = remote_proxy.load_cached_proxy_subscription()

    assert first is not None
    assert second is not None
    assert first.nodes[0].node["name"] == "cached"
    assert second.nodes[0].node["name"] == "cached"
    assert calls["count"] == 1


def test_proxy_subscription_profiles_migrate_legacy_state(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    state_dir = tmp_path / "proxy_subscriptions"
    state_dir.mkdir()
    state_path = state_dir / "subscription_state.json"
    state_path.write_text(
        json.dumps({
            "url": "https://example.com/sub",
            "saved_path": str(state_dir / "subscription.yaml"),
            "node_count": 3,
            "selected_node_key": "picked",
        }),
        encoding="utf-8",
    )
    remote_proxy.clear_proxy_subscription_state_cache()

    state = remote_proxy.load_proxy_subscription_state()
    profiles = remote_proxy.list_proxy_subscription_profiles()

    assert state["url"] == "https://example.com/sub"
    assert len(profiles) == 1
    assert profiles[0]["active"] is True
    assert profiles[0]["url"] == "https://example.com/sub"
    assert profiles[0]["node_count"] == 3
    assert profiles[0]["selected_node_key"] == "picked"


def test_proxy_subscription_profiles_switch_active_state(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)

    first = remote_proxy.save_proxy_subscription_profile("主力", "https://one.example/sub")
    remote_proxy.save_proxy_subscription_state(
        saved_path=str(tmp_path / "one.yaml"),
        node_count=1,
        node_latencies={"one": {"ok": True, "latency_ms": 20}},
    )
    second = remote_proxy.save_proxy_subscription_profile("备用", "https://two.example/sub")
    remote_proxy.save_proxy_subscription_state(
        saved_path=str(tmp_path / "two.yaml"),
        node_count=2,
        node_latencies={"two": {"ok": True, "latency_ms": 30}},
    )

    state = remote_proxy.load_proxy_subscription_state()
    assert state["active_profile_id"] == second["id"]
    assert state["url"] == "https://two.example/sub"
    assert state["node_count"] == 2
    assert set(remote_proxy.load_proxy_subscription_latencies()) == {"two"}

    remote_proxy.set_active_proxy_subscription_profile(first["id"])

    state = remote_proxy.load_proxy_subscription_state()
    assert state["active_profile_id"] == first["id"]
    assert state["url"] == "https://one.example/sub"
    assert state["node_count"] == 1
    assert set(remote_proxy.load_proxy_subscription_latencies()) == {"one"}


def test_proxy_subscription_cache_reads_from_state_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    cache_dir = tmp_path / "proxy_subscriptions"
    cache_dir.mkdir()
    first_path = cache_dir / "one.yaml"
    second_path = cache_dir / "two.yaml"
    first_path.write_text(
        "proxies:\n  - { name: one, type: vless, server: one.example.com, port: 443 }\n",
        encoding="utf-8",
    )
    second_path.write_text(
        "proxies:\n  - { name: two, type: vless, server: two.example.com, port: 443 }\n",
        encoding="utf-8",
    )

    first = remote_proxy.save_proxy_subscription_profile("主力", "https://one.example/sub")
    remote_proxy.save_proxy_subscription_state(
        saved_path=str(first_path),
        node_count=1,
        node_latencies={"one": {"ok": True, "latency_ms": 20}},
        node_qualities={"one": {"ok": True, "quality_score": 90, "quality_label": "高质量"}},
    )
    first_state = remote_proxy.load_proxy_subscription_state()
    second = remote_proxy.save_proxy_subscription_profile("备用", "https://two.example/sub")
    remote_proxy.save_proxy_subscription_state(
        saved_path=str(second_path),
        node_count=1,
        node_latencies={"two": {"ok": True, "latency_ms": 30}},
        node_qualities={"two": {"ok": True, "quality_score": 60, "quality_label": "普通"}},
    )

    assert remote_proxy.load_proxy_subscription_state()["active_profile_id"] == second["id"]
    cached = remote_proxy.load_cached_proxy_subscription(first_state)

    assert first["id"] != second["id"]
    assert cached is not None
    assert cached.nodes[0].node["name"] == "one"
    assert set(remote_proxy.load_proxy_subscription_latencies(first_state)) == {"one"}
    assert set(remote_proxy.load_proxy_subscription_qualities(first_state)) == {"one"}


def test_delete_proxy_subscription_profile_selects_remaining_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)

    first = remote_proxy.save_proxy_subscription_profile("主力", "https://one.example/sub")
    second = remote_proxy.save_proxy_subscription_profile("备用", "https://two.example/sub")

    active = remote_proxy.delete_proxy_subscription_profile(second["id"])

    assert active["id"] == first["id"]
    state = remote_proxy.load_proxy_subscription_state()
    assert state["url"] == "https://one.example/sub"
    assert len(remote_proxy.list_proxy_subscription_profiles()) == 1


def test_delete_proxy_subscription_profile_prunes_only_unreferenced_managed_cache(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    cache_dir = tmp_path / "proxy_subscriptions"
    cache_dir.mkdir()
    first_cache = cache_dir / "subscription-first.yaml"
    second_cache = cache_dir / "subscription-second.txt"
    unrelated = cache_dir / "keep-me.yaml"
    first_cache.write_text("first-secret", encoding="utf-8")
    second_cache.write_text("second-secret", encoding="utf-8")
    unrelated.write_text("unrelated", encoding="utf-8")

    first = remote_proxy.save_proxy_subscription_profile("主力", "https://one.example/sub")
    remote_proxy.save_proxy_subscription_state(saved_path=str(first_cache), node_count=1)
    second = remote_proxy.save_proxy_subscription_profile("备用", "https://two.example/sub")
    remote_proxy.save_proxy_subscription_state(saved_path=str(second_cache), node_count=1)

    remote_proxy.delete_proxy_subscription_profile(second["id"])

    assert first_cache.is_file()
    assert not second_cache.exists()
    assert unrelated.is_file()
    assert remote_proxy.load_proxy_subscription_state()["active_profile_id"] == first["id"]


def test_delete_last_proxy_subscription_profile_clears_active_url(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)

    profile = remote_proxy.save_proxy_subscription_profile("主力", "https://one.example/sub")
    remote_proxy.delete_proxy_subscription_profile(profile["id"])

    state = remote_proxy.load_proxy_subscription_state()
    assert state.get("active_profile_id") == ""
    assert state.get("url") in (None, "")
    assert remote_proxy.list_proxy_subscription_profiles() == []


def test_proxy_subscription_state_persists_auto_refresh_and_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    node = remote_proxy.parse_proxy_node("{ name: picked, type: vless, server: example.com, port: 443 }")

    remote_proxy.set_proxy_subscription_auto_refresh(True)
    remote_proxy.set_proxy_subscription_selected_node(node)

    state = remote_proxy.load_proxy_subscription_state()
    assert state["auto_refresh"] is True
    assert state["selected_node_display"] == "picked (vless://example.com:443)"
    assert state["selected_node_key"] == remote_proxy.proxy_node_key(node)


def test_proxy_subscription_auto_refresh_scopes_are_independent(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)

    remote_proxy.set_proxy_subscription_auto_refresh(True)
    assert remote_proxy.proxy_subscription_auto_refresh_enabled("local") is True
    assert remote_proxy.proxy_subscription_auto_refresh_enabled("ssh") is True

    remote_proxy.set_proxy_subscription_auto_refresh(False, scope="local")
    remote_proxy.set_proxy_subscription_auto_refresh(True, scope="ssh")

    state = remote_proxy.load_proxy_subscription_state()
    assert state["auto_refresh"] is True
    assert state["local_auto_refresh_enabled"] is False
    assert state["ssh_auto_refresh_enabled"] is True
    assert remote_proxy.proxy_subscription_auto_refresh_enabled("local") is False
    assert remote_proxy.proxy_subscription_auto_refresh_enabled("ssh") is True


def test_proxy_subscription_state_cache_reuses_reads_and_detects_external_write(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    state_dir = tmp_path / "proxy_subscriptions"
    state_dir.mkdir()
    state_path = state_dir / "subscription_state.json"
    state_path.write_text(json.dumps({"url": "https://example.com/one"}), encoding="utf-8")
    remote_proxy.clear_proxy_subscription_state_cache()

    original_read_text = type(state_path).read_text
    read_count = {"value": 0}

    def counting_read_text(self, *args, **kwargs):
        if self == state_path:
            read_count["value"] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(state_path), "read_text", counting_read_text)

    assert remote_proxy.load_proxy_subscription_state()["url"] == "https://example.com/one"
    assert remote_proxy.load_proxy_subscription_state()["url"] == "https://example.com/one"
    assert read_count["value"] == 1

    state_path.write_text(
        json.dumps({"url": "https://example.com/two", "node_count": 2}),
        encoding="utf-8",
    )

    assert remote_proxy.load_proxy_subscription_state()["url"] == "https://example.com/two"
    assert read_count["value"] == 2


def test_corrupt_proxy_subscription_state_is_quarantined(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    state_dir = tmp_path / "proxy_subscriptions"
    state_dir.mkdir()
    state_path = state_dir / "subscription_state.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    remote_proxy.clear_proxy_subscription_state_cache()

    state = remote_proxy.load_proxy_subscription_state()

    assert state == {}
    assert not state_path.exists()
    corrupt_files = list(state_dir.glob("subscription_state.json.corrupt-*"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == "{not valid json"

    remote_proxy.save_proxy_subscription_profile("恢复", "https://example.com/restored")
    restored = remote_proxy.load_proxy_subscription_state()

    assert restored["url"] == "https://example.com/restored"
    assert state_path.exists()


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


def test_inspect_ai_proxy_strict_detail_does_not_claim_live_config_loaded(monkeypatch):
    fake_client = object()
    strict_config = remote_proxy.build_mihomo_config(
        {"name": "node", "type": "vless", "server": "example.com", "port": 443},
        strict_privacy=True,
    )
    output = "\n".join(
        (
            "installed=yes",
            "running=yes",
            "pid_running=yes",
            "pid_managed=yes",
            "port_listening=yes",
            "env_file=yes",
            "start_script=yes",
            "shell_entrypoints=1",
            "vscode_entrypoints=1",
            "config_present=yes",
            "config_owned=yes",
            "config_legacy=no",
            "config=/home/me/.config/mihomo/config.yaml",
        )
    )
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, fake_client))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/me")
    monkeypatch.setattr(
        remote_proxy.remote_config,
        "_expand_remote_path",
        lambda _client, path: str(path).replace("~", "/home/me", 1),
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda *_args, **_kwargs: (0, output, ""),
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "read_remote_file",
        lambda *_args, **_kwargs: strict_config,
    )

    status = remote_proxy.inspect_ai_proxy("server")

    assert status.running is True
    assert "磁盘受管配置符合应用层严格隐私契约" in status.detail
    assert "不能单独证明当前进程内存已加载" in status.detail
    assert "应用层严格隐私已开启" not in status.detail


@pytest.mark.parametrize(
    (
        "config_owned",
        "pid_running",
        "pid_managed",
        "port_listening",
        "expected_running",
        "expected_detail",
    ),
    [
        ("yes", "yes", "yes", "yes", True, ""),
        ("no", "yes", "yes", "yes", False, "不是本工具配置"),
        ("yes", "no", "unknown", "yes", False, "端口已监听，但 pid 文件未更新"),
        ("yes", "yes", "no", "yes", False, "pid 文件指向非本工具受管"),
        ("yes", "yes", "unknown", "yes", False, "无法确认 pid 进程"),
        ("yes", "yes", "yes", "no", False, "进程存在，但端口未监听"),
        ("yes", "yes", "yes", "unknown", False, "缺少 ss/netstat"),
    ],
)
def test_inspect_ai_proxy_running_requires_owned_config_managed_pid_and_port(
    monkeypatch,
    config_owned,
    pid_running,
    pid_managed,
    port_listening,
    expected_running,
    expected_detail,
):
    fake_client = object()
    commands = []
    managed_config = remote_proxy.build_mihomo_config(
        {"name": "node", "type": "vless", "server": "example.com", "port": 443}
    )
    output = "\n".join(
        (
            "installed=yes",
            # Deliberately claim yes: the Python side must independently
            # enforce the same fail-closed conjunction as the shell.
            "running=yes",
            f"pid_running={pid_running}",
            f"pid_managed={pid_managed}",
            f"port_listening={port_listening}",
            "env_file=yes",
            "start_script=yes",
            "shell_entrypoints=1",
            "vscode_entrypoints=1",
            "config_present=yes",
            f"config_owned={config_owned}",
            f"config_legacy={'no' if config_owned == 'yes' else 'yes'}",
            "config=/home/me/.config/mihomo/config.yaml",
        )
    )
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, fake_client))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/me")
    monkeypatch.setattr(
        remote_proxy.remote_config,
        "_expand_remote_path",
        lambda _client, path: str(path).replace("~", "/home/me", 1),
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda _client, command, **_kwargs: commands.append(command) or (0, output, ""),
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "read_remote_file",
        lambda *_args, **_kwargs: managed_config,
    )

    status = remote_proxy.inspect_ai_proxy("server")

    assert status.running is expected_running
    assert status.installed is (config_owned == "yes")
    if expected_detail:
        assert expected_detail in status.detail
    assert '[ "$config_owned" = "yes" ]' in commands[0]
    assert '[ "$pid_managed" = "yes" ]' in commands[0]
    assert '[ "$port_listening" = "yes" ]' in commands[0]
    assert "port_listening=yes && running=yes" not in commands[0]


def test_start_script_checks_port_with_netstat_when_ss_is_missing():
    script = remote_proxy._build_start_script("/home/me/.config/mihomo", "/home/me/.config/api-switcher", "/home/me/bin", 7890)

    assert "command -v netstat" in script
    assert "pid_managed()" in script
    assert "if ! command -v ps" in script
    assert "Without process identity" in script
    assert "return 1" in script
    assert "cleanup_new_process()" in script
    assert script.count("cleanup_new_process\n") == 2
    assert 'rm -f "$PID_FILE"' in script
    assert "kill -9" in script
    assert "pid file does not identify this tool's managed process" in script
    assert "port $PORT is already listening before starting mihomo" in script
    assert "command -v clash-meta" in script


def test_shell_profile_writer_rejects_unclosed_managed_block(monkeypatch):
    commands = []
    monkeypatch.setattr(remote_proxy.ssh_manager, "write_remote_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda _client, command, **_kwargs: commands.append(command) or (0, "", ""),
    )

    remote_proxy._write_shell_profile_block(
        object(),
        "/home/me",
        "/home/me/.config/api-switcher/ai-proxy.env",
        "/home/me/.config/api-switcher/start-ai-proxy.sh",
        7890,
    )

    assert commands
    assert "END {if (skip == 1) exit 2}" in commands[0]
    assert "无法安全更新" in commands[0]


def test_remote_install_command_retries_mihomo_downloads_with_user_agent():
    command = remote_proxy._build_install_command(
        "/home/me",
        "/home/me/.config/mihomo",
        "/home/me/.config/api-switcher",
        "/home/me/.local/bin",
        "/home/me/.config/api-switcher/start-ai-proxy.sh",
        7890,
    )

    assert "User-Agent" in command
    assert "API-Switcher/1.0" in command
    assert "for attempt in range(1, 4)" in command
    assert "failed after 3 attempts" in command
    assert "command -v clash-meta" in command
    assert "ProxyHandler({})" in command
    assert "release asset SHA-256 mismatch" in command
    assert "hmac.compare_digest" in command
    assert "os.replace(candidate_path, target)" in command
    assert "kernel_version=" in command


def test_remote_reload_command_calls_mihomo_controller():
    command = remote_proxy._build_reload_command("/home/me/.config/mihomo/config.yaml", 7890)

    assert "127.0.0.1:8890/configs?force=true" in command
    assert '"path": "/home/me/.config/mihomo/config.yaml"' in command
    assert 'method="PUT"' in command
    assert "urllib.request.ProxyHandler({})" in command
    assert "curl --noproxy '*'" in command


def test_local_reload_controller_explicitly_bypasses_environment_proxy(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mixed-port: 17897\n", encoding="utf-8")
    captured = {}

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Opener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return Response()

    def build_opener(handler):
        captured["handler"] = handler
        return Opener()

    monkeypatch.setattr(local_proxy.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(
        local_proxy.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("controller must not use environment-aware urlopen"),
    )

    local_proxy._reload_local_mihomo_config(config_path, 17897)

    assert isinstance(captured["handler"], local_proxy.urllib.request.ProxyHandler)
    assert captured["handler"].proxies == {}
    assert captured["url"] == "http://127.0.0.1:18897/configs?force=true"
    assert captured["timeout"] == 8


def test_reload_ai_proxy_restores_config_when_controller_fails(monkeypatch):
    writes = []
    reload_results = iter(
        [
            (7, "", "connection refused"),
            (0, "", ""),
        ]
    )
    fake_client = object()

    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, fake_client))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/me")
    monkeypatch.setattr(remote_proxy.ssh_manager, "read_remote_file", lambda *_args, **_kwargs: "old config")
    monkeypatch.setattr(remote_proxy.ssh_manager, "write_remote_file", lambda _client, _path, content, **_kwargs: writes.append(content))
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda *_args, **_kwargs: next(reload_results),
    )

    with pytest.raises(RuntimeError, match="已强制重载旧配置"):
        remote_proxy.reload_ai_proxy("server", "{ name: node, type: vless, server: example.com, port: 443 }")

    assert writes == [
        remote_proxy.build_mihomo_config(
            remote_proxy.parse_proxy_node(
                "{ name: node, type: vless, server: example.com, port: 443 }"
            ),
            7890,
        ),
        "old config",
    ]


def test_reload_ai_proxy_reports_incomplete_runtime_rollback(monkeypatch):
    fake_client = object()
    writes = []
    reload_results = iter(
        [
            (7, "", "new reload response lost"),
            (8, "", "old reload failed"),
        ]
    )
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, fake_client))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/me")
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "read_remote_file",
        lambda *_args, **_kwargs: "old config",
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "write_remote_file",
        lambda _client, _path, content, **_kwargs: writes.append(content),
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda *_args, **_kwargs: next(reload_results),
    )

    with pytest.raises(RuntimeError, match="旧配置已写回，但强制重载失败: old reload failed"):
        remote_proxy.reload_ai_proxy(
            "server",
            "{ name: node, type: vless, server: example.com, port: 443 }",
        )

    assert writes[-1] == "old config"


@pytest.mark.parametrize(
    ("old_strict", "requested_mode", "expected_strict"),
    [
        (True, None, True),
        (False, True, True),
        (True, False, False),
    ],
)
def test_reload_ai_proxy_preserves_or_explicitly_changes_strict_privacy(
    monkeypatch,
    old_strict,
    requested_mode,
    expected_strict,
):
    old_node = remote_proxy.parse_proxy_node(
        "{ name: old, type: vless, server: old.example.com, port: 443 }"
    )
    old_config = remote_proxy.build_mihomo_config(
        old_node,
        strict_privacy=old_strict,
    )
    writes = []
    fake_client = object()
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, fake_client))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/me")
    monkeypatch.setattr(remote_proxy.ssh_manager, "read_remote_file", lambda *_a, **_k: old_config)
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "write_remote_file",
        lambda _client, _path, content, **_kwargs: writes.append(content),
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda *_args, **_kwargs: (0, "", ""),
    )

    remote_proxy.reload_ai_proxy(
        "server",
        "{ name: new, type: vless, server: new.example.com, port: 443 }",
        persist_selection=False,
        strict_privacy=requested_mode,
    )

    assert len(writes) == 1
    assert remote_proxy._managed_config_strict_privacy_enabled(writes[0]) is expected_strict
    assert ("MATCH,DIRECT" not in writes[0]) is expected_strict


@pytest.mark.parametrize(
    ("requested_mode", "expected_strict"),
    [
        (None, True),
        (False, False),
    ],
)
def test_reload_ai_proxy_repairs_drifted_strict_intent_unless_explicitly_disabled(
    monkeypatch,
    requested_mode,
    expected_strict,
):
    old_node = remote_proxy.parse_proxy_node(
        "{ name: old, type: vless, server: old.example.com, port: 443 }"
    )
    old_config = remote_proxy.build_mihomo_config(old_node, strict_privacy=True)
    # Simulate an older/drifted strict YAML that no longer passes the current
    # exact DNS contract while retaining this tool's explicit strict intent.
    old_config = old_config.replace("use-system-hosts: false", "use-system-hosts: true")
    assert remote_proxy._managed_config_strict_privacy_enabled(old_config) is False

    writes = []
    fake_client = object()
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, fake_client))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/me")
    monkeypatch.setattr(remote_proxy.ssh_manager, "read_remote_file", lambda *_a, **_k: old_config)
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "write_remote_file",
        lambda _client, _path, content, **_kwargs: writes.append(content),
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda *_args, **_kwargs: (0, "", ""),
    )

    remote_proxy.reload_ai_proxy(
        "server",
        "{ name: new, type: vless, server: new.example.com, port: 443 }",
        persist_selection=False,
        strict_privacy=requested_mode,
    )

    assert len(writes) == 1
    assert remote_proxy._managed_config_strict_privacy_enabled(writes[0]) is expected_strict
    assert ("MATCH,DIRECT" not in writes[0]) is expected_strict


def test_unmanaged_comment_that_mentions_strict_markers_does_not_enable_strict_intent():
    content = (
        "# This unrelated file mentions "
        f"{remote_proxy.AI_PROXY_CONFIG_MARKER} and "
        f"{remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER} in prose\n"
        "mixed-port: 7890\n"
    )

    assert remote_proxy._managed_config_strict_privacy_intended(content) is False
    assert remote_proxy._resolve_managed_strict_privacy(None, content) is False


def test_reload_ai_proxy_verified_restores_previous_node_when_candidates_fail(monkeypatch):
    original = remote_proxy.parse_proxy_node("{ name: old, type: vless, server: old.example.com, port: 443 }")
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node("{ name: bad, type: vless, server: bad.example.com, port: 443 }"),
    )
    candidate = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node("{ name: worse, type: vless, server: worse.example.com, port: 443 }"),
    )
    reloads = []
    probes = iter(
        [
            "server: AI 代理已配置，运行中: http://127.0.0.1:7890；AI 连通性 0/3 可达",
            "server: AI 代理已配置，运行中: http://127.0.0.1:7890；AI 连通性 3/3 可达",
        ]
    )
    latencies = {
        remote_proxy.proxy_node_key(candidate.node): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_node_key(candidate.node),
            True,
            latency_ms=30,
        )
    }

    def fake_reload(_server, text, _port=7890):
        reloads.append(remote_proxy.parse_proxy_node(text)["name"])
        return f"server: 已热更新远端 AI 代理节点为 {reloads[-1]}"

    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_args, **_kwargs: original)
    monkeypatch.setattr(remote_proxy, "reload_ai_proxy", fake_reload)
    monkeypatch.setattr(remote_proxy, "probe_ai_proxy", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy_stability",
        lambda *_args, **_kwargs: (
            "server: AI 代理已配置，运行中: http://127.0.0.1:7890；AI 稳定性 0/12 可达"
        ),
    )
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", lambda *_args, **_kwargs: latencies)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = remote_proxy.reload_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested.node),
        [requested, candidate],
    )

    assert reloads == ["bad", "worse", "old"]
    assert "已恢复更新前节点 old" in message
    assert "验证通过" in message


def test_reload_ai_proxy_verified_restores_previous_node_when_initial_probe_raises(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: old, type: vless, server: old.example.com, port: 443 }"
    )
    requested = remote_proxy.parse_proxy_node(
        "{ name: new, type: vless, server: new.example.com, port: 443 }"
    )
    reloads = []
    probes = iter(
        [
            RuntimeError("probe transport failed"),
            "server: AI 代理已配置，运行中: http://127.0.0.1:17890；AI 连通性 3/3 可达",
        ]
    )

    def fake_reload(
        _server,
        text,
        port=7890,
        *,
        profile_id="",
        persist_selection=True,
    ):
        reloads.append(
            (
                remote_proxy.parse_proxy_node(text)["name"],
                port,
                profile_id,
                persist_selection,
            )
        )
        return f"server: reloaded {reloads[-1][0]}"

    def fake_probe(*_args, **_kwargs):
        result = next(probes)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_a, **_k: original)
    monkeypatch.setattr(remote_proxy, "reload_ai_proxy", fake_reload)
    monkeypatch.setattr(remote_proxy, "probe_ai_proxy", fake_probe)
    monkeypatch.setattr(
        remote_proxy,
        "set_proxy_subscription_selected_node",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("selection must not persist")),
    )

    message = remote_proxy.reload_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested),
        mixed_port=17890,
        profile_id="profile-a",
        persist_selection=False,
    )

    assert reloads == [
        ("new", 17890, "profile-a", False),
        ("old", 17890, "profile-a", False),
    ]
    assert "热更新后验证执行失败: probe transport failed" in message
    assert "已恢复更新前节点 old" in message
    assert "验证通过" in message


def test_refresh_running_ai_proxy_skips_stopped_proxy(monkeypatch):
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=False,
            running=False,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )

    message = remote_proxy.refresh_running_ai_proxy_from_subscription("server", [])

    assert "未运行" in message


def test_refresh_running_ai_proxy_keeps_current_when_latency_fails(monkeypatch):
    node = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node("{ name: node, type: vless, server: node.example.com, port: 443 }"),
    )
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        remote_proxy,
        "measure_proxy_node_latencies_on_server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ssh timeout")),
    )
    monkeypatch.setattr(
        remote_proxy,
        "reload_ai_proxy_verified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should keep current node")),
    )

    message = remote_proxy.refresh_running_ai_proxy_from_subscription("server", [node])

    assert "已保留当前运行节点" in message


def test_refresh_running_ai_proxy_uses_strict_automatic_stability_gate(monkeypatch):
    node = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: current-us, type: vless, server: us.example.com, port: 443 }"
        ),
    )
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(
        remote_proxy,
        "_read_remote_managed_proxy_node",
        lambda *_args, **_kwargs: dict(node.node),
    )
    captured = {}

    def fake_reload(*_args, **kwargs):
        captured.update(kwargs)
        return "strict gate invoked"

    monkeypatch.setattr(remote_proxy, "reload_ai_proxy_verified", fake_reload)

    message = remote_proxy.refresh_running_ai_proxy_from_subscription(
        "server",
        [node],
        strict_privacy=True,
    )

    assert message == "strict gate invoked"
    assert captured["automatic_update"] is True
    assert captured["strict_privacy"] is True


def test_refresh_running_ai_proxy_never_auto_selects_hong_kong(monkeypatch):
    hong_kong = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: HK01, type: vless, server: hk.example.com, port: 443 }"
        ),
    )
    current = remote_proxy.parse_proxy_node(
        "{ name: current-us, type: vless, server: old.example.com, port: 443 }"
    )
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(
        remote_proxy,
        "_read_remote_managed_proxy_node",
        lambda *_args, **_kwargs: current,
    )
    monkeypatch.setattr(
        remote_proxy,
        "measure_proxy_node_latencies_on_server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Hong Kong must not enter automatic latency selection")
        ),
    )
    monkeypatch.setattr(
        remote_proxy,
        "reload_ai_proxy_verified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Hong Kong must not be automatically reloaded")
        ),
    )

    message = remote_proxy.refresh_running_ai_proxy_from_subscription("server", [hong_kong])

    assert "仅有香港节点" in message
    assert "香港仅允许手动选择" in message
    assert "已保留当前运行节点" in message


def test_reload_local_ai_proxy_verified_restores_previous_node_when_candidates_fail(monkeypatch):
    original = remote_proxy.parse_proxy_node("{ name: old, type: vless, server: old.example.com, port: 443 }")
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node("{ name: bad, type: vless, server: bad.example.com, port: 443 }"),
    )
    candidate = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node("{ name: worse, type: vless, server: worse.example.com, port: 443 }"),
    )
    reloads = []
    probes = iter(
        [
            "本机 AI 代理已配置，运行中: http://127.0.0.1:17897；AI 连通性 0/3 可达",
            "本机 AI 代理已配置，运行中: http://127.0.0.1:17897；AI 连通性 3/3 可达",
        ]
    )
    def fake_reload(text, **_kwargs):
        reloads.append(remote_proxy.parse_proxy_node(text)["name"])
        return f"本机 AI 代理已热更新节点为 {reloads[-1]}"

    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: original)
    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy", fake_reload)
    monkeypatch.setattr(local_proxy, "probe_local_ai_proxy", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(
        local_proxy,
        "_select_stable_automatic_local_candidate",
        lambda *_args, **_kwargs: (None, None, {}, {}),
    )
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = local_proxy.reload_local_ai_proxy_verified(
        remote_proxy.format_proxy_node(requested.node),
        [requested, candidate],
    )

    assert reloads == ["bad", "old"]
    assert "worse" not in reloads
    assert "已恢复更新前节点 old" in message
    assert "验证通过" in message


def test_reload_local_ai_proxy_verified_restores_previous_node_when_initial_probe_raises(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: old, type: vless, server: old.example.com, port: 443 }"
    )
    requested = remote_proxy.parse_proxy_node(
        "{ name: new, type: vless, server: new.example.com, port: 443 }"
    )
    reloads = []
    probes = iter(
        [
            RuntimeError("local probe failed"),
            "本机 AI 代理已配置，运行中: http://127.0.0.1:17897；AI 连通性 3/3 可达",
        ]
    )

    def fake_reload(text, *, profile_id="", **_kwargs):
        reloads.append((remote_proxy.parse_proxy_node(text)["name"], profile_id))
        return f"本机 AI 代理已热更新节点为 {reloads[-1][0]}"

    def fake_probe(*_args, **_kwargs):
        result = next(probes)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: original)
    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy", fake_reload)
    monkeypatch.setattr(local_proxy, "probe_local_ai_proxy", fake_probe)

    message = local_proxy.reload_local_ai_proxy_verified(
        remote_proxy.format_proxy_node(requested),
        profile_id="profile-a",
    )

    assert reloads == [("new", "profile-a"), ("old", "profile-a")]
    assert "热更新后验证执行失败: local probe failed" in message
    assert "已恢复更新前节点 old" in message
    assert "验证通过" in message


def test_reload_remote_proxy_fallback_skips_hong_kong_candidate(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: old, type: vless, server: old.example.com, port: 443 }"
    )
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: failed, type: vless, server: failed.example.com, port: 443 }"
        ),
    )
    hong_kong = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node(
            "{ name: Hong Kong fastest, type: vless, server: hk.example.com, port: 443 }"
        ),
    )
    safe = remote_proxy.ProxySubscriptionNode(
        3,
        remote_proxy.parse_proxy_node(
            "{ name: US backup, type: vless, server: us.example.com, port: 443 }"
        ),
    )
    reloads = []
    measured = []
    probes = iter(
        [
            "server: AI 连通性 0/3 可达",
            "server: AI 连通性 3/3 可达",
        ]
    )

    def fake_reload(_server, text, _port=7890):
        name = remote_proxy.parse_proxy_node(text)["name"]
        reloads.append(name)
        return f"reloaded {name}"

    def fake_measure(_server, nodes, **_kwargs):
        measured.extend(item.node["name"] for item in nodes)
        key = remote_proxy.proxy_node_key(safe.node)
        return {key: remote_proxy.ProxyNodeLatencyResult(key, True, latency_ms=40)}

    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_a, **_k: original)
    monkeypatch.setattr(remote_proxy, "reload_ai_proxy", fake_reload)
    monkeypatch.setattr(remote_proxy, "probe_ai_proxy", lambda *_a, **_k: next(probes))
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy_stability",
        lambda *_a, **_k: _remote_stability_summary("server:"),
    )
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", fake_measure)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = remote_proxy.reload_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested.node),
        [requested, hong_kong, safe],
    )

    assert measured == ["US backup"]
    assert reloads == ["failed", "US backup"]
    assert "验证通过" in message


def test_reload_local_proxy_fallback_skips_hong_kong_candidate(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: old, type: vless, server: old.example.com, port: 443 }"
    )
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: failed, type: vless, server: failed.example.com, port: 443 }"
        ),
    )
    hong_kong = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node(
            "{ name: 香港高速, type: vless, server: hk.example.com, port: 443 }"
        ),
    )
    safe = remote_proxy.ProxySubscriptionNode(
        3,
        remote_proxy.parse_proxy_node(
            "{ name: 日本备用, type: vless, server: jp.example.com, port: 443 }"
        ),
    )
    reloads = []
    fallback_servers = []
    probes = iter(
        [
            "本机 AI 连通性 0/3 可达",
            "本机 AI 连通性 3/3 可达",
        ]
    )

    def fake_reload(text, **_kwargs):
        name = remote_proxy.parse_proxy_node(text)["name"]
        reloads.append(name)
        fallback_servers.append(
            [node["server"] for node in _kwargs.get("fallback_nodes") or ()]
        )
        return f"reloaded {name}"

    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: original)
    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy", fake_reload)
    monkeypatch.setattr(local_proxy, "probe_local_ai_proxy", lambda *_a, **_k: next(probes))
    monkeypatch.setattr(
        local_proxy,
        "_select_stable_automatic_local_candidate",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("startup verification must not run blocking deep probes")
        ),
    )
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = local_proxy.reload_local_ai_proxy_verified(
        remote_proxy.format_proxy_node(requested.node),
        [requested, hong_kong, safe],
    )

    assert reloads == ["failed", "old"]
    assert fallback_servers[0] == ["jp.example.com"]
    assert "hk.example.com" not in fallback_servers[0]
    assert "已跳过阻塞式逐节点深测" in message
    assert "已恢复更新前节点 old" in message


def test_reload_remote_proxy_does_not_use_hong_kong_when_no_other_fallback(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: old, type: vless, server: old.example.com, port: 443 }"
    )
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: failed, type: vless, server: failed.example.com, port: 443 }"
        ),
    )
    hong_kong = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node(
            "{ name: HK only, type: vless, server: hk.example.com, port: 443 }"
        ),
    )
    reloads = []
    probes = iter(["server: AI 连通性 0/3 可达", "server: AI 连通性 3/3 可达"])

    def fake_reload(_server, text, _port=7890):
        name = remote_proxy.parse_proxy_node(text)["name"]
        reloads.append(name)
        return f"reloaded {name}"

    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_a, **_k: original)
    monkeypatch.setattr(remote_proxy, "reload_ai_proxy", fake_reload)
    monkeypatch.setattr(remote_proxy, "probe_ai_proxy", lambda *_a, **_k: next(probes))
    monkeypatch.setattr(
        remote_proxy,
        "measure_proxy_node_latencies_on_server",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Hong Kong must not be measured")),
    )

    message = remote_proxy.reload_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested.node),
        [requested, hong_kong],
    )

    assert reloads == ["failed", "old"]
    assert "验证失败" in message


def test_remote_cleanup_command_backs_up_legacy_proxy_configs_and_removes_managed_blocks():
    command = remote_proxy._build_cleanup_command("/home/me", 7890, include_legacy_config=True)

    assert "proxy-cleanup-backup" in command
    assert remote_proxy.AI_PROXY_CONFIG_MARKER in command
    assert "start-ai-proxy.sh" in command
    assert "server-env-setup" in command
    assert "VS Code settings JSON" in command
    assert "kill -9" in command
    assert "backed_up_configs" in command
    assert "systemctl --user show-environment" in command
    assert "systemctl --user unset-environment" in command
    assert 'grep -Fx -- "$key=$PROXY_URL"' in command
    assert '[ "$config_owned" = "yes" ] || return 1' in command
    assert '*mihomo*"$CONFIG_DIR"*|*clash*"$CONFIG_DIR"*' in command
    assert '$4 ~ (port "$")' in command
    assert "tr -cd '0-9'" not in command


def test_dead_proxy_reconcile_requires_owned_config_and_preserves_live_proxy():
    command = remote_proxy._build_dead_proxy_reconcile_command("/home/me", 7890)

    assert 'grep -qF "$CONFIG_MARKER" "$CONFIG_FILE"' in command
    assert '*mihomo*"$CONFIG_DIR"*|*clash*"$CONFIG_DIR"*' in command
    assert '[ "$config_owned" = "yes" ] || return 1' in command
    assert 'reason=foreign_listener' in command
    assert 'reason=managed_proxy_on_other_port' in command
    assert 'repaired_pid=yes' in command
    assert 'working=yes' in command
    assert command.index('if [ "$port_listening" = "yes" ]') < command.index('stop_managed_pid "$saved_pid"')
    assert "tr -cd '0-9'" not in command
    assert "\0" not in command


def test_dead_proxy_reconcile_blocks_unknown_listener_without_killing(monkeypatch):
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda *_args, **_kwargs: (
            0,
            "conflict=yes\nreason=foreign_listener\nlistener_pids=2468\n",
            "",
        ),
    )

    with pytest.raises(RuntimeError, match="为避免误杀，已停止部署"):
        remote_proxy._reconcile_dead_ai_proxy_runtime(object(), "/home/me", 7890)


def test_install_auto_cleans_only_confirmed_dead_managed_state(monkeypatch):
    events = []
    fake_client = object()
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, fake_client))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/me")
    monkeypatch.setattr(remote_proxy.ssh_manager, "read_remote_file", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        remote_proxy,
        "_reconcile_dead_ai_proxy_runtime",
        lambda *_args, **_kwargs: events.append("reconcile") or {"dirty": "yes", "working": "no"},
    )
    monkeypatch.setattr(
        remote_proxy,
        "cleanup_ai_proxy",
        lambda *_args, **_kwargs: events.append("cleanup") or "cleaned",
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "write_remote_file",
        lambda *_args, **_kwargs: events.append("write"),
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda *_args, **_kwargs: (0, "proxy=http://127.0.0.1:7890\n", ""),
    )
    monkeypatch.setattr(remote_proxy, "_write_shell_profile_block", lambda *_args: None)
    monkeypatch.setattr(remote_proxy, "_write_vscode_proxy_entrypoints", lambda *_args: 1)

    message = remote_proxy.install_ai_proxy(
        "server",
        "{ name: node, type: vless, server: example.com, port: 443 }",
    )

    assert events[:2] == ["reconcile", "cleanup"]
    assert events.count("write") == 3
    assert "自动清理确认失效" in message


def test_install_repairs_live_managed_proxy_and_uses_hot_reload(monkeypatch):
    fake_client = object()
    writes = []
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, fake_client))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/me")
    monkeypatch.setattr(remote_proxy.ssh_manager, "read_remote_file", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        remote_proxy,
        "_reconcile_dead_ai_proxy_runtime",
        lambda *_args, **_kwargs: {"dirty": "no", "working": "yes", "repaired_pid": "yes"},
    )
    monkeypatch.setattr(
        remote_proxy,
        "reload_ai_proxy",
        lambda *_args, **_kwargs: "hot reloaded",
    )
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "write_remote_file",
        lambda *_args, **_kwargs: writes.append(True),
    )

    message = remote_proxy.install_ai_proxy(
        "server",
        "{ name: node, type: vless, server: example.com, port: 443 }",
    )

    assert writes == []
    assert "正常工作的代理未终止" in message


def test_remove_vscode_proxy_settings_only_removes_managed_values():
    settings = {
        "http.proxy": "http://127.0.0.1:7890",
        "http.proxySupport": "override",
        "terminal.integrated.env.linux": {
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "HTTPS_PROXY": "http://other.proxy:8080",
            "NO_PROXY": "127.0.0.1,localhost,::1,*.local",
            "KEEP": "1",
        },
        "editor.fontSize": 14,
    }

    updated, changed = remote_proxy._remove_vscode_proxy_settings(settings, 7890)

    assert changed is True
    assert "http.proxy" not in updated
    assert "http.proxySupport" not in updated
    assert "HTTP_PROXY" not in updated["terminal.integrated.env.linux"]
    assert "NO_PROXY" not in updated["terminal.integrated.env.linux"]
    assert updated["terminal.integrated.env.linux"]["HTTPS_PROXY"] == "http://other.proxy:8080"
    assert updated["terminal.integrated.env.linux"]["KEEP"] == "1"
    assert updated["editor.fontSize"] == 14


def test_build_remote_probe_command_covers_python_and_curl_fallbacks():
    command = remote_proxy._build_probe_command(7890, timeout=9)

    assert "PROXY=http://127.0.0.1:7890" in command
    assert "TIMEOUT=9" in command
    assert "urllib.request.ProxyHandler" in command
    assert "command -v curl" in command
    assert "env -u NO_PROXY -u no_proxy curl --noproxy '' -x \"$PROXY\"" in command
    assert "OpenAI/ChatGPT" in command
    assert "Gemini/Google AI" in command


def test_build_remote_latency_command_uses_stdin_json_temp_file():
    command = remote_proxy._build_remote_latency_command(timeout=2.5, attempts=3, max_workers=12)

    assert "api-switcher-node-latency" in command
    assert "cat > \"$TMP_INPUT\"" in command
    assert "socket.create_connection" in command
    assert "ThreadPoolExecutor" in command
    assert "latency\\t" in command
    assert "ATTEMPTS = 3" in command


def test_parse_remote_latency_output_returns_latency_results():
    results = remote_proxy._parse_remote_latency_output(
        "noise\n"
        "latency\tkey-a\t1\t42\t\t2\n"
        "latency\tkey-b\t0\t\ttimed out\t2\n"
    )

    assert results["key-a"].ok is True
    assert results["key-a"].latency_ms == 42
    assert results["key-b"].ok is False
    assert results["key-b"].detail == "timed out"


def test_measure_proxy_node_latencies_on_server_sends_nodes_json(monkeypatch):
    sent = {}
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, object()))

    def fake_execute(_client, command, **kwargs):
        sent["command"] = command
        sent["input"] = json.loads(kwargs["input_data"])
        sent["timeout"] = kwargs["timeout"]
        assert kwargs["log_command"] is False
        return 0, "latency\tkey-1\t1\t25\t\t2\n", ""

    monkeypatch.setattr(remote_proxy, "proxy_node_key", lambda _node: "key-1")
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", fake_execute)

    results = remote_proxy.measure_proxy_node_latencies_on_server(
        "server-a",
        [{"name": "node", "type": "vless", "server": "example.com", "port": 443}],
        timeout=1,
        attempts=2,
        max_workers=4,
    )

    assert sent["input"] == [{"key": "key-1", "server": "example.com", "port": 443, "name": "node"}]
    assert sent["timeout"] >= 45
    assert results["key-1"].latency_ms == 25


def test_parse_remote_probe_output_formats_result_summaries():
    results = remote_proxy._parse_remote_probe_output(
        "noise\n"
        "probe\tOpenAI/ChatGPT\t1\tHTTP 403\t11\n"
        "probe\tGemini/Google AI\t0\ttimeout\t13\n"
    )

    assert len(results) == 2
    assert results[0].ok is True
    assert results[0].summary() == "OpenAI/ChatGPT: 可达 / HTTP 403 / 11ms"
    assert results[1].summary() == "Gemini/Google AI: 失败 / timeout / 13ms"


def test_probe_ai_proxy_skips_network_probe_when_remote_proxy_is_not_running(monkeypatch):
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=False,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(
        remote_proxy,
        "_connect_ssh",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not connect")),
    )

    summary = remote_proxy.probe_ai_proxy("server-a")

    assert "代理未运行，跳过 AI 连通性探测" in summary


def test_probe_ai_proxy_combines_status_and_remote_probe_results(monkeypatch):
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, object()))

    def fake_execute(_client, command, **kwargs):
        assert "PROXY=http://127.0.0.1:7890" in command
        assert kwargs["log_command"] is False
        return (
            0,
            "probe\tOpenAI/ChatGPT\t1\tHTTP 403\t11\n"
            "probe\tGemini/Google AI\t0\ttimeout\t13\n",
            "",
        )

    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", fake_execute)

    summary = remote_proxy.probe_ai_proxy("server-a")

    assert "AI 代理已配置，运行中" in summary
    assert "AI 连通性 1/3 可达" in summary
    assert "探测结果不完整: 实收 2/3" in summary
    assert "OpenAI/ChatGPT: 可达 / HTTP 403 / 11ms" in summary


def test_remote_stability_probe_is_three_rounds_strict_bounded_and_credential_free():
    command = remote_proxy._build_probe_command(
        7890,
        timeout=7,
        rounds=remote_proxy.REMOTE_AI_STABILITY_ROUNDS,
        strict=True,
    )
    script = command.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    compile(script, "<remote-ai-stability-probe>", "exec")
    assert "ROUNDS=3" in command
    assert "STRICT=1" in command
    assert "ThreadPoolExecutor" in command
    assert "https://api.openai.com/v1/models" in command
    assert "https://chatgpt.com/cdn-cgi/trace" in command
    assert 'loc == "HK"' in command
    assert "Authorization" not in command
    assert "Bearer " not in command


def _execute_remote_strict_probe_script(
    monkeypatch,
    capsys,
    *,
    compact_status: int = 401,
    compact_declared_length: int | None = None,
):
    command = remote_proxy._build_probe_command(
        7890,
        timeout=7,
        rounds=remote_proxy.REMOTE_AI_STABILITY_ROUNDS,
        strict=True,
    )
    script = command.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    captured_requests = []

    class Response:
        def __init__(self, status, payload):
            self.status = status
            self.code = status
            self._stream = io.BytesIO(payload)
            self.headers = {"Content-Length": str(len(payload))}

        def read(self, size=-1):
            return self._stream.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self._stream.close()
            return False

    def error_response(request, status, payload, *, declared_length=None):
        headers = {
            "Content-Length": str(
                len(payload) if declared_length is None else declared_length
            )
        }
        return remote_proxy.HTTPError(
            request.full_url,
            status,
            "probe response",
            headers,
            io.BytesIO(payload),
        )

    class FakeOpener:
        def open(self, request, *, timeout):
            assert timeout == 7
            captured_requests.append(request)
            if request.full_url == remote_proxy.REMOTE_CODEX_COMPACT_PROBE_URL:
                payload = (
                    b'{"error":{"message":"Missing API key",'
                    b'"type":"invalid_api_key"}}'
                )
                raise error_response(
                    request,
                    compact_status,
                    payload,
                    declared_length=compact_declared_length,
                )
            if request.full_url == "https://chatgpt.com/cdn-cgi/trace":
                return Response(200, b"ip=203.0.113.9\nloc=US\ncolo=SJC\n")
            if request.full_url == "https://api.openai.com/v1/models":
                payload = b'{"error":{"message":"Missing API key","type":"invalid_api_key"}}'
                raise error_response(request, 401, payload)
            if request.full_url == "https://api.anthropic.com/v1/models":
                payload = b'{"error":{"message":"x-api-key required","type":"authentication_error"}}'
                raise error_response(request, 401, payload)
            if request.full_url == "https://generativelanguage.googleapis.com/v1beta/models":
                payload = b'{"error":{"message":"API key not valid","status":"INVALID_ARGUMENT"}}'
                raise error_response(request, 403, payload)
            raise AssertionError(f"unexpected probe URL: {request.full_url}")

    monkeypatch.setattr(
        remote_proxy.urlrequest,
        "build_opener",
        lambda _handler: FakeOpener(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "remote-probe",
            "http://127.0.0.1:7890",
            "7",
            json.dumps(remote_proxy.REMOTE_AI_STABILITY_TARGETS, ensure_ascii=False),
            str(remote_proxy.REMOTE_AI_STABILITY_ROUNDS),
            "1",
        ],
    )

    exec(compile(script, "<remote-ai-stability-probe>", "exec"), {"__name__": "__main__"})
    output = capsys.readouterr().out
    compact_requests = [
        request
        for request in captured_requests
        if request.full_url == remote_proxy.REMOTE_CODEX_COMPACT_PROBE_URL
    ]
    assert len(compact_requests) == 1
    return output, compact_requests[0]


def test_remote_compact_probe_uses_fixed_credential_free_body_and_accepts_structured_401(
    monkeypatch,
    capsys,
):
    output, request = _execute_remote_strict_probe_script(monkeypatch, capsys)
    lines = [line for line in output.splitlines() if line.startswith("probe\t")]
    headers = {key.casefold(): value for key, value in request.header_items()}

    assert len(lines) == remote_proxy.REMOTE_AI_STABILITY_EXPECTED_PROBES
    assert all("\t1\t" in line for line in lines)
    assert request.get_method() == "POST"
    assert len(request.data) == remote_proxy.REMOTE_CODEX_COMPACT_PROBE_PAYLOAD_BYTES
    assert b"api-switcher-network-probe-no-model" in request.data
    assert json.loads(request.data.decode("utf-8"))["model"] == "api-switcher-network-probe-no-model"
    assert "authorization" not in headers


@pytest.mark.parametrize("status", [403, 429, 500])
def test_remote_compact_probe_rejects_policy_rate_limit_and_server_errors(
    monkeypatch,
    capsys,
    status,
):
    output, _request = _execute_remote_strict_probe_script(
        monkeypatch,
        capsys,
        compact_status=status,
    )
    compact_line = next(
        line
        for line in output.splitlines()
        if line.startswith(f"probe\t{remote_proxy.REMOTE_CODEX_COMPACT_PROBE_LABEL}\t")
    )

    assert "\t0\t" in compact_line
    assert f"HTTP {status}" in compact_line


def test_remote_compact_probe_rejects_truncated_structured_401(
    monkeypatch,
    capsys,
):
    output, _request = _execute_remote_strict_probe_script(
        monkeypatch,
        capsys,
        compact_status=401,
        compact_declared_length=100,
    )
    compact_line = next(
        line
        for line in output.splitlines()
        if line.startswith(f"probe\t{remote_proxy.REMOTE_CODEX_COMPACT_PROBE_LABEL}\t")
    )

    assert "\t0\t" in compact_line
    assert "截断" in compact_line


def test_remote_stability_summary_rejects_hong_kong_and_incomplete_results():
    expected = remote_proxy.REMOTE_AI_STABILITY_EXPECTED_PROBES
    complete = (
        f"server: AI 代理已配置，运行中；AI 稳定性 {expected}/{expected} 可达；"
        "ChatGPT 出口: 可达 / HTTP 200，ChatGPT 实际出口 loc=US"
    )
    hong_kong = complete.replace("loc=US", "loc=HK（香港）")
    incomplete = complete.replace(f"{expected}/{expected}", f"{expected - 1}/{expected}")

    assert remote_proxy._probe_stability_summary_all_ok(complete) is True
    assert remote_proxy._probe_stability_summary_all_ok(hong_kong) is False
    assert remote_proxy._probe_stability_summary_all_ok(incomplete) is False


def test_isolated_candidate_command_is_bounded_locked_and_cleans_everything():
    command = remote_proxy._build_isolated_candidate_probe_command("/home/me", timeout=7)

    assert ".candidate-probe.lock" in command
    assert 'TMP="$(mktemp -d "$BASE/candidate.XXXXXX")"' in command
    assert 'chmod 600 "$TMP/config.yaml"' in command
    assert 'kill -TERM "$CANDIDATE_PID"' in command
    assert 'kill -KILL "$CANDIDATE_PID"' in command
    assert 'wait "$CANDIDATE_PID"' in command
    assert 'rm -rf -- "$TMP"' in command
    assert 'readlink "$LOCK"' in command
    assert 'ln -s "$$" "$LOCK"' in command
    assert 'kill -0 "$lock_owner"' in command
    assert 'for stale_dir in "$BASE"/candidate.*' in command
    assert 'stale_cmd="$(ps -p "$stale_pid" -o args=' in command
    assert '*mihomo*"$stale_dir"*' in command
    assert "ROUNDS=3" in command
    assert "STRICT=1" in command
    assert 'os.environ.pop(key, None)' in command
    assert ".config/mihomo/config.yaml" not in command
    assert "ai-proxy.env" not in command
    assert "server-env-setup" not in command
    assert "API_KEY" not in command
    assert "\0" not in command
    assert r"tr '\0' ' '" in command


def test_isolated_candidate_probe_transfers_0600_template_and_accepts_all_results(monkeypatch):
    captured = {}
    output = _remote_stability_output()

    def fake_execute(_client, command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return 0, output, ""

    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", fake_execute)
    node = remote_proxy.parse_proxy_node(
        "{ name: US candidate, type: vless, server: us.example.com, port: 443 }"
    )

    results = remote_proxy._probe_ai_proxy_candidate_isolated(object(), "/home/me", node)

    assert len(results) == remote_proxy.REMOTE_AI_STABILITY_EXPECTED_PROBES
    assert "__API_SWITCHER_CANDIDATE_PORT__" in captured["input_data"]
    assert "__API_SWITCHER_CANDIDATE_CONTROLLER_PORT__" in captured["input_data"]
    parsed = remote_proxy.yaml.safe_load(captured["input_data"])
    assert parsed["proxies"][0]["name"] == remote_proxy.AI_PROXY_INTERNAL_NODE_NAME
    assert parsed["proxy-groups"][0]["proxies"] == [
        remote_proxy.AI_PROXY_INTERNAL_NODE_NAME
    ]
    assert remote_proxy._managed_proxy_display_name(captured["input_data"]) == "US candidate"
    assert captured["log_command"] is False
    assert captured["timeout"] <= 60


def test_isolated_candidate_probe_missing_tool_fails_closed(monkeypatch):
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda *_args, **_kwargs: (12, "", "远端缺少可执行的 mihomo/clash"),
    )
    node = remote_proxy.parse_proxy_node(
        "{ name: US candidate, type: vless, server: us.example.com, port: 443 }"
    )

    with pytest.raises(RuntimeError, match="缺少可执行的 mihomo/clash"):
        remote_proxy._probe_ai_proxy_candidate_isolated(object(), "/home/me", node)


def test_isolated_candidate_probe_rejects_hong_kong_actual_exit(monkeypatch):
    output_lines = _remote_stability_output().splitlines()
    output_lines[1] = "probe\tChatGPT 出口\t1\t第1/3轮 HTTP 200，实际出口 loc=HK（香港）\t10"
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda *_args, **_kwargs: (0, "\n".join(output_lines), ""),
    )
    node = remote_proxy.parse_proxy_node(
        "{ name: Unknown region, type: vless, server: edge.example.com, port: 443 }"
    )

    with pytest.raises(RuntimeError, match="实际出口为香港"):
        remote_proxy._probe_ai_proxy_candidate_isolated(object(), "/home/me", node)


def test_public_isolated_candidate_probe_rejects_hong_kong_before_ssh(monkeypatch):
    monkeypatch.setattr(
        remote_proxy,
        "_connect_ssh",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Hong Kong candidate must be rejected before SSH")
        ),
    )

    with pytest.raises(RuntimeError, match="香港节点仅允许手动选择"):
        remote_proxy.probe_ai_proxy_candidate_isolated(
            "server",
            "{ name: Hong Kong 01, type: vless, server: hk.example.com, port: 443 }",
        )


def test_automatic_reload_failed_isolated_candidate_never_reloads(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: original, type: vless, server: old.example.com, port: 443 }"
    )
    requested = remote_proxy.parse_proxy_node(
        "{ name: requested, type: vless, server: new.example.com, port: 443 }"
    )
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_a, **_k: original)
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy_candidate_isolated",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("strict probe failed")),
    )
    monkeypatch.setattr(
        remote_proxy,
        "reload_ai_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed isolated candidate must not reload")
        ),
    )

    message = remote_proxy.reload_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested),
        automatic_update=True,
    )

    assert "已保留当前运行节点" in message


def test_automatic_reload_missing_current_snapshot_fails_closed(monkeypatch):
    requested = remote_proxy.parse_proxy_node(
        "{ name: requested, type: vless, server: new.example.com, port: 443 }"
    )
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_a, **_k: None)
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy_candidate_isolated",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing snapshot must fail before isolated probe")
        ),
    )
    monkeypatch.setattr(
        remote_proxy,
        "reload_ai_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing snapshot must not reload")
        ),
    )

    message = remote_proxy.reload_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested),
        automatic_update=True,
    )

    assert "未读取到自动更新前节点" in message
    assert "已保留当前运行节点" in message


def test_automatic_reload_applies_once_only_after_isolated_probe(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: original, type: vless, server: old.example.com, port: 443 }"
    )
    requested = remote_proxy.parse_proxy_node(
        "{ name: requested, type: vless, server: new.example.com, port: 443 }"
    )
    events = []
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_a, **_k: original)
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy_candidate_isolated",
        lambda *_args, **_kwargs: events.append("probe")
        or _remote_stability_summary(),
    )
    monkeypatch.setattr(
        remote_proxy,
        "reload_ai_proxy",
        lambda *_args, **_kwargs: events.append("reload") or "reloaded once",
    )

    message = remote_proxy.reload_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested),
        automatic_update=True,
    )

    assert events == ["probe", "reload"]
    assert "隔离验证通过" in message


def test_automatic_reload_refuses_to_overwrite_node_changed_during_probe(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: original, type: vless, server: old.example.com, port: 443 }"
    )
    changed = remote_proxy.parse_proxy_node(
        "{ name: manual, type: vless, server: manual.example.com, port: 443 }"
    )
    requested = remote_proxy.parse_proxy_node(
        "{ name: requested, type: vless, server: new.example.com, port: 443 }"
    )
    reads = iter([original, changed])
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_a, **_k: next(reads))
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy_candidate_isolated",
        lambda *_args, **_kwargs: _remote_stability_summary(),
    )
    monkeypatch.setattr(
        remote_proxy,
        "reload_ai_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("changed current node must not be overwritten")
        ),
    )

    message = remote_proxy.reload_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested),
        automatic_update=True,
    )

    assert "当前节点已变化" in message


def test_probe_ai_proxy_stability_requires_all_short_and_compact_results(monkeypatch):
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: remote_proxy.RemoteAIProxyStatus(
            installed=True,
            running=True,
            config_path="/home/me/.config/mihomo/config.yaml",
            proxy_url="http://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, object()))
    output = _remote_stability_output()

    def fake_execute(_client, command, **kwargs):
        assert "STRICT=1" in command
        assert "ROUNDS=3" in command
        assert kwargs["timeout"] <= 60
        assert kwargs["log_command"] is False
        return 0, output, ""

    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", fake_execute)

    summary = remote_proxy.probe_ai_proxy_stability("server-a", timeout=7)

    expected = remote_proxy.REMOTE_AI_STABILITY_EXPECTED_PROBES
    assert f"AI 稳定性 {expected}/{expected} 可达" in summary
    assert remote_proxy._probe_stability_summary_all_ok(summary) is True


def test_install_ai_proxy_verified_keeps_working_requested_node(monkeypatch):
    installs = []
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: _remote_proxy_status(running=False),
    )
    monkeypatch.setattr(remote_proxy, "install_ai_proxy", lambda _server, text, _port=7890: installs.append(text) or "installed")
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy_candidate_isolated",
        lambda *_args, **_kwargs: _remote_stability_summary(),
    )
    monkeypatch.setattr(
        remote_proxy,
        "measure_proxy_node_latencies_on_server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not fallback")),
    )

    message = remote_proxy.install_ai_proxy_verified(
        "server",
        "{ name: good, type: vless, server: good.example.com, port: 443 }",
    )

    assert len(installs) == 1
    assert "验证通过" in message


def test_install_ai_proxy_verified_uses_safe_hot_reload_when_proxy_is_running(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: _remote_proxy_status(running=True),
    )
    monkeypatch.setattr(
        remote_proxy,
        "install_ai_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a running proxy must never use destructive install")
        ),
    )

    def fake_reload(server, text, candidates, **kwargs):
        captured.update(
            server=server,
            text=text,
            candidates=candidates,
            kwargs=kwargs,
        )
        return "safe hot reload"

    monkeypatch.setattr(remote_proxy, "reload_ai_proxy_verified", fake_reload)
    candidates = ("candidate-sentinel",)

    message = remote_proxy.install_ai_proxy_verified(
        "server",
        "{ name: good, type: vless, server: good.example.com, port: 443 }",
        candidates,
        mixed_port=17890,
        max_candidates=4,
        quality_results={"quality": "sentinel"},
        strict_privacy=True,
    )

    assert message == "safe hot reload"
    assert captured["server"] == "server"
    assert captured["candidates"] is candidates
    assert captured["kwargs"] == {
        "mixed_port": 17890,
        "max_candidates": 4,
        "quality_results": {"quality": "sentinel"},
        "automatic_update": True,
        "strict_privacy": True,
    }


def test_install_ai_proxy_verified_falls_back_to_working_candidate(monkeypatch):
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node("{ name: bad, type: vless, server: bad.example.com, port: 443 }"),
    )
    candidate = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node("{ name: good, type: vless, server: good.example.com, port: 443 }"),
    )
    hong_kong = remote_proxy.ProxySubscriptionNode(
        3,
        remote_proxy.parse_proxy_node(
            "{ name: HK01 fastest, type: vless, server: hk.example.com, port: 443 }"
        ),
    )
    installs = []
    measured = []
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: _remote_proxy_status(running=False),
    )

    def fake_install(_server, text, _port=7890):
        installs.append(remote_proxy.parse_proxy_node(text)["name"])
        return "installed"

    latencies = {
        remote_proxy.proxy_node_key(candidate.node): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_node_key(candidate.node),
            True,
            latency_ms=22,
        )
    }

    monkeypatch.setattr(remote_proxy, "install_ai_proxy", fake_install)
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy_candidate_isolated",
        lambda _server, text, **_kwargs: (
            _remote_stability_summary()
            if remote_proxy.parse_proxy_node(text)["name"] == "good"
            else (_ for _ in ()).throw(RuntimeError("candidate failed"))
        ),
    )

    def fake_measure(_server, nodes, **_kwargs):
        measured.extend(item.node["name"] for item in nodes)
        return latencies

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", fake_measure)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = remote_proxy.install_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested.node),
        [requested, hong_kong, candidate],
    )

    assert measured == ["bad", "good"]
    assert installs == ["good"]
    assert "自动切换到 good" in message
    assert "验证通过" in message


def test_install_local_proxy_verified_skips_hong_kong_fallback(monkeypatch):
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: bad, type: vless, server: bad.example.com, port: 443 }"
        ),
    )
    hong_kong = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node(
            "{ name: HKG 01 fastest, type: vless, server: hk.example.com, port: 443 }"
        ),
    )
    safe = remote_proxy.ProxySubscriptionNode(
        3,
        remote_proxy.parse_proxy_node(
            "{ name: Japan safe, type: vless, server: jp.example.com, port: 443 }"
        ),
    )
    installs = []
    fallback_servers = []
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: False)
    probes = iter(
        [
            "本机 AI 连通性 0/3 可达",
            "本机 AI 连通性 3/3 可达",
        ]
    )
    def fake_install(text, *_args, **kwargs):
        installs.append(remote_proxy.parse_proxy_node(text)["name"])
        fallback_servers.append(
            [node["server"] for node in kwargs.get("fallback_nodes") or ()]
        )
        return "installed"

    monkeypatch.setattr(local_proxy, "install_local_ai_proxy", fake_install)
    monkeypatch.setattr(local_proxy, "probe_local_ai_proxy", lambda *_a, **_k: next(probes))
    rollbacks = []
    monkeypatch.setattr(
        local_proxy,
        "stop_local_ai_proxy",
        lambda **_kwargs: rollbacks.append(True) or "本机 AI 代理已停止",
    )
    monkeypatch.setattr(
        local_proxy,
        "_select_stable_automatic_local_candidate",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("startup verification must not run blocking deep probes")
        ),
    )
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = local_proxy.install_local_ai_proxy_verified(
        remote_proxy.format_proxy_node(requested.node),
        [requested, hong_kong, safe],
    )

    assert installs == ["bad"]
    assert fallback_servers == [["jp.example.com"]]
    assert "hk.example.com" not in fallback_servers[0]
    assert "启动验证失败" in message
    assert "已快速复检 2 节点故障切换池" in message
    assert "已恢复启动前设置" in message
    assert rollbacks == [True]


def test_install_local_proxy_verified_uses_hot_reload_when_managed_proxy_is_running(
    monkeypatch,
):
    captured = {}
    state = {"mixed_port": 17897}
    monkeypatch.setattr(local_proxy, "_load_state", lambda: state)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda value: value is state)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda port: port == 17897)
    monkeypatch.setattr(
        local_proxy,
        "install_local_ai_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a running managed proxy must not be restarted")
        ),
    )

    def fake_reload(text, candidates, **kwargs):
        captured.update(text=text, candidates=candidates, kwargs=kwargs)
        return "safe local hot reload"

    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy_verified", fake_reload)
    candidates = ("candidate-sentinel",)

    message = local_proxy.install_local_ai_proxy_verified(
        "{ name: good, type: vless, server: good.example.com, port: 443 }",
        candidates,
        max_candidates=4,
        quality_results={"quality": "sentinel"},
    )

    assert message == "safe local hot reload"
    assert captured["candidates"] is candidates
    assert captured["kwargs"] == {
        "max_candidates": 4,
        "quality_results": {"quality": "sentinel"},
        "automatic_update": False,
    }


def test_running_local_start_uses_reload_rollback_when_requested_node_fails(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: original, type: vless, server: old.example.com, port: 443 }"
    )
    requested = remote_proxy.parse_proxy_node(
        "{ name: requested, type: vless, server: bad.example.com, port: 443 }"
    )
    state = {"mixed_port": 17897}
    reloads = []
    monkeypatch.setattr(local_proxy, "_load_state", lambda: state)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda value: value is state)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda port: port == 17897)
    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: original)
    monkeypatch.setattr(
        local_proxy,
        "install_local_ai_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("running proxy must not use install")
        ),
    )

    def fake_reload(text, *_args, **_kwargs):
        reloads.append(remote_proxy.parse_proxy_node(text)["name"])
        return f"reloaded {reloads[-1]}"

    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy", fake_reload)
    monkeypatch.setattr(
        local_proxy,
        "probe_local_ai_proxy",
        lambda *_args, **_kwargs: "本机 AI 连通性 0/3 可达",
    )

    message = local_proxy.install_local_ai_proxy_verified(
        remote_proxy.format_proxy_node(requested),
    )

    assert reloads == ["requested", "original"]
    assert "已恢复更新前节点 original" in message


def test_install_remote_proxy_verified_failed_candidates_never_touch_formal_proxy(monkeypatch):
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node("{ name: requested, type: vless, server: bad.example.com, port: 443 }"),
    )
    candidate = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node("{ name: candidate, type: vless, server: worse.example.com, port: 443 }"),
    )
    latency = remote_proxy.ProxyNodeLatencyResult(
        remote_proxy.proxy_node_key(candidate.node),
        True,
        latency_ms=20,
    )
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: _remote_proxy_status(running=False),
    )
    monkeypatch.setattr(
        remote_proxy,
        "install_ai_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed isolated candidate must not be installed")
        ),
    )
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy_candidate_isolated",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("isolated failed")),
    )
    monkeypatch.setattr(
        remote_proxy,
        "measure_proxy_node_latencies_on_server",
        lambda *_args, **_kwargs: {remote_proxy.proxy_node_key(candidate.node): latency},
    )

    message = remote_proxy.install_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested.node),
        [requested, candidate],
    )

    assert "未修改正式代理" in message


def test_install_local_proxy_failed_validation_never_reinstalls_nodes(monkeypatch):
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node("{ name: requested, type: vless, server: bad.example.com, port: 443 }"),
    )
    candidate = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node("{ name: candidate, type: vless, server: worse.example.com, port: 443 }"),
    )
    calls = []
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: False)

    def fake_install(text, _port=17897, **_kwargs):
        name = remote_proxy.parse_proxy_node(text)["name"]
        calls.append(name)
        return f"installed {name}"
    monkeypatch.setattr(local_proxy, "install_local_ai_proxy", fake_install)
    monkeypatch.setattr(
        local_proxy,
        "probe_local_ai_proxy",
        lambda *_args, **_kwargs: "本机 AI 连通性 0/3 可达",
    )
    monkeypatch.setattr(
        local_proxy,
        "_select_stable_automatic_local_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup verification must not run blocking deep probes")
        ),
    )
    rollbacks = []
    monkeypatch.setattr(
        local_proxy,
        "stop_local_ai_proxy",
        lambda **_kwargs: rollbacks.append(True) or "本机 AI 代理已停止",
    )

    message = local_proxy.install_local_ai_proxy_verified(
        remote_proxy.format_proxy_node(requested.node),
        [requested, candidate],
    )

    assert calls == ["requested"]
    assert "启动验证失败" in message
    assert "已恢复启动前设置" in message
    assert rollbacks == [True]


def test_new_local_proxy_start_keeps_single_reachable_codex_entry(monkeypatch):
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_ai_proxy_after_failover_warmup",
        lambda: (
            "本机 AI 连通性 1/4 可达；"
            "OpenAI API: 可达 / HTTP 401 / 20ms；"
            "OpenAI/ChatGPT: 失败 / timeout；"
            "Claude/Anthropic: 失败 / timeout；"
            "Gemini/Google AI: 失败 / timeout",
            True,
        ),
    )
    monkeypatch.setattr(
        local_proxy,
        "stop_local_ai_proxy",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("one reachable Codex entry must not be rolled back")
        ),
    )

    message = local_proxy._verify_new_local_proxy_start("started")

    assert "Codex 至少一个入口已通过（OpenAI API）" in message
    assert "启动验证失败" not in message
    assert "已等待内核故障切换初始化并复检" in message


def test_install_ai_proxy_verified_prefers_quality_ranked_candidate(monkeypatch):
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node("{ name: bad, type: vless, server: bad.example.com, port: 443 }"),
    )
    fast_hosting = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node("{ name: fast-hosting, type: vless, server: fast.example.com, port: 443 }"),
    )
    slow_residential = remote_proxy.ProxySubscriptionNode(
        3,
        remote_proxy.parse_proxy_node("{ name: slow-residential, type: vless, server: slow.example.com, port: 443 }"),
    )
    installs = []
    monkeypatch.setattr(
        remote_proxy,
        "inspect_ai_proxy",
        lambda *_args, **_kwargs: _remote_proxy_status(running=False),
    )

    def fake_install(_server, text, _port=7890):
        installs.append(remote_proxy.parse_proxy_node(text)["name"])
        return "installed"

    latencies = {
        remote_proxy.proxy_node_key(fast_hosting.node): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_node_key(fast_hosting.node),
            True,
            latency_ms=15,
        ),
        remote_proxy.proxy_node_key(slow_residential.node): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_node_key(slow_residential.node),
            True,
            latency_ms=120,
        ),
    }
    qualities = {
        remote_proxy.proxy_node_key(fast_hosting.node): remote_proxy.ProxyNodeQualityResult(
            remote_proxy.proxy_node_key(fast_hosting.node),
            True,
            ip_type="IDC机房 IP",
            risk_score=72,
            quality_score=43,
            quality_label="机房风险",
        ),
        remote_proxy.proxy_node_key(slow_residential.node): remote_proxy.ProxyNodeQualityResult(
            remote_proxy.proxy_node_key(slow_residential.node),
            True,
            ip_type="住宅宽带",
            risk_score=12,
            quality_score=96,
            quality_label="家宽高质",
        ),
    }

    monkeypatch.setattr(remote_proxy, "install_ai_proxy", fake_install)
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy_candidate_isolated",
        lambda _server, text, **_kwargs: (
            _remote_stability_summary()
            if remote_proxy.parse_proxy_node(text)["name"] == "slow-residential"
            else (_ for _ in ()).throw(RuntimeError("candidate failed"))
        ),
    )
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", lambda *_args, **_kwargs: latencies)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = remote_proxy.install_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested.node),
        [requested, fast_hosting, slow_residential],
        quality_results=qualities,
    )

    assert installs == ["slow-residential"]
    assert "自动切换到 slow-residential" in message
    assert "验证通过" in message


def test_reload_ai_proxy_verified_still_probes_when_node_is_unchanged(monkeypatch):
    probes = []
    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(remote_proxy, "reload_ai_proxy", lambda *_args, **_kwargs: "server: 运行节点已是最新配置，无需热更新")
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy",
        lambda *_args, **_kwargs: probes.append(1)
        or "server: AI 代理已配置，运行中: http://127.0.0.1:7890；AI 连通性 3/3 可达",
    )

    message = remote_proxy.reload_ai_proxy_verified(
        "server",
        "{ name: same, type: vless, server: same.example.com, port: 443 }",
    )

    assert probes == [1]
    assert "无需热更新" in message
    assert "验证通过" in message


def test_current_remote_ai_proxy_node_key_reads_managed_node(monkeypatch):
    node = remote_proxy.parse_proxy_node("{ name: current, type: vless, server: current.example.com, port: 443 }")
    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_args, **_kwargs: node)

    assert remote_proxy.current_remote_ai_proxy_node_key("server") == remote_proxy.proxy_node_key(node)


def test_read_remote_managed_proxy_node_restores_display_name(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: DIRECT, type: vless, server: node.example.com, port: 443 }"
    )
    config = remote_proxy.build_mihomo_config(original, strict_privacy=True)
    fake_client = object()
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name: (None, fake_client))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/home/me")
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "read_remote_file",
        lambda *_args, **_kwargs: config,
    )

    restored = remote_proxy._read_remote_managed_proxy_node("server")

    assert restored is not None
    assert restored["name"] == "DIRECT"
    assert remote_proxy.proxy_node_key(restored) == remote_proxy.proxy_node_key(original)


def test_current_local_ai_proxy_node_key_falls_back_to_managed_config(monkeypatch):
    node = remote_proxy.parse_proxy_node("{ name: current, type: vless, server: current.example.com, port: 443 }")
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: node)

    assert local_proxy.current_local_ai_proxy_node_key() == remote_proxy.proxy_node_key(node)


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


def test_managed_proxy_process_does_not_inherit_its_own_proxy_environment():
    script = remote_proxy._build_start_script(
        "/home/me/.config/mihomo",
        "/home/me/.config/api-switcher",
        "/home/me/.local/bin",
        7890,
    )

    unset_line = "unset " + " ".join(remote_proxy.PROXY_ENV_KEYS)
    assert unset_line in script
    assert script.index(unset_line) < script.index('nohup "$BIN"')


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


def test_select_local_mixed_port_skips_busy_controller(monkeypatch):
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: None)
    monkeypatch.setattr(local_proxy, "_is_pid_running", lambda _pid: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda port: port == 18897)

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

    assert status.running is False
    assert "pid 文件指向非本工具代理进程" in status.detail
    assert "Windows 环境变量未完全指向本机代理" in status.detail
    assert "Windows 系统代理未指向本机代理" in status.detail
    assert "VS Code 本机设置未完全指向本机代理" in status.detail


@pytest.mark.parametrize(
    ("strict_privacy", "expected"),
    [
        (True, "严格隐私偏好已保存，代理未运行，待下次启动生效"),
        (False, "代理未运行；兼容分流偏好已保存"),
    ],
)
def test_inspect_local_proxy_reports_application_privacy_boundary(
    monkeypatch,
    tmp_path,
    strict_privacy,
    expected,
):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.save_local_proxy_preferences(strict_privacy=strict_privacy)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: None)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: False)

    status = local_proxy.inspect_local_ai_proxy()

    assert expected in status.detail
    assert status.running is False
    assert status.strict_privacy_active is False
    assert status.strict_privacy_desired is strict_privacy


def test_windows_system_proxy_expected_values_match_managed_proxy():
    values = local_proxy._windows_system_proxy_expected_values(17897)

    assert values["ProxyEnable"] == 1
    assert values["ProxyServer"] == "127.0.0.1:17897"
    assert values["AutoConfigURL"] == ""
    assert values["AutoDetect"] == 0
    assert local_proxy._windows_system_proxy_matches_values(values, 17897) is True
    assert local_proxy._windows_system_proxy_matches_values({**values, "ProxyServer": "127.0.0.1:18000"}, 17897) is False
    assert local_proxy._windows_system_proxy_matches_values({**values, "AutoDetect": 1}, 17897) is False
    assert local_proxy._windows_system_proxy_matches_values({**values, "ProxyEnable": "broken"}, 17897) is False


def test_windows_simple_loopback_proxy_status_is_narrow(monkeypatch):
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        local_proxy,
        "_read_windows_system_proxy_values",
        lambda: {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7897"},
    )
    assert local_proxy._windows_enabled_simple_loopback_proxy_endpoint() == (
        "127.0.0.1",
        7897,
    )

    monkeypatch.setattr(
        local_proxy,
        "_read_windows_system_proxy_values",
        lambda: {
            "ProxyEnable": 1,
            "ProxyServer": "http=127.0.0.1:7897;https=127.0.0.1:7898",
        },
    )
    assert local_proxy._windows_enabled_simple_loopback_proxy_endpoint() is None


def test_windows_system_proxy_restore_tolerates_unrelated_field_drift(monkeypatch):
    import winreg

    current = {
        "ProxyEnable": (True, 1, winreg.REG_DWORD),
        "ProxyServer": (True, "127.0.0.1:17897", winreg.REG_SZ),
        "ProxyOverride": (True, "user-changed", winreg.REG_SZ),
        "AutoConfigURL": (False, "", None),
        "AutoDetect": (True, 0, winreg.REG_DWORD),
    }
    previous = {
        "ProxyEnable": {"exists": True, "value": 0, "type": winreg.REG_DWORD},
        "ProxyServer": {"exists": False, "value": "", "type": None},
        "ProxyOverride": {"exists": True, "value": "old", "type": winreg.REG_SZ},
        "AutoConfigURL": {
            "exists": True,
            "value": "https://pac.example/proxy.pac",
            "type": winreg.REG_SZ,
        },
        "AutoDetect": {"exists": True, "value": 1, "type": winreg.REG_DWORD},
    }
    writes = []
    deletes = []
    notifications = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        local_proxy,
        "_read_windows_system_proxy_value",
        lambda name: current[name],
    )
    monkeypatch.setattr(winreg, "CreateKeyEx", lambda *_args, **_kwargs: Key())
    monkeypatch.setattr(
        winreg,
        "SetValueEx",
        lambda _key, name, _reserved, value_type, value: writes.append(
            (name, value_type, value)
        ),
    )
    monkeypatch.setattr(
        winreg,
        "DeleteValue",
        lambda _key, name: deletes.append(name),
    )
    monkeypatch.setattr(
        local_proxy,
        "_notify_windows_proxy_change",
        lambda: notifications.append(True),
    )

    local_proxy._restore_windows_system_proxy(
        {"previous_system_proxy": previous},
        17897,
    )

    assert ("ProxyEnable", winreg.REG_DWORD, 0) in writes
    assert ("AutoConfigURL", winreg.REG_SZ, "https://pac.example/proxy.pac") in writes
    assert ("AutoDetect", winreg.REG_DWORD, 1) in writes
    assert "ProxyServer" in deletes
    assert not any(name == "ProxyOverride" for name, _type, _value in writes)
    assert "ProxyOverride" not in deletes
    assert notifications == [True]


def test_local_proxy_preferences_build_custom_routing_rules(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", tmp_path / "mihomo")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")

    local_proxy.set_builtin_proxy_site_enabled("github", True)
    local_proxy.set_local_proxy_non_cn_mode(True)
    domain_entry = local_proxy.add_custom_proxy_target("https://www.youtube.com/watch?v=1")
    ip_entry = local_proxy.add_custom_proxy_target("8.8.8.8")
    config = local_proxy._build_local_mihomo_config(
        {"name": "node", "type": "vless", "server": "example.com", "port": 443},
        17897,
    )

    assert domain_entry["value"] == "www.youtube.com"
    assert ip_entry["value"] == "8.8.8.8/32"
    assert "DOMAIN-SUFFIX,github.com,AI-PROXY" in config
    assert "DOMAIN-SUFFIX,www.youtube.com,AI-PROXY" in config
    assert "IP-CIDR,8.8.8.8/32,AI-PROXY,no-resolve" in config
    assert "GEOIP,CN,DIRECT" in config
    assert "MATCH,AI-PROXY" in config

    assert local_proxy.remove_custom_proxy_target(ip_entry["id"]) is True
    updated = local_proxy._build_local_mihomo_config(
        {"name": "node", "type": "vless", "server": "example.com", "port": 443},
        17897,
    )
    assert "IP-CIDR,8.8.8.8/32,AI-PROXY,no-resolve" not in updated


def test_local_proxy_strict_privacy_preference_builds_fail_closed_config(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")

    saved = local_proxy.set_local_proxy_strict_privacy("yes")
    config = local_proxy._build_local_mihomo_config(
        {"name": "node", "type": "vless", "server": "example.com", "port": 443},
        17897,
    )

    assert saved["strict_privacy"] is True
    assert "MATCH,AI-PROXY" in config
    assert "MATCH,DIRECT" not in config
    assert "GEOIP,CN,DIRECT" not in config
    assert "respect-rules: true" in config
    assert "proxy-server-nameserver:" in config
    assert "ipv6: false" in config


def test_local_proxy_keep_running_on_exit_defaults_to_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")

    assert local_proxy.local_proxy_keep_running_on_exit_enabled() is True

    saved = local_proxy.set_local_proxy_keep_running_on_exit(False)
    assert saved["keep_running_on_exit"] is False
    assert local_proxy.local_proxy_keep_running_on_exit_enabled() is False

    saved = local_proxy.set_local_proxy_keep_running_on_exit(True)
    assert saved["keep_running_on_exit"] is True
    assert local_proxy.local_proxy_keep_running_on_exit_enabled() is True


def test_local_proxy_preferences_parse_string_booleans(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    payload = {
        "start_on_login": "false",
        "keep_running_on_exit": "off",
        "proxy_non_cn": "yes",
        "strict_privacy": "on",
        "builtin_sites": {"github": "true", "youtube": "0"},
        "custom_targets": [
            {"target": "example.com", "enabled": "false"},
            {"target": "8.8.8.8", "enabled": "on"},
        ],
    }
    local_proxy.LOCAL_PROXY_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    local_proxy.LOCAL_PROXY_PREFS_PATH.write_text(json.dumps(payload), encoding="utf-8")

    preferences = local_proxy.load_local_proxy_preferences()

    assert preferences["start_on_login"] is False
    assert preferences["keep_running_on_exit"] is False
    assert preferences["proxy_non_cn"] is True
    assert preferences["strict_privacy"] is True
    assert preferences["builtin_sites"]["github"] is True
    assert preferences["builtin_sites"]["youtube"] is False
    assert preferences["custom_targets"][0]["enabled"] is False
    assert preferences["custom_targets"][1]["enabled"] is True


def test_local_proxy_preferences_cache_reuses_unchanged_file(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    payload = {
        "start_on_login": True,
        "keep_running_on_exit": True,
        "proxy_non_cn": True,
        "builtin_sites": {"youtube": True},
    }
    local_proxy.LOCAL_PROXY_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    local_proxy.LOCAL_PROXY_PREFS_PATH.write_text(json.dumps(payload), encoding="utf-8")
    local_proxy.clear_local_proxy_preferences_cache()
    original_loads = local_proxy.json.loads
    calls = {"count": 0}

    def counting_loads(text, *args, **kwargs):
        calls["count"] += 1
        return original_loads(text, *args, **kwargs)

    monkeypatch.setattr(local_proxy.json, "loads", counting_loads)

    first = local_proxy.load_local_proxy_preferences()
    first["proxy_non_cn"] = False
    second = local_proxy.load_local_proxy_preferences()

    assert calls["count"] == 1
    assert second["proxy_non_cn"] is True
    assert second["builtin_sites"]["youtube"] is True


def test_corrupt_local_proxy_preferences_are_quarantined(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    local_proxy.LOCAL_PROXY_PREFS_PATH.write_text("{bad prefs", encoding="utf-8")
    local_proxy.clear_local_proxy_preferences_cache()

    preferences = local_proxy.load_local_proxy_preferences()

    assert preferences["keep_running_on_exit"] is True
    assert not local_proxy.LOCAL_PROXY_PREFS_PATH.exists()
    corrupt_files = list(tmp_path.glob("preferences.json.corrupt-*"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == "{bad prefs"

    local_proxy.set_local_proxy_start_on_login(True)
    restored = local_proxy.load_local_proxy_preferences()

    assert restored["start_on_login"] is True
    assert local_proxy.LOCAL_PROXY_PREFS_PATH.exists()


def test_local_proxy_preference_setters_parse_string_booleans(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")

    local_proxy.set_local_proxy_start_on_login("false")
    local_proxy.set_local_proxy_keep_running_on_exit("off")
    local_proxy.set_local_proxy_non_cn_mode("yes")
    local_proxy.set_local_proxy_strict_privacy("true")
    local_proxy.set_builtin_proxy_site_enabled("github", "true")
    disabled = local_proxy.add_custom_proxy_target("example.com", enabled="false")
    enabled = local_proxy.add_custom_proxy_target("8.8.8.8", enabled="on")

    preferences = local_proxy.load_local_proxy_preferences()

    assert preferences["start_on_login"] is False
    assert preferences["keep_running_on_exit"] is False
    assert preferences["proxy_non_cn"] is True
    assert preferences["strict_privacy"] is True
    assert preferences["builtin_sites"]["github"] is True
    assert disabled["enabled"] is False
    assert enabled["enabled"] is True


def test_local_proxy_auto_start_uses_last_saved_node(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", tmp_path / "mihomo")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", tmp_path / "mihomo.pid")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    local_proxy.set_local_proxy_start_on_login(True)
    local_proxy.save_local_proxy_preferences(
        last_node={"name": "saved", "type": "vless", "server": "saved.example.com", "port": 443}
    )
    starts = []
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(
        local_proxy,
        "install_local_ai_proxy",
        lambda text, **_kwargs: starts.append(text) or "started",
    )
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_ai_proxy_after_failover_warmup",
        lambda: ("本机 AI 连通性 4/4 可达", False),
    )

    assert "验证通过" in local_proxy.auto_start_local_ai_proxy_if_enabled()
    assert "saved.example.com" in starts[0]


def test_local_proxy_startup_node_can_be_saved_from_current_node(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", tmp_path / "mihomo")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", tmp_path / "mihomo.pid")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)

    summary = local_proxy.set_local_proxy_startup_node(
        "{ name: boot, type: vless, server: boot.example.com, port: 443 }"
    )
    local_proxy.set_local_proxy_start_on_login(True)
    starts = []
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: False)
    monkeypatch.setattr(
        local_proxy,
        "install_local_ai_proxy",
        lambda text, **_kwargs: starts.append(text) or "started",
    )
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_ai_proxy_after_failover_warmup",
        lambda: ("本机 AI 连通性 4/4 可达", False),
    )

    assert "boot.example.com" in summary
    assert "boot.example.com" in local_proxy.local_proxy_startup_node_summary()
    assert "验证通过" in local_proxy.auto_start_local_ai_proxy_if_enabled()
    assert "boot.example.com" in starts[0]


def test_local_proxy_startup_node_reports_save_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", tmp_path / "mihomo")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", tmp_path / "mihomo.pid")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")

    def fail_save_preferences(**_updates):
        raise OSError("readonly preferences")

    monkeypatch.setattr(local_proxy, "save_local_proxy_preferences", fail_save_preferences)

    with pytest.raises(OSError, match="readonly preferences"):
        local_proxy.set_local_proxy_startup_node(
            "{ name: boot, type: vless, server: boot.example.com, port: 443 }"
        )


def test_local_proxy_auto_start_skips_when_managed_proxy_is_alive(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    local_proxy._save_state({"mixed_port": 17898})
    local_proxy.set_local_proxy_start_on_login(True)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda port: port == 17898)
    monkeypatch.setattr(
        local_proxy,
        "install_local_ai_proxy",
        lambda _text: (_ for _ in ()).throw(AssertionError("should not restart live managed proxy")),
    )

    message = local_proxy.auto_start_local_ai_proxy_if_enabled()

    assert "已在运行" in message
    assert "17898" in message


def test_local_proxy_auto_start_preserves_existing_verified_fallback_pool(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    local_proxy.set_local_proxy_start_on_login(True)
    primary = {"name": "primary", "type": "vless", "server": "primary.example.com", "port": 443}
    fallback = {"name": "backup", "type": "vless", "server": "backup.example.com", "port": 443}
    local_proxy.save_local_proxy_preferences(last_node=primary)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: False)
    monkeypatch.setattr(local_proxy, "_existing_local_proxy_fallback_nodes", lambda _node: (fallback,))
    starts = []

    def capture_start(text, **kwargs):
        starts.append((text, kwargs))
        return "started"

    monkeypatch.setattr(local_proxy, "install_local_ai_proxy", capture_start)
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_ai_proxy_after_failover_warmup",
        lambda: ("本机 AI 连通性 4/4 可达", False),
    )

    assert "验证通过" in local_proxy.auto_start_local_ai_proxy_if_enabled()
    assert starts[0][1]["fallback_nodes"] == (fallback,)


def test_local_proxy_auto_start_recovers_collapsed_pool_from_linked_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "preferences.json")
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    local_proxy.set_local_proxy_start_on_login(True)
    primary = {"name": "primary", "type": "vless", "server": "primary.example.com", "port": 443}
    fallback = {"name": "backup", "type": "vless", "server": "backup.example.com", "port": 443}
    local_proxy.save_local_proxy_preferences(last_node=primary)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: False)
    monkeypatch.setattr(local_proxy, "_existing_local_proxy_fallback_nodes", lambda _node: ())
    monkeypatch.setattr(local_proxy, "_cached_subscription_fallback_nodes", lambda _node: (fallback,))
    starts = []
    monkeypatch.setattr(
        local_proxy,
        "install_local_ai_proxy",
        lambda text, **kwargs: starts.append((text, kwargs)) or "started",
    )
    monkeypatch.setattr(
        local_proxy,
        "_probe_local_ai_proxy_after_failover_warmup",
        lambda: ("本机 AI 连通性 4/4 可达", False),
    )

    assert "验证通过" in local_proxy.auto_start_local_ai_proxy_if_enabled()
    assert starts[0][1]["fallback_nodes"] == (fallback,)


def test_cached_subscription_pool_requires_exact_selected_node_link(monkeypatch):
    primary = {"name": "manual", "type": "vless", "server": "manual.example.com", "port": 443}
    cache_loads = []
    monkeypatch.setattr(
        remote_proxy,
        "load_proxy_subscription_state",
        lambda: {"selected_node_key": "0" * 64},
    )
    monkeypatch.setattr(
        remote_proxy,
        "load_cached_proxy_subscription",
        lambda _state: cache_loads.append(True),
    )

    assert local_proxy._cached_subscription_fallback_nodes(primary) == ()
    assert cache_loads == []


def test_cached_subscription_pool_uses_linked_managed_nodes(monkeypatch):
    primary = {"name": "primary", "type": "vless", "server": "primary.example.com", "port": 443}
    fallback = {"name": "backup", "type": "vless", "server": "backup.example.com", "port": 443}
    item = remote_proxy.ProxySubscriptionNode(index=1, node=fallback)
    state = {"selected_node_key": remote_proxy.proxy_node_key(primary)}
    qualities = {"quality": {"ok": True}}
    captured = []
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(
        remote_proxy,
        "load_cached_proxy_subscription",
        lambda _state: remote_proxy.ProxySubscriptionResult(nodes=(item,), saved_path="managed.yaml"),
    )
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda _state: qualities)
    monkeypatch.setattr(
        local_proxy,
        "_local_proxy_fallback_nodes",
        lambda selected, nodes, evidence: captured.append((selected, nodes, evidence)) or (fallback,),
    )

    assert local_proxy._cached_subscription_fallback_nodes(primary) == (fallback,)
    assert captured == [(primary, (item,), qualities)]


def test_apply_local_proxy_routing_skips_unmanaged_listener(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    local_proxy._save_state({"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(
        local_proxy,
        "reload_local_ai_proxy",
        lambda _text: (_ for _ in ()).throw(AssertionError("should not reload unmanaged proxy")),
    )

    message = local_proxy.apply_local_proxy_routing_to_running()

    assert "下次启动时生效" in message


def test_local_proxy_state_cache_reuses_reads_and_detects_external_write(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    local_proxy.LOCAL_PROXY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    local_proxy.LOCAL_PROXY_STATE_PATH.write_text(json.dumps({"mixed_port": 17897}), encoding="utf-8")
    local_proxy.clear_local_proxy_state_cache()
    original_loads = local_proxy.json.loads
    calls = {"count": 0}

    def counting_loads(text, *args, **kwargs):
        calls["count"] += 1
        return original_loads(text, *args, **kwargs)

    monkeypatch.setattr(local_proxy.json, "loads", counting_loads)

    first = local_proxy._load_state()
    first["mixed_port"] = 18000
    second = local_proxy._load_state()

    assert calls["count"] == 1
    assert second["mixed_port"] == 17897

    local_proxy.LOCAL_PROXY_STATE_PATH.write_text(json.dumps({"mixed_port": 17898}), encoding="utf-8")
    third = local_proxy._load_state()

    assert calls["count"] == 2
    assert third["mixed_port"] == 17898


def test_corrupt_local_proxy_state_is_quarantined(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    local_proxy.LOCAL_PROXY_STATE_PATH.write_text("{bad state", encoding="utf-8")
    local_proxy.clear_local_proxy_state_cache()

    state = local_proxy._load_state()

    assert state == {}
    assert not local_proxy.LOCAL_PROXY_STATE_PATH.exists()
    corrupt_files = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == "{bad state"

    local_proxy._save_state({"mixed_port": 17899})
    restored = local_proxy._load_state()

    assert restored["mixed_port"] == 17899
    assert local_proxy.LOCAL_PROXY_STATE_PATH.exists()


def test_subscription_auto_update_skips_unmanaged_local_proxy(monkeypatch, tmp_path):
    node = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node("{ name: node, type: vless, server: example.com, port: 443 }"),
    )
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    local_proxy._save_state({"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(
        local_proxy,
        "reload_local_ai_proxy_verified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not update unmanaged proxy")),
    )

    message = local_proxy.refresh_running_local_ai_proxy_from_subscription([node])

    assert "未运行" in message


def test_subscription_auto_update_never_selects_hong_kong(monkeypatch):
    hong_kong = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: HKG 01, type: vless, server: hk.example.com, port: 443 }"
        ),
    )
    current = remote_proxy.parse_proxy_node(
        "{ name: current-us, type: vless, server: old.example.com, port: 443 }"
    )
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: current)
    monkeypatch.setattr(
        remote_proxy,
        "measure_proxy_node_latencies",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Hong Kong must not enter automatic latency selection")
        ),
    )
    monkeypatch.setattr(
        local_proxy,
        "reload_local_ai_proxy_verified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Hong Kong must not be automatically reloaded")
        ),
    )

    message = local_proxy.refresh_running_local_ai_proxy_from_subscription([hong_kong])

    assert "仅有香港节点" in message
    assert "香港仅允许手动选择" in message
    assert "已保留当前运行节点" in message


def test_subscription_auto_update_deep_failure_keeps_running_node_without_reload(monkeypatch):
    candidate = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: new-us, type: vless, server: new.example.com, port: 443 }"
        ),
    )
    current = remote_proxy.parse_proxy_node(
        "{ name: current-us, type: vless, server: old.example.com, port: 443 }"
    )
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: current)
    monkeypatch.setattr(
        local_proxy,
        "_select_stable_automatic_local_candidate",
        lambda *_args, **_kwargs: (None, None, {}, {}),
    )
    monkeypatch.setattr(
        local_proxy,
        "reload_local_ai_proxy_verified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("deep-failed candidate must never be applied")
        ),
    )

    message = local_proxy.refresh_running_local_ai_proxy_from_subscription([candidate])

    assert "没有候选通过 Codex 长会话网络深测" in message
    assert "已保留当前运行节点" in message


def test_subscription_auto_update_missing_current_node_never_deep_tests_or_reloads(monkeypatch):
    candidate = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: new-us, type: vless, server: new.example.com, port: 443 }"
        ),
    )
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: None)
    monkeypatch.setattr(
        local_proxy,
        "_select_stable_automatic_local_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not deep-test without a recoverable original node")
        ),
    )

    message = local_proxy.refresh_running_local_ai_proxy_from_subscription([candidate])

    assert "无法读取当前运行节点" in message
    assert "已保留当前运行节点" in message


def test_subscription_auto_update_discards_result_if_node_changes_during_deep_test(monkeypatch):
    candidate = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: new-us, type: vless, server: new.example.com, port: 443 }"
        ),
    )
    original = remote_proxy.parse_proxy_node(
        "{ name: original, type: vless, server: old.example.com, port: 443 }"
    )
    manually_changed = remote_proxy.parse_proxy_node(
        "{ name: manual, type: vless, server: manual.example.com, port: 443 }"
    )
    stable = _stable_local_prevalidation(candidate)
    reads = iter((original, manually_changed))
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: next(reads))
    monkeypatch.setattr(
        local_proxy,
        "_select_stable_automatic_local_candidate",
        lambda *_args, **_kwargs: (candidate, stable, {stable.node_key: stable}, {}),
    )
    monkeypatch.setattr(
        local_proxy,
        "reload_local_ai_proxy_verified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale deep result must not be applied")
        ),
    )

    message = local_proxy.refresh_running_local_ai_proxy_from_subscription([candidate])

    assert "深测期间当前运行节点已变化" in message
    assert "未切换节点" in message


def test_subscription_auto_update_passes_exact_deep_prevalidation_once(monkeypatch):
    candidate = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node(
            "{ name: new-us, type: vless, server: new.example.com, port: 443 }"
        ),
    )
    current = remote_proxy.parse_proxy_node(
        "{ name: current-us, type: vless, server: old.example.com, port: 443 }"
    )
    stable = _stable_local_prevalidation(candidate)
    calls = {"select": 0, "reload": []}
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: current)

    def fake_select(*_args, **_kwargs):
        calls["select"] += 1
        return candidate, stable, {stable.node_key: stable}, {}

    def fake_reload(text, *_args, **kwargs):
        calls["reload"].append((remote_proxy.proxy_node_key(remote_proxy.parse_proxy_node(text)), kwargs))
        return "本机 AI 代理已热更新；验证通过"

    monkeypatch.setattr(local_proxy, "_select_stable_automatic_local_candidate", fake_select)
    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy_verified", fake_reload)

    message = local_proxy.refresh_running_local_ai_proxy_from_subscription(
        [candidate], profile_id="profile-a"
    )

    assert message.endswith("验证通过")
    assert calls["select"] == 1
    assert len(calls["reload"]) == 1
    applied_key, kwargs = calls["reload"][0]
    assert applied_key == stable.node_key
    assert kwargs["automatic_update"] is True
    assert kwargs["_prevalidated_result"] is stable
    assert remote_proxy.proxy_node_key(kwargs["_expected_original_node"]) == remote_proxy.proxy_node_key(current)
    assert kwargs["profile_id"] == "profile-a"


def test_automatic_reload_rejects_mismatched_prevalidation_before_apply(monkeypatch):
    requested = remote_proxy.parse_proxy_node(
        "{ name: requested, type: vless, server: requested.example.com, port: 443 }"
    )
    other = remote_proxy.parse_proxy_node(
        "{ name: other, type: vless, server: other.example.com, port: 443 }"
    )
    wrong_result = _stable_local_prevalidation(other)
    monkeypatch.setattr(
        local_proxy,
        "reload_local_ai_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("mismatched prevalidation must be rejected before reload")
        ),
    )

    message = local_proxy.reload_local_ai_proxy_verified(
        remote_proxy.format_proxy_node(requested),
        automatic_update=True,
        _prevalidated_result=wrong_result,
    )

    assert "未通过精确匹配" in message
    assert "已保留当前运行节点" in message


def test_restore_local_proxy_node_does_not_report_skipped_reload_as_success(monkeypatch):
    original = remote_proxy.parse_proxy_node(
        "{ name: original, type: vless, server: old.example.com, port: 443 }"
    )
    attempted = remote_proxy.parse_proxy_node(
        "{ name: attempted, type: vless, server: new.example.com, port: 443 }"
    )
    monkeypatch.setattr(
        local_proxy,
        "reload_local_ai_proxy",
        lambda *_args, **_kwargs: "本机 AI 代理未运行，已跳过热更新",
    )
    monkeypatch.setattr(
        local_proxy,
        "probe_local_ai_proxy",
        lambda: (_ for _ in ()).throw(AssertionError("skipped reload must not be probed")),
    )

    message = local_proxy._restore_local_proxy_node_after_failed_update(original, attempted)

    assert "恢复更新前节点未执行成功" in message
    assert "已恢复更新前节点" not in message


def test_probe_local_ai_proxy_reports_each_target(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mixed-port: 17897", encoding="utf-8")
    probes = {
        "OpenAI API": local_proxy.LocalAIProxyProbeResult(
            "OpenAI API", True, status=401, elapsed_ms=10
        ),
        "OpenAI/ChatGPT": local_proxy.LocalAIProxyProbeResult(
            "OpenAI/ChatGPT", True, status=403, elapsed_ms=11
        ),
        "Claude/Anthropic": local_proxy.LocalAIProxyProbeResult(
            "Claude/Anthropic", True, status=405, elapsed_ms=12
        ),
        "Gemini/Google AI": local_proxy.LocalAIProxyProbeResult(
            "Gemini/Google AI", False, detail="timeout", elapsed_ms=13
        ),
    }

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
    monkeypatch.setattr(
        local_proxy,
        "_probe_url_through_proxy",
        lambda _proxy, label, _url, **_kwargs: probes[label],
    )

    summary = local_proxy.probe_local_ai_proxy()

    assert "AI 连通性 3/4 可达" in summary
    assert "OpenAI API: 可达 / HTTP 401 / 10ms" in summary
    assert "OpenAI/ChatGPT: 可达 / HTTP 403 / 11ms" in summary
    assert "Gemini/Google AI: 失败 / timeout / 13ms" in summary


def test_probe_local_ai_proxy_runs_all_targets_in_parallel(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mixed-port: 17897", encoding="utf-8")
    barrier = threading.Barrier(len(local_proxy.LOCAL_AI_PROBE_TARGETS), timeout=1.0)
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

    def probe(_proxy, label, _url, **_kwargs):
        barrier.wait()
        return local_proxy.LocalAIProxyProbeResult(label, True, status=200)

    monkeypatch.setattr(local_proxy, "_probe_url_through_proxy", probe)

    summary = local_proxy.probe_local_ai_proxy()

    assert "AI 连通性 4/4 可达" in summary


def test_reload_local_ai_proxy_uses_controller_and_updates_state(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(remote_proxy.build_mihomo_config({"name": "old", "type": "vless", "server": "old.example.com", "port": 443}, 17897), encoding="utf-8")
    saved_states = []

    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897, "config_path": str(config_path)})
    monkeypatch.setattr(local_proxy, "_save_state", lambda state: saved_states.append(state))
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 4321)
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda *_args, **_kwargs: local_proxy.LocalAIProxyStatus(
            installed=True,
            running=True,
            config_path=str(config_path),
            proxy_url="http://127.0.0.1:17897",
        ),
    )
    monkeypatch.setattr(local_proxy, "_reload_local_mihomo_config", lambda path, port: None)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = local_proxy.reload_local_ai_proxy("{ name: new, type: vless, server: new.example.com, port: 443 }")

    assert "已热更新" in message
    assert "new.example.com" in config_path.read_text(encoding="utf-8")
    assert saved_states[-1]["node_name"] == "new"
    assert saved_states[-1]["applied_config_pid"] == 4321


def test_reload_local_ai_proxy_forces_old_config_after_ambiguous_controller_failure(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.yaml"
    original = remote_proxy.build_mihomo_config(
        {"name": "old", "type": "vless", "server": "old.example.com", "port": 443},
        17897,
    )
    config_path.write_text(original, encoding="utf-8")
    reload_snapshots = []

    monkeypatch.setattr(
        local_proxy,
        "_load_state",
        lambda: {"mixed_port": 17897, "config_path": str(config_path)},
    )
    monkeypatch.setattr(local_proxy, "_save_state", lambda _state: None)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda *_args, **_kwargs: local_proxy.LocalAIProxyStatus(
            installed=True,
            running=True,
            config_path=str(config_path),
            proxy_url="http://127.0.0.1:17897",
        ),
    )

    def fake_reload(path, _port):
        reload_snapshots.append(path.read_text(encoding="utf-8"))
        if len(reload_snapshots) == 1:
            raise TimeoutError("controller response lost")

    monkeypatch.setattr(local_proxy, "_reload_local_mihomo_config", fake_reload)

    with pytest.raises(RuntimeError, match="已强制恢复旧配置"):
        local_proxy.reload_local_ai_proxy(
            "{ name: new, type: vless, server: new.example.com, port: 443 }"
        )

    assert len(reload_snapshots) == 2
    assert "new.example.com" in reload_snapshots[0]
    assert reload_snapshots[1] == original
    assert config_path.read_text(encoding="utf-8") == original


def test_reload_local_proxy_skips_unmanaged_listener(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    original = remote_proxy.build_mihomo_config(
        {"name": "old", "type": "vless", "server": "old.example.com", "port": 443},
        17897,
    )
    config_path.write_text(original, encoding="utf-8")

    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897, "config_path": str(config_path)})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(
        local_proxy,
        "_reload_local_mihomo_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not reload unmanaged proxy")),
    )

    message = local_proxy.reload_local_ai_proxy("{ name: new, type: vless, server: new.example.com, port: 443 }")

    assert "不是本工具受管进程" in message
    assert config_path.read_text(encoding="utf-8") == original


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
    monkeypatch.setattr(
        remote_proxy,
        "_subscription_proxy_environment_diagnostic",
        lambda _url: remote_proxy.ProxyEnvironmentDiagnostic(),
    )
    monkeypatch.setattr(local_proxy.time, "sleep", lambda _seconds: None)

    payload = local_proxy._read_url_with_retries(
        local_proxy.urllib.request.Request("https://example.com/file"),
        timeout=1,
        label="下载测试",
        retries=2,
    )

    assert payload == b"ok"
    assert len(calls) == 2


def test_read_url_with_retries_skips_refused_loopback_proxy(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"ok"

    class DirectOpener:
        def open(self, _request, **_kwargs):
            calls.append("direct")
            return Response()

    monkeypatch.setattr(
        remote_proxy,
        "_subscription_proxy_environment_diagnostic",
        lambda _url: remote_proxy.ProxyEnvironmentDiagnostic(
            invalid_proxy_urls=("http://127.0.0.1:7897",),
            invalid_windows_proxy=True,
        ),
    )
    monkeypatch.setattr(
        local_proxy.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("refused loopback proxy must be skipped")
        ),
    )
    monkeypatch.setattr(
        local_proxy.urllib.request,
        "build_opener",
        lambda _handler: DirectOpener(),
    )

    payload = local_proxy._read_url_with_retries(
        local_proxy.urllib.request.Request("https://example.com/file"),
        timeout=1,
        label="下载测试",
        retries=2,
    )

    assert payload == b"ok"
    assert calls == ["direct"]


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


def test_install_local_proxy_saves_restore_checkpoint_before_start(monkeypatch, tmp_path):
    saved_states = []

    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", tmp_path / "mihomo")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", tmp_path / "mihomo.pid")
    monkeypatch.setattr(local_proxy, "_select_local_mixed_port", lambda _port: 17897)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: tmp_path / "mihomo.exe")
    monkeypatch.setattr(local_proxy, "_capture_previous_env", lambda: {"HTTP_PROXY": {"exists": True, "value": "old"}})
    monkeypatch.setattr(local_proxy, "_capture_vscode_proxy_state", lambda _settings: {"http.proxy": {"exists": False}})
    monkeypatch.setattr(local_proxy, "_capture_windows_system_proxy_state", lambda: {"ProxyEnable": {"exists": True, "value": 0, "type": 4}})
    monkeypatch.setattr(local_proxy.vscode_parser, "read_vscode_settings", lambda: {})
    monkeypatch.setattr(local_proxy, "_save_state", lambda state: saved_states.append(dict(state)))
    monkeypatch.setattr(local_proxy, "_start_local_mihomo", lambda *_args: (_ for _ in ()).throw(RuntimeError("start failed")))
    monkeypatch.setattr(local_proxy, "_restore_local_env", lambda *_args: None)
    monkeypatch.setattr(local_proxy, "_restore_local_vscode_proxy", lambda *_args: None)
    monkeypatch.setattr(local_proxy, "_restore_windows_system_proxy", lambda *_args: None)
    monkeypatch.setattr(local_proxy, "_cleanup_managed_process", lambda *_args: None)

    with pytest.raises(RuntimeError, match="start failed"):
        local_proxy.install_local_ai_proxy("{ name: node, type: vless, server: example.com, port: 443 }")

    assert saved_states[0]["installing"] is True
    assert saved_states[0]["previous_env"]["HTTP_PROXY"]["value"] == "old"
    assert saved_states[0]["config_path"].endswith("config.yaml")
    assert saved_states[-1] == {}


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


def test_stop_local_proxy_keeps_restore_state_when_restore_fails(monkeypatch, tmp_path):
    saved_states = []
    state = {
        "mixed_port": 17897,
        "pid": 12345,
        "previous_env": {"HTTP_PROXY": {"exists": True, "value": "old"}},
    }

    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", tmp_path / "missing.pid")
    monkeypatch.setattr(local_proxy, "_load_state", lambda: dict(state))
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: None)
    monkeypatch.setattr(
        local_proxy,
        "_restore_local_env",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("env restore failed")),
    )
    monkeypatch.setattr(local_proxy, "_restore_local_vscode_proxy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(local_proxy, "_restore_windows_system_proxy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(local_proxy, "_save_state", lambda next_state: saved_states.append(dict(next_state)))

    message = local_proxy.stop_local_ai_proxy()

    assert "恢复设置失败" in message
    assert saved_states[-1]["previous_env"] == state["previous_env"]
    assert "pid" not in saved_states[-1]
    assert "env restore failed" in saved_states[-1]["last_restore_error"]


def test_fetch_proxy_subscription_honors_retry_after_for_rate_limit(monkeypatch, tmp_path):
    class Headers(dict):
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
            return b"proxies:\n  - { name: rate-limited, type: vless, server: example.com, port: 443 }\n"

    calls = 0
    sleeps = []

    def fake_urlopen(request, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            headers = Headers({"Retry-After": "2"})
            raise remote_proxy.HTTPError(request.full_url, 429, "Too Many Requests", headers, None)
        return Response()

    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy.urlrequest, "urlopen", fake_urlopen)
    monkeypatch.setattr(remote_proxy.urlrequest, "getproxies", lambda: {})
    monkeypatch.setattr(remote_proxy.time, "sleep", lambda delay: sleeps.append(delay))

    result = remote_proxy.fetch_proxy_subscription(
        "https://example.com/sub",
        timeout=10,
        retry_base_delay=0,
    )

    assert calls == 2
    assert sleeps == [2.0]
    assert result.nodes[0].node["name"] == "rate-limited"
