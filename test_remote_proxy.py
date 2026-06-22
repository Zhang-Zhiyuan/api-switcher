from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

import pytest

from core import local_proxy, network_diagnostic_settings, network_diagnostics, remote_proxy


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
    assert 'name: "node-a"' in config
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
        return [(None, None, None, "", ("198.51.100.77", 0))]

    def http_get(url, _timeout):
        if "proxycheck.io/v3/198.51.100.77" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        "198.51.100.77": {
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
        if "ipwho.is/198.51.100.77" in url:
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
    assert result.ip == "198.51.100.77"
    assert result.quality_label == "家宽高质"
    assert result.quality_score >= 90
    assert result.risk_score == 8
    assert result.sources == (network_diagnostic_settings.SERVICE_PROXYCHECK,)
    assert remote_proxy.proxy_node_quality_source_label(result) == "ProxyCheck"
    assert remote_proxy.proxy_node_quality_for_ai_proxy_ok(result) is True


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
            ip="198.51.100.80",
            region="其他",
            ip_type="家庭宽带/住宅 IP",
            risk_score=6,
            risk_label="极低",
            quality_score=100,
            quality_label="家宽高质",
            detail="Net.Coffee trust_score=94",
            checked_at=remote_proxy._now_iso(),
            sources=(network_diagnostic_settings.SERVICE_NETCOFFEE,),
            quality_signature=signature,
        )
    })

    result = remote_proxy.assess_proxy_node_quality(
        node,
        http_get=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network should be skipped")),
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("198.51.100.80", 0))],
        settings=settings,
        enabled_services=[network_diagnostic_settings.SERVICE_NETCOFFEE],
    )

    assert result.ok is True
    assert result.cached is True
    assert result.quality_score == 100
    assert result.quality_signature == signature
    assert "缓存命中" in result.detail


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
            ip="198.51.100.81",
            ip_type="家庭宽带/住宅 IP",
            quality_score=100,
            quality_label="家宽高质",
            checked_at=remote_proxy._now_iso(),
            sources=(network_diagnostic_settings.SERVICE_NETCOFFEE,),
            quality_signature=signature,
        )
    })

    new_ip = "198.51.100.82"
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


def test_assess_proxy_node_quality_rejects_residential_business_conflict_for_ai_proxy():
    node = remote_proxy.parse_proxy_node(
        "{ name: AI代理冲突, type: vless, server: mixed.example.com, port: 443 }"
    )

    def resolver(host, *_args, **_kwargs):
        assert host == "mixed.example.com"
        return [(None, None, None, "", ("198.51.100.78", 0))]

    def http_get(url, _timeout):
        if "proxycheck.io/v3/198.51.100.78" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        "198.51.100.78": {
                            "network": {"type": "Residential", "provider": "Example Fiber"},
                            "detections": {"anonymous": False, "risk": 7},
                        },
                    }
                ),
            )
        if "ipqualityscore.com/api/json/ip/ipqs-key/198.51.100.78" in url:
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
        if "ipwho.is/198.51.100.78" in url:
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
        return [(None, None, None, "", ("198.51.100.79", 0))]

    def http_get(url, _timeout):
        if "proxycheck.io/v3/198.51.100.79" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps(
                    {
                        "status": "ok",
                        "198.51.100.79": {
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
        if "ipwho.is/198.51.100.79" in url:
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
        resolver=lambda *_args, **_kwargs: [(None, None, None, "", ("198.51.100.88", 0))],
        settings=network_diagnostic_settings.settings_from_values(
            {network_diagnostic_settings.SERVICE_PROXYCHECK},
            {},
        ),
    )

    assert result.ok is False
    assert result.ip == "198.51.100.88"
    assert result.quality_label == "检测失败"
    assert result.sources == (network_diagnostic_settings.SERVICE_PROXYCHECK,)
    assert "quota exploded" in result.detail


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
            ip="198.51.100.90",
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
        ),
    }

    sorted_nodes = remote_proxy.sort_proxy_subscription_nodes(nodes, latencies, qualities, prefer_quality=True)
    best = remote_proxy.best_proxy_subscription_node_for_ai_proxy(nodes, qualities, latencies)

    assert sorted_nodes[0].node["name"] == "美国 家宽"
    assert best is not None
    assert best.node["name"] == "美国 家宽"


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


def test_start_script_checks_port_with_netstat_when_ss_is_missing():
    script = remote_proxy._build_start_script("/home/me/.config/mihomo", "/home/me/.config/api-switcher", "/home/me/bin", 7890)

    assert "command -v netstat" in script
    assert "pid_managed()" in script
    assert "kill -9" in script
    assert "pid file points to unmanaged process" in script
    assert "port $PORT is already listening before starting mihomo" in script


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
    assert "download failed after 3 attempts" in command


def test_remote_reload_command_calls_mihomo_controller():
    command = remote_proxy._build_reload_command("/home/me/.config/mihomo/config.yaml", 7890)

    assert "127.0.0.1:8890/configs?force=true" in command
    assert '"path": "/home/me/.config/mihomo/config.yaml"' in command
    assert 'method="PUT"' in command


def test_reload_ai_proxy_restores_config_when_controller_fails(monkeypatch):
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
    monkeypatch.setattr(remote_proxy.ssh_manager, "read_remote_file", lambda *_args, **_kwargs: "old config")
    monkeypatch.setattr(remote_proxy.ssh_manager, "write_remote_file", lambda _client, _path, content, **_kwargs: writes.append(content))
    monkeypatch.setattr(
        remote_proxy.ssh_manager,
        "execute_command_with_status",
        lambda *_args, **_kwargs: (7, "", "connection refused"),
    )

    with pytest.raises(RuntimeError, match="控制口不可用"):
        remote_proxy.reload_ai_proxy("server", "{ name: node, type: vless, server: example.com, port: 443 }")

    assert writes[-1] == "old config"


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
            "本机 AI 代理已配置，运行中: http://127.0.0.1:17897；AI 连通性 0/3 可达",
            "本机 AI 代理已配置，运行中: http://127.0.0.1:17897；AI 连通性 3/3 可达",
        ]
    )
    latencies = {
        remote_proxy.proxy_node_key(candidate.node): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_node_key(candidate.node),
            True,
            latency_ms=35,
        )
    }

    def fake_reload(text):
        reloads.append(remote_proxy.parse_proxy_node(text)["name"])
        return f"本机 AI 代理已热更新节点为 {reloads[-1]}"

    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: original)
    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy", fake_reload)
    monkeypatch.setattr(local_proxy, "probe_local_ai_proxy", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", lambda *_args, **_kwargs: latencies)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = local_proxy.reload_local_ai_proxy_verified(
        remote_proxy.format_proxy_node(requested.node),
        [requested, candidate],
    )

    assert reloads == ["bad", "worse", "old"]
    assert "已恢复更新前节点 old" in message
    assert "验证通过" in message


def test_remote_cleanup_command_backs_up_legacy_proxy_configs_and_removes_managed_blocks():
    command = remote_proxy._build_cleanup_command("/home/me", 7890, include_legacy_config=True)

    assert "proxy-cleanup-backup" in command
    assert remote_proxy.AI_PROXY_CONFIG_MARKER in command
    assert "start-ai-proxy.sh" in command
    assert "server-env-setup" in command
    assert "VS Code settings JSON" in command
    assert "kill -9" in command
    assert "backed_up_configs" in command


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
    assert "AI 连通性 1/2 可达" in summary
    assert "OpenAI/ChatGPT: 可达 / HTTP 403 / 11ms" in summary


def test_install_ai_proxy_verified_keeps_working_requested_node(monkeypatch):
    installs = []
    monkeypatch.setattr(remote_proxy, "install_ai_proxy", lambda _server, text, _port=7890: installs.append(text) or "installed")
    monkeypatch.setattr(
        remote_proxy,
        "probe_ai_proxy",
        lambda *_args, **_kwargs: "server: AI 代理已配置，运行中: http://127.0.0.1:7890；AI 连通性 3/3 可达",
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


def test_install_ai_proxy_verified_falls_back_to_working_candidate(monkeypatch):
    requested = remote_proxy.ProxySubscriptionNode(
        1,
        remote_proxy.parse_proxy_node("{ name: bad, type: vless, server: bad.example.com, port: 443 }"),
    )
    candidate = remote_proxy.ProxySubscriptionNode(
        2,
        remote_proxy.parse_proxy_node("{ name: good, type: vless, server: good.example.com, port: 443 }"),
    )
    installs = []
    probes = iter([
        "server: AI 代理已配置，运行中: http://127.0.0.1:7890；AI 连通性 0/3 可达",
        "server: AI 代理已配置，运行中: http://127.0.0.1:7890；AI 连通性 3/3 可达",
    ])

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
    monkeypatch.setattr(remote_proxy, "probe_ai_proxy", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", lambda *_args, **_kwargs: latencies)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = remote_proxy.install_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested.node),
        [requested, candidate],
    )

    assert installs == ["bad", "good"]
    assert "自动切换到 good" in message
    assert "验证通过" in message


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
    probes = iter([
        "server: AI 代理已配置，运行中: http://127.0.0.1:7890；AI 连通性 0/3 可达",
        "server: AI 代理已配置，运行中: http://127.0.0.1:7890；AI 连通性 3/3 可达",
    ])

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
    monkeypatch.setattr(remote_proxy, "probe_ai_proxy", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", lambda *_args, **_kwargs: latencies)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = remote_proxy.install_ai_proxy_verified(
        "server",
        remote_proxy.format_proxy_node(requested.node),
        [requested, fast_hosting, slow_residential],
        quality_results=qualities,
    )

    assert installs == ["bad", "slow-residential"]
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
    assert values["AutoConfigURL"] == ""
    assert values["AutoDetect"] == 0
    assert local_proxy._windows_system_proxy_matches_values(values, 17897) is True
    assert local_proxy._windows_system_proxy_matches_values({**values, "ProxyServer": "127.0.0.1:18000"}, 17897) is False
    assert local_proxy._windows_system_proxy_matches_values({**values, "AutoDetect": 1}, 17897) is False


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
    local_proxy.set_builtin_proxy_site_enabled("github", "true")
    disabled = local_proxy.add_custom_proxy_target("example.com", enabled="false")
    enabled = local_proxy.add_custom_proxy_target("8.8.8.8", enabled="on")

    preferences = local_proxy.load_local_proxy_preferences()

    assert preferences["start_on_login"] is False
    assert preferences["keep_running_on_exit"] is False
    assert preferences["proxy_non_cn"] is True
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
    monkeypatch.setattr(local_proxy, "install_local_ai_proxy", lambda text: starts.append(text) or "started")

    assert local_proxy.auto_start_local_ai_proxy_if_enabled() == "started"
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
    monkeypatch.setattr(local_proxy, "install_local_ai_proxy", lambda text: starts.append(text) or "started")

    assert "boot.example.com" in summary
    assert "boot.example.com" in local_proxy.local_proxy_startup_node_summary()
    assert local_proxy.auto_start_local_ai_proxy_if_enabled() == "started"
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


def test_reload_local_ai_proxy_uses_controller_and_updates_state(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(remote_proxy.build_mihomo_config({"name": "old", "type": "vless", "server": "old.example.com", "port": 443}, 17897), encoding="utf-8")
    saved_states = []

    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897, "config_path": str(config_path)})
    monkeypatch.setattr(local_proxy, "_save_state", lambda state: saved_states.append(state))
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: True)
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
    monkeypatch.setattr(local_proxy, "_reload_local_mihomo_config", lambda path, port: None)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", lambda _node: {})

    message = local_proxy.reload_local_ai_proxy("{ name: new, type: vless, server: new.example.com, port: 443 }")

    assert "已热更新" in message
    assert "new.example.com" in config_path.read_text(encoding="utf-8")
    assert saved_states[-1]["node_name"] == "new"


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
