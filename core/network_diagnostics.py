"""Public-network diagnostics built from free, replaceable data sources."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from core.network_diagnostic_settings import (
    SERVICE_IPQS,
    SERVICE_PING0,
    SERVICE_PROXYCHECK,
    SERVICE_VPNAPI,
    normalize_services,
    parse_api_keys,
)


USER_AGENT = "API-Switcher-Network-Diagnostics/1.0"
DEFAULT_TIMEOUT = 8.0

PUBLIC_IP_ENDPOINTS = (
    ("IPv4", "https://api.ipify.org?format=json"),
    ("IPv6", "https://api6.ipify.org?format=json"),
    ("默认出口", "https://api64.ipify.org?format=json"),
)

PING0_FREE_GEO_ENDPOINTS = {
    "IPv4": "https://ipv4.ping0.cc/geo",
    "IPv6": "https://ipv6.ping0.cc/geo",
    "默认出口": "https://ping0.cc/geo",
}
PING0_DETAIL_URL = "https://ping0.cc/ip/{ip}"
PING0_PING_URL = "https://ping0.cc/ping/{ip}"
PING0_API_URL = "https://ping0.cc/apiloc/apikey({api_key})/ip({ip})"
PROXYCHECK_API_URL = "https://proxycheck.io/v3/{ip}"
IPQS_API_URL = "https://ipqualityscore.com/api/json/ip/{api_key}/{ip}"
VPNAPI_URL = "https://vpnapi.io/api/{ip}"
IPV4_CANDIDATE_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


IDC_KEYWORDS = (
    "akamai",
    "alibaba",
    "amazon",
    "aws",
    "azure",
    "baidu cloud",
    "cloud",
    "cloudflare",
    "colo",
    "colocation",
    "contabo",
    "data center",
    "datacenter",
    "digitalocean",
    "fastly",
    "gcore",
    "google",
    "hetzner",
    "huawei cloud",
    "hosting",
    "hostwinds",
    "leaseweb",
    "linode",
    "microsoft",
    "netcup",
    "oracle",
    "ovh",
    "rackspace",
    "server",
    "tencent",
    "vps",
    "vultr",
)

ISP_KEYWORDS = (
    "broadband",
    "cable",
    "china mobile",
    "china telecom",
    "china unicom",
    "comcast",
    "communications",
    "fiber",
    "fibre",
    "isp",
    "mobile",
    "spectrum",
    "telecom",
    "telefonica",
    "telstra",
    "verizon",
    "vodafone",
)

PROXY_KEYWORDS = (
    "anonymous",
    "exit node",
    "proxy",
    "relay",
    "socks",
    "tor",
    "vpn",
)

DYNAMIC_KEYWORDS = (
    "broadband",
    "cable",
    "dhcp",
    "dialup",
    "dsl",
    "dynamic",
    "fiber",
    "fibre",
    "home",
    "pool",
    "pppoe",
    "residential",
)


@dataclass
class HttpResult:
    """Small transport result used to keep the diagnostics testable."""

    url: str
    ok: bool
    text: str = ""
    status_code: Optional[int] = None
    response_time: Optional[float] = None
    error: str = ""


@dataclass
class EndpointProbe:
    """Result of a public-IP endpoint probe."""

    label: str
    url: str
    ok: bool
    ip: str = ""
    response_time: Optional[float] = None
    status_code: Optional[int] = None
    error: str = ""


@dataclass
class GeoInfo:
    """Normalized geolocation and network ownership details."""

    ip: str
    source: str = ""
    ok: bool = False
    country: str = ""
    country_code: str = ""
    region: str = ""
    city: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: str = ""
    asn: str = ""
    asn_name: str = ""
    org: str = ""
    isp: str = ""
    security: dict[str, bool] = field(default_factory=dict)
    error: str = ""

    def location_text(self) -> str:
        parts = [self.country, self.region, self.city]
        return " ".join(part for part in parts if part) or "-"

    def owner_text(self) -> str:
        parts = [self.asn, self.asn_name or self.org or self.isp]
        return " ".join(part for part in parts if part) or "-"


@dataclass
class IpClassification:
    """Heuristic classification when no private IP intelligence is available."""

    ip_type: str
    risk_score: int
    risk_label: str
    confidence: str
    signals: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass
class Ping0Quality:
    """Ping0 quality data or links for a reachable IP."""

    ip: str
    ok: bool = False
    source: str = ""
    detail_url: str = ""
    ping_url: str = ""
    response_time: Optional[float] = None
    error: str = ""
    location: str = ""
    country: str = ""
    province: str = ""
    city: str = ""
    asn: str = ""
    asn_name: str = ""
    org: str = ""
    isidc: Optional[bool] = None
    iprisk: Optional[int] = None
    isnative: Optional[bool] = None
    asntype: str = ""
    orgtype: str = ""
    api_key_label: str = ""
    attempts: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_paid_quality(self) -> bool:
        return self.ok and self.source == "ping0-api" and (self.isidc is not None or self.iprisk is not None)

    def quality_text(self) -> str:
        if self.source == "disabled":
            return "Ping0 未启用"
        if not self.ok:
            return f"Ping0 未完成: {self.error or '未获取到结果'}"
        if self.has_paid_quality:
            ip_type = "IDC机房IP" if self.isidc else "家庭/非IDC宽带IP"
            risk = str(self.iprisk) if self.iprisk is not None else "-"
            native = "原生IP" if self.isnative else ("广播IP" if self.isnative is False else "未知")
            key = f" | {self.api_key_label}" if self.api_key_label else ""
            return f"{ip_type} | 风控值 {risk} | {native}{key}"
        return "Ping0 免费 Geo 已返回；完整风控/IP 类型需要 Ping0 API Key"


@dataclass
class ReputationInfo:
    """Normalized IP reputation result from an external provider."""

    ip: str
    source: str
    ok: bool = False
    response_time: Optional[float] = None
    network_type: str = ""
    provider: str = ""
    organization: str = ""
    asn: str = ""
    risk_score: Optional[int] = None
    fraud_score: Optional[int] = None
    confidence_score: Optional[int] = None
    flags: dict[str, bool] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)
    api_key_label: str = ""
    attempts: list[str] = field(default_factory=list)
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def source_label(self) -> str:
        return {
            "proxycheck": "ProxyCheck",
            "ipqs": "IPQualityScore",
            "vpnapi": "VPNAPI.io",
        }.get(self.source, self.source)

    @property
    def network_type_label(self) -> str:
        return _network_type_label(self.network_type)

    def summary_text(self) -> str:
        if not self.ok:
            return f"{self.source_label}: 未完成 ({self.error or '请求失败'})"
        parts = [self.source_label]
        if self.network_type:
            parts.append(f"类型 {self.network_type_label}")
        if self.risk_score is not None:
            parts.append(f"风险 {self.risk_score}%")
        if self.fraud_score is not None:
            parts.append(f"欺诈分 {self.fraud_score}")
        if self.api_key_label:
            parts.append(self.api_key_label)
        active_flags = _active_flag_names(self.flags)
        if active_flags:
            parts.append("命中 " + "/".join(active_flags))
        if self.provider or self.organization:
            parts.append(self.provider or self.organization)
        return " | ".join(parts)


@dataclass
class IpDiagnostic:
    """Complete diagnostic for one observed public IP."""

    label: str
    ip: str
    probe: EndpointProbe
    geo: GeoInfo
    ping0: Ping0Quality
    reputation: list[ReputationInfo] = field(default_factory=list)
    reverse_dns: str = ""
    classification: IpClassification = field(
        default_factory=lambda: IpClassification(
            ip_type="未知",
            risk_score=50,
            risk_label="未知",
            confidence="低",
        )
    )


@dataclass
class NetworkDiagnosticReport:
    """Top-level network diagnostic report."""

    generated_at: str
    probes: list[EndpointProbe] = field(default_factory=list)
    diagnostics: list[IpDiagnostic] = field(default_factory=list)
    summary: str = ""
    notices: list[str] = field(default_factory=list)

    @property
    def has_ipv4(self) -> bool:
        return any(_is_ip_version(diag.ip, 4) for diag in self.diagnostics)

    @property
    def has_ipv6(self) -> bool:
        return any(_is_ip_version(diag.ip, 6) for diag in self.diagnostics)


HttpGetter = Callable[[str, float], HttpResult]
ReverseResolver = Callable[[str], str]


def detect_network(
    timeout: float = DEFAULT_TIMEOUT,
    ping0_api_key: str = "",
    proxycheck_api_key: str = "",
    ipqs_api_key: str = "",
    vpnapi_api_key: str = "",
    enabled_services: Optional[list[str] | set[str] | tuple[str, ...]] = None,
    ping0_api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
    proxycheck_api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
    ipqs_api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
    vpnapi_api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
    http_get: Optional[HttpGetter] = None,
    reverse_resolver: Optional[ReverseResolver] = None,
) -> NetworkDiagnosticReport:
    """Speed-test public exits and enrich reachable IPs with quality data."""

    http_get = http_get or _http_get
    reverse_resolver = reverse_resolver or _reverse_dns
    ping0_keys = _key_pool(ping0_api_keys, ping0_api_key)
    proxycheck_keys = _key_pool(proxycheck_api_keys, proxycheck_api_key)
    ipqs_keys = _key_pool(ipqs_api_keys, ipqs_api_key)
    vpnapi_keys = _key_pool(vpnapi_api_keys, vpnapi_api_key)
    services = _enabled_services(
        enabled_services,
        ping0_keys=ping0_keys,
        proxycheck_keys=proxycheck_keys,
        ipqs_keys=ipqs_keys,
        vpnapi_keys=vpnapi_keys,
    )
    probes: list[EndpointProbe] = []
    diagnostics: list[IpDiagnostic] = []
    seen_ips: set[str] = set()

    for label, url in PUBLIC_IP_ENDPOINTS:
        probe = probe_public_ip(label, url, timeout, http_get)
        probes.append(probe)
        if not probe.ok or not probe.ip or probe.ip in seen_ips:
            continue

        seen_ips.add(probe.ip)
        if SERVICE_PING0 in services:
            ping0 = lookup_ping0_quality(probe.ip, probe.label, timeout, http_get, api_keys=ping0_keys)
        else:
            ping0 = _disabled_ping0_quality(probe.ip)
        reputation = lookup_reputation(
            probe.ip,
            timeout,
            http_get,
            enabled_services=services,
            proxycheck_api_keys=proxycheck_keys,
            ipqs_api_keys=ipqs_keys,
            vpnapi_api_keys=vpnapi_keys,
        )
        geo = lookup_geo(probe.ip, timeout, http_get)
        rdns = reverse_resolver(probe.ip)
        classification = classify_ip(geo, rdns, ping0, reputation)
        diagnostics.append(
            IpDiagnostic(
                label=label,
                ip=probe.ip,
                probe=probe,
                geo=geo,
                ping0=ping0,
                reputation=reputation,
                reverse_dns=rdns,
                classification=classification,
            )
        )

    notices = [
        "检测流程为先测速，再只对可连通公网出口调用用户启用的质量接口。",
        "多个 API Key 会按顺序尝试；某个 Key 失败或限额后会自动使用下一个。",
        "多源分类会优先标记 VPN、Proxy、Tor、Relay 等匿名网络；家宽/商宽/蜂窝/机房以各服务返回的网络类型为准。",
    ]
    if SERVICE_PROXYCHECK in services:
        notices.append("ProxyCheck 可无 Key 调用；配置多个 Key 后会优先使用 Key 池。")
    if SERVICE_PING0 not in services:
        notices.append("Ping0 已由用户关闭。")
    elif not ping0_keys:
        notices.append("未填写 Ping0 API Key 时，只使用 Ping0 免费 Geo 和详情页链接；指定 IP 风控值需要 Ping0 官方 API。")
    if SERVICE_IPQS not in services:
        notices.append("IPQualityScore 已由用户关闭。")
    elif not ipqs_keys:
        notices.append("IPQualityScore 已启用但未配置 Key，本次不会发起 IPQS 请求。")
    if SERVICE_VPNAPI not in services:
        notices.append("VPNAPI.io 已由用户关闭。")
    elif not vpnapi_keys:
        notices.append("VPNAPI.io 已启用但未配置 Key，本次不会发起 VPNAPI.io 请求。")

    report = NetworkDiagnosticReport(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        probes=probes,
        diagnostics=diagnostics,
        notices=notices,
    )
    report.summary = summarize_report(report)
    return report


def probe_public_ip(label: str, url: str, timeout: float, http_get: HttpGetter) -> EndpointProbe:
    result = http_get(url, timeout)
    if not result.ok:
        return EndpointProbe(
            label=label,
            url=url,
            ok=False,
            response_time=result.response_time,
            status_code=result.status_code,
            error=result.error or "请求失败",
        )

    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        data = result.text.strip()

    ip = _extract_ip_value(data)
    if not _valid_ip(ip):
        return EndpointProbe(
            label=label,
            url=url,
            ok=False,
            response_time=result.response_time,
            status_code=result.status_code,
            error="未返回有效 IP 地址",
        )

    return EndpointProbe(
        label=label,
        url=url,
        ok=True,
        ip=ip,
        response_time=result.response_time,
        status_code=result.status_code,
    )


def lookup_ping0_quality(
    ip: str,
    label: str,
    timeout: float,
    http_get: HttpGetter,
    api_key: str = "",
    api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
) -> Ping0Quality:
    """Look up Ping0 quality for a reachable IP.

    The paid API is used only when an API key is provided. Without a key, this
    uses Ping0's free current-visitor Geo endpoint for the matching stack and
    still exposes the Ping0 detail and Ping pages for the IP.
    """

    quality = Ping0Quality(
        ip=ip,
        detail_url=PING0_DETAIL_URL.format(ip=urllib.parse.quote(ip, safe=":.")),
        ping_url=PING0_PING_URL.format(ip=urllib.parse.quote(ip, safe=":.")),
    )
    keys = _key_pool(api_keys, api_key)
    attempts: list[str] = []
    for index, key in enumerate(keys):
        paid_quality = _lookup_ping0_paid_quality(quality, timeout, http_get, key)
        paid_quality.api_key_label = _key_label(index)
        if paid_quality.ok:
            paid_quality.attempts = attempts + [f"{_key_label(index)} 成功"]
            return paid_quality
        attempts.append(f"{_key_label(index)} 失败: {paid_quality.error or '请求失败'}")

    if attempts:
        quality.attempts = attempts
    return _lookup_ping0_free_geo(quality, label, timeout, http_get)


def _lookup_ping0_paid_quality(
    quality: Ping0Quality,
    timeout: float,
    http_get: HttpGetter,
    api_key: str,
) -> Ping0Quality:
    quality.ok = False
    quality.error = ""
    url = PING0_API_URL.format(
        api_key=urllib.parse.quote(api_key, safe=""),
        ip=urllib.parse.quote(quality.ip, safe=":."),
    )
    result = http_get(url, timeout)
    quality.response_time = result.response_time
    quality.source = "ping0-api"
    if not result.ok:
        quality.error = result.error or "Ping0 API 请求失败"
        return quality

    try:
        data = json.loads(result.text)
    except json.JSONDecodeError as exc:
        quality.error = f"Ping0 API JSON 解析失败: {exc}"
        return quality

    if not isinstance(data, dict):
        quality.error = "Ping0 API 返回结构异常"
        return quality
    payload = _payload_dict(data, "data", "result", "ipdata")
    if _payload_is_error(data) or (payload is not data and _payload_is_error(payload)):
        quality.error = _payload_error_text(payload, _payload_error_text(data, "Ping0 API 查询失败"))
        return quality
    returned_ip = _first_text(payload, "ip", "query", "address")
    if returned_ip and _valid_ip(returned_ip) and returned_ip != quality.ip:
        quality.error = f"Ping0 API 返回 IP 不匹配: {returned_ip}"
        return quality

    quality.ok = True
    quality.raw = payload
    quality.location = _first_text(payload, "location", "loc")
    quality.country = _first_text(payload, "country")
    quality.province = _first_text(payload, "province", "region", "state")
    quality.city = _first_text(payload, "city")
    quality.asn = _normalize_asn(_first_value(payload, "asn", "as"))
    quality.asn_name = _first_text(payload, "asnname", "asn_name", "asname")
    quality.org = _first_text(payload, "org", "organization", "organisation")
    quality.isidc = _optional_bool(_first_value(payload, "isidc", "is_idc", "idc"))
    quality.iprisk = _optional_int(_first_value(payload, "iprisk", "ip_risk", "risk", "risk_score"))
    quality.isnative = _optional_bool(_first_value(payload, "isnative", "is_native", "native"))
    quality.asntype = _first_text(payload, "asntype", "asn_type")
    quality.orgtype = _first_text(payload, "orgtype", "org_type")
    return quality


def _lookup_ping0_free_geo(
    quality: Ping0Quality,
    label: str,
    timeout: float,
    http_get: HttpGetter,
) -> Ping0Quality:
    url = PING0_FREE_GEO_ENDPOINTS.get(label) or PING0_FREE_GEO_ENDPOINTS["默认出口"]
    result = http_get(url, timeout)
    quality.response_time = result.response_time
    quality.source = "ping0-free-geo"
    if not result.ok:
        quality.error = result.error or "Ping0 免费 Geo 请求失败"
        return quality

    lines = [line.strip() for line in result.text.splitlines() if line.strip()]
    if not lines:
        quality.error = "Ping0 免费 Geo 未返回内容"
        return quality
    returned_ip = lines[0]
    if _valid_ip(returned_ip) and returned_ip != quality.ip:
        quality.error = f"Ping0 免费 Geo 返回 IP 不匹配: {returned_ip}"
        return quality

    quality.ok = True
    if _valid_ip(returned_ip):
        quality.ip = returned_ip
    quality.location = lines[1] if len(lines) > 1 else ""
    quality.asn = _normalize_asn(lines[2] if len(lines) > 2 else "")
    quality.org = lines[3] if len(lines) > 3 else ""
    quality.raw = {
        "ip": quality.ip,
        "location": quality.location,
        "asn": quality.asn,
        "org": quality.org,
    }
    return quality


def lookup_reputation(
    ip: str,
    timeout: float,
    http_get: HttpGetter,
    proxycheck_api_key: str = "",
    ipqs_api_key: str = "",
    vpnapi_api_key: str = "",
    enabled_services: Optional[list[str] | set[str] | tuple[str, ...]] = None,
    proxycheck_api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
    ipqs_api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
    vpnapi_api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
) -> list[ReputationInfo]:
    """Look up reputation sources that can classify residential/hosting/proxy usage."""

    proxycheck_keys = _key_pool(proxycheck_api_keys, proxycheck_api_key)
    ipqs_keys = _key_pool(ipqs_api_keys, ipqs_api_key)
    vpnapi_keys = _key_pool(vpnapi_api_keys, vpnapi_api_key)
    services = _enabled_services(
        enabled_services,
        ping0_keys=[],
        proxycheck_keys=proxycheck_keys,
        ipqs_keys=ipqs_keys,
        vpnapi_keys=vpnapi_keys,
    )
    results: list[ReputationInfo] = []
    if SERVICE_PROXYCHECK in services:
        results.append(lookup_proxycheck_reputation(ip, timeout, http_get, api_keys=proxycheck_keys))
    if SERVICE_IPQS in services:
        results.append(lookup_ipqs_reputation(ip, timeout, http_get, api_keys=ipqs_keys))
    if SERVICE_VPNAPI in services:
        results.append(lookup_vpnapi_reputation(ip, timeout, http_get, api_keys=vpnapi_keys))
    return results


def lookup_proxycheck_reputation(
    ip: str,
    timeout: float,
    http_get: HttpGetter,
    api_key: str = "",
    api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
) -> ReputationInfo:
    keys = _key_pool(api_keys, api_key)
    keys_to_try = keys + [""] if keys else [""]
    attempts: list[str] = []
    for index, key in enumerate(keys_to_try):
        info = _lookup_proxycheck_reputation_once(ip, timeout, http_get, key)
        info.api_key_label = _key_label(index) if key else "无 Key"
        if info.ok:
            info.attempts = attempts + [f"{info.api_key_label} 成功"]
            return info
        attempts.append(f"{info.api_key_label} 失败: {info.error or '请求失败'}")
    info.attempts = attempts
    return info


def _lookup_proxycheck_reputation_once(
    ip: str,
    timeout: float,
    http_get: HttpGetter,
    api_key: str = "",
) -> ReputationInfo:
    safe_ip = urllib.parse.quote(ip, safe=":.")
    params = {"p": "0", "tag": "0"}
    api_key = (api_key or "").strip()
    if api_key:
        params["key"] = api_key
    url = f"{PROXYCHECK_API_URL.format(ip=safe_ip)}?{urllib.parse.urlencode(params)}"
    result = http_get(url, timeout)
    info = ReputationInfo(ip=ip, source="proxycheck", response_time=result.response_time)
    if not result.ok:
        info.error = result.error or "ProxyCheck 请求失败"
        return info

    try:
        data = json.loads(result.text)
    except json.JSONDecodeError as exc:
        info.error = f"ProxyCheck JSON 解析失败: {exc}"
        return info

    if not isinstance(data, dict):
        info.error = "ProxyCheck 返回结构异常"
        return info
    if _payload_is_error(data):
        info.error = _payload_error_text(data, "ProxyCheck 查询失败")
        return info

    record = _extract_proxycheck_record(data, ip)
    if not isinstance(record, dict):
        info.error = "ProxyCheck 未返回当前 IP 记录"
        return info

    network = record.get("network") if isinstance(record.get("network"), dict) else record
    detections = record.get("detections") if isinstance(record.get("detections"), dict) else record
    info.ok = True
    info.raw = record
    record_type = _first_text(record, "type", "connection_type", "network_type")
    info.network_type = _proxycheck_network_type(record_type, network)
    info.provider = _first_text(network, "provider", "isp") or _first_text(record, "provider", "isp")
    info.organization = _first_text(network, "organisation", "organization", "org") or _first_text(record, "organisation", "organization", "org")
    info.asn = _normalize_asn(_first_value(network, "asn", "as") or _first_value(record, "asn", "as"))
    info.risk_score = _optional_int(_first_value(detections, "risk", "risk_score", "score") or _first_value(record, "risk", "risk_score", "score") or data.get("risk"))
    info.confidence_score = _optional_int(_first_value(detections, "confidence", "confidence_score") or _first_value(record, "confidence", "confidence_score"))
    info.flags = _extract_bool_flags(
        detections,
        (
            "anonymous",
            "proxy",
            "vpn",
            "tor",
            "relay",
            "hosting",
            "scraper",
            "compromised",
            "residential_proxy",
        ),
    )
    if not info.flags:
        info.flags = _extract_bool_flags(record, ("anonymous", "proxy", "vpn", "tor", "relay", "hosting"))
    if _proxycheck_anonymous_type(record_type):
        info.flags[record_type.strip().lower()] = True
    if info.network_type:
        info.signals.append(f"ProxyCheck network.type={info.network_type}")
    active = _active_flag_names(info.flags)
    if active:
        info.signals.append("ProxyCheck detections: " + ", ".join(active))
    if info.risk_score is not None:
        info.signals.append(f"ProxyCheck risk={info.risk_score}")
    return info


def lookup_ipqs_reputation(
    ip: str,
    timeout: float,
    http_get: HttpGetter,
    api_key: str = "",
    api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
) -> ReputationInfo:
    keys = _key_pool(api_keys, api_key)
    if not keys:
        return ReputationInfo(ip=ip, source="ipqs", error="未配置 IPQualityScore API Key")
    attempts: list[str] = []
    for index, key in enumerate(keys):
        info = _lookup_ipqs_reputation_once(ip, timeout, http_get, key)
        info.api_key_label = _key_label(index)
        if info.ok:
            info.attempts = attempts + [f"{info.api_key_label} 成功"]
            return info
        attempts.append(f"{info.api_key_label} 失败: {info.error or '请求失败'}")
    info.attempts = attempts
    return info


def _lookup_ipqs_reputation_once(
    ip: str,
    timeout: float,
    http_get: HttpGetter,
    api_key: str,
) -> ReputationInfo:
    safe_ip = urllib.parse.quote(ip, safe=":.")
    safe_key = urllib.parse.quote((api_key or "").strip(), safe="")
    params = {
        "strictness": "1",
        "allow_public_access_points": "true",
        "fast": "true",
    }
    url = f"{IPQS_API_URL.format(api_key=safe_key, ip=safe_ip)}?{urllib.parse.urlencode(params)}"
    result = http_get(url, timeout)
    info = ReputationInfo(ip=ip, source="ipqs", response_time=result.response_time)
    if not result.ok:
        info.error = result.error or "IPQualityScore 请求失败"
        return info

    try:
        data = json.loads(result.text)
    except json.JSONDecodeError as exc:
        info.error = f"IPQualityScore JSON 解析失败: {exc}"
        return info

    if not isinstance(data, dict):
        info.error = "IPQualityScore 返回结构异常"
        return info
    payload = _payload_dict(data, "data", "result")
    if _payload_is_error(data) or (payload is not data and _payload_is_error(payload)):
        info.error = _payload_error_text(payload, _payload_error_text(data, "IPQualityScore 查询失败"))
        return info
    info.ok = True
    info.raw = payload
    info.network_type = _first_text(payload, "connection_type", "network_type", "type")
    info.provider = _first_text(payload, "ISP", "isp", "provider")
    info.organization = _first_text(payload, "organization", "Organization", "org")
    info.asn = _normalize_asn(_first_value(payload, "ASN", "asn", "as"))
    info.fraud_score = _optional_int(_first_value(payload, "fraud_score", "risk_score", "risk"))
    info.risk_score = info.fraud_score
    info.flags = _extract_bool_flags(
        payload,
        (
            "proxy",
            "vpn",
            "tor",
            "active_vpn",
            "active_tor",
            "recent_abuse",
            "frequent_abuser",
            "high_risk_attacks",
            "bot_status",
            "shared_connection",
            "dynamic_connection",
            "mobile",
        ),
    )
    if info.network_type:
        info.signals.append(f"IPQS connection_type={info.network_type}")
    active = _active_flag_names(info.flags)
    if active:
        info.signals.append("IPQS flags: " + ", ".join(active))
    if info.fraud_score is not None:
        info.signals.append(f"IPQS fraud_score={info.fraud_score}")
    return info


def lookup_vpnapi_reputation(
    ip: str,
    timeout: float,
    http_get: HttpGetter,
    api_key: str = "",
    api_keys: Optional[list[str] | tuple[str, ...] | str] = None,
) -> ReputationInfo:
    keys = _key_pool(api_keys, api_key)
    if not keys:
        return ReputationInfo(ip=ip, source="vpnapi", error="未配置 VPNAPI.io API Key")
    attempts: list[str] = []
    for index, key in enumerate(keys):
        info = _lookup_vpnapi_reputation_once(ip, timeout, http_get, key)
        info.api_key_label = _key_label(index)
        if info.ok:
            info.attempts = attempts + [f"{info.api_key_label} 成功"]
            return info
        attempts.append(f"{info.api_key_label} 失败: {info.error or '请求失败'}")
    info.attempts = attempts
    return info


def _lookup_vpnapi_reputation_once(
    ip: str,
    timeout: float,
    http_get: HttpGetter,
    api_key: str,
) -> ReputationInfo:
    safe_ip = urllib.parse.quote(ip, safe=":.")
    url = f"{VPNAPI_URL.format(ip=safe_ip)}?{urllib.parse.urlencode({'key': (api_key or '').strip()})}"
    result = http_get(url, timeout)
    info = ReputationInfo(ip=ip, source="vpnapi", response_time=result.response_time)
    if not result.ok:
        info.error = result.error or "VPNAPI.io 请求失败"
        return info

    try:
        data = json.loads(result.text)
    except json.JSONDecodeError as exc:
        info.error = f"VPNAPI.io JSON 解析失败: {exc}"
        return info

    if not isinstance(data, dict):
        info.error = "VPNAPI.io 返回结构异常"
        return info
    payload = _payload_dict(data, "data", "result")
    if (_payload_is_error(data) or data.get("message") or (payload is not data and _payload_is_error(payload))) and not payload.get("security"):
        info.error = _payload_error_text(payload, _payload_error_text(data, "VPNAPI.io 查询失败"))
        return info
    security = payload.get("security") if isinstance(payload.get("security"), dict) else {}
    network = payload.get("network") if isinstance(payload.get("network"), dict) else {}
    info.ok = True
    info.raw = payload
    info.provider = _first_text(network, "autonomous_system_organization", "organization", "org")
    info.organization = info.provider
    info.asn = _normalize_asn(_first_value(network, "autonomous_system_number", "asn", "as"))
    info.flags = _extract_bool_flags(security, ("vpn", "proxy", "tor", "relay"))
    active = _active_flag_names(info.flags)
    if active:
        info.signals.append("VPNAPI.io security: " + ", ".join(active))
    else:
        info.signals.append("VPNAPI.io 未命中 VPN/Proxy/Tor/Relay")
    return info


def lookup_geo(ip: str, timeout: float, http_get: HttpGetter) -> GeoInfo:
    safe_ip = urllib.parse.quote(ip, safe=":.")
    url = f"https://ipwho.is/{safe_ip}"
    result = http_get(url, timeout)
    if not result.ok:
        return GeoInfo(ip=ip, source="ipwho.is", ok=False, error=result.error or "查询失败")

    try:
        data = json.loads(result.text)
    except json.JSONDecodeError as exc:
        return GeoInfo(ip=ip, source="ipwho.is", ok=False, error=f"JSON 解析失败: {exc}")

    if not isinstance(data, dict):
        return GeoInfo(ip=ip, source="ipwho.is", ok=False, error="返回结构异常")
    if _optional_boolish(data.get("success")) is False:
        return GeoInfo(ip=ip, source="ipwho.is", ok=False, error=str(data.get("message") or "查询失败"))

    connection = data.get("connection") if isinstance(data.get("connection"), dict) else {}
    security = data.get("security") if isinstance(data.get("security"), dict) else {}
    asn = connection.get("asn") or data.get("asn") or ""
    asn_text = f"AS{asn}" if asn and not str(asn).upper().startswith("AS") else str(asn or "")

    return GeoInfo(
        ip=ip,
        source="ipwho.is",
        ok=True,
        country=str(data.get("country") or ""),
        country_code=str(data.get("country_code") or ""),
        region=str(data.get("region") or ""),
        city=str(data.get("city") or ""),
        latitude=_to_float(data.get("latitude")),
        longitude=_to_float(data.get("longitude")),
        timezone=str(data.get("timezone", {}).get("id") if isinstance(data.get("timezone"), dict) else data.get("timezone") or ""),
        asn=asn_text,
        asn_name=str(connection.get("org") or ""),
        org=str(connection.get("org") or data.get("org") or ""),
        isp=str(connection.get("isp") or data.get("isp") or ""),
        security={
            key: parsed
            for key, value in security.items()
            if (parsed := _optional_boolish(value)) is not None
        },
    )


def classify_ip(
    geo: GeoInfo,
    reverse_dns: str = "",
    ping0: Optional[Ping0Quality] = None,
    reputation: Optional[list[ReputationInfo]] = None,
) -> IpClassification:
    reputation = reputation or []
    reputation_classification = _classify_from_reputation(reputation, ping0)
    if reputation_classification:
        return reputation_classification

    if ping0 and ping0.has_paid_quality:
        signals = _ping0_signals(ping0)
        risk = ping0.iprisk if ping0.iprisk is not None else (52 if ping0.isidc is True else 18)
        risk = max(0, min(100, int(risk)))
        if ping0.isidc is True:
            ip_type = "IDC/云机房"
        elif ping0.isidc is False:
            ip_type = "家庭/非IDC宽带"
        else:
            ip_type = "Ping0质量已返回"
        return IpClassification(
            ip_type=ip_type,
            risk_score=risk,
            risk_label=_risk_label(risk),
            confidence="高",
            signals=signals,
            limitations=[
                "Ping0 数据来自其官方接口，准确性和额度以 Ping0 服务为准。",
                "适用场景仍建议结合实际业务登录、请求和平台反馈验证。",
            ],
        )

    text = " ".join(
        part
        for part in (
            geo.asn_name,
            geo.org,
            geo.isp,
            reverse_dns,
        )
        if part
    ).lower()

    security_hits = [
        key.upper()
        for key, enabled in geo.security.items()
        if enabled and key.lower() in {"vpn", "proxy", "tor", "relay", "hosting"}
    ]
    proxy_hits = _keyword_hits(text, PROXY_KEYWORDS)
    idc_hits = _keyword_hits(text, IDC_KEYWORDS)
    isp_hits = _keyword_hits(text, ISP_KEYWORDS)
    dynamic_hits = _keyword_hits(text, DYNAMIC_KEYWORDS)

    signals: list[str] = []
    risk = 35
    confidence = "低"
    ip_type = "未知"

    if security_hits or proxy_hits:
        ip_type = "代理/VPN/Tor 可疑"
        risk = 78
        confidence = "中"
        if security_hits:
            signals.append("上游安全字段命中: " + ", ".join(security_hits))
        if proxy_hits:
            signals.append("代理关键词命中: " + ", ".join(proxy_hits))
    elif idc_hits:
        ip_type = "IDC/云机房"
        risk = 52
        confidence = "中"
        signals.append("机房关键词命中: " + ", ".join(idc_hits[:4]))
    elif dynamic_hits or isp_hits:
        ip_type = "运营商/宽带"
        risk = 18
        confidence = "中" if dynamic_hits else "低"
        if dynamic_hits:
            signals.append("动态/宽带关键词命中: " + ", ".join(dynamic_hits[:4]))
        elif isp_hits:
            signals.append("运营商关键词命中: " + ", ".join(isp_hits[:4]))
    else:
        signals.append("未命中已知机房、代理或宽带关键词")

    if not geo.ok:
        risk = min(100, risk + 8)
        signals.append("Geo/ASN 查询失败")
    if not reverse_dns:
        signals.append("未获取到反向 DNS")

    risk = max(0, min(100, risk))
    return IpClassification(
        ip_type=ip_type,
        risk_score=risk,
        risk_label=_risk_label(risk),
        confidence=confidence,
        signals=signals,
        limitations=[
            "没有外部信誉源明确分类时，无法可靠区分真实家宽、商宽和机房转售段。",
            "没有攻击/垃圾邮件/爆破历史，风险分只是当前公开信息的启发式结果。",
            "没有注册地与广播路径历史，暂不判断原生 IP 或广播 IP。",
        ],
    )


def _classify_from_reputation(
    reputation: list[ReputationInfo],
    ping0: Optional[Ping0Quality] = None,
) -> Optional[IpClassification]:
    ok_results = [item for item in reputation if item.ok]
    if not ok_results:
        return None

    signals = _reputation_signals(ok_results)
    if ping0 and ping0.has_paid_quality:
        signals.extend(_ping0_signals(ping0))

    categories = _reputation_category_votes(ok_results, ping0)
    category_summary = _category_vote_summary(categories)
    if category_summary:
        signals.append("多源类型投票: " + category_summary)

    if ping0 and ping0.has_paid_quality and ping0.iprisk is not None and ping0.iprisk >= 75:
        signals.append(f"Ping0 高风控值: {ping0.iprisk}")

    suspicious = [
        item
        for item in ok_results
        if _has_anonymity_flag(item.flags) or (_explicit_reputation_score(item) or 0) >= 75
    ]
    ping0_suspicious = bool(
        ping0 and ping0.has_paid_quality and ping0.iprisk is not None and ping0.iprisk >= 75
    )
    if suspicious or ping0_suspicious:
        has_anonymity_signal = any(_has_anonymity_flag(item.flags) for item in suspicious)
        risk_values = [_reputation_score(item, 78) for item in suspicious]
        if ping0_suspicious and ping0 and ping0.iprisk is not None:
            risk_values.append(ping0.iprisk)
        risk = max(78, max(risk_values or [78]))
        risk = max(0, min(100, risk))
        residentialish = bool(categories["residential"] or categories["mobile"])
        if has_anonymity_signal and residentialish:
            ip_type = "住宅代理/匿名出口可疑"
        elif has_anonymity_signal:
            ip_type = "代理/VPN/Tor 可疑"
        elif residentialish:
            ip_type = "住宅 IP 高风险"
        else:
            ip_type = "高风险出口可疑"
        confidence = "高" if len(suspicious) >= 2 or risk >= 85 or ping0_suspicious else "中"
        return IpClassification(
            ip_type=ip_type,
            risk_score=risk,
            risk_label=_risk_label(risk),
            confidence=confidence,
            signals=signals,
            limitations=[
                "信誉接口的命中代表第三方观测结果，可能存在短期误报或漏报。",
                "住宅代理可能仍显示为家宽归属，需同时关注 Proxy/VPN/Tor/Relay 等匿名信号。",
            ],
        )

    if not any(categories.values()):
        return None

    hosting = categories["hosting"]
    business = categories["business"]
    mobile = categories["mobile"]
    residential = categories["residential"]

    if hosting:
        base_risk = max(_category_vote_risk(item, 55) for item in hosting)
        risk_floor = 62 if residential else 52
        risk = max(risk_floor, min(100, base_risk))
        if residential:
            signals.append("多源冲突: 家宽信号与 IDC/机房信号同时存在，按更保守的机房风险处理。")
        return IpClassification(
            ip_type="IDC/云机房",
            risk_score=risk,
            risk_label=_risk_label(risk),
            confidence="高" if len(hosting) >= 2 or residential else "中",
            signals=signals,
            limitations=[
                "机房分类来自第三方网络类型字段，转售、CDN 和企业出口可能造成边界模糊。",
                "当家宽与机房来源冲突时，AI 代理筛选会按风险更高的一侧处理。",
            ],
        )

    if residential:
        base_risk = max(_category_vote_risk(item, 16) for item in residential)
        conflict_votes = business + mobile
        if conflict_votes:
            conflict_label = "商宽" if business else "移动网络"
            risk = max(38, min(74, base_risk + 16))
            signals.append(f"多源冲突: 家宽信号与{conflict_label}信号同时存在，降低高质量候选置信度。")
            return IpClassification(
                ip_type=f"家宽/{conflict_label}冲突",
                risk_score=max(0, risk),
                risk_label=_risk_label(max(0, risk)),
                confidence="中",
                signals=signals,
                limitations=[
                    "家宽分类存在其他来源冲突，暂不按高质量家宽直接优先。",
                    "企业、校园、公共 Wi-Fi、CGNAT 或代理池可能造成第三方来源判断不一致。",
                ],
            )
        risk = min(74, base_risk)
        return IpClassification(
            ip_type="家庭宽带/住宅 IP",
            risk_score=max(0, risk),
            risk_label=_risk_label(max(0, risk)),
            confidence="高" if len(residential) >= 2 or len(ok_results) >= 2 else "中",
            signals=signals,
            limitations=[
                "家宽分类表示 IP 段归属更像住宅网络，不代表该 IP 从未被代理池或滥用记录使用。",
            ],
        )

    if mobile:
        risk = min(74, max(_category_vote_risk(item, 24) for item in mobile))
        return IpClassification(
            ip_type="蜂窝/移动网络",
            risk_score=max(0, risk),
            risk_label=_risk_label(max(0, risk)),
            confidence="中",
            signals=signals,
            limitations=[
                "移动网络常有 CGNAT 和多人共享出口，正常用户与代理流量的边界需要结合业务行为判断。",
            ],
        )

    if business:
        risk = min(74, max(_category_vote_risk(item, 34) for item in business))
        return IpClassification(
            ip_type="企业/商宽 IP",
            risk_score=max(0, risk),
            risk_label=_risk_label(max(0, risk)),
            confidence="中",
            signals=signals,
            limitations=[
                "企业/商宽出口可能是办公室、学校、公共 Wi-Fi 或公司网关，不一定等同个人家宽。",
            ],
        )

    return None


def _reputation_score(result: ReputationInfo, default: int) -> int:
    if result.risk_score is not None:
        return result.risk_score
    if result.fraud_score is not None:
        return result.fraud_score
    return default


def _explicit_reputation_score(result: ReputationInfo) -> Optional[int]:
    if result.risk_score is not None:
        return result.risk_score
    if result.fraud_score is not None:
        return result.fraud_score
    return None


def _category_vote_risk(result: ReputationInfo | str, default: int) -> int:
    if isinstance(result, ReputationInfo):
        risk = _reputation_score(result, default)
        if result.flags.get("shared_connection"):
            risk = max(risk, 42)
        if result.flags.get("dynamic_connection") and _network_type_category(result.network_type) == "residential":
            risk = min(risk, 24)
        return risk
    return default


def _reputation_category_votes(
    results: list[ReputationInfo],
    ping0: Optional[Ping0Quality] = None,
) -> dict[str, list[ReputationInfo | str]]:
    categories: dict[str, list[ReputationInfo | str]] = {
        "hosting": [],
        "residential": [],
        "business": [],
        "mobile": [],
    }
    for result in results:
        category = _network_type_category(result.network_type)
        if not category and result.flags.get("hosting"):
            category = "hosting"
        if category in categories:
            categories[category].append(result)

    if ping0 and ping0.has_paid_quality:
        if ping0.isidc is True:
            categories["hosting"].append("Ping0 isidc=True")
        elif ping0.isidc is False:
            categories["residential"].append("Ping0 isidc=False")
    return categories


def _category_vote_summary(categories: dict[str, list[ReputationInfo | str]]) -> str:
    labels = {
        "residential": "家宽",
        "business": "商宽",
        "mobile": "移动",
        "hosting": "机房",
    }
    parts = []
    for key in ("residential", "business", "mobile", "hosting"):
        votes = categories.get(key) or []
        if votes:
            parts.append(f"{labels[key]}{len(votes)}")
    return "、".join(parts)


def _reputation_signals(results: list[ReputationInfo]) -> list[str]:
    signals: list[str] = []
    for result in results:
        if result.signals:
            signals.extend(result.signals)
        else:
            signals.append(result.summary_text())
    return signals


def _ping0_signals(ping0: Ping0Quality) -> list[str]:
    signals = ["Ping0 指定 IP 接口已返回质量字段"]
    if ping0.isidc is not None:
        signals.append(f"isidc={ping0.isidc}")
    if ping0.isnative is not None:
        signals.append(f"isnative={ping0.isnative}")
    if ping0.asntype or ping0.orgtype:
        signals.append(f"asntype={ping0.asntype or '-'} orgtype={ping0.orgtype or '-'}")
    return signals


def _key_pool(keys: Optional[list[str] | tuple[str, ...] | str], legacy_key: str = "") -> list[str]:
    values = parse_api_keys(keys)
    values.extend(parse_api_keys(legacy_key))
    return list(dict.fromkeys(values))


def _enabled_services(
    enabled_services: Optional[list[str] | set[str] | tuple[str, ...]],
    *,
    ping0_keys: list[str],
    proxycheck_keys: list[str],
    ipqs_keys: list[str],
    vpnapi_keys: list[str],
) -> set[str]:
    if enabled_services is not None:
        return set(normalize_services(enabled_services))

    services = {SERVICE_PING0, SERVICE_PROXYCHECK}
    if ipqs_keys:
        services.add(SERVICE_IPQS)
    if vpnapi_keys:
        services.add(SERVICE_VPNAPI)
    return services


def _disabled_ping0_quality(ip: str) -> Ping0Quality:
    return Ping0Quality(
        ip=ip,
        ok=False,
        source="disabled",
        detail_url=PING0_DETAIL_URL.format(ip=urllib.parse.quote(ip, safe=":.")),
        ping_url=PING0_PING_URL.format(ip=urllib.parse.quote(ip, safe=":.")),
        error="用户未启用 Ping0",
    )


def _key_label(index: int) -> str:
    return f"Key #{index + 1}"


def _extract_ip_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("ip", "query", "address", "public_ip", "ip_address"):
            candidate = _extract_ip_value(value.get(key))
            if candidate:
                return candidate
        for key in ("data", "result"):
            nested = value.get(key)
            candidate = _extract_ip_value(nested)
            if candidate:
                return candidate
        return ""
    if isinstance(value, list):
        for item in value:
            candidate = _extract_ip_value(item)
            if candidate:
                return candidate
        return ""
    text = str(value or "").strip().strip('"\'`[](){}')
    if _valid_ip(text):
        return text
    for token in _split_ip_candidates(text):
        cleaned = token.strip().strip('"\'`[](){}<>,;')
        if _valid_ip(cleaned):
            return cleaned
    return ""


def _split_ip_candidates(text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        candidate = value.strip().strip('"\'`[](){}<>,;')
        if candidate and candidate not in seen:
            seen.add(candidate)
            values.append(candidate)

    for match in IPV4_CANDIDATE_RE.finditer(text or ""):
        add(match.group(0))

    for token in re.split(r"[\s,;，；、|]+", text.replace("\r", "\n")):
        cleaned = token.strip()
        if not cleaned:
            continue
        add(cleaned)
        if "=" in cleaned:
            add(cleaned.rsplit("=", 1)[-1])
        label_match = re.match(r"^[^\d:=：]{1,32}[:：](.+)$", cleaned)
        if label_match and not _valid_ip(cleaned):
            add(label_match.group(1))
    return values


def re_split_ip_candidates(text: str) -> list[str]:
    return _split_ip_candidates(text)


def _payload_dict(data: dict[str, Any], *nested_keys: str) -> dict[str, Any]:
    for key in nested_keys:
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return data


def _payload_is_error(data: dict[str, Any]) -> bool:
    if _optional_boolish(data.get("success")) is False:
        return True
    status = str(data.get("status") or "").strip().lower()
    if status in {"denied", "error", "fail", "failed", "invalid"}:
        return True
    code = _optional_int(data.get("code"))
    if code is not None and code not in {0, 200}:
        return True
    return bool(data.get("error") and _optional_boolish(data.get("success")) is not True)


def _payload_error_text(data: dict[str, Any], fallback: str) -> str:
    for key in ("message", "error", "reason", "detail", "status"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    lowered = {str(key).lower(): value for key, value in data.items()}
    for key in keys:
        value = lowered.get(str(key).lower())
        if value not in (None, ""):
            return value
    return None


def _first_text(data: dict[str, Any], *keys: str) -> str:
    value = _first_value(data, *keys)
    return str(value).strip() if value not in (None, "") else ""


def _proxycheck_network_type(record_type: str, network: dict[str, Any]) -> str:
    network_type = _first_text(network, "type", "connection_type", "network_type")
    if network_type and _network_type_category(network_type):
        return network_type
    if record_type and _network_type_category(record_type):
        return record_type
    return ""


def _proxycheck_anonymous_type(value: str) -> bool:
    return str(value or "").strip().lower() in {"anonymous", "proxy", "vpn", "tor", "relay", "socks"}


def summarize_report(report: NetworkDiagnosticReport) -> str:
    if not report.diagnostics:
        failed = sum(1 for probe in report.probes if not probe.ok)
        return f"未检测到可用公网出口；{failed} 个公开端点请求失败。"

    stack = "IPv4/IPv6 双栈" if report.has_ipv4 and report.has_ipv6 else ("仅 IPv6" if report.has_ipv6 else "仅 IPv4")
    fastest = min(
        report.diagnostics,
        key=lambda diag: diag.probe.response_time if diag.probe.response_time is not None else float("inf"),
    )
    highest = max(report.diagnostics, key=lambda diag: diag.classification.risk_score)
    fastest_time = f"{fastest.probe.response_time:.2f}s" if fastest.probe.response_time is not None else "-"
    return (
        f"检测到 {stack}；最快可连通出口 {fastest.ip} "
        f"({fastest_time})；最高风险为 "
        f"{highest.classification.risk_score}%（{highest.classification.risk_label}，{highest.ip}）。"
    )


def _http_get(url: str, timeout: float) -> HttpResult:
    start = time.perf_counter()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status_code = response.getcode()
            text = raw.decode("utf-8", errors="replace")
            return HttpResult(
                url=url,
                ok=200 <= int(status_code or 0) < 300,
                text=text,
                status_code=status_code,
                response_time=time.perf_counter() - start,
                error="" if 200 <= int(status_code or 0) < 300 else f"HTTP {status_code}",
            )
    except urllib.error.HTTPError as exc:
        return HttpResult(
            url=url,
            ok=False,
            status_code=exc.code,
            response_time=time.perf_counter() - start,
            error=f"HTTP {exc.code}",
        )
    except Exception as exc:
        return HttpResult(
            url=url,
            ok=False,
            response_time=time.perf_counter() - start,
            error=str(exc),
        )


def _reverse_dns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def _risk_label(score: int) -> str:
    if score <= 15:
        return "极低"
    if score <= 30:
        return "较低"
    if score <= 50:
        return "中性"
    if score <= 70:
        return "偏高"
    return "高风险"


def _network_type_label(value: str) -> str:
    category = _network_type_category(value)
    if category == "residential":
        return "家庭宽带/住宅"
    if category == "business":
        return "企业/商宽"
    if category == "mobile":
        return "蜂窝/移动网络"
    if category == "hosting":
        return "IDC/云机房"
    return value or "-"


def _network_type_category(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if normalized in {
        "residential",
        "consumer",
        "home",
        "home broadband",
        "fixed broadband",
        "consumer broadband",
        "broadband",
        "cable",
        "dsl",
        "dialup",
        "isp",
        "fixed line",
        "fixed line isp",
    }:
        return "residential"
    if normalized in {"business", "corporate", "commercial", "commercial isp", "education", "enterprise", "organization", "school", "government"}:
        return "business"
    if normalized in {"wireless", "cellular", "mobile", "mobile isp", "carrier grade nat", "cg nat", "cgnat"}:
        return "mobile"
    if normalized in {
        "hosting",
        "data center",
        "datacenter",
        "data centre",
        "cloud",
        "cloud provider",
        "cdn",
        "server",
        "vps",
        "dedicated",
        "colo",
        "colocation",
        "hosting provider",
    }:
        return "hosting"
    return ""


def _has_anonymity_flag(flags: dict[str, bool]) -> bool:
    return any(
        flags.get(key, False)
        for key in (
            "anonymous",
            "proxy",
            "vpn",
            "tor",
            "relay",
            "active_vpn",
            "active_tor",
            "scraper",
            "compromised",
            "residential_proxy",
            "recent_abuse",
            "frequent_abuser",
            "high_risk_attacks",
            "bot_status",
        )
    )


def _active_flag_names(flags: dict[str, bool]) -> list[str]:
    labels = {
        "anonymous": "Anonymous",
        "proxy": "Proxy",
        "vpn": "VPN",
        "tor": "Tor",
        "relay": "Relay",
        "hosting": "Hosting",
        "scraper": "Scraper",
        "compromised": "Compromised",
        "residential_proxy": "Residential Proxy",
        "active_vpn": "Active VPN",
        "active_tor": "Active Tor",
        "recent_abuse": "Recent Abuse",
        "frequent_abuser": "Frequent Abuser",
        "high_risk_attacks": "High Risk Attacks",
        "bot_status": "Bot",
        "shared_connection": "Shared",
        "dynamic_connection": "Dynamic",
        "mobile": "Mobile",
    }
    return [labels.get(key, key) for key, value in flags.items() if value]


def _extract_bool_flags(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, bool]:
    return {key: parsed for key in keys if (parsed := _optional_boolish(data.get(key))) is not None}


def _extract_proxycheck_record(data: dict[str, Any], ip: str) -> Optional[dict[str, Any]]:
    if isinstance(data.get(ip), dict):
        return data[ip]
    if isinstance(data.get("result"), dict):
        result = data["result"]
        if isinstance(result.get(ip), dict):
            return result[ip]
        return result
    for key, value in data.items():
        if key in {"status", "message", "query time", "node", "warning"}:
            continue
        if isinstance(value, dict) and _valid_ip(str(key)):
            return value
    if "network" in data or "detections" in data:
        return data
    return None


def _optional_boolish(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _normalize_asn(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.upper().startswith("AS") else f"AS{text}"


def _optional_bool(value: Any) -> Optional[bool]:
    return _optional_boolish(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _is_ip_version(value: str, version: int) -> bool:
    try:
        return ipaddress.ip_address(value).version == version
    except ValueError:
        return False


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
