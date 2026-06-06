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
    assert "私有历史风控库" in report.notices[0]


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
