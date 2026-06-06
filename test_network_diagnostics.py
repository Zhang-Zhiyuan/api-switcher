import json

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
        http_get=fake,
        reverse_resolver=lambda _ip: "",
    )

    assert len(report.diagnostics) == 1
    diagnostic = report.diagnostics[0]
    assert diagnostic.ping0.has_paid_quality is True
    assert diagnostic.ping0.iprisk == 5
    assert diagnostic.classification.ip_type == "家庭/非IDC宽带"
    assert diagnostic.classification.confidence == "高"
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
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0": {
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
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0": {
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
        http_get=_fake_http_get(mapping),
        reverse_resolver=lambda _ip: "",
    )

    diagnostic = report.diagnostics[0]
    assert diagnostic.classification.ip_type == "IDC/云机房"
    assert diagnostic.classification.risk_score >= 52
    assert "IDC" in diagnostic.reputation[0].summary_text()


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
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0": {
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
        http_get=_fake_http_get(mapping),
        reverse_resolver=lambda _ip: "",
    )

    diagnostic = report.diagnostics[0]
    assert len(diagnostic.reputation) == 2
    assert diagnostic.reputation[1].source == "vpnapi"
    assert diagnostic.classification.ip_type == "代理/VPN/Tor 可疑"
    assert diagnostic.classification.risk_score >= 78
    assert any("VPNAPI.io security: VPN" in signal for signal in diagnostic.classification.signals)


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
        f"https://proxycheck.io/v3/{ip}?p=0&tag=0": {
            "status": "ok",
            ip: {"network": {"type": "Residential"}, "detections": {"anonymous": False, "risk": 8}},
        },
        f"https://ipqualityscore.com/api/json/ip/ipqs-key/{ip}?strictness=1&allow_public_access_points=true&fast=true": {
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
        http_get=_fake_http_get(mapping),
        reverse_resolver=lambda _ip: "",
    )

    diagnostic = report.diagnostics[0]
    assert len(diagnostic.reputation) == 2
    assert diagnostic.reputation[1].fraud_score == 82
    assert diagnostic.classification.ip_type == "住宅代理/匿名出口可疑"
    assert diagnostic.classification.risk_score == 82
    assert any("IPQS fraud_score=82" in signal for signal in diagnostic.classification.signals)


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
    assert not any("ping0.cc" in url or "proxycheck.io" in url or "ipqualityscore.com" in url or "vpnapi.io" in url for url in seen_urls)


def test_ipqs_key_pool_rotates_after_limited_key():
    ip = "203.0.113.35"
    first_url = f"https://ipqualityscore.com/api/json/ip/limited-key/{ip}?strictness=1&allow_public_access_points=true&fast=true"
    second_url = f"https://ipqualityscore.com/api/json/ip/fresh-key/{ip}?strictness=1&allow_public_access_points=true&fast=true"
    mapping = {
        "https://api.ipify.org?format=json": {"ip": ip},
        "https://api6.ipify.org?format=json": network_diagnostics.HttpResult(
            url="https://api6.ipify.org?format=json",
            ok=False,
            error="network unreachable",
        ),
        "https://api64.ipify.org?format=json": {"ip": ip},
        first_url: network_diagnostics.HttpResult(url=first_url, ok=False, status_code=429, error="HTTP 429"),
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
    assert diagnostic.reputation[0].attempts == ["Key #1 失败: HTTP 429", "Key #2 成功"]
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
