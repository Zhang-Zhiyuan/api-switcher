import json

import pytest

from core import network_diagnostics


def _fake_http_get(mapping):
    def fake(url, timeout):
        value = mapping.get(url)
        if isinstance(value, network_diagnostics.HttpResult):
            return value
        if value is None:
            return network_diagnostics.HttpResult(url=url, ok=False, error="not found")
        text = json.dumps(value) if not isinstance(value, str) else value
        return network_diagnostics.HttpResult(url=url, ok=True, text=text, status_code=200, response_time=0.01)

    return fake


def test_detect_network_classifies_cloud_exit_without_private_data():
    ip = "203.0.113.10"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api6.ipify.org?format=json": network_diagnostics.HttpResult(
            url="https://api6.ipify.org?format=json",
            ok=False,
            error="network unreachable",
        ),
        "https://api64.ipify.org?format=json": {"ip": ip},
        f"https://ipwho.is/{ip}": {
            "success": True,
            "country": "United States",
            "region": "Virginia",
            "city": "Ashburn",
            "connection": {
                "asn": 14618,
                "org": "Amazon.com, Inc.",
                "isp": "Amazon Web Services",
            },
        },
    }

    report = network_diagnostics.detect_network(
        enabled_services=[],
        http_get=_fake_http_get(mapping),
        reverse_resolver=lambda _ip: "ec2-203-0-113-10.compute-1.amazonaws.com",
    )

    assert report.has_ipv4 is True
    assert report.has_ipv6 is False
    assert len(report.diagnostics) == 1
    diagnostic = report.diagnostics[0]
    assert diagnostic.geo.asn == "AS14618"
    assert diagnostic.classification.ip_type == "IDC/云机房"
    assert diagnostic.classification.risk_score >= 50
    assert "先测速" in report.notices[0]
    assert not any("ipqualityscore" in notice.lower() for notice in diagnostic.classification.signals)


def test_detect_network_uses_ping0_paid_api_only_for_reachable_ips():
    ip = "203.0.113.10"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api6.ipify.org?format=json": network_diagnostics.HttpResult(
            url="https://api6.ipify.org?format=json",
            ok=False,
            error="network unreachable",
        ),
        "https://api64.ipify.org?format=json": {"ip": ip},
        "https://ping0.cc/apiloc/apikey(test-key)/ip(203.0.113.10)": {
            "ip": ip,
            "location": "中国 宁夏回族自治区固原市中国电信",
            "country": "中国",
            "province": "宁夏回族自治区",
            "city": "固原市",
            "asn": "AS4134",
            "asnname": "Chinanet Backbone",
            "org": "CHINANET ningxia province network",
            "isidc": False,
            "iprisk": 5,
            "isnative": True,
            "asntype": "isp",
            "orgtype": "isp",
        },
        f"https://ipwho.is/{ip}": {
            "success": True,
            "country": "China",
            "connection": {"asn": 4134, "org": "Chinanet Backbone", "isp": "China Telecom"},
        },
    }
    seen_urls = []

    def fake(url, timeout):
        seen_urls.append(url)
        return _fake_http_get(mapping)(url, timeout)

    report = network_diagnostics.detect_network(
        ping0_api_key="test-key",
        enabled_services=[network_diagnostics.SERVICE_PING0],
        http_get=fake,
        reverse_resolver=lambda _ip: "",
    )

    assert len(report.diagnostics) == 1
    diagnostic = report.diagnostics[0]
    assert diagnostic.ping0.has_paid_quality is True
    assert diagnostic.ping0.iprisk == 5
    assert diagnostic.classification.ip_type == "运营商/宽带"
    assert diagnostic.classification.confidence == "低"
    assert "仅证明非 IDC" in " ".join(diagnostic.classification.signals)
    assert "https://ping0.cc/apiloc/apikey(test-key)/ip(203.0.113.10)" in seen_urls
    assert not any("api6" in url and "ping0.cc/apiloc" in url for url in seen_urls)
    assert not any("ipqualityscore.com" in url or "vpnapi.io" in url for url in seen_urls)


def test_ping0_free_geo_is_used_without_api_key():
    ip = "198.51.100.20"
    mapping = {
        "https://ipv4.ping0.cc/geo": f"{ip}\n美国 加利福尼亚州 洛杉矶\nAS7922\nComcast Cable Communications\n",
    }

    quality = network_diagnostics.lookup_ping0_quality(
        ip,
        "IPv4",
        1.0,
        _fake_http_get(mapping),
        api_key="",
    )

    assert quality.ok is True
    assert quality.source == "ping0-free-geo"
    assert quality.has_paid_quality is False
    assert quality.asn == "AS7922"
    assert "完整风控" in quality.quality_text()


def test_ping0_free_geo_mismatch_keeps_link_only_status():
    ip = "198.51.100.20"
    returned_ip = "203.0.113.20"
    mapping = {
        "https://ipv4.ping0.cc/geo": f"{returned_ip}\n美国 加利福尼亚州 洛杉矶\nAS7922\nComcast Cable Communications\n",
    }

    quality = network_diagnostics.lookup_ping0_quality(
        ip,
        "IPv4",
        1.0,
        _fake_http_get(mapping),
        api_key="",
    )

    assert quality.ok is False
    assert quality.source == "ping0-link-only"
    assert quality.raw["returned_ip"] == returned_ip
    assert "链接已生成" in quality.quality_text()
    assert ip in quality.detail_url


def test_detect_network_keeps_probe_order_when_endpoint_raises():
    ip = "198.51.100.21"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api64.ipify.org?format=json": {"ip": ip},
        f"https://ipwho.is/{ip}": {
            "success": True,
            "country": "United States",
            "connection": {"asn": 7922, "org": "Example ISP", "isp": "Example ISP"},
        },
    }

    def fake(url, timeout):
        if "api6.ipify.org" in url:
            raise RuntimeError("ipv6 endpoint exploded")
        return _fake_http_get(mapping)(url, timeout)

    report = network_diagnostics.detect_network(
        enabled_services=[],
        http_get=fake,
        reverse_resolver=lambda _ip: "",
    )

    assert [probe.label for probe in report.probes] == ["IPv4", "IPv6", "默认出口"]
    assert report.probes[1].ok is False
    assert "ipv6 endpoint exploded" in report.probes[1].error
    assert len(report.diagnostics) == 1


def test_probe_public_ip_accepts_text_response_with_extra_label():
    result = network_diagnostics.probe_public_ip(
        "IPv4",
        "https://example.test/ip",
        1.0,
        _fake_http_get({"https://example.test/ip": "current ip: 198.51.100.44\n"}),
    )

    assert result.ok is True
    assert result.ip == "198.51.100.44"


def test_probe_public_ip_accepts_key_value_text_response():
    result = network_diagnostics.probe_public_ip(
        "IPv4",
        "https://example.test/ip",
        1.0,
        _fake_http_get({"https://example.test/ip": "ip=198.51.100.46, source=edge"}),
    )

    assert result.ok is True
    assert result.ip == "198.51.100.46"


def test_probe_public_ip_accepts_ipv6_label_without_space():
    result = network_diagnostics.probe_public_ip(
        "IPv6",
        "https://example.test/ip",
        1.0,
        _fake_http_get({"https://example.test/ip": "ip:2001:db8::1"}),
    )

    assert result.ok is True
    assert result.ip == "2001:db8::1"


def test_ping0_paid_api_parses_nested_payload_and_string_values():
    ip = "198.51.100.45"
    mapping = {
        "https://ping0.cc/apiloc/apikey(key-a)/ip(198.51.100.45)": {
            "code": 0,
            "data": {
                "ip": ip,
                "location": "中国 香港 香港",
                "asn": 9304,
                "asn_name": "HGC Global Communications Limited",
                "organization": "HGC Global Communications Limited",
                "is_idc": "false",
                "ip_risk": "12.0",
                "is_native": "yes",
            },
        },
    }

    quality = network_diagnostics.lookup_ping0_quality(
        ip,
        "IPv4",
        1.0,
        _fake_http_get(mapping),
        api_keys=["key-a"],
    )

    assert quality.ok is True
    assert quality.has_paid_quality is True
    assert quality.asn == "AS9304"
    assert quality.isidc is False
    assert quality.iprisk == 12
    assert quality.isnative is True
    assert quality.api_key_label == "Key #1"


def test_classify_ip_marks_broadband_like_owner_as_lower_risk():
    geo = network_diagnostics.GeoInfo(
        ip="198.51.100.20",
        ok=True,
        org="Comcast Cable Communications",
        isp="Comcast Broadband",
    )

    classification = network_diagnostics.classify_ip(geo, "c-198-51-100-20.hsd1.ca.comcast.net")

    assert classification.ip_type == "运营商/宽带"
    assert classification.risk_score <= 25
    assert any("宽带关键词" in signal or "动态" in signal for signal in classification.signals)


def test_proxycheck_residential_classifies_home_broadband():
    ip = "203.0.113.30"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api6.ipify.org?format=json": network_diagnostics.HttpResult(
            url="https://api6.ipify.org?format=json",
            ok=False,
            error="network unreachable",
        ),
        "https://api64.ipify.org?format=json": {"ip": ip},
        "https://ipv4.ping0.cc/geo": f"{ip}\n美国 加利福尼亚州 洛杉矶\nAS7922\nComcast Cable Communications\n",
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            ip: {
                "network": {
                    "asn": "AS7922",
                    "provider": "Comcast Cable Communications",
                    "organisation": "Comcast Cable Communications",
                    "type": "Residential",
                },
                "detections": {
                    "anonymous": False,
                    "proxy": False,
                    "vpn": False,
                    "tor": False,
                    "risk": 7,
                    "confidence": 93,
                },
            },
        },
        f"https://ipwho.is/{ip}": {
            "success": True,
            "country": "United States",
            "connection": {"asn": 7922, "org": "Comcast Cable Communications", "isp": "Comcast Broadband"},
        },
    }

    report = network_diagnostics.detect_network(
        enabled_services=[network_diagnostics.SERVICE_PROXYCHECK],
        http_get=_fake_http_get(mapping),
        reverse_resolver=lambda _ip: "c-203-0-113-30.hsd1.ca.comcast.net",
    )

    diagnostic = report.diagnostics[0]
    assert diagnostic.reputation[0].ok is True
    assert diagnostic.reputation[0].network_type == "Residential"
    assert diagnostic.classification.ip_type == "家庭宽带/住宅 IP"
    assert diagnostic.classification.risk_score <= 30
    assert any("network.type=Residential" in signal for signal in diagnostic.classification.signals)


def test_proxycheck_hosting_classifies_data_center():
    ip = "203.0.113.31"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api6.ipify.org?format=json": network_diagnostics.HttpResult(
            url="https://api6.ipify.org?format=json",
            ok=False,
            error="network unreachable",
        ),
        "https://api64.ipify.org?format=json": {"ip": ip},
        "https://ipv4.ping0.cc/geo": f"{ip}\n美国 弗吉尼亚州 阿什本\nAS14618\nAmazon.com, Inc.\n",
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            ip: {
                "network": {
                    "asn": "AS14618",
                    "provider": "Amazon.com, Inc.",
                    "organisation": "Amazon.com, Inc.",
                    "type": "Hosting",
                },
                "detections": {"anonymous": False, "risk": 33, "confidence": 90},
            },
        },
        f"https://ipwho.is/{ip}": {
            "success": True,
            "country": "United States",
            "connection": {"asn": 14618, "org": "Amazon.com, Inc.", "isp": "Amazon Web Services"},
        },
    }

    report = network_diagnostics.detect_network(
        enabled_services=[network_diagnostics.SERVICE_PROXYCHECK],
        http_get=_fake_http_get(mapping),
        reverse_resolver=lambda _ip: "",
    )

    diagnostic = report.diagnostics[0]
    assert diagnostic.classification.ip_type == "IDC/云机房"
    assert diagnostic.classification.risk_score >= 52
    assert "IDC" in diagnostic.reputation[0].summary_text()


def test_proxycheck_hosting_flag_without_type_classifies_data_center():
    ip = "203.0.113.39"
    mapping = {
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            ip: {
                "detections": {
                    "anonymous": False,
                    "proxy": False,
                    "vpn": False,
                    "tor": False,
                    "hosting": "true",
                    "risk": "44",
                },
            },
        },
    }

    info = network_diagnostics.lookup_proxycheck_reputation(ip, 1.0, _fake_http_get(mapping))
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        reputation=[info],
    )

    assert info.ok is True
    assert info.network_type == ""
    assert info.flags["hosting"] is True
    assert classification.ip_type == "IDC/云机房"
    assert classification.risk_score >= 52


def test_proxycheck_preserves_zero_risk_and_confidence_values():
    ip = "8.8.8.8"
    mapping = {
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            ip: {
                "network": {"type": "Business"},
                "location": {
                    "country_name": "United States",
                    "country_code": "us",
                    "region_name": "California",
                    "city_name": "Mountain View",
                },
                "detections": {"anonymous": False, "risk": 0, "confidence": 0},
            },
        },
    }

    info = network_diagnostics.lookup_proxycheck_reputation(ip, 1.0, _fake_http_get(mapping))

    assert info.ok is True
    assert info.risk_score == 0
    assert info.confidence_score == 0
    assert (info.country_code, info.country, info.region, info.city) == (
        "US",
        "United States",
        "California",
        "Mountain View",
    )


def test_proxycheck_rejects_an_unrelated_ip_record():
    ip = "8.8.8.8"
    mapping = {
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            "1.1.1.1": {
                "network": {"type": "Residential"},
                "detections": {"anonymous": False, "risk": 0},
            },
        },
    }

    info = network_diagnostics.lookup_proxycheck_reputation(ip, 1.0, _fake_http_get(mapping))

    assert info.ok is False
    assert "当前 IP" in info.error


def test_proxycheck_accepts_equivalent_ipv6_record_key():
    ip = "2001:4860:4860::8888"
    expanded = "2001:4860:4860:0:0:0:0:8888"
    mapping = {
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            expanded: {
                "network": {"type": "Business"},
                "detections": {"anonymous": False, "risk": 10},
            },
        },
    }

    info = network_diagnostics.lookup_proxycheck_reputation(ip, 1.0, _fake_http_get(mapping))

    assert info.ok is True
    assert info.network_type == "Business"


def test_ipapi_hosting_vpn_abuser_classifies_suspicious_exit():
    ip = "8.8.8.8"
    mapping = {
        f"https://api.ipapi.is?q={ip}": {
            "ip": ip,
            "is_datacenter": True,
            "is_vpn": True,
            "is_proxy": False,
            "is_tor": False,
            "is_abuser": True,
            "company": {
                "name": "Google LLC",
                "type": "hosting",
                "abuser_score": "0.0039 (Low)",
            },
            "asn": {
                "asn": 15169,
                "org": "Google LLC",
                "type": "hosting",
            },
            "location": {
                "country": "United States",
                "country_code": "us",
                "state": "California",
                "city": "Mountain View",
            },
        },
    }

    info = network_diagnostics.lookup_ipapi_reputation(ip, 1.0, _fake_http_get(mapping))
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        reputation=[info],
    )

    assert info.ok is True
    assert info.source == network_diagnostics.SERVICE_IPAPI
    assert info.network_type == "hosting"
    assert info.flags["hosting"] is True
    assert info.flags["vpn"] is True
    assert info.flags["abuser"] is True
    assert info.risk_score >= 82
    assert info.asn == "AS15169"
    assert (info.country_code, info.country, info.region, info.city) == (
        "US",
        "United States",
        "California",
        "Mountain View",
    )
    assert classification.ip_type == "代理/VPN/Tor 可疑"
    assert classification.risk_score >= 82
    assert any("ipapi.is flags" in signal for signal in classification.signals)


def test_ipapi_isp_type_is_auxiliary_not_proof_of_home_broadband():
    ip = "198.51.100.50"
    mapping = {
        f"https://api.ipapi.is?q={ip}": {
            "ip": ip,
            "is_datacenter": "false",
            "is_vpn": "false",
            "is_proxy": "false",
            "is_tor": "false",
            "is_abuser": "false",
            "company": {
                "name": "Example Fiber",
                "type": "isp",
                "abuser_score": "0.002 (Low)",
            },
            "asn": {
                "asn": "64510",
                "org": "Example Fiber",
                "type": "isp",
            },
        },
    }

    info = network_diagnostics.lookup_ipapi_reputation(ip, 1.0, _fake_http_get(mapping))
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        reputation=[info],
    )

    assert info.ok is True
    assert info.network_type == "isp"
    assert info.flags["hosting"] is False
    assert info.flags["vpn"] is False
    assert info.risk_score is None
    assert classification.ip_type == "未知"
    assert classification.confidence == "低"


def test_netcoffee_trust_score_classifies_ai_residential_quality():
    ip = "118.143.41.200"
    mapping = {
        f"https://ip.net.coffee/api/ip/lookup/{ip}": {
            "ip": ip,
            "is_datacenter": False,
            "isResidential": True,
            "is_vpn": False,
            "is_proxy": False,
            "is_tor": False,
            "is_abuser": False,
            "is_mobile": False,
            "company_type": "isp",
            "company_name": "HGC Global Communications Limited",
            "asn": 9304,
            "asOrganization": "HGC Global Communications Limited",
            "countryCode": "hk",
            "country": "Hong Kong",
            "region": "Hong Kong",
            "city": "Hong Kong",
            "trust_score": 100,
            "abuser_score": "0.0006 (Low)",
            "ai_verdict": {"label": "Clean residential", "confidence": 95},
        },
        f"https://ip.net.coffee/api/iprisk/{ip}": {
            "ip": "118.143.41.194",
            "cidr": "118.143.41.0/24",
            "is_datacenter": False,
            "isResidential": True,
            "is_vpn": False,
            "is_proxy": False,
            "is_tor": False,
            "is_abuser": False,
            "company_type": "isp",
            "trust_score": 100,
        },
    }

    info = network_diagnostics.lookup_netcoffee_reputation(ip, 1.0, _fake_http_get(mapping))
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        reputation=[info],
    )

    assert info.ok is True
    assert info.source == network_diagnostics.SERVICE_NETCOFFEE
    assert info.network_type == "residential"
    assert info.risk_score == 0
    assert info.confidence_score == 95
    assert info.asn == "AS9304"
    assert (info.country_code, info.country, info.region, info.city) == (
        "HK",
        "Hong Kong",
        "Hong Kong",
        "Hong Kong",
    )
    assert classification.ip_type == "家庭宽带/住宅 IP"
    assert classification.risk_score <= 16
    assert any("trust_score=100" in signal for signal in classification.signals)


def test_netcoffee_same_cidr_iprisk_does_not_apply_sibling_abuse_flags():
    ip = "1.1.1.1"
    mapping = {
        f"https://ip.net.coffee/api/ip/lookup/{ip}": {
            "ip": ip,
            "is_datacenter": True,
            "isResidential": False,
            "is_vpn": False,
            "is_proxy": False,
            "is_tor": False,
            "is_abuser": False,
            "company_type": "hosting",
            "company_name": "Cloudflare, Inc.",
            "asn": 13335,
            "trust_score": 41,
            "ai_verdict": {"label": "公共 DNS 任播", "confidence": 100},
        },
        f"https://ip.net.coffee/api/iprisk/{ip}": {
            "ip": "1.1.1.2",
            "cidr": "1.1.1.0/24",
            "is_datacenter": True,
            "is_abuser": True,
            "abuser_score": "0.0234 (Elevated)",
            "company_type": "hosting",
            "trust_score": 41,
        },
    }

    info = network_diagnostics.lookup_netcoffee_reputation(ip, 1.0, _fake_http_get(mapping))
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        reputation=[info],
    )

    assert info.ok is True
    assert info.risk_score == 59
    assert classification.ip_type == "IDC/云机房"
    assert classification.risk_score == 59
    assert not any("threat=78" in signal for signal in classification.signals)
    assert any("仅合并 trust_score" in signal for signal in classification.signals)


def test_netcoffee_low_abuser_score_does_not_force_high_risk():
    ip = "8.8.8.8"
    mapping = {
        f"https://ip.net.coffee/api/ip/lookup/{ip}": {
            "ip": ip,
            "is_datacenter": True,
            "isResidential": False,
            "is_vpn": False,
            "is_proxy": False,
            "is_tor": False,
            "is_abuser": False,
            "company_type": "hosting",
            "company_name": "Google LLC",
            "asn": 15169,
            "trust_score": 63,
            "ai_verdict": {"label": "公共 DNS 任播", "confidence": 100},
        },
        f"https://ip.net.coffee/api/iprisk/{ip}": {
            "ip": ip,
            "is_datacenter": True,
            "is_abuser": True,
            "abuser_score": "0.0039 (Low)",
            "company_type": "hosting",
            "trust_score": 63,
        },
    }

    info = network_diagnostics.lookup_netcoffee_reputation(ip, 1.0, _fake_http_get(mapping))
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        reputation=[info],
    )

    assert info.ok is True
    assert info.flags["abuser"] is False
    assert info.risk_score == 55
    assert classification.ip_type == "IDC/云机房"
    assert classification.risk_score == 55


def test_detect_network_defaults_to_netcoffee_only():
    ip = "198.51.100.52"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api6.ipify.org?format=json": network_diagnostics.HttpResult(
            url="https://api6.ipify.org?format=json",
            ok=False,
            error="network unreachable",
        ),
        "https://api64.ipify.org?format=json": {"ip": ip},
        f"https://ip.net.coffee/api/ip/lookup/{ip}": {
            "ip": ip,
            "is_datacenter": True,
            "isResidential": False,
            "is_vpn": False,
            "is_proxy": False,
            "is_tor": False,
            "company_type": "hosting",
            "company_name": "Example Cloud",
            "asn": 64500,
            "trust_score": 45,
        },
        f"https://ip.net.coffee/api/iprisk/{ip}": {
            "ip": ip,
            "is_datacenter": True,
            "company_type": "hosting",
            "trust_score": 45,
        },
        f"https://ipwho.is/{ip}": {
            "success": True,
            "country": "United States",
            "connection": {"asn": 64500, "org": "Example Cloud", "isp": "Example Cloud"},
        },
    }
    seen_urls = []

    def fake(url, timeout):
        seen_urls.append(url)
        return _fake_http_get(mapping)(url, timeout)

    report = network_diagnostics.detect_network(
        http_get=fake,
        reverse_resolver=lambda _ip: "",
    )

    diagnostic = report.diagnostics[0]
    assert diagnostic.reputation[0].source == network_diagnostics.SERVICE_NETCOFFEE
    assert diagnostic.classification.ip_type == "IDC/云机房"
    assert any("ip.net.coffee" in url for url in seen_urls)
    assert not any("proxycheck.io" in url or "api.ipapi.is" in url or "ping0.cc" in url for url in seen_urls)


def test_proxycheck_v3_device_estimate_is_parsed_and_affects_risk():
    ip = "203.0.113.41"
    mapping = {
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            ip: {
                "network": {
                    "asn": "AS7922",
                    "provider": "Example Fiber",
                    "organisation": "Example Fiber",
                    "type": "Residential",
                },
                "detections": {
                    "anonymous": False,
                    "proxy": False,
                    "vpn": False,
                    "tor": False,
                    "risk": 7,
                    "confidence": 96,
                },
                "device_estimate": {
                    "address": 12,
                    "subnet": 60,
                },
            },
        },
    }

    info = network_diagnostics.lookup_proxycheck_reputation(ip, 1.0, _fake_http_get(mapping))
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        reputation=[info],
    )

    assert info.ok is True
    assert info.shared_count == 12
    assert info.subnet_shared_count == 60
    assert "共享设备约 12/网段约 60" in info.summary_text()
    assert any("device_estimate.address=12 subnet=60" in signal for signal in info.signals)
    assert classification.ip_type == "家庭宽带/住宅 IP"
    assert classification.risk_score >= 65


def test_proxycheck_v3_subnet_only_device_estimate_is_displayed():
    ip = "203.0.113.43"
    mapping = {
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            ip: {
                "network": {"type": "Business"},
                "detections": {"anonymous": False, "confidence": 100},
                "device_estimate": {
                    "address": None,
                    "subnet": 3,
                },
            },
        },
    }

    info = network_diagnostics.lookup_proxycheck_reputation(ip, 1.0, _fake_http_get(mapping))

    assert info.ok is True
    assert info.shared_count is None
    assert info.subnet_shared_count == 3
    assert "共享设备未知/网段约 3" in info.summary_text()
    assert "ProxyCheck device_estimate.subnet=3" in info.signals


def test_proxycheck_legacy_vpn_type_is_parsed_as_anonymous_flag():
    ip = "203.0.113.36"
    mapping = {
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            ip: {
                "proxy": "yes",
                "type": "VPN",
                "risk": "86.0",
                "provider": "Example VPN",
                "asn": "AS64501",
            },
        },
    }

    info = network_diagnostics.lookup_proxycheck_reputation(ip, 1.0, _fake_http_get(mapping))

    assert info.ok is True
    assert info.network_type == ""
    assert info.flags["proxy"] is True
    assert info.flags["vpn"] is True
    assert info.risk_score == 86


def test_ipqs_dynamic_connection_does_not_hide_shared_connection_risk():
    ip = "203.0.113.42"
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        reputation=[
            network_diagnostics.ReputationInfo(
                ip=ip,
                source="ipqs",
                ok=True,
                network_type="Residential",
                fraud_score=12,
                flags={"shared_connection": True, "dynamic_connection": True},
            ),
        ],
    )

    assert classification.ip_type == "家庭宽带/住宅 IP"
    assert classification.risk_score >= 42


def test_vpnapi_flags_anonymous_network_when_key_is_present():
    ip = "203.0.113.32"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api6.ipify.org?format=json": network_diagnostics.HttpResult(
            url="https://api6.ipify.org?format=json",
            ok=False,
            error="network unreachable",
        ),
        "https://api64.ipify.org?format=json": {"ip": ip},
        "https://ipv4.ping0.cc/geo": f"{ip}\n美国 纽约\nAS64500\nExample Network\n",
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            ip: {"network": {"type": "Business"}, "detections": {"anonymous": False, "risk": 10}},
        },
        f"https://vpnapi.io/api/{ip}?key=vpn-key": {
            "ip": ip,
            "security": {"vpn": True, "proxy": False, "tor": False, "relay": False},
            "network": {"autonomous_system_number": "AS64500", "autonomous_system_organization": "Example Network"},
        },
        f"https://ipwho.is/{ip}": {
            "success": True,
            "country": "United States",
            "connection": {"asn": 64500, "org": "Example Network", "isp": "Example ISP"},
        },
    }

    report = network_diagnostics.detect_network(
        vpnapi_api_key="vpn-key",
        enabled_services=[
            network_diagnostics.SERVICE_PROXYCHECK,
            network_diagnostics.SERVICE_VPNAPI,
        ],
        http_get=_fake_http_get(mapping),
        reverse_resolver=lambda _ip: "",
    )

    diagnostic = report.diagnostics[0]
    assert len(diagnostic.reputation) == 2
    assert diagnostic.reputation[1].source == "vpnapi"
    assert diagnostic.classification.ip_type == "代理/VPN/Tor 可疑"
    assert diagnostic.classification.risk_score >= 78
    assert any("VPNAPI.io security: VPN" in signal for signal in diagnostic.classification.signals)


def test_vpnapi_parses_nested_payload_and_string_booleans():
    ip = "203.0.113.37"
    mapping = {
        f"https://vpnapi.io/api/{ip}?key=vpn-key": {
            "data": {
                "security": {"vpn": "true", "proxy": "false", "tor": "0", "relay": "1"},
                "network": {
                    "asn": 64502,
                    "organization": "Example Relay Network",
                },
                "location": {
                    "country": "Hong Kong",
                    "country_code": "hk",
                    "region": "Hong Kong",
                    "city": "Hong Kong",
                },
            },
        },
    }

    info = network_diagnostics.lookup_vpnapi_reputation(ip, 1.0, _fake_http_get(mapping), api_keys=["vpn-key"])

    assert info.ok is True
    assert info.flags["vpn"] is True
    assert info.flags["relay"] is True
    assert info.flags["proxy"] is False
    assert info.asn == "AS64502"
    assert info.provider == "Example Relay Network"
    assert (info.country_code, info.country, info.region, info.city) == (
        "HK",
        "Hong Kong",
        "Hong Kong",
        "Hong Kong",
    )


def test_lookup_reputation_isolates_single_provider_exception():
    ip = "203.0.113.44"

    def fake(url, timeout):
        if "proxycheck.io" in url:
            raise RuntimeError("proxycheck transport failed")
        if "vpnapi.io" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=True,
                text=json.dumps({
                    "ip": ip,
                    "security": {"vpn": False, "proxy": False, "tor": False, "relay": False},
                }),
            )
        raise AssertionError(f"unexpected URL: {url}")

    results = network_diagnostics.lookup_reputation(
        ip,
        1.0,
        fake,
        enabled_services=[
            network_diagnostics.SERVICE_PROXYCHECK,
            network_diagnostics.SERVICE_VPNAPI,
        ],
        vpnapi_api_keys=["vpn-key"],
    )

    assert [item.source for item in results] == [
        network_diagnostics.SERVICE_PROXYCHECK,
        network_diagnostics.SERVICE_VPNAPI,
    ]
    assert results[0].ok is False
    assert "proxycheck transport failed" in results[0].error
    assert results[1].ok is True


def test_ipqs_fraud_score_and_connection_type_are_used_with_key():
    ip = "203.0.113.33"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api6.ipify.org?format=json": network_diagnostics.HttpResult(
            url="https://api6.ipify.org?format=json",
            ok=False,
            error="network unreachable",
        ),
        "https://api64.ipify.org?format=json": {"ip": ip},
        "https://ipv4.ping0.cc/geo": f"{ip}\n美国 加利福尼亚州 洛杉矶\nAS7922\nComcast Cable Communications\n",
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0&ver=24-June-2026": {
            "status": "ok",
            ip: {"network": {"type": "Residential"}, "detections": {"anonymous": False, "risk": 8}},
        },
        f"https://ipqualityscore.com/api/json/ip/ipqs-key/{ip}?strictness=1&allow_public_access_points=true": {
            "success": True,
            "proxy": True,
            "vpn": False,
            "tor": False,
            "recent_abuse": True,
            "connection_type": "Residential",
            "fraud_score": 82,
            "ISP": "Comcast Cable",
            "organization": "Comcast Cable Communications",
            "ASN": 7922,
        },
        f"https://ipwho.is/{ip}": {
            "success": True,
            "country": "United States",
            "connection": {"asn": 7922, "org": "Comcast Cable Communications", "isp": "Comcast Broadband"},
        },
    }

    report = network_diagnostics.detect_network(
        ipqs_api_key="ipqs-key",
        enabled_services=[
            network_diagnostics.SERVICE_PROXYCHECK,
            network_diagnostics.SERVICE_IPQS,
        ],
        http_get=_fake_http_get(mapping),
        reverse_resolver=lambda _ip: "",
    )

    diagnostic = report.diagnostics[0]
    assert len(diagnostic.reputation) == 2
    assert diagnostic.reputation[1].fraud_score == 82
    assert diagnostic.classification.ip_type == "住宅代理/匿名出口可疑"
    assert diagnostic.classification.risk_score == 82
    assert any("IPQS fraud_score=82" in signal for signal in diagnostic.classification.signals)


def test_multisource_residential_business_conflict_lowers_confidence():
    ip = "203.0.113.39"
    geo = network_diagnostics.GeoInfo(ip=ip, ok=True)
    classification = network_diagnostics.classify_ip(
        geo,
        reputation=[
            network_diagnostics.ReputationInfo(
                ip=ip,
                source="proxycheck",
                ok=True,
                network_type="Residential",
                risk_score=7,
            ),
            network_diagnostics.ReputationInfo(
                ip=ip,
                source="ipqs",
                ok=True,
                network_type="Business",
                risk_score=22,
            ),
        ],
    )

    assert classification.ip_type == "家宽/商宽冲突"
    assert classification.risk_score > 35
    assert classification.confidence == "中"
    assert any("多源冲突" in signal for signal in classification.signals)


@pytest.mark.parametrize("upstream_risk", [51, 60, 74])
def test_explicit_risk_without_network_type_is_never_dropped(upstream_risk):
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.61", ok=True),
        reputation=[
            network_diagnostics.ReputationInfo(
                ip="203.0.113.61",
                source="ipqs",
                ok=True,
                risk_score=upstream_risk,
            )
        ],
    )

    assert classification.ip_type == "网络类型未知"
    assert classification.risk_score == upstream_risk
    assert any("保留其显式风险值" in signal for signal in classification.signals)


@pytest.mark.parametrize(
    ("upstream_risk", "expected_type"),
    [(75, "网络类型未知"), (76, "高风险出口可疑")],
)
def test_proxycheck_uses_its_documented_dangerous_risk_threshold(upstream_risk, expected_type):
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.62", ok=True),
        reputation=[
            network_diagnostics.ReputationInfo(
                ip="203.0.113.62",
                source="proxycheck",
                ok=True,
                risk_score=upstream_risk,
            )
        ],
    )

    assert classification.ip_type == expected_type
    assert classification.risk_score == (75 if upstream_risk == 75 else 78)


@pytest.mark.parametrize(
    ("upstream_risk", "expected_type"),
    [(69, "网络类型未知"), (70, "高风险出口可疑")],
)
def test_ping0_uses_its_documented_extreme_risk_threshold(upstream_risk, expected_type):
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.78", ok=True),
        ping0=network_diagnostics.Ping0Quality(
            ip="203.0.113.78",
            ok=True,
            source="ping0-api",
            iprisk=upstream_risk,
            isidc=False,
        ),
    )

    assert classification.ip_type == expected_type
    assert classification.risk_score == (69 if upstream_risk == 69 else 78)


def test_abuse_history_is_not_mislabeled_as_anonymous_proxy():
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.63", ok=True),
        reputation=[
            network_diagnostics.ReputationInfo(
                ip="203.0.113.63",
                source="ipqs",
                ok=True,
                network_type="Residential",
                risk_score=12,
                flags={"recent_abuse": True, "proxy": False, "vpn": False, "tor": False},
            )
        ],
    )

    assert classification.ip_type == "住宅 IP 高风险"
    assert "代理" not in classification.ip_type
    assert "匿名" not in classification.ip_type
    assert classification.risk_score >= 78


def test_duplicate_same_source_abuse_does_not_create_high_confidence_consensus():
    duplicate_abuse = [
        network_diagnostics.ReputationInfo(
            ip="203.0.113.66",
            source="ipqs",
            ok=True,
            network_type="Residential",
            risk_score=12,
            flags={"recent_abuse": True},
        )
        for _ in range(2)
    ]

    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.66", ok=True),
        reputation=duplicate_abuse,
    )

    assert classification.ip_type == "住宅 IP 高风险"
    assert classification.confidence == "中"


def test_failed_reputation_result_cannot_suppress_geo_network_lookup():
    failed = network_diagnostics.ReputationInfo(
        ip="203.0.113.67",
        source="proxycheck",
        ok=False,
        network_type="Residential",
        error="soft failure",
    )

    assert network_diagnostics.reputation_has_network_classification([failed]) is False


def test_neutral_successful_source_does_not_inflate_residential_confidence():
    residential = network_diagnostics.ReputationInfo(
        ip="203.0.113.64",
        source="proxycheck",
        ok=True,
        network_type="Residential",
        risk_score=7,
    )
    neutral = network_diagnostics.ReputationInfo(
        ip="203.0.113.64",
        source="vpnapi",
        ok=True,
        flags={"vpn": False, "proxy": False, "tor": False, "relay": False},
    )

    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.64", ok=True),
        reputation=[residential, neutral],
    )

    assert classification.ip_type == "家庭宽带/住宅 IP"
    assert classification.confidence == "中"


def test_duplicate_same_source_does_not_count_as_independent_consensus():
    duplicate_votes = [
        network_diagnostics.ReputationInfo(
            ip="203.0.113.65",
            source="proxycheck",
            ok=True,
            network_type="Residential",
            risk_score=5,
        )
        for _ in range(2)
    ]

    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.65", ok=True),
        reputation=duplicate_votes,
    )

    assert classification.confidence == "中"


def test_same_source_residential_and_hosting_signals_are_an_explicit_conflict():
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.68", ok=True),
        reputation=[
            network_diagnostics.ReputationInfo(
                ip="203.0.113.68",
                source="proxycheck",
                ok=True,
                network_type="Residential",
                risk_score=10,
                flags={"hosting": True},
            )
        ],
    )

    assert classification.ip_type == "家宽/机房冲突"
    assert classification.confidence == "中"
    assert classification.risk_score >= 62


def test_ipqs_user_agent_mobile_flag_is_not_a_mobile_network_vote():
    result = network_diagnostics.ReputationInfo(
        ip="203.0.113.69",
        source="ipqs",
        ok=True,
        flags={"mobile": True},
    )

    assert network_diagnostics.reputation_has_network_classification([result]) is False


def test_global_risk_floor_is_not_lost_when_another_source_supplies_residential_type():
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.70", ok=True),
        reputation=[
            network_diagnostics.ReputationInfo(
                ip="203.0.113.70",
                source="proxycheck",
                ok=True,
                network_type="Residential",
                risk_score=8,
            ),
            network_diagnostics.ReputationInfo(
                ip="203.0.113.70",
                source="ipqs",
                ok=True,
                risk_score=74,
            ),
        ],
    )

    assert classification.ip_type == "家庭宽带/住宅 IP"
    assert classification.risk_score == 74


def test_risk_only_reputation_is_merged_with_geo_type_instead_of_hiding_it():
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(
            ip="203.0.113.71",
            ok=True,
            org="Amazon Web Services",
        ),
        reputation=[
            network_diagnostics.ReputationInfo(
                ip="203.0.113.71",
                source="ipqs",
                ok=True,
                risk_score=60,
            )
        ],
    )

    assert classification.ip_type == "IDC/云机房"
    assert classification.risk_score == 60


def test_adding_risk_only_reputation_cannot_lower_ping0_risk():
    ping0 = network_diagnostics.Ping0Quality(
        ip="203.0.113.72",
        ok=True,
        source="ping0-api",
        iprisk=10,
    )
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.72", ok=True),
        ping0=ping0,
        reputation=[
            network_diagnostics.ReputationInfo(
                ip="203.0.113.72",
                source="ipqs",
                ok=True,
                risk_score=74,
            )
        ],
    )

    assert classification.ip_type == "网络类型未知"
    assert classification.risk_score == 74


def test_ping0_non_native_is_preserved_across_reputation_classification():
    ping0 = network_diagnostics.Ping0Quality(
        ip="203.0.113.73",
        ok=True,
        source="ping0-api",
        iprisk=10,
        isidc=False,
        isnative=False,
    )
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.73", ok=True),
        ping0=ping0,
        reputation=[
            network_diagnostics.ReputationInfo(
                ip="203.0.113.73",
                source="proxycheck",
                ok=True,
                network_type="Residential",
                risk_score=8,
            )
        ],
    )

    assert "非原生/广播" in classification.ip_type
    assert classification.risk_score >= 45
    assert classification.confidence == "中"


def test_ping0_non_idc_alone_is_not_a_residential_vote():
    ping0 = network_diagnostics.Ping0Quality(
        ip="203.0.113.75",
        ok=True,
        source="ping0-api",
        iprisk=8,
        isidc=False,
        isnative=True,
    )

    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip="203.0.113.75", ok=True),
        ping0=ping0,
    )

    assert classification.ip_type == "网络类型未知"
    assert "家庭" not in classification.ip_type
    assert "住宅" not in classification.ip_type
    assert classification.confidence == "低"
    assert "仅证明非 IDC" in " ".join(classification.signals)


def test_ping0_non_idc_does_not_inflate_independent_residential_confidence():
    ip = "203.0.113.76"
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        ping0=network_diagnostics.Ping0Quality(
            ip=ip,
            ok=True,
            source="ping0-api",
            iprisk=8,
            isidc=False,
            isnative=True,
        ),
        reputation=[
            network_diagnostics.ReputationInfo(
                ip=ip,
                source="proxycheck",
                ok=True,
                network_type="Residential",
                risk_score=8,
            )
        ],
    )

    assert classification.ip_type == "家庭宽带/住宅 IP"
    assert classification.confidence == "中"


def test_netcoffee_isp_ownership_without_address_residential_flag_is_not_home_broadband():
    ip = "9.9.9.9"
    mapping = {
        f"https://ip.net.coffee/api/ip/lookup/{ip}": {
            "ip": ip,
            "company_type": "isp",
            "trust_score": 100,
        },
        f"https://ip.net.coffee/api/iprisk/{ip}": {"ip": ip, "trust_score": 100},
    }

    info = network_diagnostics.lookup_netcoffee_reputation(ip, 1.0, _fake_http_get(mapping))
    classification = network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        reputation=[info],
    )

    assert info.network_type == ""
    assert classification.ip_type == "网络类型未知"


def test_ipapi_address_mobile_flag_overrides_isp_ownership_metadata():
    ip = "9.9.9.10"
    mapping = {
        f"https://api.ipapi.is?q={ip}": {
            "ip": ip,
            "is_mobile": True,
            "is_datacenter": False,
            "company": {"type": "isp"},
        }
    }

    info = network_diagnostics.lookup_ipapi_reputation(ip, 1.0, _fake_http_get(mapping))

    assert info.network_type == "mobile"
    assert network_diagnostics.classify_ip(
        network_diagnostics.GeoInfo(ip=ip, ok=True),
        reputation=[info],
    ).ip_type == "蜂窝/移动网络"


def test_ping0_idc_conflict_overrides_third_party_residential_for_ai_safety():
    ip = "203.0.113.40"
    geo = network_diagnostics.GeoInfo(ip=ip, ok=True)
    ping0 = network_diagnostics.Ping0Quality(
        ip=ip,
        ok=True,
        source="ping0-api",
        isidc=True,
        iprisk=18,
    )
    classification = network_diagnostics.classify_ip(
        geo,
        ping0=ping0,
        reputation=[
            network_diagnostics.ReputationInfo(
                ip=ip,
                source="proxycheck",
                ok=True,
                network_type="Residential",
                risk_score=8,
            ),
        ],
    )

    assert classification.ip_type == "家宽/机房冲突"
    assert classification.confidence == "中"
    assert classification.risk_score >= 62
    assert any("家宽信号与 IDC/机房信号" in signal for signal in classification.signals)


def test_ipqs_key_pool_rotates_after_success_false_string():
    ip = "203.0.113.38"
    first_url = f"https://ipqualityscore.com/api/json/ip/expired-key/{ip}?strictness=1&allow_public_access_points=true"
    second_url = f"https://ipqualityscore.com/api/json/ip/ok-key/{ip}?strictness=1&allow_public_access_points=true"
    mapping = {
        first_url: {"result": {"success": "false", "message": "quota exhausted"}},
        second_url: {
            "success": "true",
            "connection_type": "Business",
            "fraud_score": "31.0",
            "country_code": "hk",
            "region": "Hong Kong",
            "city": "Hong Kong",
            "proxy": "false",
            "vpn": "false",
            "tor": "false",
        },
    }

    info = network_diagnostics.lookup_ipqs_reputation(ip, 1.0, _fake_http_get(mapping), api_keys=["expired-key", "ok-key"])

    assert info.ok is True
    assert info.api_key_label == "Key #2"
    assert info.network_type == "Business"
    assert info.fraud_score == 31
    assert (info.country_code, info.country, info.region, info.city) == (
        "HK",
        "",
        "Hong Kong",
        "Hong Kong",
    )
    assert info.attempts == ["Key #1 失败: quota exhausted", "Key #2 成功"]


def test_ipqs_key_pool_does_not_multiply_service_outage_by_key_count():
    ip = "203.0.113.74"
    seen_urls = []

    def http_get(url, _timeout):
        seen_urls.append(url)
        return network_diagnostics.HttpResult(
            url=url,
            ok=False,
            status_code=503,
            error="HTTP 503",
        )

    info = network_diagnostics.lookup_ipqs_reputation(
        ip,
        1.0,
        http_get,
        api_keys=["first-key", "second-key"],
    )

    assert info.ok is False
    assert len(seen_urls) == 1
    assert "first-key" in seen_urls[0]


def test_ipqs_key_pool_rotates_on_http_429_even_when_json_message_omits_rate_limit_words():
    ip = "203.0.113.77"
    seen_urls = []

    def http_get(url, _timeout):
        seen_urls.append(url)
        if "first-key" in url:
            return network_diagnostics.HttpResult(
                url=url,
                ok=False,
                status_code=429,
                error="request failed",
                text=json.dumps({"success": False, "message": "temporarily blocked"}),
            )
        return network_diagnostics.HttpResult(
            url=url,
            ok=True,
            status_code=200,
            text=json.dumps(
                {
                    "success": True,
                    "proxy": False,
                    "vpn": False,
                    "tor": False,
                    "connection_type": "Residential",
                    "fraud_score": 7,
                }
            ),
        )

    info = network_diagnostics.lookup_ipqs_reputation(
        ip,
        1.0,
        http_get,
        api_keys=["first-key", "second-key"],
    )

    assert info.ok is True
    assert len(seen_urls) == 2
    assert info.api_key_label == "Key #2"
    assert info.attempts == ["Key #1 失败: HTTP 429: temporarily blocked", "Key #2 成功"]


def test_user_can_disable_quality_services():
    ip = "203.0.113.34"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api6.ipify.org?format=json": network_diagnostics.HttpResult(
            url="https://api6.ipify.org?format=json",
            ok=False,
            error="network unreachable",
        ),
        "https://api64.ipify.org?format=json": {"ip": ip},
        f"https://ipwho.is/{ip}": {
            "success": True,
            "country": "United States",
            "connection": {"asn": 7922, "org": "Comcast Cable Communications", "isp": "Comcast Broadband"},
        },
    }
    seen_urls = []

    def fake(url, timeout):
        seen_urls.append(url)
        return _fake_http_get(mapping)(url, timeout)

    report = network_diagnostics.detect_network(
        enabled_services=[],
        http_get=fake,
        reverse_resolver=lambda _ip: "c-203-0-113-34.hsd1.ca.comcast.net",
    )

    diagnostic = report.diagnostics[0]
    assert diagnostic.ping0.source == "disabled"
    assert diagnostic.reputation == []
    assert diagnostic.classification.ip_type == "运营商/宽带"
    assert not any("ping0.cc" in url or "proxycheck.io" in url or "api.ipapi.is" in url or "ipqualityscore.com" in url or "vpnapi.io" in url for url in seen_urls)


def test_ipqs_key_pool_rotates_after_limited_key():
    ip = "203.0.113.35"
    first_url = f"https://ipqualityscore.com/api/json/ip/limited-key/{ip}?strictness=1&allow_public_access_points=true"
    second_url = f"https://ipqualityscore.com/api/json/ip/fresh-key/{ip}?strictness=1&allow_public_access_points=true"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api6.ipify.org?format=json": network_diagnostics.HttpResult(
            url="https://api6.ipify.org?format=json",
            ok=False,
            error="network unreachable",
        ),
        "https://api64.ipify.org?format=json": {"ip": ip},
        first_url: network_diagnostics.HttpResult(
            url=first_url,
            ok=False,
            status_code=429,
            error="HTTP 429",
            text=json.dumps({"success": False, "message": "quota exhausted"}),
        ),
        second_url: {
            "success": True,
            "proxy": False,
            "vpn": False,
            "tor": False,
            "connection_type": "Residential",
            "fraud_score": 9,
            "ISP": "Comcast Cable",
            "organization": "Comcast Cable Communications",
            "ASN": 7922,
        },
        f"https://ipwho.is/{ip}": {
            "success": True,
            "country": "United States",
            "connection": {"asn": 7922, "org": "Comcast Cable Communications", "isp": "Comcast Broadband"},
        },
    }
    seen_urls = []

    def fake(url, timeout):
        seen_urls.append(url)
        return _fake_http_get(mapping)(url, timeout)

    report = network_diagnostics.detect_network(
        enabled_services=[network_diagnostics.SERVICE_IPQS],
        ipqs_api_keys=["limited-key", "fresh-key"],
        http_get=fake,
        reverse_resolver=lambda _ip: "",
    )

    diagnostic = report.diagnostics[0]
    assert first_url in seen_urls
    assert second_url in seen_urls
    assert len(diagnostic.reputation) == 1
    assert diagnostic.reputation[0].ok is True
    assert diagnostic.reputation[0].api_key_label == "Key #2"
    assert diagnostic.reputation[0].attempts == ["Key #1 失败: HTTP 429: quota exhausted", "Key #2 成功"]
    assert diagnostic.classification.ip_type == "家庭宽带/住宅 IP"


def test_probe_public_ip_rejects_invalid_endpoint_response():
    result = network_diagnostics.probe_public_ip(
        "IPv4",
        "https://example.test/ip",
        1.0,
        _fake_http_get({"https://example.test/ip": {"ip": "not-an-ip"}}),
    )

    assert result.ok is False
    assert "有效 IP" in result.error
