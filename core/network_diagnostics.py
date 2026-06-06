"""Public-network diagnostics built from free, replaceable data sources."""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional


USER_AGENT = "API-Switcher-Network-Diagnostics/1.0"
DEFAULT_TIMEOUT = 8.0

PUBLIC_IP_ENDPOINTS = (
    ("IPv4", "https://api.ipify.org?format=json"),
    ("IPv6", "https://api6.ipify.org?format=json"),
    ("默认出口", "https://api64.ipify.org?format=json"),
)


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
class IpDiagnostic:
    """Complete diagnostic for one observed public IP."""

    label: str
    ip: str
    probe: EndpointProbe
    geo: GeoInfo
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
    http_get: Optional[HttpGetter] = None,
    reverse_resolver: Optional[ReverseResolver] = None,
) -> NetworkDiagnosticReport:
    """Detect public network exits and enrich them with free public data."""

    http_get = http_get or _http_get
    reverse_resolver = reverse_resolver or _reverse_dns
    probes: list[EndpointProbe] = []
    diagnostics: list[IpDiagnostic] = []
    seen_ips: set[str] = set()

    for label, url in PUBLIC_IP_ENDPOINTS:
        probe = probe_public_ip(label, url, timeout, http_get)
        probes.append(probe)
        if not probe.ok or not probe.ip or probe.ip in seen_ips:
            continue

        seen_ips.add(probe.ip)
        geo = lookup_geo(probe.ip, timeout, http_get)
        rdns = reverse_resolver(probe.ip)
        classification = classify_ip(geo, rdns)
        diagnostics.append(
            IpDiagnostic(
                label=label,
                ip=probe.ip,
                probe=probe,
                geo=geo,
                reverse_dns=rdns,
                classification=classification,
            )
        )

    report = NetworkDiagnosticReport(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        probes=probes,
        diagnostics=diagnostics,
        notices=[
            "当前版本使用公开数据源和关键词启发式，不包含私有历史风控库。",
            "IP 类型、风险分仅供排查网络环境使用，不能等同于平台真实风控结论。",
        ],
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
        data = {"ip": result.text.strip()}

    ip = str(data.get("ip") or "").strip() if isinstance(data, dict) else ""
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
    if data.get("success") is False:
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
        security={key: bool(value) for key, value in security.items() if isinstance(value, bool)},
    )


def classify_ip(geo: GeoInfo, reverse_dns: str = "") -> IpClassification:
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
            "没有 IP 段人工标注，无法可靠区分真实家宽、商宽和机房转售段。",
            "没有攻击/垃圾邮件/爆破历史，风险分只是当前公开信息的启发式结果。",
            "没有注册地与广播路径历史，暂不判断原生 IP 或广播 IP。",
        ],
    )


def summarize_report(report: NetworkDiagnosticReport) -> str:
    if not report.diagnostics:
        failed = sum(1 for probe in report.probes if not probe.ok)
        return f"未检测到可用公网出口；{failed} 个公开端点请求失败。"

    stack = "IPv4/IPv6 双栈" if report.has_ipv4 and report.has_ipv6 else ("仅 IPv6" if report.has_ipv6 else "仅 IPv4")
    highest = max(report.diagnostics, key=lambda diag: diag.classification.risk_score)
    return (
        f"检测到 {stack}；最高启发式风险为 "
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
