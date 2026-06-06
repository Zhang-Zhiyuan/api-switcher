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


def test_probe_public_ip_rejects_invalid_endpoint_response():
    result = network_diagnostics.probe_public_ip(
        "IPv4",
        "https://example.test/ip",
        1.0,
        _fake_http_get({"https://example.test/ip": {"ip": "not-an-ip"}}),
    )

    assert result.ok is False
    assert "有效 IP" in result.error
