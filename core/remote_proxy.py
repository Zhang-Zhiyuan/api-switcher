from __future__ import annotations

import base64
import binascii
import copy
from email.utils import parsedate_to_datetime
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import shlex
import socket
import threading
import time
import uuid
import zlib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib import parse as urlparse
from urllib import request as urlrequest

import yaml

from config.paths import STORAGE_DIR
from core.lazy_imports import LazyAttribute, LazyModule


network_diagnostic_settings = LazyModule("core.network_diagnostic_settings")
network_diagnostics = LazyModule("core.network_diagnostics")
profile_manager = LazyModule("core.profile_manager")
remote_config = LazyModule("core.remote_config")
ssh_manager = LazyAttribute("core.ssh_manager", "ssh_manager")


AI_PROXY_DOMAINS = (
    "chatgpt.com",
    "openai.com",
    "oaistatic.com",
    "oaiusercontent.com",
    "auth0.openai.com",
    "anthropic.com",
    "claude.ai",
    "gemini.google.com",
    "generativelanguage.googleapis.com",
    "oauth2.googleapis.com",
    "www.googleapis.com",
    "aiplatform.googleapis.com",
    "cloudcode-pa.googleapis.com",
    "aistudio.google.com",
    "ai.google.dev",
    "makersuite.google.com",
)

REMOTE_AI_PROBE_TARGETS = (
    ("OpenAI/ChatGPT", "https://chatgpt.com/cdn-cgi/trace"),
    ("Claude/Anthropic", "https://api.anthropic.com/"),
    ("Gemini/Google AI", "https://generativelanguage.googleapis.com/"),
)
REMOTE_AI_STABILITY_TARGETS = (
    ("OpenAI API", "https://api.openai.com/v1/models"),
    ("ChatGPT 出口", "https://chatgpt.com/cdn-cgi/trace"),
    ("Claude/Anthropic", "https://api.anthropic.com/v1/models"),
    ("Gemini/Google AI", "https://generativelanguage.googleapis.com/v1beta/models"),
)
REMOTE_AI_STABILITY_ROUNDS = 3
REMOTE_CODEX_COMPACT_PROBE_LABEL = "Codex compact 路径"
REMOTE_CODEX_COMPACT_PROBE_URL = "https://api.openai.com/v1/responses/compact"
REMOTE_CODEX_COMPACT_PROBE_PAYLOAD_BYTES = 128 * 1024
REMOTE_AI_STABILITY_EXPECTED_PROBES = (
    REMOTE_AI_STABILITY_ROUNDS * len(REMOTE_AI_STABILITY_TARGETS) + 1
)
_ISOLATED_CANDIDATE_CONFIG_PORT = 17897
REMOTE_AI_PROXY_STARTUP_GRACE_SECONDS = 20


class _NoBypassProxyHandler(urlrequest.ProxyHandler):
    """ProxyHandler variant that deliberately ignores NO_PROXY/proxy_bypass."""

    def proxy_open(self, req, proxy, type):  # noqa: A002 - urllib handler API
        original_selector = req.selector
        proxy_type, user, password, hostport = urlrequest._parse_proxy(proxy)
        if proxy_type is None:
            proxy_type = type
        if user and password:
            user_pass = urlrequest.unquote(user) + ":" + urlrequest.unquote(password)
            creds = base64.b64encode(user_pass.encode()).decode("ascii")
            req.add_header("Proxy-authorization", "Basic " + creds)
        hostport = urlrequest.unquote(hostport)
        req.set_proxy(hostport, proxy_type)
        if req.type == type:
            return None
        # Match urllib's redirect-to-new-scheme behavior without its
        # proxy_bypass(req.host) branch.
        req.type = proxy_type
        req.selector = original_selector
        return self.parent.open(req, timeout=req.timeout)

AI_PROXY_CONFIG_MARKER = "# Managed by API切换器 AI proxy"
AI_PROXY_STRICT_PRIVACY_MARKER = "# API-Switcher-Strict-Privacy: application-layer"
AI_PROXY_INTERNAL_NODE_NAME = "API-SWITCHER-NODE"
AI_PROXY_FALLBACK_NODE_PREFIX = "API-SWITCHER-FALLBACK-"
AI_PROXY_FALLBACK_MAX_NODES = 5
# Use the unauthenticated OpenAI API response as the live-route authority.  A
# generic 200 page can stay reachable while the actual Codex/OpenAI path is
# blocked.  Mihomo accepts multiple expected statuses separated by ``/``;
# 401 is the normal response without a key, while 200 keeps compatibility with
# gateways that expose a public model catalogue.
AI_PROXY_HEALTH_CHECK_URL = "https://api.openai.com/v1/models"
AI_PROXY_HEALTH_CHECK_EXPECTED_STATUS = "200/401"
AI_PROXY_HEALTH_CHECK_INTERVAL_SECONDS = 10
AI_PROXY_HEALTH_CHECK_TIMEOUT_MS = 5000
AI_PROXY_HEALTH_CHECK_MAX_FAILURES = 1
AI_PROXY_DISPLAY_NAME_MARKER = "# API-Switcher-Node-Name-B64:"
PRIVATE_DIRECT_IP_RULES = (
    "IP-CIDR,0.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",
    "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,169.254.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
    "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,224.0.0.0/4,DIRECT,no-resolve",
    "IP-CIDR6,::1/128,DIRECT,no-resolve",
    "IP-CIDR6,fc00::/7,DIRECT,no-resolve",
    "IP-CIDR6,fe80::/10,DIRECT,no-resolve",
)

PROXY_ENV_KEYS = (
    "API_SWITCHER_AI_PROXY_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)

VSCODE_SERVER_ENV_SETUP_PATHS = (
    "~/.vscode-server/server-env-setup",
    "~/.vscode-server-insiders/server-env-setup",
    "~/.cursor-server/server-env-setup",
)

VSCODE_ENV_BLOCK_START = "# >>> API切换器 AI proxy VS Code >>>"
VSCODE_ENV_BLOCK_END = "# <<< API切换器 AI proxy VS Code <<<"
_PROXY_SUBSCRIPTION_STATE_LOCK = threading.RLock()
_PROXY_SUBSCRIPTION_HOT_UPDATE_LOCK = threading.Lock()
_PROXY_SUBSCRIPTION_STATE_CACHE: dict | None = None
_PROXY_SUBSCRIPTION_STATE_CACHE_SIGNATURE: tuple[str, int | None, int | None] | None = None
_PROXY_SUBSCRIPTION_NODES_CACHE: tuple[ProxySubscriptionNode, ...] | None = None
_PROXY_SUBSCRIPTION_NODES_CACHE_SIGNATURE: tuple[str, int | None, int | None, str] | None = None
PROXY_QUALITY_CACHE_TTL_SECONDS = 6 * 60 * 60
PROXY_QUALITY_CACHE_SCHEMA_VERSION = 6
PROXY_QUALITY_HTTP_CONCURRENCY_BUDGET = 12
PROXY_LATENCY_DEFAULT_MAX_WORKERS = 32
PROXY_LATENCY_MAX_WORKERS = 64
PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER = "server_entry"
PROXY_LATENCY_CACHE_TTL_SECONDS = 30 * 60
PROXY_QUALITY_KEY_REQUIRED_SERVICES = frozenset({
    network_diagnostic_settings.SERVICE_PING0,
    network_diagnostic_settings.SERVICE_IPQS,
    network_diagnostic_settings.SERVICE_VPNAPI,
})
_PROXY_SUBSCRIPTION_PROFILE_FIELDS = {
    "url",
    "source_path",
    "saved_path",
    "last_fetched_at",
    "node_count",
    "content_type",
    "charset",
    "selected_node_key",
    "selected_node_display",
    "node_latencies",
    "node_latencies_updated_at",
    "node_qualities",
    "node_qualities_updated_at",
}

PROXY_SUBSCRIPTION_MAX_BYTES = 5 * 1024 * 1024
PROXY_SUBSCRIPTION_USER_AGENTS = (
    "clash.meta",
    "mihomo",
    "ClashforWindows/0.20.39",
    "ClashVergeRev",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) API-Switcher/1.0",
)

# These are mihomo routing primitives, not usable subscription transports. If
# accepted as a proxy-node ``type``, a crafted document could make a candidate
# probe or a nominally strict config use bypass semantics.
PROXY_NODE_FORBIDDEN_OUTBOUND_TYPES = frozenset(
    {"direct", "pass", "reject", "reject-drop", "dns"}
)


def _strict_privacy_dns_config(proxy_route: str = "AI-PROXY") -> dict:
    """Return strict DNS bound to the exact managed/isolated proxy route."""

    route = str(proxy_route or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", route):
        raise ValueError("严格 DNS 代理路由名称无效")

    return {
        "enable": True,
        "ipv6": False,
        "enhanced-mode": "redir-host",
        "use-hosts": True,
        "use-system-hosts": False,
        "respect-rules": True,
        # These IP literals bootstrap only the resolver hostnames below.  The
        # mainland-reachable pair avoids making first connection depend on a
        # direct route to an overseas resolver.
        "default-nameserver": ["223.5.5.5", "119.29.29.29"],
        # Proxy-node hostnames must be resolved before the proxy exists.  Keep
        # that narrowly scoped lookup encrypted but explicitly DIRECT to avoid
        # a chicken-and-egg loop.  Destination DNS remains on AI-PROXY below.
        "proxy-server-nameserver": [
            "https://doh.pub/dns-query#DIRECT",
            "https://dns.alidns.com/dns-query#DIRECT",
        ],
        "nameserver": [
            f"https://1.1.1.1/dns-query#{route}",
            f"https://8.8.8.8/dns-query#{route}",
        ],
    }


def _mainland_compatible_dns_config(proxy_domains) -> dict:
    """Resolve node hosts locally while sending selected AI DNS via the proxy."""

    direct_doh = [
        "https://doh.pub/dns-query#DIRECT",
        "https://dns.alidns.com/dns-query#DIRECT",
    ]
    proxied_doh = [
        "https://1.1.1.1/dns-query#AI-PROXY",
        "https://8.8.8.8/dns-query#AI-PROXY",
    ]
    policy = {}
    for value in proxy_domains or ():
        domain = str(value or "").strip().strip(".").casefold()
        if domain.startswith("*."):
            domain = domain[2:]
        if domain.startswith("+."):
            domain = domain[2:]
        if domain:
            policy[f"+.{domain}"] = list(proxied_doh)
    return {
        "enable": True,
        "ipv6": True,
        "enhanced-mode": "redir-host",
        "use-hosts": True,
        "use-system-hosts": True,
        "respect-rules": False,
        "default-nameserver": ["223.5.5.5", "119.29.29.29"],
        "proxy-server-nameserver": list(direct_doh),
        "direct-nameserver": list(direct_doh),
        "direct-nameserver-follow-policy": False,
        "nameserver": list(direct_doh),
        "nameserver-policy": policy,
    }


PROXY_SUBSCRIPTION_PERMANENT_HTTP_ERRORS = frozenset({400, 401, 404, 405, 410, 414, 422})
PROXY_SUBSCRIPTION_RETRYABLE_HTTP_ERRORS = frozenset(
    {
        403,
        406,
        408,
        423,
        425,
        429,
        451,
        500,
        502,
        503,
        504,
        520,
        521,
        522,
        523,
        524,
        525,
        526,
        530,
    }
)

SUBSCRIPTION_METADATA_NODE_NAME_PATTERNS = (
    r"剩余流量",
    r"已用流量",
    r"流量.*(重置|到期|剩余|用尽|不足)",
    r"距离.*重置",
    r"下次重置",
    r"套餐.*到期",
    r"(到期|过期)时间",
    r"官网",
    r"防失联",
    r"发布页",
    r"订阅(信息|地址|链接)?",
    r"联通移动用",
    r"电信移动用",
    r"\b(traffic|remaining|reset|expire|expiry|subscription|official|website)\b",
)

PROXY_REGION_RULES = (
    (
        "香港",
        (
            r"香港",
            r"(?<![a-z0-9])h[.\s_-]*k(?:[.\s_-]*g)?(?:[\s_-]*\d+)?(?![a-z0-9])",
            r"hong[.\s_-]*kong",
            r"🇭🇰",
        ),
    ),
    ("台湾", (r"台湾", r"台灣", r"\btw\b", r"taiwan", r"hinet", r"🇹🇼")),
    ("日本", (r"日本", r"\bjp\b", r"japan", r"tokyo", r"osaka", r"东京", r"大阪", r"🇯🇵")),
    ("新加坡", (r"新加坡", r"\bsg\b", r"singapore", r"🇸🇬")),
    ("美国", (r"美国", r"美國", r"\bus\b", r"\busa\b", r"united\s*states", r"america", r"los\s*angeles", r"san\s*jose", r"🇺🇸")),
    ("韩国", (r"韩国", r"韓國", r"\bkr\b", r"korea", r"seoul", r"🇰🇷")),
    ("英国", (r"英国", r"英國", r"\buk\b", r"\bgb\b", r"united\s*kingdom", r"london", r"🇬🇧")),
    ("德国", (r"德国", r"德國", r"\bde\b", r"germany", r"frankfurt", r"🇩🇪")),
    ("法国", (r"法国", r"法國", r"\bfr\b", r"france", r"paris", r"🇫🇷")),
    ("荷兰", (r"荷兰", r"荷蘭", r"\bnl\b", r"netherlands", r"amsterdam", r"🇳🇱")),
    ("加拿大", (r"加拿大", r"\bca\b", r"canada", r"toronto", r"vancouver", r"🇨🇦")),
    ("澳大利亚", (r"澳大利亚", r"澳洲", r"\bau\b", r"australia", r"sydney", r"🇦🇺")),
    ("越南", (r"越南", r"\bvn\b", r"vietnam", r"🇻🇳")),
    ("泰国", (r"泰国", r"泰國", r"\bth\b", r"thailand", r"bangkok", r"🇹🇭")),
    ("马来西亚", (r"马来西亚", r"馬來西亞", r"\bmy\b", r"malaysia", r"kuala\s*lumpur", r"🇲🇾")),
    ("菲律宾", (r"菲律宾", r"菲律賓", r"\bph\b", r"philippines", r"manila", r"🇵🇭")),
    ("印度", (r"印度", r"\bin\b", r"india", r"mumbai", r"delhi", r"🇮🇳")),
    ("俄罗斯", (r"俄罗斯", r"俄羅斯", r"\bru\b", r"russia", r"moscow", r"🇷🇺")),
    ("土耳其", (r"土耳其", r"\btr\b", r"turkey", r"istanbul", r"🇹🇷")),
    ("巴西", (r"巴西", r"\bbr\b", r"brazil", r"sao\s*paulo", r"🇧🇷")),
)

PROXY_REGION_TLD_MAP = {
    "hk": "香港",
    "tw": "台湾",
    "jp": "日本",
    "sg": "新加坡",
    "us": "美国",
    "kr": "韩国",
    "uk": "英国",
    "gb": "英国",
    "de": "德国",
    "fr": "法国",
    "nl": "荷兰",
    "ca": "加拿大",
    "au": "澳大利亚",
    "vn": "越南",
    "th": "泰国",
    "my": "马来西亚",
    "ph": "菲律宾",
    "in": "印度",
    "ru": "俄罗斯",
    "tr": "土耳其",
    "br": "巴西",
}

PROXY_REGION_ORDER = tuple(region for region, _patterns in PROXY_REGION_RULES) + ("其他",)
PROXY_AUTO_SELECTION_BLOCKED_REGIONS = frozenset({"香港"})
PROXY_REGION_MATCHERS = tuple(
    (region, tuple(re.compile(pattern, flags=re.I) for pattern in patterns))
    for region, patterns in PROXY_REGION_RULES
)


@dataclass(frozen=True)
class RemoteAIProxyStatus:
    installed: bool
    running: bool
    config_path: str
    proxy_url: str
    detail: str = ""
    environment_ready: bool = True
    start_script_ready: bool = True
    shell_entrypoints_ready: bool = True
    vscode_entrypoints_ready: bool = True

    @property
    def integrations_ready(self) -> bool:
        """Whether new shells and VS Code Remote can inherit this proxy."""

        return bool(
            self.environment_ready
            and self.start_script_ready
            and self.shell_entrypoints_ready
            and self.vscode_entrypoints_ready
        )

    def summary(self) -> str:
        state = "运行中" if self.running else "未运行"
        installed = "已配置" if self.installed else "未配置"
        detail = f"；{self.detail}" if self.detail else ""
        return f"AI 代理{installed}，{state}: {self.proxy_url}{detail}"


class RemoteMihomoCoreMissingError(RuntimeError):
    """The remote candidate probe cannot start because no compatible core exists."""


@dataclass(frozen=True)
class RemoteAIProxyProbeResult:
    label: str
    ok: bool
    detail: str = ""
    elapsed_ms: int = 0

    def summary(self) -> str:
        prefix = "可达" if self.ok else "失败"
        elapsed = f"{self.elapsed_ms}ms" if self.elapsed_ms else ""
        pieces = [piece for piece in (prefix, self.detail, elapsed) if piece]
        return f"{self.label}: {' / '.join(pieces)}"


@dataclass(frozen=True)
class ProxySubscriptionNode:
    index: int
    node: dict
    source: str = ""
    node_key: str = field(default="", compare=False, repr=False)
    region: str = field(default="", compare=False, repr=False)

    def display_name(self) -> str:
        return f"{self.index}. {describe_proxy_node(self.node)}"


@dataclass(frozen=True)
class ProxyNodeLatencyResult:
    node_key: str
    ok: bool
    latency_ms: int | None = None
    detail: str = ""
    attempts: int = 0
    measured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def label(self) -> str:
        if self.ok and self.latency_ms is not None:
            return f"{self.latency_ms}ms"
        return "不可连"


@dataclass(frozen=True)
class ProxyNodeQualityResult:
    node_key: str
    ok: bool
    host: str = ""
    ip: str = ""
    region: str = ""
    ip_type: str = ""
    risk_score: int | None = None
    risk_label: str = ""
    quality_score: int = 0
    quality_label: str = ""
    confidence: str = ""
    detail: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sources: tuple[str, ...] = ()
    attempted_sources: tuple[str, ...] = ()
    coverage_complete: bool = False
    assessment_scope: str = PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER
    classification_basis: str = ""
    quality_signature: str = ""
    cached: bool = False

    def label(self) -> str:
        if not self.ok:
            return "质量未测"
        pieces = [self.quality_label or self.ip_type or "质量已测"]
        if self.risk_score is not None:
            pieces.append(f"风险{self.risk_score}%")
        if self.quality_score:
            pieces.append(f"评分{self.quality_score}")
        if self.confidence:
            pieces.append(f"置信{self.confidence}")
        return " ".join(pieces)


@dataclass(frozen=True)
class ProxyEnvironmentDiagnostic:
    """Result of checking environment proxies used by a subscription request."""

    invalid_variables: tuple[str, ...] = ()
    invalid_proxy_urls: tuple[str, ...] = ()
    invalid_windows_proxy: bool = False
    reconciled_warning: str = ""

    @property
    def has_invalid_proxy(self) -> bool:
        return bool(self.invalid_variables or self.invalid_windows_proxy)

    def warning(self, *, bypassed: bool = True) -> str:
        if self.reconciled_warning:
            return self.reconciled_warning
        if not self.has_invalid_proxy:
            return ""
        proxies = ", ".join(self.invalid_proxy_urls)
        if self.invalid_variables:
            source = f"环境变量 {', '.join(self.invalid_variables)}"
        else:
            source = "Windows 当前用户系统代理"
        action = (
            "本次订阅请求已临时绕过该代理"
            if bypassed
            else "当前严格隐私模式未绕过该代理"
        )
        untouched = (
            "未修改系统环境变量"
            if self.invalid_variables
            else "未修改 Windows 系统代理设置"
        )
        return (
            f"检测到无效本机代理配置（{source}={proxies}，回环端口无法连接）；"
            f"{action}，{untouched}"
        )


@dataclass(frozen=True)
class ProxySubscriptionResult:
    nodes: tuple[ProxySubscriptionNode, ...]
    saved_path: str
    url: str = ""
    last_fetched_at: str = ""
    proxy_warning: str = ""


@dataclass
class _ProxySubscriptionDownloadTrace:
    """Internal, credential-free transport feedback for one subscription fetch."""

    recovery_proxy_attempted: bool = False
    recovery_proxy_used: bool = False
    recovery_proxy_unavailable: bool = False
    recovery_routes_attempted: int = 0
    recovery_signatures_attempted: int = 0

    def warning(self) -> str:
        if not self.recovery_proxy_used:
            return ""
        route_detail = (
            f"（尝试 {self.recovery_routes_attempted} 个已有节点）"
            if self.recovery_routes_attempted > 1
            else ""
        )
        return (
            f"常规下载不可用时，已通过现有受管节点的隔离代理完成更新{route_detail}；"
            "一次性代理已退出，未改动当前节点或系统代理/环境变量"
        )

    def failure_suffix(self) -> str:
        if self.recovery_proxy_attempted:
            detail = (
                f"（尝试 {self.recovery_routes_attempted} 个节点、"
                f"{self.recovery_signatures_attempted} 次兼容请求）"
                if self.recovery_routes_attempted or self.recovery_signatures_attempted
                else ""
            )
            return f"；已尝试现有受管节点的隔离代理兜底{detail}"
        if self.recovery_proxy_unavailable:
            return "；现有受管节点的隔离代理兜底不可用"
        return ""


class _ProxySubscriptionPayloadError(ValueError):
    """A successful HTTP response that is not a usable subscription document."""


@dataclass(frozen=True)
class _ProxySubscriptionRecoverySession:
    proxy_map: dict[str, str]
    route_count: int = 1
    route_selector: Callable[[int, float], object] | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def select_route(self, index: int, timeout_seconds: float) -> None:
        if index == 0 and self.route_selector is None:
            return
        if self.route_selector is None:
            raise RuntimeError("订阅兜底代理不支持切换到下一个节点")
        self.route_selector(int(index), max(0.001, float(timeout_seconds)))


def parse_proxy_node(text: str) -> dict:
    """Parse a Clash proxy node from an inline YAML/JSON-ish snippet."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("请先粘贴 Clash 代理节点")

    if not re.search(r"(?m)^[ \t]*proxies\s*:", raw):
        uri_nodes = _parse_proxy_uri_lines(raw)
        if uri_nodes:
            return uri_nodes[0]

    candidate = _extract_first_proxy_entry(raw) or raw
    if candidate.startswith("-"):
        candidate = candidate[1:].strip()
    inline_candidate = _extract_first_inline_map(candidate)
    if inline_candidate:
        candidate = inline_candidate

    yaml_node = _parse_yaml_proxy_node(candidate)
    if yaml_node:
        return yaml_node

    if candidate.startswith("{") and candidate.endswith("}"):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = _parse_inline_map(candidate[1:-1])
    else:
        parsed = _parse_block_map(candidate)

    if not isinstance(parsed, dict):
        raise ValueError("代理节点格式不正确")
    parsed = _normalize_proxy_node(parsed)
    return parsed


def parse_proxy_subscription_content(text: str) -> tuple[ProxySubscriptionNode, ...]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("订阅内容为空")

    nodes: list[dict] = []
    for variant in _subscription_text_variants(raw):
        nodes.extend(_parse_yaml_proxy_nodes(variant))
        nodes.extend(_parse_custom_proxy_nodes(variant))
        nodes.extend(_parse_proxy_uri_lines(variant))

    if not nodes:
        try:
            nodes.append(parse_proxy_node(raw))
        except Exception as exc:
            raise ValueError("订阅内容里没有识别到可用的 Clash/mihomo 节点") from exc

    unique_nodes = _dedupe_proxy_nodes(nodes)
    if not unique_nodes:
        raise ValueError("订阅内容里没有识别到可用的 Clash/mihomo 节点")
    return tuple(
        ProxySubscriptionNode(
            index=index,
            node=node,
            source="subscription",
            node_key=proxy_node_key(node),
            region=proxy_node_region(node),
        )
        for index, node in enumerate(unique_nodes, 1)
    )


def fetch_proxy_subscription(
    url: str,
    timeout: int = 45,
    max_bytes: int = PROXY_SUBSCRIPTION_MAX_BYTES,
    persist: bool = True,
    retries: int = len(PROXY_SUBSCRIPTION_USER_AGENTS),
    retry_base_delay: float = 1.0,
    profile_id: str = "",
    activate: bool = True,
    allow_direct_fallback: bool = True,
    recovery_proxy_provider: Callable[[float], object] | None = None,
) -> ProxySubscriptionResult:
    parsed_url = urlparse.urlparse((url or "").strip())
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("订阅链接必须是 http 或 https 地址")
    normalized_url = urlparse.urlunparse(parsed_url)
    proxy_diagnostic = _subscription_proxy_environment_diagnostic(normalized_url)
    proxy_diagnostic = _reconcile_subscription_proxy_environment(
        normalized_url,
        proxy_diagnostic,
        allow_direct_fallback=allow_direct_fallback,
    )

    request = urlrequest.Request(
        normalized_url,
        headers={
            # A number of subscription providers return a Clash document only
            # when the client identifies itself as a compatible core.
            "User-Agent": PROXY_SUBSCRIPTION_USER_AGENTS[0],
            "Accept": "text/plain, application/yaml, application/json, */*",
            "Accept-Encoding": "gzip, deflate",
            "Clash-Version": "1.18.0",
        },
    )

    download_trace = _ProxySubscriptionDownloadTrace()
    payload, content_type, charset = _download_proxy_subscription(
        request=request,
        timeout=timeout,
        max_bytes=max_bytes,
        retries=retries,
        retry_base_delay=retry_base_delay,
        allow_direct_fallback=allow_direct_fallback,
        proxy_diagnostic=proxy_diagnostic,
        recovery_proxy_provider=recovery_proxy_provider,
        download_trace=download_trace,
    )
    text = _decode_subscription_bytes(payload, charset)
    nodes = parse_proxy_subscription_content(text)
    saved_path = (
        _proxy_subscription_cache_path(normalized_url, payload, content_type)
        if persist
        else None
    )
    fetched_at = _now_iso()
    if persist:
        with _PROXY_SUBSCRIPTION_STATE_LOCK:
            state = _normalize_proxy_subscription_state(load_proxy_subscription_state())
            profiles = state.setdefault("profiles", {})
            clean_id = str(profile_id or "").strip()
            if clean_id:
                target_id = clean_id
            else:
                target_id = _proxy_subscription_profile_id(normalized_url)
                for candidate_id, candidate in profiles.items():
                    if (
                        isinstance(candidate, dict)
                        and str(candidate.get("url") or "").strip() == normalized_url
                    ):
                        target_id = str(candidate_id)
                        break
            profile = dict(profiles.get(target_id) or {})
            profile.update(
                {
                    "id": target_id,
                    "name": str(profile.get("name") or _proxy_subscription_default_name(normalized_url))[:80],
                    "url": normalized_url,
                    "saved_path": str(saved_path),
                    "last_fetched_at": fetched_at,
                    "node_count": len(nodes),
                    "content_type": content_type,
                    "charset": charset,
                    "updated_at": fetched_at,
                }
            )
            profiles[target_id] = _normalize_proxy_subscription_profile(profile, target_id)
            if activate:
                state["active_profile_id"] = target_id
            _commit_proxy_subscription_cache_and_state(
                saved_path,
                payload,
                _sync_active_profile_to_state(state),
            )
    return ProxySubscriptionResult(
        nodes=nodes,
        saved_path=str(saved_path) if saved_path is not None else "",
        url=normalized_url,
        last_fetched_at=fetched_at,
        proxy_warning="；".join(
            item
            for item in (
                proxy_diagnostic.warning(bypassed=allow_direct_fallback),
                download_trace.warning(),
            )
            if item
        ),
    )


def _read_local_proxy_file_payload(
    path: str | Path,
    max_bytes: int,
) -> tuple[Path, bytes]:
    """Resolve and read a local proxy document with a strict byte limit."""

    source_path = Path(path).expanduser()
    try:
        source_path = source_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"本地 Clash 配置文件不存在或无法访问: {exc}") from exc
    if not source_path.is_file():
        raise ValueError("请选择本地 Clash/mihomo YAML 配置文件")

    limit = _normalize_subscription_max_bytes(max_bytes)
    try:
        with source_path.open("rb") as source:
            payload = source.read(limit + 1)
    except OSError as exc:
        raise ValueError(f"读取本地 Clash 配置失败: {exc}") from exc
    if len(payload) > limit:
        raise ValueError(f"本地 Clash 配置超过 {_subscription_size_limit_label(limit)}，已停止读取")
    return source_path, payload


def read_proxy_node_text_file(
    path: str | Path,
    *,
    max_bytes: int = PROXY_SUBSCRIPTION_MAX_BYTES,
) -> str:
    """Read a manually selected proxy node/config file without unbounded I/O."""

    _source_path, payload = _read_local_proxy_file_payload(path, max_bytes)
    return _decode_subscription_bytes(payload, "")


def import_proxy_subscription_file(
    path: str | Path,
    *,
    max_bytes: int = PROXY_SUBSCRIPTION_MAX_BYTES,
    persist: bool = True,
    profile_id: str = "",
    activate: bool = True,
) -> ProxySubscriptionResult:
    """Import all nodes from a local Clash/mihomo YAML file.

    The source is copied into the managed cache so the imported nodes remain
    available if the original file is later moved or removed.
    """

    source_path, payload = _read_local_proxy_file_payload(path, max_bytes)

    text = _decode_subscription_bytes(payload, "")
    nodes = parse_proxy_subscription_content(text)
    imported_at = _now_iso()
    saved_path = (
        _proxy_subscription_cache_path(
            f"local:{source_path}",
            payload,
            "application/yaml",
        )
        if persist
        else None
    )
    if persist:
        _save_local_proxy_subscription_profile(
            source_path,
            saved_path,
            node_count=len(nodes),
            imported_at=imported_at,
            profile_id=profile_id,
            activate=activate,
            payload=payload,
        )
    return ProxySubscriptionResult(
        nodes=nodes,
        saved_path=str(saved_path) if saved_path is not None else "",
        last_fetched_at=imported_at,
    )


def _save_local_proxy_subscription_profile(
    source_path: Path,
    saved_path: Path,
    *,
    node_count: int,
    imported_at: str,
    profile_id: str = "",
    activate: bool = True,
    payload: bytes,
) -> dict:
    source_text = str(source_path)
    source_key = source_text.casefold() if source_path.drive else source_text
    clean_id = str(profile_id or "").strip()
    if not clean_id:
        clean_id = "file-" + hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:16]

    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        state = _normalize_proxy_subscription_state(load_proxy_subscription_state())
        profiles = state.setdefault("profiles", {})
        existing_id = clean_id
        if existing_id not in profiles:
            for candidate_id, candidate in profiles.items():
                if (
                    isinstance(candidate, dict)
                    and str(candidate.get("source_path") or "").strip() == source_text
                ):
                    existing_id = str(candidate_id)
                    break
        profile = dict(profiles.get(existing_id) or {})
        profile.update(
            {
                "id": existing_id,
                "name": str(profile.get("name") or source_path.stem or "本地 Clash 配置")[:80],
                "url": "",
                "source_path": source_text,
                "saved_path": str(saved_path),
                "last_fetched_at": imported_at,
                "node_count": max(0, int(node_count)),
                "content_type": "application/yaml",
                "charset": "auto",
                "updated_at": imported_at,
            }
        )
        profiles[existing_id] = _normalize_proxy_subscription_profile(profile, existing_id)
        if activate:
            state["active_profile_id"] = existing_id
        normalized_state = _sync_active_profile_to_state(state)
        _commit_proxy_subscription_cache_and_state(saved_path, payload, normalized_state)
        return copy.deepcopy(_normalize_proxy_subscription_state(normalized_state))


def save_proxy_subscription_profile_state(profile_id: str, **updates) -> dict:
    clean_id = str(profile_id or "").strip()
    if not clean_id:
        raise ValueError("订阅配置不存在")
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        state = _normalize_proxy_subscription_state(load_proxy_subscription_state())
        profiles = state.setdefault("profiles", {})
        profile = dict(profiles.get(clean_id) or {})
        if not profile:
            updated_url = str(updates.get("url") or "").strip()
            if not updated_url:
                raise ValueError("订阅配置不存在")
            profile = {
                "id": clean_id,
                "name": _proxy_subscription_default_name(updated_url),
                "url": updated_url,
            }
        for key, value in updates.items():
            if value is None:
                continue
            if key in _PROXY_SUBSCRIPTION_PROFILE_FIELDS:
                profile[key] = value
        profile["updated_at"] = _now_iso()
        profiles[clean_id] = _normalize_proxy_subscription_profile(profile, clean_id)
        return _persist_proxy_subscription_state(_sync_active_profile_to_state(state))


def load_cached_proxy_subscription(state: dict | None = None) -> ProxySubscriptionResult | None:
    state = _normalize_proxy_subscription_state(state) if state is not None else load_proxy_subscription_state()
    saved_path = str(state.get("saved_path") or "").strip()
    if not saved_path:
        return None
    path = Path(saved_path)
    charset = str(state.get("charset") or "utf-8-sig")
    try:
        signature = _proxy_subscription_nodes_signature(path, charset)
        nodes = _load_cached_proxy_subscription_nodes(path, charset, signature)
    except FileNotFoundError:
        return None
    except ValueError:
        return None
    return ProxySubscriptionResult(
        nodes=nodes,
        saved_path=str(path),
        url=str(state.get("url") or ""),
        last_fetched_at=str(state.get("last_fetched_at") or ""),
    )


def list_proxy_subscription_profiles() -> list[dict]:
    state = load_proxy_subscription_state()
    active_id = str(state.get("active_profile_id") or "")
    profiles = state.get("profiles") if isinstance(state.get("profiles"), dict) else {}
    results = []
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        item = _normalize_proxy_subscription_profile(profile, str(profile_id))
        item["active"] = item.get("id") == active_id
        results.append(item)
    return sorted(results, key=lambda item: (0 if item.get("active") else 1, str(item.get("name") or "").casefold()))


def active_proxy_subscription_profile() -> dict:
    state = load_proxy_subscription_state()
    profile = _active_proxy_subscription_profile(state)
    return copy.deepcopy(profile) if profile else {}


def save_proxy_subscription_profile(
    name: str,
    url: str,
    *,
    profile_id: str = "",
    activate: bool = True,
    quiet: bool = False,
) -> dict:
    parsed_url = urlparse.urlparse((url or "").strip())
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("订阅链接必须是 http 或 https 地址")
    normalized_url = urlparse.urlunparse(parsed_url)
    clean_id = str(profile_id or "").strip()
    if not clean_id:
        clean_id = _proxy_subscription_profile_id(normalized_url)
    requested_name = str(name or "").strip()

    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        state = _normalize_proxy_subscription_state(load_proxy_subscription_state())
        profiles = state.setdefault("profiles", {})
        existing_id = clean_id
        if existing_id not in profiles:
            for candidate_id, candidate in profiles.items():
                if isinstance(candidate, dict) and str(candidate.get("url") or "").strip() == normalized_url:
                    existing_id = str(candidate_id)
                    break
        profile = dict(profiles.get(existing_id) or {})
        clean_name = requested_name or str(profile.get("name") or "").strip() or _proxy_subscription_default_name(normalized_url)
        profile.update(
            {
                "id": existing_id,
                "name": clean_name[:80],
                "url": normalized_url,
                "updated_at": _now_iso(),
            }
        )
        profiles[existing_id] = _normalize_proxy_subscription_profile(profile, existing_id)
        if activate:
            state["active_profile_id"] = existing_id
        _persist_proxy_subscription_state(_sync_active_profile_to_state(state))
        if not quiet:
            clear_proxy_subscription_state_cache()
        return copy.deepcopy(profiles[existing_id])


def set_active_proxy_subscription_profile(profile_id: str) -> dict:
    clean_id = str(profile_id or "").strip()
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        state = _normalize_proxy_subscription_state(load_proxy_subscription_state())
        profiles = state.get("profiles") if isinstance(state.get("profiles"), dict) else {}
        if clean_id not in profiles:
            raise ValueError("订阅配置不存在")
        state["active_profile_id"] = clean_id
        _persist_proxy_subscription_state(_sync_active_profile_to_state(state))
        return copy.deepcopy(_active_proxy_subscription_profile(state) or {})


def rename_proxy_subscription_profile(profile_id: str, name: str) -> dict:
    clean_id = str(profile_id or "").strip()
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("请填写订阅名称")
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        state = _normalize_proxy_subscription_state(load_proxy_subscription_state())
        profiles = state.get("profiles") if isinstance(state.get("profiles"), dict) else {}
        profile = dict(profiles.get(clean_id) or {})
        if not profile:
            raise ValueError("订阅配置不存在")
        profile["name"] = clean_name[:80]
        profile["updated_at"] = _now_iso()
        profiles[clean_id] = _normalize_proxy_subscription_profile(profile, clean_id)
        _persist_proxy_subscription_state(_sync_active_profile_to_state(state))
        return copy.deepcopy(profiles[clean_id])


def delete_proxy_subscription_profile(profile_id: str) -> dict:
    clean_id = str(profile_id or "").strip()
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        state = _normalize_proxy_subscription_state(load_proxy_subscription_state())
        profiles = state.get("profiles") if isinstance(state.get("profiles"), dict) else {}
        if clean_id not in profiles:
            raise ValueError("订阅配置不存在")
        profiles.pop(clean_id, None)
        if state.get("active_profile_id") == clean_id:
            state["active_profile_id"] = next(iter(profiles), "")
        if not profiles:
            state["active_profile_id"] = ""
            for key in _PROXY_SUBSCRIPTION_PROFILE_FIELDS:
                state.pop(key, None)
        persisted_state = _persist_proxy_subscription_state(
            _sync_active_profile_to_state(state)
        )
        _prune_unreferenced_proxy_subscription_caches(persisted_state)
        return copy.deepcopy(_active_proxy_subscription_profile(state) or {})


def load_proxy_subscription_state() -> dict:
    path = _proxy_subscription_state_path()
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        signature = _proxy_subscription_state_signature(path)
        if (
            _PROXY_SUBSCRIPTION_STATE_CACHE is not None
            and _PROXY_SUBSCRIPTION_STATE_CACHE_SIGNATURE == signature
        ):
            return copy.deepcopy(_PROXY_SUBSCRIPTION_STATE_CACHE)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _cache_proxy_subscription_state({}, signature)
            return {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            _quarantine_corrupt_proxy_subscription_state(path)
            _cache_proxy_subscription_state({}, _proxy_subscription_state_signature(path))
            return {}
        state = _normalize_proxy_subscription_state(data if isinstance(data, dict) else {})
        _cache_proxy_subscription_state(state, signature)
        return copy.deepcopy(state)


def try_acquire_proxy_subscription_hot_update() -> bool:
    """Reserve the single subscription hot-update flight shared by both proxy tabs."""

    return _PROXY_SUBSCRIPTION_HOT_UPDATE_LOCK.acquire(blocking=False)


def release_proxy_subscription_hot_update() -> None:
    """Release a reservation obtained by ``try_acquire_proxy_subscription_hot_update``."""

    _PROXY_SUBSCRIPTION_HOT_UPDATE_LOCK.release()


def save_proxy_subscription_state(**updates) -> dict:
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        state = _normalize_proxy_subscription_state(load_proxy_subscription_state())
        active_id = str(updates.get("active_profile_id") or state.get("active_profile_id") or "")
        updated_url = str(updates.get("url") or "").strip()
        if not active_id and updated_url:
            active_id = _proxy_subscription_profile_id(updated_url)
        if active_id:
            state["active_profile_id"] = active_id
        profiles = state.setdefault("profiles", {})
        active_profile = _active_proxy_subscription_profile(state)
        if active_profile is None and active_id:
            active_profile = {
                "id": active_id,
                "name": _proxy_subscription_default_name(updated_url) if updated_url else "默认订阅",
            }
            if updated_url:
                active_profile["url"] = updated_url
            profiles[active_id] = active_profile
        for key, value in updates.items():
            if value is None:
                continue
            if key in _PROXY_SUBSCRIPTION_PROFILE_FIELDS and active_profile is not None:
                active_profile[key] = value
            state[key] = value
        if active_profile is not None:
            profile_id = str(active_profile.get("id") or state.get("active_profile_id") or "")
            profiles[profile_id] = _normalize_proxy_subscription_profile(active_profile, profile_id)
        return _persist_proxy_subscription_state(_sync_active_profile_to_state(state))


def _persist_proxy_subscription_state(state: dict) -> dict:
    directory = _proxy_subscription_dir()
    directory.mkdir(parents=True, exist_ok=True)
    state = _normalize_proxy_subscription_state(state)
    state["updated_at"] = _now_iso()
    path = _proxy_subscription_state_path()
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        _replace_proxy_subscription_state_file(temp_path, path)
        _cache_proxy_subscription_state(state)
    finally:
        temp_path.unlink(missing_ok=True)
    return copy.deepcopy(state)


def _normalize_proxy_subscription_state(state: dict) -> dict:
    normalized = copy.deepcopy(state) if isinstance(state, dict) else {}
    raw_profiles = normalized.get("profiles") if isinstance(normalized.get("profiles"), dict) else {}
    profiles: dict[str, dict] = {}
    for profile_id, profile in raw_profiles.items():
        if not isinstance(profile, dict):
            continue
        item = _normalize_proxy_subscription_profile(profile, str(profile_id))
        profiles[item["id"]] = item

    legacy_url = str(normalized.get("url") or "").strip()
    active_id = str(normalized.get("active_profile_id") or "").strip()
    should_migrate_legacy = bool(legacy_url) and (not profiles or not active_id or active_id not in profiles)
    if should_migrate_legacy:
        legacy_id = _proxy_subscription_profile_id(legacy_url)
        profile = dict(profiles.get(active_id) or profiles.get(legacy_id) or {})
        for key in _PROXY_SUBSCRIPTION_PROFILE_FIELDS:
            if key in normalized and normalized.get(key) is not None:
                profile[key] = normalized.get(key)
        profile.setdefault("name", _proxy_subscription_default_name(legacy_url))
        profile["url"] = legacy_url
        profile = _normalize_proxy_subscription_profile(profile, active_id or legacy_id)
        profiles[profile["id"]] = profile
        active_id = profile["id"]
    elif not active_id and profiles:
        active_id = next(iter(profiles))
    elif active_id and active_id not in profiles and profiles:
        active_id = next(iter(profiles))

    normalized["profiles"] = profiles
    normalized["active_profile_id"] = active_id
    return _sync_active_profile_to_state(normalized)


def _normalize_proxy_subscription_profile(profile: dict, fallback_id: str = "") -> dict:
    item = copy.deepcopy(profile) if isinstance(profile, dict) else {}
    url = str(item.get("url") or "").strip()
    profile_id = str(item.get("id") or fallback_id or "").strip()
    if not profile_id:
        profile_id = _proxy_subscription_profile_id(url) if url else uuid.uuid4().hex[:12]
    item["id"] = profile_id[:64]
    item["name"] = str(item.get("name") or _proxy_subscription_default_name(url) or "默认订阅").strip()[:80]
    item["url"] = url
    item["source_path"] = str(item.get("source_path") or "").strip()
    item["saved_path"] = str(item.get("saved_path") or "").strip()
    item["last_fetched_at"] = str(item.get("last_fetched_at") or "").strip()
    item["content_type"] = str(item.get("content_type") or "").strip()
    item["charset"] = str(item.get("charset") or "utf-8-sig").strip() or "utf-8-sig"
    item["selected_node_key"] = str(item.get("selected_node_key") or "").strip()
    item["selected_node_display"] = str(item.get("selected_node_display") or "").strip()[:180]
    item["node_count"] = max(0, _int_or_default(item.get("node_count"), 0))
    item["node_latencies"] = item.get("node_latencies") if isinstance(item.get("node_latencies"), dict) else {}
    item["node_qualities"] = item.get("node_qualities") if isinstance(item.get("node_qualities"), dict) else {}
    return item


def _sync_active_profile_to_state(state: dict) -> dict:
    normalized = copy.deepcopy(state) if isinstance(state, dict) else {}
    profiles = normalized.get("profiles") if isinstance(normalized.get("profiles"), dict) else {}
    active_id = str(normalized.get("active_profile_id") or "").strip()
    active = profiles.get(active_id) if active_id else None
    if isinstance(active, dict):
        active = _normalize_proxy_subscription_profile(active, active_id)
        profiles[active["id"]] = active
        normalized["active_profile_id"] = active["id"]
        # Top-level fields are a compatibility mirror of the active profile.
        # Clear the previous mirror first so optional timestamps from another
        # profile cannot survive an active-profile switch.
        for key in _PROXY_SUBSCRIPTION_PROFILE_FIELDS:
            normalized.pop(key, None)
        for key in _PROXY_SUBSCRIPTION_PROFILE_FIELDS:
            if key in active:
                normalized[key] = copy.deepcopy(active[key])
    elif not profiles:
        normalized["active_profile_id"] = ""
    normalized["profiles"] = profiles
    return normalized


def _active_proxy_subscription_profile(state: dict) -> dict | None:
    profiles = state.get("profiles") if isinstance(state.get("profiles"), dict) else {}
    active_id = str(state.get("active_profile_id") or "").strip()
    profile = profiles.get(active_id) if active_id else None
    return profile if isinstance(profile, dict) else None


def _proxy_subscription_profile_id(url: str) -> str:
    normalized = str(url or "").strip()
    return "sub-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _proxy_subscription_default_name(url: str) -> str:
    parsed = urlparse.urlparse(str(url or "").strip())
    host = parsed.netloc.split("@")[-1].split(":")[0] if parsed.netloc else ""
    return host or "默认订阅"


def clear_proxy_subscription_state_cache() -> None:
    global _PROXY_SUBSCRIPTION_STATE_CACHE, _PROXY_SUBSCRIPTION_STATE_CACHE_SIGNATURE
    global _PROXY_SUBSCRIPTION_NODES_CACHE, _PROXY_SUBSCRIPTION_NODES_CACHE_SIGNATURE
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        _PROXY_SUBSCRIPTION_STATE_CACHE = None
        _PROXY_SUBSCRIPTION_STATE_CACHE_SIGNATURE = None
        _PROXY_SUBSCRIPTION_NODES_CACHE = None
        _PROXY_SUBSCRIPTION_NODES_CACHE_SIGNATURE = None


def _proxy_subscription_state_signature(path: Path | None = None) -> tuple[str, int | None, int | None]:
    state_path = path or _proxy_subscription_state_path()
    path_key = str(state_path.resolve(strict=False))
    try:
        stat = state_path.stat()
        return (path_key, int(stat.st_mtime_ns), int(stat.st_size))
    except FileNotFoundError:
        return (path_key, None, None)


def _cache_proxy_subscription_state(
    state: dict,
    signature: tuple[str, int | None, int | None] | None = None,
) -> None:
    global _PROXY_SUBSCRIPTION_STATE_CACHE, _PROXY_SUBSCRIPTION_STATE_CACHE_SIGNATURE
    _PROXY_SUBSCRIPTION_STATE_CACHE = copy.deepcopy(state)
    _PROXY_SUBSCRIPTION_STATE_CACHE_SIGNATURE = signature or _proxy_subscription_state_signature()


def _replace_proxy_subscription_state_file(temp_path: Path, target_path: Path) -> None:
    for attempt in range(6):
        try:
            temp_path.replace(target_path)
            return
        except PermissionError:
            if attempt >= 5:
                raise
            time.sleep(0.03 * (attempt + 1))


def _quarantine_corrupt_proxy_subscription_state(path: Path) -> Path | None:
    try:
        if not path.exists():
            return None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        target = path.with_name(f"{path.name}.corrupt-{timestamp}-{uuid.uuid4().hex[:8]}")
        path.replace(target)
        return target
    except OSError:
        return None


def _proxy_subscription_nodes_signature(path: Path, charset: str) -> tuple[str, int | None, int | None, str]:
    path_key = str(path.resolve(strict=False))
    charset_key = str(charset or "utf-8-sig")
    try:
        stat = path.stat()
        return (path_key, int(stat.st_mtime_ns), int(stat.st_size), charset_key)
    except FileNotFoundError:
        return (path_key, None, None, charset_key)


def _load_cached_proxy_subscription_nodes(
    path: Path,
    charset: str,
    signature: tuple[str, int | None, int | None, str],
) -> tuple[ProxySubscriptionNode, ...]:
    global _PROXY_SUBSCRIPTION_NODES_CACHE, _PROXY_SUBSCRIPTION_NODES_CACHE_SIGNATURE
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        if (
            _PROXY_SUBSCRIPTION_NODES_CACHE is not None
            and _PROXY_SUBSCRIPTION_NODES_CACHE_SIGNATURE == signature
        ):
            return _PROXY_SUBSCRIPTION_NODES_CACHE
    # State files are user-editable and may point outside the managed cache.
    # Reject ordinary oversized files before reading, then verify again to
    # cover a file that grew between stat and read. Keep raw OSError semantics
    # here: callers intentionally distinguish a missing cache from permission
    # or sharing failures instead of silently treating every I/O error as an
    # invalid subscription.
    limit = _normalize_subscription_max_bytes(PROXY_SUBSCRIPTION_MAX_BYTES)
    if path.stat().st_size > limit:
        raise ValueError(
            f"缓存 Clash 配置超过 {_subscription_size_limit_label(limit)}，已停止读取"
        )
    payload = path.read_bytes()
    if len(payload) > limit:
        raise ValueError(
            f"缓存 Clash 配置超过 {_subscription_size_limit_label(limit)}，已停止读取"
        )
    text = _decode_subscription_bytes(payload, charset)
    nodes = parse_proxy_subscription_content(text)
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        _PROXY_SUBSCRIPTION_NODES_CACHE = nodes
        _PROXY_SUBSCRIPTION_NODES_CACHE_SIGNATURE = signature
    return nodes


def proxy_subscription_auto_refresh_enabled(scope: str = "") -> bool:
    state = load_proxy_subscription_state()
    scoped_key = _proxy_subscription_auto_refresh_key(scope)
    if scoped_key and scoped_key in state:
        return bool(state.get(scoped_key))
    return bool(state.get("auto_refresh"))


def set_proxy_subscription_auto_refresh(enabled: bool, scope: str = "") -> dict:
    scoped_key = _proxy_subscription_auto_refresh_key(scope)
    if scoped_key:
        return save_proxy_subscription_state(**{scoped_key: bool(enabled)})
    return save_proxy_subscription_state(auto_refresh=bool(enabled))


def set_proxy_subscription_selected_node(node: dict | None, *, profile_id: str = "") -> dict:
    if not node:
        updates = {"selected_node_key": "", "selected_node_display": ""}
    else:
        normalized = _normalize_proxy_node(node)
        updates = {
            "selected_node_key": proxy_node_key(normalized),
            "selected_node_display": describe_proxy_node(normalized),
        }
    if profile_id:
        return save_proxy_subscription_profile_state(profile_id, **updates)
    return save_proxy_subscription_state(**updates)


def save_proxy_subscription_latencies(
    latencies: dict[str, ProxyNodeLatencyResult | dict],
    *,
    profile_id: str = "",
) -> dict:
    measured_at = _now_iso()
    payload = {}
    for key, result in (latencies or {}).items():
        node_key = str(key or "").strip()
        if not node_key:
            continue
        payload[node_key] = {
            "ok": proxy_node_latency_ok(result),
            "latency_ms": proxy_node_latency_ms(result),
            "detail": proxy_node_latency_detail(result),
            "attempts": proxy_node_latency_attempts(result),
            "measured_at": proxy_node_latency_measured_at(result) or measured_at,
        }
    updates = {
        "node_latencies": payload,
        "node_latencies_updated_at": measured_at,
    }
    if profile_id:
        return save_proxy_subscription_profile_state(profile_id, **updates)
    return save_proxy_subscription_state(**updates)


def save_proxy_subscription_qualities(qualities: dict[str, ProxyNodeQualityResult | dict]) -> dict:
    payload = _proxy_subscription_quality_payload(qualities)
    return save_proxy_subscription_state(node_qualities=payload, node_qualities_updated_at=_now_iso())


def merge_proxy_subscription_qualities(
    qualities: dict[str, ProxyNodeQualityResult | dict],
    *,
    profile_id: str = "",
) -> dict:
    """Atomically merge a quality batch into the profile captured when it started."""

    payload = _proxy_subscription_quality_payload(qualities)
    measured_at = _now_iso()
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        state = _normalize_proxy_subscription_state(load_proxy_subscription_state())
        target_id = str(profile_id or state.get("active_profile_id") or "").strip()
        profiles = state.setdefault("profiles", {})
        profile = profiles.get(target_id) if target_id else None
        if not isinstance(profile, dict):
            if profile_id:
                raise ValueError("检测期间订阅分组已被删除，结果未写入其他分组")
            return save_proxy_subscription_state(
                node_qualities=payload,
                node_qualities_updated_at=measured_at,
            )
        merged = dict(profile.get("node_qualities") or {})
        merged.update(payload)
        profile["node_qualities"] = merged
        profile["node_qualities_updated_at"] = measured_at
        profile["updated_at"] = measured_at
        profiles[target_id] = _normalize_proxy_subscription_profile(profile, target_id)
        state["profiles"] = profiles
        return _persist_proxy_subscription_state(_sync_active_profile_to_state(state))


def merge_proxy_quality_refresh_results(
    existing: dict[str, ProxyNodeQualityResult | dict] | None,
    refreshed: dict[str, ProxyNodeQualityResult | dict] | None,
) -> tuple[dict[str, ProxyNodeQualityResult | dict], dict[str, ProxyNodeQualityResult | dict]]:
    """Merge a refresh without replacing equivalent complete evidence with a partial retry."""

    merged = dict(existing or {})
    persisted: dict[str, ProxyNodeQualityResult | dict] = {}
    for key, result in (refreshed or {}).items():
        if not proxy_node_quality_measured(result) or proxy_node_quality_cancelled(result):
            continue
        previous = merged.get(key)
        same_signature = bool(
            proxy_node_quality_signature(result)
            and proxy_node_quality_signature(result) == proxy_node_quality_signature(previous)
        )
        preserve_previous = bool(
            same_signature
            and proxy_node_quality_coverage_complete(previous)
            and _quality_checked_at_fresh(
                proxy_node_quality_checked_at(previous),
                PROXY_QUALITY_CACHE_TTL_SECONDS,
            )
            and not proxy_node_quality_coverage_complete(result)
        )
        if preserve_previous:
            continue
        merged[key] = result
        persisted[key] = result
    return merged, persisted


def _proxy_subscription_quality_payload(
    qualities: dict[str, ProxyNodeQualityResult | dict],
) -> dict[str, dict]:
    payload = {}
    for key, result in (qualities or {}).items():
        node_key = str(key or "").strip()
        if not node_key:
            continue
        payload[node_key] = {
            "ok": proxy_node_quality_measured(result),
            "host": proxy_node_quality_host(result),
            "ip": proxy_node_quality_ip(result),
            "region": proxy_node_quality_region(result),
            "ip_type": proxy_node_quality_ip_type(result),
            "risk_score": proxy_node_quality_risk_score(result),
            "risk_label": proxy_node_quality_risk_label(result),
            "quality_score": proxy_node_quality_score(result),
            "quality_label": proxy_node_quality_label(result),
            "confidence": proxy_node_quality_confidence(result),
            "detail": proxy_node_quality_detail(result),
            "checked_at": proxy_node_quality_checked_at(result) or _now_iso(),
            "sources": list(proxy_node_quality_sources(result)),
            "attempted_sources": list(proxy_node_quality_attempted_sources(result)),
            "coverage_complete": proxy_node_quality_coverage_complete(result),
            "assessment_scope": proxy_node_quality_assessment_scope(result),
            "classification_basis": proxy_node_quality_classification_basis(result),
            "quality_signature": proxy_node_quality_signature(result),
            "cached": proxy_node_quality_cached(result),
        }
    return payload


def load_proxy_subscription_latencies(state: dict | None = None) -> dict[str, dict]:
    state = _normalize_proxy_subscription_state(state) if state is not None else load_proxy_subscription_state()
    raw = state.get("node_latencies")
    if not isinstance(raw, dict):
        return {}
    results = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        node_key = str(key or "").strip()
        if not node_key:
            continue
        latency_ms = value.get("latency_ms")
        try:
            latency_value = int(latency_ms) if latency_ms is not None else None
        except (TypeError, ValueError):
            latency_value = None
        results[node_key] = {
            "ok": bool(value.get("ok") and latency_value is not None),
            "latency_ms": latency_value,
            "detail": str(value.get("detail") or "")[:160],
            "attempts": _int_or_default(value.get("attempts"), 0),
            "measured_at": str(value.get("measured_at") or ""),
        }
    return results


def load_proxy_subscription_qualities(state: dict | None = None) -> dict[str, dict]:
    state = _normalize_proxy_subscription_state(state) if state is not None else load_proxy_subscription_state()
    raw = state.get("node_qualities")
    if not isinstance(raw, dict):
        return {}
    results = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        node_key = str(key or "").strip()
        if not node_key:
            continue
        results[node_key] = {
            "ok": bool(value.get("ok")),
            "host": str(value.get("host") or "")[:180],
            "ip": str(value.get("ip") or "")[:80],
            "region": str(value.get("region") or "")[:40],
            "ip_type": str(value.get("ip_type") or "")[:80],
            "risk_score": _optional_int(value.get("risk_score")),
            "risk_label": str(value.get("risk_label") or "")[:40],
            "quality_score": max(0, min(100, _int_or_default(value.get("quality_score"), 0))),
            "quality_label": str(value.get("quality_label") or "")[:60],
            "confidence": str(value.get("confidence") or "")[:20],
            "detail": str(value.get("detail") or "")[:220],
            "checked_at": str(value.get("checked_at") or "")[:40],
            "sources": list(network_diagnostic_settings.normalize_services(value.get("sources") or [])),
            "attempted_sources": list(
                network_diagnostic_settings.normalize_services(value.get("attempted_sources") or [])
            ),
            "coverage_complete": bool(value.get("coverage_complete")),
            "assessment_scope": str(
                value.get("assessment_scope") or PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER
            )[:40],
            "classification_basis": str(value.get("classification_basis") or "")[:40],
            "quality_signature": str(value.get("quality_signature") or "")[:96],
            "cached": bool(value.get("cached")),
        }
    return results


def proxy_node_key(node: dict) -> str:
    normalized = _normalize_proxy_node(node)
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def proxy_subscription_node_key(item: ProxySubscriptionNode) -> str:
    cached = str(getattr(item, "node_key", "") or "")
    return cached or proxy_node_key(item.node)


def proxy_subscription_node_region(item: ProxySubscriptionNode) -> str:
    cached = str(getattr(item, "region", "") or "")
    return cached or proxy_node_region(item.node)


def proxy_subscription_node_is_hong_kong(item: ProxySubscriptionNode) -> bool:
    """Return whether a subscription node is identified as Hong Kong."""
    return proxy_subscription_node_region(item) == "香港"


def proxy_region_is_hong_kong(value: object) -> bool:
    """Recognize Hong Kong country/region labels returned by quality providers."""

    text = str(value or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in PROXY_REGION_MATCHERS[0][1])


def _proxy_region_from_quality_evidence(
    *,
    geo=None,
    ping0=None,
    reputation=None,
) -> str:
    """Normalize structured IP-location evidence, conservatively prioritizing Hong Kong."""

    location_values: list[str] = []
    country_values: list[str] = []

    def add_fields(source, *, require_ok: bool = True) -> None:
        if source is None or (require_ok and not bool(getattr(source, "ok", False))):
            return
        country_code = str(getattr(source, "country_code", "") or "").strip()
        country = str(getattr(source, "country", "") or "").strip()
        values = (
            country_code,
            country,
            str(getattr(source, "region", "") or "").strip(),
            str(getattr(source, "province", "") or "").strip(),
            str(getattr(source, "city", "") or "").strip(),
            str(getattr(source, "location", "") or "").strip(),
        )
        location_values.extend(value for value in values if value)
        country_values.extend(value for value in (country_code, country) if value)

    add_fields(geo)
    if ping0 is not None and bool(getattr(ping0, "has_paid_quality", False)):
        add_fields(ping0)
    for item in reputation or ():
        add_fields(item)

    # A single successful structured source identifying Hong Kong is sufficient to
    # block automatic selection, even if another provider reports a conflicting area.
    for region, patterns in PROXY_REGION_MATCHERS:
        if any(pattern.search(value) for value in location_values for pattern in patterns):
            return region
    return country_values[0] if country_values else ""


def proxy_subscription_node_auto_selectable(
    item: ProxySubscriptionNode,
    quality_result: ProxyNodeQualityResult | dict | None = None,
) -> bool:
    """Return whether policy permits the program to choose this node automatically."""

    return (
        isinstance(item, ProxySubscriptionNode)
        and proxy_subscription_node_region(item) not in PROXY_AUTO_SELECTION_BLOCKED_REGIONS
        and not proxy_region_is_hong_kong(proxy_node_quality_region(quality_result))
    )


def automatic_proxy_subscription_nodes(
    nodes,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
) -> tuple[ProxySubscriptionNode, ...]:
    """Return automatic-selection candidates while preserving manual-only nodes in the UI."""

    qualities = quality_results or {}
    return tuple(
        item
        for item in (nodes or [])
        if isinstance(item, ProxySubscriptionNode)
        and proxy_subscription_node_auto_selectable(
            item,
            qualities.get(proxy_subscription_node_key(item)),
        )
    )


def describe_proxy_node(node: dict) -> str:
    normalized = _normalize_proxy_node(node)
    return (
        f"{normalized['name']} "
        f"({normalized['type']}://{normalized['server']}:{normalized['port']})"
    )


def format_proxy_node(node: dict) -> str:
    return _dump_yaml(_normalize_proxy_node(node))


def proxy_node_region(node: dict) -> str:
    normalized = _normalize_proxy_node(node)
    text = f"{normalized.get('name', '')} {normalized.get('server', '')}".lower()
    for region, patterns in PROXY_REGION_MATCHERS:
        for pattern in patterns:
            if pattern.search(text):
                return region

    server = str(normalized.get("server") or "").strip().lower().rstrip(".")
    tld = server.rsplit(".", 1)[-1] if "." in server else ""
    return PROXY_REGION_TLD_MAP.get(tld, "其他")


def ping0_detail_url_for_proxy_node(node: dict) -> str:
    normalized = _normalize_proxy_node(node)
    target = str(normalized.get("server") or "").strip().strip("[]")
    if not target:
        raise ValueError("代理节点缺少服务器地址，无法打开 Ping0")
    return "https://ping0.cc/ip/" + urlparse.quote(target, safe=":.")


def sort_proxy_subscription_nodes(
    nodes,
    latency_results: dict[str, ProxyNodeLatencyResult | dict] | None = None,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
    prefer_quality: bool = False,
) -> tuple[ProxySubscriptionNode, ...]:
    items = tuple(item for item in (nodes or []) if isinstance(item, ProxySubscriptionNode))
    latencies = latency_results or {}
    qualities = quality_results or {}

    def sort_key(item: ProxySubscriptionNode):
        node_key = proxy_subscription_node_key(item)
        region = proxy_subscription_node_region(item)
        region_index = PROXY_REGION_ORDER.index(region) if region in PROXY_REGION_ORDER else len(PROXY_REGION_ORDER)
        quality_result = qualities.get(node_key)
        quality_score = proxy_node_quality_score(quality_result)
        quality_measured_sort = 0 if proxy_node_quality_measured(quality_result) else 1
        quality_sort = -quality_score if prefer_quality else 0
        ai_proxy_sort = 0 if proxy_node_quality_for_ai_proxy_ok(quality_result) else 1
        latency_result = latencies.get(node_key)
        latency_fresh = proxy_node_latency_fresh(latency_result)
        latency = proxy_node_latency_ms(latency_result) if latency_fresh else None
        if latency_fresh and proxy_node_latency_ok(latency_result):
            status_sort = 0
        elif latency_fresh:
            status_sort = 2
        else:
            status_sort = 1
        latency_sort = latency if latency is not None else 10**9
        display_name = str(item.node.get("name") or item.display_name()).lower()
        if prefer_quality:
            return (status_sort, ai_proxy_sort, quality_measured_sort, quality_sort, latency_sort, region_index, region, display_name)
        return (region_index, region, status_sort, latency_sort, display_name)

    return tuple(sorted(items, key=sort_key))


def best_proxy_subscription_node_for_ai_proxy(
    nodes,
    quality_results: dict[str, ProxyNodeQualityResult | dict],
    latency_results: dict[str, ProxyNodeLatencyResult | dict] | None = None,
) -> ProxySubscriptionNode | None:
    ranked = sort_proxy_subscription_nodes(
        automatic_proxy_subscription_nodes(nodes, quality_results),
        latency_results=latency_results,
        quality_results=quality_results,
        prefer_quality=True,
    )
    for item in ranked:
        key = proxy_subscription_node_key(item)
        latency = (latency_results or {}).get(key)
        explicitly_unreachable = proxy_node_latency_explicitly_unreachable(latency)
        quality = quality_results.get(key)
        if (
            not explicitly_unreachable
            and proxy_node_quality_fresh(quality)
            and proxy_node_quality_for_ai_proxy_ok(quality)
        ):
            return item
    return None


def best_proxy_subscription_node_by_latency(
    nodes,
    latency_results: dict[str, ProxyNodeLatencyResult | dict] | None,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
) -> ProxySubscriptionNode | None:
    """Choose the fastest reachable node allowed by automatic-selection policy."""

    best = None
    best_latency = None
    for item in automatic_proxy_subscription_nodes(nodes, quality_results):
        result = (latency_results or {}).get(proxy_subscription_node_key(item))
        latency = proxy_node_latency_ms(result)
        if latency is None or not proxy_node_latency_ok(result):
            continue
        if best is None or latency < best_latency:
            best = item
            best_latency = latency
    return best


def best_proxy_subscription_node_for_hot_update(
    nodes,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None,
    latency_results: dict[str, ProxyNodeLatencyResult | dict] | None,
) -> tuple[ProxySubscriptionNode | None, str]:
    """Choose a connected fallback without preferring a known-bad IP over unknown evidence."""

    connected = tuple(
        item
        for item in sort_proxy_subscription_nodes(
            automatic_proxy_subscription_nodes(nodes, quality_results),
            latency_results,
        )
        if proxy_node_latency_ok((latency_results or {}).get(proxy_subscription_node_key(item)))
    )
    if not connected:
        blocked_connected = any(
            not proxy_subscription_node_auto_selectable(
                item,
                (quality_results or {}).get(proxy_subscription_node_key(item)),
            )
            and proxy_node_latency_ok(
                (latency_results or {}).get(proxy_subscription_node_key(item))
            )
            for item in (nodes or [])
            if isinstance(item, ProxySubscriptionNode)
        )
        if blocked_connected:
            return None, "policy_excluded"
        return None, "unreachable"
    if not quality_results:
        return connected[0], "latency"

    qualified = best_proxy_subscription_node_for_ai_proxy(
        connected,
        quality_results,
        latency_results,
    )
    if qualified is not None:
        return qualified, "quality"

    unknown = tuple(
        item
        for item in connected
        if not proxy_node_quality_decisive_for_ai_proxy(
            quality_results.get(proxy_subscription_node_key(item))
        )
    )
    if unknown:
        return unknown[0], "unknown_quality"
    return None, "quality_rejected"


def ranked_proxy_subscription_nodes_for_ai_probe(
    nodes,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
    latency_results: dict[str, ProxyNodeLatencyResult | dict] | None = None,
) -> tuple[ProxySubscriptionNode, ...]:
    """Rank candidates for AI proxy validation, preferring high-quality IPs first."""
    return sort_proxy_subscription_nodes(
        automatic_proxy_subscription_nodes(nodes, quality_results),
        latency_results=latency_results,
        quality_results=quality_results or {},
        prefer_quality=bool(quality_results),
    )


def measure_proxy_node_latency(
    node: dict,
    timeout: float = 3.0,
    attempts: int = 2,
    *,
    require_all: bool = False,
) -> ProxyNodeLatencyResult:
    normalized = _normalize_proxy_node(node)
    node_key = proxy_node_key(normalized)
    attempts = max(1, _int_or_default(attempts, 2))
    timeout = _normalize_timeout(timeout, 3.0)
    latencies = []
    last_error = ""
    endpoint = (str(normalized["server"]), int(normalized["port"]))

    for _attempt in range(attempts):
        started = time.perf_counter()
        try:
            with socket.create_connection(endpoint, timeout=timeout):
                latencies.append(max(1, int((time.perf_counter() - started) * 1000)))
        except Exception as exc:
            last_error = str(exc).splitlines()[0][:120] or exc.__class__.__name__

    all_attempts_succeeded = len(latencies) == attempts
    if latencies and (all_attempts_succeeded or not require_all):
        if require_all:
            ordered_latencies = sorted(latencies)
            midpoint = len(ordered_latencies) // 2
            if len(ordered_latencies) % 2:
                representative_latency = ordered_latencies[midpoint]
            else:
                representative_latency = int(
                    round((ordered_latencies[midpoint - 1] + ordered_latencies[midpoint]) / 2)
                )
        else:
            representative_latency = min(latencies)
        return ProxyNodeLatencyResult(
            node_key=node_key,
            ok=True,
            latency_ms=representative_latency,
            attempts=attempts,
            measured_at=_now_iso(),
        )
    if require_all and latencies:
        detail = f"TCP 仅 {len(latencies)}/{attempts} 次成功"
        if last_error:
            detail = f"{detail}: {last_error}"
        return ProxyNodeLatencyResult(
            node_key=node_key,
            ok=False,
            latency_ms=None,
            detail=detail,
            attempts=attempts,
            measured_at=_now_iso(),
        )
    return ProxyNodeLatencyResult(
        node_key=node_key,
        ok=False,
        latency_ms=None,
        detail=last_error or "TCP 连接失败",
        attempts=attempts,
        measured_at=_now_iso(),
    )


def measure_proxy_node_latencies(
    nodes,
    timeout: float = 3.0,
    attempts: int = 2,
    max_workers: int = PROXY_LATENCY_DEFAULT_MAX_WORKERS,
    *,
    require_all: bool = False,
) -> dict[str, ProxyNodeLatencyResult]:
    items = []
    seen = set()
    for item in nodes or []:
        node = item.node if isinstance(item, ProxySubscriptionNode) else item
        if not isinstance(node, dict):
            continue
        try:
            normalized = _normalize_proxy_node(node)
            node_key = proxy_node_key(normalized)
        except Exception:
            continue
        if node_key in seen:
            continue
        items.append(normalized)
        seen.add(node_key)

    if not items:
        return {}

    worker_count = min(
        max(1, _int_or_default(max_workers, PROXY_LATENCY_DEFAULT_MAX_WORKERS)),
        PROXY_LATENCY_MAX_WORKERS,
        len(items),
    )
    results: dict[str, ProxyNodeLatencyResult] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                measure_proxy_node_latency,
                node,
                timeout,
                attempts,
                require_all=require_all,
            ): proxy_node_key(node)
            for node in items
        }
        for future in as_completed(futures):
            node_key = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = ProxyNodeLatencyResult(
                    node_key=node_key,
                    ok=False,
                    latency_ms=None,
                    detail=str(exc).splitlines()[0][:120] or type(exc).__name__,
                    attempts=max(1, _int_or_default(attempts, 2)),
                    measured_at=_now_iso(),
                )
            results[result.node_key] = result
    return results


def assess_proxy_node_quality(
    node: dict,
    timeout: float = 5.0,
    *,
    http_get=None,
    resolver=None,
    settings=None,
    enabled_services=None,
    use_cache: bool = True,
    cached_quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
    cache_ttl_seconds: int = PROXY_QUALITY_CACHE_TTL_SECONDS,
    cache_index: dict[tuple[str, str], ProxyNodeQualityResult | dict] | None = None,
    batch_flights: dict[tuple[str, str], Future] | None = None,
    batch_flights_lock=None,
    cancel_event=None,
) -> ProxyNodeQualityResult:
    normalized = _normalize_proxy_node(node)
    node_key = proxy_node_key(normalized)
    host = str(normalized.get("server") or "")
    region = proxy_node_region(normalized)
    services = []
    quality_signature = ""
    if cancel_event is not None and cancel_event.is_set():
        return _proxy_node_quality_error_result(normalized, "已取消", "用户已取消本次家宽检测")
    settings = settings if settings is not None else network_diagnostic_settings.load_settings()
    if enabled_services is not None:
        configured_services = network_diagnostic_settings.normalize_services(enabled_services)
    elif hasattr(settings, "enabled_services"):
        configured_services = settings.enabled_services()
    else:
        configured_services = []
    services = proxy_quality_effective_services(settings, configured_services)
    skipped_services = [service for service in configured_services if service not in services]
    if not services:
        skipped_label = quality_source_label_from_settings(settings, skipped_services)
        detail = (
            f"已启用的服务器 IP 检测源均不可执行（{skipped_label} 缺少 API Key）"
            if skipped_services
            else "未启用可执行的服务器 IP 质量检测源"
        )
        return _proxy_node_quality_error_result(normalized, "检测源不可用", detail)
    try:
        ip = _resolve_proxy_node_ip(normalized, resolver=resolver)
    except Exception as exc:
        return ProxyNodeQualityResult(
            node_key=node_key,
            ok=False,
            host=host,
            region=region,
            quality_label="解析失败",
            detail=str(exc).splitlines()[0][:180] or "节点服务器解析失败",
            checked_at=_now_iso(),
        )

    flight_future = None
    flight_owner = False
    try:
        quality_signature = proxy_quality_settings_signature(settings, services)
        if use_cache:
            cached = _cached_proxy_node_quality_result(
                node_key,
                host,
                region,
                ip,
                quality_signature,
                cached_quality_results,
                cache_ttl_seconds,
                cache_index=cache_index,
            )
            if cached is not None:
                return cached
        if batch_flights is not None and batch_flights_lock is not None:
            flight_key = (ip, quality_signature)
            with batch_flights_lock:
                flight_future = batch_flights.get(flight_key)
                if flight_future is None:
                    flight_future = Future()
                    batch_flights[flight_key] = flight_future
                    flight_owner = True
            if not flight_owner:
                shared_result = flight_future.result()
                return _proxy_node_quality_result_for_node(shared_result, normalized)
        service_set = set(services)
        http_get = http_get or network_diagnostics._http_get
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("用户已取消本次家宽检测")
        if network_diagnostic_settings.SERVICE_PING0 in service_set:
            ping0_keys = _diagnostic_settings_keys(settings, network_diagnostic_settings.SERVICE_PING0)
            if ping0_keys:
                ping0 = network_diagnostics.lookup_ping0_quality(
                    ip,
                    "默认出口",
                    timeout,
                    http_get,
                    api_keys=ping0_keys,
                    allow_free_fallback=False,
                )
            else:
                ping0 = network_diagnostics.Ping0Quality(
                    ip=ip,
                    source="ping0-link-only",
                    detail_url=network_diagnostics.PING0_DETAIL_URL.format(ip=urlparse.quote(ip, safe=":.")),
                    ping_url=network_diagnostics.PING0_PING_URL.format(ip=urlparse.quote(ip, safe=":.")),
                    error="服务器 IP 质量检测需要 Ping0 API Key，已跳过访客出口接口",
                )
        else:
            ping0 = network_diagnostics._disabled_ping0_quality(ip)
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("用户已取消本次家宽检测")
        reputation = network_diagnostics.lookup_reputation(
            ip,
            timeout,
            http_get,
            enabled_services=services,
            proxycheck_api_keys=_diagnostic_settings_keys(settings, network_diagnostic_settings.SERVICE_PROXYCHECK),
            ipapi_api_keys=_diagnostic_settings_keys(settings, network_diagnostic_settings.SERVICE_IPAPI),
            ipqs_api_keys=_diagnostic_settings_keys(settings, network_diagnostic_settings.SERVICE_IPQS),
            vpnapi_api_keys=_diagnostic_settings_keys(settings, network_diagnostic_settings.SERVICE_VPNAPI),
        )
        successful_sources = [
            item.source
            for item in reputation
            if network_diagnostics.reputation_has_usable_evidence(item)
        ]
        if ping0.has_paid_quality:
            successful_sources.append(network_diagnostic_settings.SERVICE_PING0)
        successful_sources = network_diagnostic_settings.normalize_services(successful_sources)
        source_coverage_complete = bool(services) and set(successful_sources) == set(services)
        has_network_classification = network_diagnostics.reputation_has_network_classification(
            reputation,
            ping0,
        )
        reputation_only_classification = network_diagnostics._classify_from_reputation(reputation)
        reputation_classification = network_diagnostics._classify_from_reputation(reputation, ping0)
        detected_region = _proxy_region_from_quality_evidence(
            ping0=ping0,
            reputation=reputation,
        )
        geo_required = (
            not has_network_classification
            and reputation_classification is None
        ) or not detected_region
        if not geo_required:
            geo = network_diagnostics.GeoInfo(ip=ip, ok=True)
        else:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("用户已取消本次家宽检测")
            geo = network_diagnostics.lookup_geo(ip, timeout, http_get)
        detected_region = _proxy_region_from_quality_evidence(
            geo=geo,
            ping0=ping0,
            reputation=reputation,
        )
        quality_region = detected_region or region
        coverage_complete = source_coverage_complete and (not geo_required or geo.ok)
        if not successful_sources and not geo.ok:
            failures = [item.error for item in reputation if item.error]
            if ping0.error and network_diagnostic_settings.SERVICE_PING0 in service_set:
                failures.append(ping0.error)
            if geo.error:
                failures.append(f"Geo/ASN: {geo.error}")
            raise RuntimeError("；".join(failures[:3]) or "所有质量证据源均未返回有效结果")
        classification = network_diagnostics.classify_ip(geo, ping0=ping0, reputation=reputation)
        if reputation_only_classification is not None:
            classification_basis = "信誉源网络/风险字段"
        elif reputation_classification is not None and ping0.has_paid_quality:
            classification_basis = "Ping0 指定 IP"
        else:
            classification_basis = "Geo/ASN 辅助"
        quality_score = _proxy_quality_score(classification)
        quality_label = _proxy_quality_label(classification, quality_score)
        if not coverage_complete and quality_label in {"家宽高质", "家宽可用"}:
            quality_label = "家宽待复核"
        detail_parts = [
            f"服务器入口 IP；出口地区{quality_region}；分类依据{classification_basis}；"
            f"置信度{classification.confidence}；"
            f"有效源 {len(successful_sources)}/{len(services)}"
        ]
        if skipped_services:
            detail_parts.append(
                "缺 Key 已跳过: "
                + quality_source_label_from_settings(settings, skipped_services)
            )
        if geo.ok and geo.owner_text() != "-":
            detail_parts.append(geo.owner_text())
        elif geo_required and geo.error:
            detail_parts.append(f"Geo/ASN: {geo.error}")
        multi_source_signals = [
            signal for signal in classification.signals if signal.startswith("多源")
        ]
        multi_source_signals.sort(key=lambda signal: ("冲突" not in signal, signal))
        detail_parts.extend(multi_source_signals)
        if network_diagnostic_settings.SERVICE_PING0 in service_set:
            if ping0.has_paid_quality:
                detail_parts.append(ping0.quality_text())
            elif ping0.error:
                detail_parts.append(f"Ping0: {ping0.error}")
        for item in reputation:
            if item.ok:
                detail_parts.append(item.summary_text())
            elif item.error:
                detail_parts.append(f"{item.source_label}: {item.error}")
        result = ProxyNodeQualityResult(
            node_key=node_key,
            ok=True,
            host=host,
            ip=ip,
            region=quality_region,
            ip_type=classification.ip_type,
            risk_score=classification.risk_score,
            risk_label=classification.risk_label,
            quality_score=quality_score,
            quality_label=quality_label,
            confidence=classification.confidence,
            detail="；".join(detail_parts[:3])[:220],
            checked_at=_now_iso(),
            sources=tuple(successful_sources),
            attempted_sources=tuple(services),
            coverage_complete=coverage_complete,
            assessment_scope=PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER,
            classification_basis=classification_basis,
            quality_signature=quality_signature,
        )
    except Exception as exc:
        cancelled = (
            isinstance(exc, InterruptedError)
            and cancel_event is not None
            and cancel_event.is_set()
        )
        result = _proxy_node_quality_error_result(
            normalized,
            "已取消" if cancelled else "检测失败",
            str(exc).splitlines()[0][:180] or "节点服务器 IP 质量检测失败",
            ip=ip,
            sources=services,
        )
    if flight_owner and flight_future is not None:
        flight_future.set_result(result)
    return result


def _proxy_node_quality_result_for_node(
    result: ProxyNodeQualityResult | dict,
    node: dict,
) -> ProxyNodeQualityResult:
    normalized = _normalize_proxy_node(node)
    detail = proxy_node_quality_detail(result)
    reuse_marker = "同一 IP 批次复用"
    if reuse_marker not in detail:
        detail = f"{detail}；{reuse_marker}".strip("；")[:220]
    return ProxyNodeQualityResult(
        node_key=proxy_node_key(normalized),
        ok=proxy_node_quality_measured(result),
        host=str(normalized.get("server") or proxy_node_quality_host(result)),
        ip=proxy_node_quality_ip(result),
        region=proxy_node_quality_region(result) or proxy_node_region(normalized),
        ip_type=proxy_node_quality_ip_type(result),
        risk_score=proxy_node_quality_risk_score(result),
        risk_label=proxy_node_quality_risk_label(result),
        quality_score=proxy_node_quality_score(result),
        quality_label=proxy_node_quality_label(result),
        confidence=proxy_node_quality_confidence(result),
        detail=detail,
        checked_at=proxy_node_quality_checked_at(result),
        sources=proxy_node_quality_sources(result),
        attempted_sources=proxy_node_quality_attempted_sources(result),
        coverage_complete=proxy_node_quality_coverage_complete(result),
        assessment_scope=proxy_node_quality_assessment_scope(result),
        classification_basis=proxy_node_quality_classification_basis(result),
        quality_signature=proxy_node_quality_signature(result),
        cached=proxy_node_quality_cached(result),
    )


def _proxy_quality_batch_worker_count(requested: int, node_count: int, services) -> int:
    requested_count = max(1, _int_or_default(requested, 8))
    normalized_services = network_diagnostic_settings.normalize_services(services or [])
    reputation_source_count = sum(
        1 for service in normalized_services if service != network_diagnostic_settings.SERVICE_PING0
    )
    per_node_parallelism = max(1, reputation_source_count)
    budget_count = max(1, PROXY_QUALITY_HTTP_CONCURRENCY_BUDGET // per_node_parallelism)
    return min(requested_count, max(1, int(node_count)), budget_count)


def _proxy_quality_batch_resolver(resolver=None):
    base_resolver = resolver or socket.getaddrinfo
    flights: dict[tuple, Future] = {}
    lock = threading.Lock()

    def resolve(*args, **kwargs):
        host = str(args[0] if args else kwargs.get("host") or "")
        key = (
            host,
            tuple(repr(value) for value in args[1:]),
            tuple(sorted((str(name), repr(value)) for name, value in kwargs.items())),
        )
        with lock:
            future = flights.get(key)
            owner = future is None
            if owner:
                future = Future()
                flights[key] = future
        if not owner:
            return future.result()
        try:
            result = base_resolver(*args, **kwargs)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        future.set_result(result)
        return result

    return resolve


def _proxy_quality_report_progress(
    progress_callback,
    completed: int,
    total: int,
    result: ProxyNodeQualityResult,
) -> None:
    if not progress_callback:
        return
    try:
        progress_callback(completed, total, result)
    except Exception:
        pass


def _proxy_quality_cancelled_result(node: dict) -> ProxyNodeQualityResult:
    return _proxy_node_quality_error_result(
        node,
        "已取消",
        "用户已取消本次家宽检测",
    )


def _proxy_quality_result_from_exception(node: dict, exc: Exception) -> ProxyNodeQualityResult:
    return _proxy_node_quality_error_result(
        node,
        "检测失败",
        str(exc).splitlines()[0][:180] or "节点服务器 IP 质量检测失败",
    )


def _proxy_quality_resolve_groups(
    items: list[dict],
    *,
    resolver=None,
    max_workers: int = 8,
    progress_callback=None,
    cancel_event=None,
) -> tuple[list[tuple[str, list[dict]]], dict[str, ProxyNodeQualityResult]]:
    groups_by_ip: dict[str, list[dict]] = {}
    ordered_ips: list[str] = []
    results: dict[str, ProxyNodeQualityResult] = {}
    if not items:
        return [], results

    shared_resolver = _proxy_quality_batch_resolver(resolver)
    worker_count = min(max(1, _int_or_default(max_workers, 8)), len(items))
    completed = 0
    total = len(items)

    def record(result: ProxyNodeQualityResult) -> None:
        nonlocal completed
        results[result.node_key] = result
        completed += 1
        _proxy_quality_report_progress(progress_callback, completed, total, result)

    if cancel_event is not None and cancel_event.is_set():
        for node in items:
            record(_proxy_quality_cancelled_result(node))
        return [], results

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_resolve_proxy_node_ip, node, resolver=shared_resolver): node
            for node in items
        }
        for future in as_completed(futures):
            node = futures[future]
            if cancel_event is not None and cancel_event.is_set():
                record(_proxy_quality_cancelled_result(node))
                continue
            try:
                ip = future.result()
            except Exception as exc:
                record(
                    ProxyNodeQualityResult(
                        node_key=proxy_node_key(node),
                        ok=False,
                        host=str(node.get("server") or ""),
                        region=proxy_node_region(node),
                        quality_label="解析失败",
                        detail=str(exc).splitlines()[0][:180] or "节点服务器解析失败",
                        checked_at=_now_iso(),
                    )
                )
                continue
            if ip not in groups_by_ip:
                groups_by_ip[ip] = []
                ordered_ips.append(ip)
            groups_by_ip[ip].append(node)
    if cancel_event is not None and cancel_event.is_set():
        existing_result_keys = set(results)
        for nodes_for_ip in groups_by_ip.values():
            for node in nodes_for_ip:
                key = proxy_node_key(node)
                if key not in existing_result_keys:
                    record(_proxy_quality_cancelled_result(node))
                    existing_result_keys.add(key)
        return [], results
    return [(ip, groups_by_ip[ip]) for ip in ordered_ips], results


def assess_proxy_node_qualities(
    nodes,
    timeout: float = 5.0,
    max_workers: int = 8,
    *,
    http_get=None,
    resolver=None,
    settings=None,
    enabled_services=None,
    progress_callback=None,
    cancel_event=None,
) -> dict[str, ProxyNodeQualityResult]:
    items = []
    seen = set()
    for item in nodes or []:
        node = item.node if isinstance(item, ProxySubscriptionNode) else item
        if not isinstance(node, dict):
            continue
        try:
            normalized = _normalize_proxy_node(node)
            node_key = proxy_node_key(normalized)
        except Exception:
            continue
        if node_key in seen:
            continue
        seen.add(node_key)
        items.append(normalized)
    if not items:
        return {}

    settings = settings or network_diagnostic_settings.load_settings()
    if enabled_services is not None:
        services = network_diagnostic_settings.normalize_services(enabled_services)
    elif hasattr(settings, "enabled_services"):
        services = settings.enabled_services()
    else:
        services = []
    effective_services = proxy_quality_effective_services(settings, services)
    if not effective_services:
        results: dict[str, ProxyNodeQualityResult] = {}
        for node in items:
            try:
                result = assess_proxy_node_quality(
                    node,
                    timeout,
                    http_get=http_get,
                    resolver=resolver,
                    settings=settings,
                    enabled_services=services,
                    use_cache=False,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                result = _proxy_quality_result_from_exception(node, exc)
            results[result.node_key] = result
            _proxy_quality_report_progress(progress_callback, len(results), len(items), result)
        return results

    groups, results = _proxy_quality_resolve_groups(
        items,
        resolver=resolver,
        max_workers=max_workers,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    total = len(items)
    completed = len(results)
    if not groups:
        return results

    cached_quality_results = load_proxy_subscription_qualities()
    cache_index = _build_proxy_quality_cache_index(cached_quality_results)
    worker_count = _proxy_quality_batch_worker_count(max_workers, len(groups), effective_services)
    batch_flights: dict[tuple[str, str], Future] = {}
    batch_flights_lock = threading.Lock()
    pending_groups = iter(groups)
    futures: dict[Future, tuple[str, list[dict]]] = {}

    def submit_next(executor) -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return False
        try:
            ip, grouped_nodes = next(pending_groups)
        except StopIteration:
            return False
        primary = grouped_nodes[0]
        futures[
            executor.submit(
                assess_proxy_node_quality,
                primary,
                timeout,
                http_get=http_get,
                resolver=lambda *_args, **_kwargs: [(None, None, None, "", (ip, 0))],
                settings=settings,
                enabled_services=effective_services,
                cached_quality_results=cached_quality_results,
                cache_ttl_seconds=PROXY_QUALITY_CACHE_TTL_SECONDS,
                cache_index=cache_index,
                batch_flights=batch_flights,
                batch_flights_lock=batch_flights_lock,
                cancel_event=cancel_event,
            )
        ] = (ip, grouped_nodes)
        return True

    def record(result: ProxyNodeQualityResult) -> None:
        nonlocal completed
        results[result.node_key] = result
        completed += 1
        _proxy_quality_report_progress(progress_callback, completed, total, result)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for _ in range(worker_count):
            if not submit_next(executor):
                break
        while futures:
            for future in as_completed(tuple(futures)):
                _ip, grouped_nodes = futures.pop(future)
                primary = grouped_nodes[0]
                try:
                    primary_result = future.result()
                except Exception as exc:
                    primary_result = _proxy_quality_result_from_exception(primary, exc)
                record(primary_result)
                for node in grouped_nodes[1:]:
                    rebound = _proxy_node_quality_result_for_node(primary_result, node)
                    record(rebound)
                submit_next(executor)
                break
        if cancel_event is not None and cancel_event.is_set():
            for _ip, grouped_nodes in pending_groups:
                for node in grouped_nodes:
                    record(_proxy_quality_cancelled_result(node))
    return results


def measure_proxy_node_latencies_on_server(
    ssh_name: str,
    nodes,
    timeout: float = 3.0,
    attempts: int = 2,
    max_workers: int = PROXY_LATENCY_DEFAULT_MAX_WORKERS,
) -> dict[str, ProxyNodeLatencyResult]:
    items = []
    seen = set()
    for item in nodes or []:
        node = item.node if isinstance(item, ProxySubscriptionNode) else item
        if not isinstance(node, dict):
            continue
        try:
            normalized = _normalize_proxy_node(node)
            node_key = proxy_node_key(normalized)
        except Exception:
            continue
        if node_key in seen:
            continue
        items.append(
            {
                "key": node_key,
                "server": str(normalized["server"]),
                "port": int(normalized["port"]),
                "name": str(normalized["name"]),
            }
        )
        seen.add(node_key)

    if not items:
        return {}

    timeout_value = _normalize_timeout(timeout, 3.0)
    attempts_value = max(1, _int_or_default(attempts, 2))
    workers_value = max(
        1,
        min(
            _int_or_default(max_workers, PROXY_LATENCY_DEFAULT_MAX_WORKERS),
            PROXY_LATENCY_MAX_WORKERS,
        ),
    )
    _ssh_profile, client = _connect_ssh(ssh_name)
    command = _build_remote_latency_command(timeout_value, attempts_value, workers_value)
    command_timeout = _remote_latency_command_timeout(len(items), timeout_value, attempts_value, workers_value)
    status, stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        timeout=command_timeout,
        input_data=json.dumps(items, ensure_ascii=False),
        log_command=False,
    )
    if status != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"{ssh_name}: 远端节点测速失败: {detail or status}")
    return _parse_remote_latency_output(stdout)


def proxy_node_latency_ok(result: ProxyNodeLatencyResult | dict | None) -> bool:
    if isinstance(result, ProxyNodeLatencyResult):
        return bool(result.ok and result.latency_ms is not None)
    if isinstance(result, dict):
        return bool(result.get("ok") and proxy_node_latency_ms(result) is not None)
    return False


def proxy_node_latency_ms(result: ProxyNodeLatencyResult | dict | None) -> int | None:
    if isinstance(result, ProxyNodeLatencyResult):
        return int(result.latency_ms) if result.latency_ms is not None else None
    if isinstance(result, dict):
        value = result.get("latency_ms")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def proxy_node_latency_detail(result: ProxyNodeLatencyResult | dict | None) -> str:
    if isinstance(result, ProxyNodeLatencyResult):
        return str(result.detail or "")[:160]
    if isinstance(result, dict):
        return str(result.get("detail") or "")[:160]
    return ""


def proxy_node_latency_attempts(result: ProxyNodeLatencyResult | dict | None) -> int:
    if isinstance(result, ProxyNodeLatencyResult):
        return int(result.attempts or 0)
    if isinstance(result, dict):
        return _int_or_default(result.get("attempts"), 0)
    return 0


def proxy_node_latency_label(result: ProxyNodeLatencyResult | dict | None) -> str:
    if result is not None and not proxy_node_latency_fresh(result):
        return "已过期"
    latency = proxy_node_latency_ms(result)
    if latency is not None and proxy_node_latency_ok(result):
        return f"{latency}ms"
    if result is None:
        return "未测"
    return "不可连"


def proxy_node_latency_measured_at(result: ProxyNodeLatencyResult | dict | None) -> str:
    if isinstance(result, ProxyNodeLatencyResult):
        return str(result.measured_at or "")
    if isinstance(result, dict):
        return str(result.get("measured_at") or "")
    return ""


def proxy_node_latency_fresh(
    result: ProxyNodeLatencyResult | dict | None,
    ttl_seconds: int = PROXY_LATENCY_CACHE_TTL_SECONDS,
) -> bool:
    if isinstance(result, ProxyNodeLatencyResult):
        measured_at = proxy_node_latency_measured_at(result)
        return bool(measured_at and _quality_checked_at_fresh(measured_at, ttl_seconds))
    if not isinstance(result, dict):
        return False
    return _quality_checked_at_fresh(str(result.get("measured_at") or ""), ttl_seconds)


def proxy_node_latency_explicitly_unreachable(
    result: ProxyNodeLatencyResult | dict | None,
) -> bool:
    return bool(
        proxy_node_latency_fresh(result)
        and not proxy_node_latency_ok(result)
    )


def proxy_node_quality_measured(result: ProxyNodeQualityResult | dict | None) -> bool:
    if isinstance(result, ProxyNodeQualityResult):
        return bool(result.ok)
    if isinstance(result, dict):
        return bool(result.get("ok"))
    return False


def proxy_node_quality_score(result: ProxyNodeQualityResult | dict | None) -> int:
    if isinstance(result, ProxyNodeQualityResult):
        value = result.quality_score
    elif isinstance(result, dict):
        value = result.get("quality_score")
    else:
        value = 0
    return max(0, min(100, _int_or_default(value, 0)))


def proxy_node_quality_label(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        return result.quality_label or ("质量已测" if result.ok else "质量未测")
    if isinstance(result, dict):
        return str(result.get("quality_label") or ("质量已测" if result.get("ok") else "质量未测"))
    return "质量未测"


def proxy_node_quality_confidence(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        return str(result.confidence or "")
    if isinstance(result, dict):
        return str(result.get("confidence") or "")
    return ""


def proxy_node_quality_detail(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        return str(result.detail or "")[:220]
    if isinstance(result, dict):
        return str(result.get("detail") or "")[:220]
    return ""


def proxy_node_quality_ip_type(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        return str(result.ip_type or "")
    if isinstance(result, dict):
        return str(result.get("ip_type") or "")
    return ""


def proxy_node_quality_ip(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        return str(result.ip or "")
    if isinstance(result, dict):
        return str(result.get("ip") or "")
    return ""


def proxy_node_quality_host(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        return str(result.host or "")
    if isinstance(result, dict):
        return str(result.get("host") or "")
    return ""


def proxy_node_quality_region(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        return str(result.region or "")
    if isinstance(result, dict):
        return str(result.get("region") or "")
    return ""


def proxy_node_quality_risk_score(result: ProxyNodeQualityResult | dict | None) -> int | None:
    if isinstance(result, ProxyNodeQualityResult):
        value = result.risk_score
    elif isinstance(result, dict):
        value = result.get("risk_score")
    else:
        value = None
    return _optional_int(value)


def proxy_node_quality_risk_label(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        return str(result.risk_label or "")
    if isinstance(result, dict):
        return str(result.get("risk_label") or "")
    return ""


def proxy_node_quality_checked_at(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        return str(result.checked_at or "")
    if isinstance(result, dict):
        return str(result.get("checked_at") or "")
    return ""


def proxy_node_quality_fresh(
    result: ProxyNodeQualityResult | dict | None,
    ttl_seconds: int = PROXY_QUALITY_CACHE_TTL_SECONDS,
) -> bool:
    return bool(
        proxy_node_quality_measured(result)
        and _quality_checked_at_fresh(proxy_node_quality_checked_at(result), ttl_seconds)
    )


def proxy_node_quality_decisive_for_ai_proxy(
    result: ProxyNodeQualityResult | dict | None,
) -> bool:
    """Return whether fresh, complete evidence can safely qualify or reject a node."""

    return bool(proxy_node_quality_fresh(result) and proxy_node_quality_coverage_complete(result))


def proxy_node_quality_sources(result: ProxyNodeQualityResult | dict | None) -> tuple[str, ...]:
    if isinstance(result, ProxyNodeQualityResult):
        return tuple(network_diagnostic_settings.normalize_services(list(result.sources or ())))
    if isinstance(result, dict):
        return tuple(network_diagnostic_settings.normalize_services(result.get("sources") or []))
    return ()


def proxy_node_quality_attempted_sources(result: ProxyNodeQualityResult | dict | None) -> tuple[str, ...]:
    if isinstance(result, ProxyNodeQualityResult):
        values = result.attempted_sources
    elif isinstance(result, dict):
        values = result.get("attempted_sources")
    else:
        values = ()
    return tuple(network_diagnostic_settings.normalize_services(list(values or ())))


def proxy_node_quality_coverage_complete(result: ProxyNodeQualityResult | dict | None) -> bool:
    if isinstance(result, ProxyNodeQualityResult):
        return bool(result.coverage_complete)
    if isinstance(result, dict):
        return bool(result.get("coverage_complete"))
    return False


def proxy_node_quality_assessment_scope(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        value = result.assessment_scope
    elif isinstance(result, dict):
        value = result.get("assessment_scope")
    else:
        value = ""
    return str(value or PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER)


def proxy_node_quality_classification_basis(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        value = result.classification_basis
    elif isinstance(result, dict):
        value = result.get("classification_basis")
    else:
        value = ""
    return str(value or "")


def proxy_node_quality_signature(result: ProxyNodeQualityResult | dict | None) -> str:
    if isinstance(result, ProxyNodeQualityResult):
        return str(result.quality_signature or "")
    if isinstance(result, dict):
        return str(result.get("quality_signature") or "")
    return ""


def proxy_node_quality_cached(result: ProxyNodeQualityResult | dict | None) -> bool:
    if isinstance(result, ProxyNodeQualityResult):
        return bool(result.cached)
    if isinstance(result, dict):
        return bool(result.get("cached"))
    return False


def proxy_node_quality_cancelled(result: ProxyNodeQualityResult | dict | None) -> bool:
    return not proxy_node_quality_measured(result) and proxy_node_quality_label(result) == "已取消"


def proxy_node_quality_cacheable(result: ProxyNodeQualityResult | dict | None) -> bool:
    successful_sources = set(proxy_node_quality_sources(result))
    attempted_sources = set(proxy_node_quality_attempted_sources(result))
    return bool(
        proxy_node_quality_measured(result)
        and proxy_node_quality_coverage_complete(result)
        and successful_sources
        and successful_sources == attempted_sources
        and proxy_node_quality_assessment_scope(result) == PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER
        and proxy_node_quality_classification_basis(result)
        in {"信誉源网络/风险字段", "Ping0 指定 IP", "Geo/ASN 辅助"}
        and proxy_node_quality_signature(result)
    )


def proxy_node_quality_source_label(result: ProxyNodeQualityResult | dict | None) -> str:
    sources = proxy_node_quality_sources(result)
    basis = proxy_node_quality_classification_basis(result)
    if not sources:
        return basis or ("无有效质量源" if proxy_node_quality_attempted_sources(result) else "未标明检测源")
    label = " + ".join(network_diagnostic_settings.SERVICE_LABELS.get(source, source) for source in sources)
    if basis == "Geo/ASN 辅助":
        label += " + Geo/ASN辅助"
    return label


def proxy_quality_effective_services(settings=None, enabled_services=None) -> list[str]:
    if settings is None:
        settings = network_diagnostic_settings.load_settings()
    if enabled_services is not None:
        services = network_diagnostic_settings.normalize_services(enabled_services)
    elif hasattr(settings, "enabled_services"):
        services = settings.enabled_services()
    else:
        services = []
    return [
        service
        for service in services
        if service not in PROXY_QUALITY_KEY_REQUIRED_SERVICES
        or bool(_diagnostic_settings_keys(settings, service))
    ]


def quality_source_label_from_settings(settings=None, enabled_services=None) -> str:
    if enabled_services is not None:
        services = network_diagnostic_settings.normalize_services(enabled_services)
    else:
        settings = settings or network_diagnostic_settings.load_settings()
        services = settings.enabled_services() if hasattr(settings, "enabled_services") else []
    if not services:
        return "未启用检测源"
    return " + ".join(network_diagnostic_settings.SERVICE_LABELS.get(service, service) for service in services)


def proxy_node_quality_for_ai_proxy_ok(result: ProxyNodeQualityResult | dict | None) -> bool:
    if not proxy_node_quality_measured(result):
        return False
    ip_type = proxy_node_quality_ip_type(result)
    label = proxy_node_quality_label(result)
    risk = proxy_node_quality_risk_score(result)
    score = proxy_node_quality_score(result)
    confidence = proxy_node_quality_confidence(result)
    classification_basis = proxy_node_quality_classification_basis(result)
    disqualified = any(
        marker in ip_type or marker in label
        for marker in ("冲突", "代理", "VPN", "Tor", "匿名", "高风险", "非原生", "广播")
    )
    residential = any(marker in ip_type for marker in ("家庭", "住宅", "家宽"))
    return (
        residential
        and not disqualified
        and proxy_node_quality_coverage_complete(result)
        and classification_basis in {"信誉源网络/风险字段", "Ping0 指定 IP"}
        and confidence in {"中", "高"}
        and score >= 80
        and risk is not None
        and risk <= 35
    )


def _normalize_proxy_node(node: dict) -> dict:
    parsed = {str(k).strip(): v for k, v in node.items() if str(k).strip()}
    _apply_proxy_node_aliases(parsed)
    if not _has_value(parsed.get("name")) and _has_value(parsed.get("server")):
        type_label = str(parsed.get("type") or "proxy").strip() or "proxy"
        port_label = str(parsed.get("port") or "").strip()
        parsed["name"] = f"{type_label}-{parsed['server']}{':' + port_label if port_label else ''}"
    required = ["name", "type", "server", "port"]
    missing = [key for key in required if not _has_value(parsed.get(key))]
    if missing:
        raise ValueError("代理节点缺少字段: " + "、".join(missing))
    for key in ("name", "type", "server"):
        parsed[key] = str(parsed[key]).strip()
    if parsed["type"].casefold() in PROXY_NODE_FORBIDDEN_OUTBOUND_TYPES:
        raise ValueError(
            f"代理节点 type={parsed['type']} 是路由内置类型，不能作为上游节点"
        )
    if parsed["type"].lower() == "hy2":
        parsed["type"] = "hysteria2"
    elif parsed["type"].lower() == "socks":
        parsed["type"] = "socks5"
    parsed["port"] = _normalize_port(parsed["port"], "代理节点端口")
    return parsed


def _apply_proxy_node_aliases(parsed: dict) -> None:
    aliases = {
        "name": ("tag", "remark", "remarks", "ps"),
        "server": ("address", "host"),
        "port": ("server_port", "server-port", "serverPort"),
    }
    for canonical, names in aliases.items():
        if _has_value(parsed.get(canonical)):
            continue
        for alias in names:
            if _has_value(parsed.get(alias)):
                parsed[canonical] = parsed[alias]
                break


def build_mihomo_config(
    proxy_node: dict,
    mixed_port: int = 7890,
    *,
    fallback_proxy_nodes: tuple[dict, ...] | list[dict] | None = None,
    health_checked_group: bool = False,
    log_level: str = "warning",
    extra_proxy_domains: tuple[str, ...] | list[str] | None = None,
    extra_proxy_ip_cidrs: tuple[str, ...] | list[str] | None = None,
    proxy_non_cn: bool = False,
    strict_privacy: bool = False,
    resilient_transport: bool = False,
    mainland_dns: bool = False,
) -> str:
    primary_node = _normalize_proxy_node(proxy_node)
    display_name = str(primary_node.get("name") or "AI_PROXY").strip() or "AI_PROXY"
    # Subscription display names share mihomo's outbound namespace with
    # built-ins and proxy groups.  Never let names such as DIRECT/REJECT/PASS
    # or AI-PROXY become a routing target.
    nodes = _managed_mihomo_proxy_nodes(primary_node, fallback_proxy_nodes)
    node_names = [str(node["name"]) for node in nodes]
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    proxy_domains = _unique_clean_values(AI_PROXY_DOMAINS, extra_proxy_domains)
    rules = [
        *(f"DOMAIN-SUFFIX,{domain},AI-PROXY" for domain in proxy_domains),
        *(_ip_cidr_rule(cidr) for cidr in _unique_clean_values(extra_proxy_ip_cidrs)),
    ]
    if strict_privacy:
        # This is deliberately fail-closed for every public destination that
        # has already entered mihomo.  It remains an application-layer proxy:
        # callers that ignore the configured proxy are outside this boundary.
        rules.extend(PRIVATE_DIRECT_IP_RULES)
        rules.append("MATCH,AI-PROXY")
    elif proxy_non_cn:
        rules.extend(PRIVATE_DIRECT_IP_RULES)
        rules.extend([
            "GEOIP,CN,DIRECT",
            "MATCH,AI-PROXY",
        ])
    else:
        rules.append("MATCH,DIRECT")
    config = {
        "mixed-port": mixed_port,
        "external-controller": f"127.0.0.1:{mihomo_controller_port(mixed_port)}",
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": _normalize_mihomo_log_level(log_level),
        "ipv6": not strict_privacy,
        "proxies": nodes,
        "proxy-groups": [
            _managed_ai_proxy_group(
                node_names,
                health_checked=bool(health_checked_group or len(node_names) > 1),
            )
        ],
        "rules": rules,
    }
    if resilient_transport:
        # Mainland routes commonly return several addresses with very uneven
        # reachability. Race them and keep idle streaming sessions alive so a
        # single slow/broken address or NAT timeout does not stall Codex.
        config.update(
            {
                "tcp-concurrent": True,
                "keep-alive-interval": 15,
                "keep-alive-idle": 15,
                "disable-keep-alive": False,
            }
        )
    if strict_privacy:
        # DoH requests follow the routing rules, so public DNS queries use the
        # selected proxy.  proxy-server-nameserver is intentionally encrypted
        # but independent of those rules to resolve a hostname-based node.
        # Mainland-reachable bootstrap IPs resolve only those DoH hostnames;
        # ordinary public destination DNS still follows AI-PROXY.
        config["dns"] = _strict_privacy_dns_config()
    elif mainland_dns:
        # Compatibility mode keeps normal/direct names on mainland-reachable
        # encrypted resolvers, but selected AI names are resolved through the
        # already bootstrapped AI-PROXY route to avoid poisoned system DNS.
        config["dns"] = _mainland_compatible_dns_config(proxy_domains)
    markers = [
        AI_PROXY_CONFIG_MARKER,
        _managed_proxy_display_name_marker(display_name),
    ]
    if strict_privacy:
        markers.append(AI_PROXY_STRICT_PRIVACY_MARKER)
    return "\n".join(markers) + "\n" + _dump_yaml(config)


def _normalize_mihomo_log_level(value: object) -> str:
    level = str(value or "warning").strip().casefold()
    return level if level in {"silent", "error", "warning", "info", "debug"} else "warning"


def _proxy_node_connection_key(node: dict) -> str:
    normalized = _normalize_proxy_node(node)
    normalized["name"] = "API-SWITCHER-CONNECTION"
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _managed_mihomo_proxy_nodes(
    primary_node: dict,
    fallback_proxy_nodes: tuple[dict, ...] | list[dict] | None,
) -> list[dict]:
    primary = _normalize_proxy_node(primary_node)
    primary["name"] = AI_PROXY_INTERNAL_NODE_NAME
    nodes = [primary]
    seen = {_proxy_node_connection_key(primary_node)}
    for candidate in fallback_proxy_nodes or ():
        if len(nodes) >= AI_PROXY_FALLBACK_MAX_NODES:
            break
        try:
            normalized = _normalize_proxy_node(candidate)
            connection_key = _proxy_node_connection_key(normalized)
        except (TypeError, ValueError):
            continue
        # A standalone managed pool cannot safely resolve a subscription node
        # whose outbound depends on another, omitted subscription name.
        if str(normalized.get("dialer-proxy") or "").strip():
            continue
        if connection_key in seen:
            continue
        normalized["name"] = f"{AI_PROXY_FALLBACK_NODE_PREFIX}{len(nodes)}"
        nodes.append(normalized)
        seen.add(connection_key)
    return nodes


def _remote_proxy_fallback_nodes(
    primary_node: dict,
    candidate_nodes,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
) -> tuple[dict, ...]:
    """Build a bounded policy-safe remote failover pool from the active cache."""

    try:
        primary_connection_key = _proxy_node_connection_key(primary_node)
    except (TypeError, ValueError):
        return ()
    qualities = quality_results or {}
    candidates = ranked_proxy_subscription_nodes_for_ai_probe(
        automatic_proxy_subscription_nodes(candidate_nodes, qualities),
        qualities,
    )
    selected: list[dict] = []
    seen = {primary_connection_key}
    limit = max(0, AI_PROXY_FALLBACK_MAX_NODES - 1)
    for item in candidates:
        if len(selected) >= limit:
            break
        item_key = proxy_subscription_node_key(item)
        quality = qualities.get(item_key)
        if (
            proxy_node_quality_decisive_for_ai_proxy(quality)
            and not proxy_node_quality_for_ai_proxy_ok(quality)
        ):
            continue
        try:
            normalized = _normalize_proxy_node(item.node)
            connection_key = _proxy_node_connection_key(normalized)
        except (TypeError, ValueError):
            continue
        if str(normalized.get("dialer-proxy") or "").strip():
            continue
        if connection_key in seen:
            continue
        selected.append(normalized)
        seen.add(connection_key)
    return tuple(selected)


def _existing_remote_proxy_fallback_nodes(
    config_content: str,
    primary_node: dict,
) -> tuple[dict, ...]:
    """Preserve a managed pool when a direct hot reload omits candidates."""

    content = str(config_content or "")
    if AI_PROXY_CONFIG_MARKER not in content:
        return ()
    try:
        parsed = yaml.safe_load(content)
        proxy_nodes = parsed.get("proxies") if isinstance(parsed, dict) else None
        primary_key = _proxy_node_connection_key(primary_node)
    except Exception:
        return ()
    if not isinstance(proxy_nodes, list):
        return ()

    selected: list[dict] = []
    seen = {primary_key}
    for node in proxy_nodes:
        if len(selected) >= AI_PROXY_FALLBACK_MAX_NODES - 1:
            break
        if not isinstance(node, dict):
            continue
        try:
            normalized = _normalize_proxy_node(node)
            connection_key = _proxy_node_connection_key(normalized)
        except (TypeError, ValueError):
            continue
        if str(normalized.get("dialer-proxy") or "").strip() or connection_key in seen:
            continue
        selected.append(normalized)
        seen.add(connection_key)
    return tuple(selected)


def _managed_ai_proxy_group(node_names: list[str], *, health_checked: bool) -> dict:
    names = [str(name or "").strip() for name in node_names if str(name or "").strip()]
    if not names:
        raise ValueError("mihomo 受管代理组至少需要一个节点")
    if not health_checked:
        return {"name": "AI-PROXY", "type": "select", "proxies": names}
    return {
        "name": "AI-PROXY",
        "type": "fallback",
        "proxies": names,
        "url": AI_PROXY_HEALTH_CHECK_URL,
        "interval": AI_PROXY_HEALTH_CHECK_INTERVAL_SECONDS,
        "lazy": False,
        "timeout": AI_PROXY_HEALTH_CHECK_TIMEOUT_MS,
        "max-failed-times": AI_PROXY_HEALTH_CHECK_MAX_FAILURES,
        "expected-status": AI_PROXY_HEALTH_CHECK_EXPECTED_STATUS,
    }


def _managed_proxy_display_name_marker(display_name: str) -> str:
    encoded = base64.urlsafe_b64encode(str(display_name).encode("utf-8")).decode("ascii")
    return f"{AI_PROXY_DISPLAY_NAME_MARKER} {encoded}"


def _managed_proxy_display_name(content: str) -> str:
    matches = re.findall(
        rf"(?m)^{re.escape(AI_PROXY_DISPLAY_NAME_MARKER)}[ \t]+([A-Za-z0-9_-]+={{0,2}})[ \t]*$",
        str(content or ""),
    )
    if len(matches) != 1 or len(matches[0]) > 16384:
        return ""
    try:
        decoded = base64.b64decode(matches[0], altchars=b"-_", validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""
    return decoded.strip()


def _managed_config_strict_privacy_enabled(content: str) -> bool:
    """Recognize strict mode only on a managed, fail-closed mihomo config."""

    text = str(content or "")
    if AI_PROXY_CONFIG_MARKER not in text:
        return False
    try:
        parsed = yaml.safe_load(text)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    rules = parsed.get("rules")
    dns = parsed.get("dns")
    proxy_nodes = parsed.get("proxies")
    proxy_groups = parsed.get("proxy-groups")
    node_names = [
        str(node.get("name") or "").strip()
        for node in proxy_nodes or ()
        if isinstance(node, dict)
    ]
    expected_node_names = [AI_PROXY_INTERNAL_NODE_NAME]
    expected_node_names.extend(
        f"{AI_PROXY_FALLBACK_NODE_PREFIX}{index}"
        for index in range(1, len(node_names))
    )
    managed_nodes_valid = bool(
        isinstance(proxy_nodes, list)
        and 1 <= len(proxy_nodes) <= AI_PROXY_FALLBACK_MAX_NODES
        and len(node_names) == len(proxy_nodes)
        and node_names == expected_node_names
    )
    if managed_nodes_valid:
        try:
            connection_keys = [_proxy_node_connection_key(node) for node in proxy_nodes]
        except (TypeError, ValueError):
            managed_nodes_valid = False
        else:
            managed_nodes_valid = len(connection_keys) == len(set(connection_keys)) and all(
                not str(node.get("dialer-proxy") or "").strip()
                for node in proxy_nodes[1:]
                if isinstance(node, dict)
            )
    ai_proxy_groups = [
        group
        for group in proxy_groups or ()
        if isinstance(group, dict) and str(group.get("name") or "").strip() == "AI-PROXY"
    ]
    ai_proxy_group = ai_proxy_groups[0] if len(ai_proxy_groups) == 1 else None
    group_proxy_names = (
        ai_proxy_group.get("proxies")
        if isinstance(ai_proxy_group, dict)
        else None
    )
    if not node_names:
        return False
    select_group = _managed_ai_proxy_group(node_names, health_checked=False)
    fallback_group = _managed_ai_proxy_group(node_names, health_checked=True)
    proxy_group_contract_valid = bool(
        managed_nodes_valid
        and isinstance(proxy_groups, list)
        and len(proxy_groups) == 1
        and isinstance(ai_proxy_group, dict)
        and isinstance(group_proxy_names, list)
        and group_proxy_names == node_names
        and ai_proxy_group in (select_group, fallback_group)
    )
    def strict_rule_action(rule) -> str:
        text_rule = str(rule or "").strip()
        parts = [part.strip() for part in text_rule.split(",")]
        if len(parts) < 2:
            return ""
        return parts[-2] if parts[-1] == "no-resolve" else parts[-1]

    normalized_rules = tuple(str(rule or "").strip() for rule in rules or ())
    direct_rules = tuple(
        rule for rule in normalized_rules if strict_rule_action(rule) == "DIRECT"
    )

    controller = str(parsed.get("external-controller") or "").strip()
    try:
        controller_host, controller_port = controller.rsplit(":", 1)
        controller_is_loopback = (
            controller_host == "127.0.0.1"
            and 1 <= int(controller_port) <= 65535
        )
    except (TypeError, ValueError):
        controller_is_loopback = False

    return bool(
        isinstance(rules, list)
        and rules
        and parsed.get("allow-lan") is False
        and proxy_group_contract_valid
        and str(parsed.get("bind-address") or "").strip() == "127.0.0.1"
        and str(parsed.get("mode") or "").strip() == "rule"
        and controller_is_loopback
        and normalized_rules[-1] == "MATCH,AI-PROXY"
        and all(
            rule in PRIVATE_DIRECT_IP_RULES or strict_rule_action(rule) == "AI-PROXY"
            for rule in normalized_rules
        )
        and direct_rules == PRIVATE_DIRECT_IP_RULES
        and parsed.get("ipv6") is False
        # This is an intentionally exact managed-config contract.  Besides
        # rejecting plaintext resolvers, equality rejects alternate URL
        # authorities/userinfo/query/fragment and extra policy/listen fields
        # that could create an unverified DNS path.
        and isinstance(dns, dict)
        and dns.get("enable") is True
        and dns.get("ipv6") is False
        and dns.get("use-hosts") is True
        and dns.get("use-system-hosts") is False
        and dns.get("respect-rules") is True
        and dns == _strict_privacy_dns_config()
    )


def _managed_config_strict_privacy_intended(content: str) -> bool:
    """Preserve an explicitly managed strict intent even when YAML drifted.

    A drifted strict config must be repaired with the current strict template,
    not silently rebuilt as the compatibility/DIRECT template.  Both markers
    must occupy their own complete lines so prose or another comment merely
    mentioning their text cannot opt an unrelated config into strict mode.
    """

    text = str(content or "")

    def exact_marker_line(marker: str) -> bool:
        return re.search(
            rf"(?m)^[ \t]*{re.escape(marker)}[ \t]*\r?$",
            text,
        ) is not None

    return exact_marker_line(AI_PROXY_CONFIG_MARKER) and exact_marker_line(
        AI_PROXY_STRICT_PRIVACY_MARKER
    )


def _resolve_managed_strict_privacy(value, current_config: str = "") -> bool:
    if value is None:
        return _managed_config_strict_privacy_intended(current_config)
    if isinstance(value, str):
        return _truthy(value)
    return bool(value)


def _strict_privacy_call_kwargs(value) -> dict[str, bool]:
    """Avoid changing legacy/mock call shapes until a mode was explicitly chosen."""

    if value is None:
        return {}
    return {"strict_privacy": _resolve_managed_strict_privacy(value)}


def _unique_clean_values(*groups) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group or ():
            text = str(value or "").strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            values.append(text)
    return tuple(values)


def _ip_cidr_rule(cidr: str) -> str:
    text = str(cidr or "").strip()
    rule_type = "IP-CIDR6" if ":" in text else "IP-CIDR"
    return f"{rule_type},{text},AI-PROXY,no-resolve"


def mihomo_controller_port(mixed_port: int) -> int:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    if mixed_port <= 64535:
        return mixed_port + 1000
    return max(1, mixed_port - 1000)


def _build_isolated_candidate_config(proxy_node: dict) -> str:
    """Build a credential-safe config template whose listen ports are filled remotely."""

    mixed_port = _ISOLATED_CANDIDATE_CONFIG_PORT
    controller_port = mihomo_controller_port(mixed_port)
    config = build_mihomo_config(
        proxy_node,
        mixed_port,
        health_checked_group=True,
        resilient_transport=True,
        mainland_dns=True,
    )
    mixed_marker = f"mixed-port: {mixed_port}"
    controller_marker = f'127.0.0.1:{controller_port}'
    if mixed_marker not in config or controller_marker not in config:
        raise RuntimeError("无法构建隔离候选配置模板")
    return config.replace(
        mixed_marker,
        "mixed-port: __API_SWITCHER_CANDIDATE_PORT__",
        1,
    ).replace(
        controller_marker,
        "127.0.0.1:__API_SWITCHER_CANDIDATE_CONTROLLER_PORT__",
        1,
    )


def _build_isolated_candidate_probe_command(home: str, timeout: int = 8) -> str:
    """Return a bounded remote probe that always terminates its temporary process."""

    try:
        timeout = max(1, min(60, int(timeout)))
    except (TypeError, ValueError):
        timeout = 8
    base_dir = posixpath.join(home, ".config", "api-switcher")
    strict_probe = _build_probe_command(
        _ISOLATED_CANDIDATE_CONFIG_PORT,
        timeout,
        rounds=REMOTE_AI_STABILITY_ROUNDS,
        strict=True,
    ).replace(
        f"PROXY=http://127.0.0.1:{_ISOLATED_CANDIDATE_CONFIG_PORT}",
        "PROXY=http://127.0.0.1:$PORT",
        1,
    )
    return f"""set -eu
umask 077
BASE={shlex.quote(base_dir)}
LOCK="$BASE/.candidate-probe.lock"
LOCK_HELD=0
TMP=""
CANDIDATE_PID=""
cleanup_candidate() {{
  trap - EXIT HUP INT TERM
  if [ -n "$CANDIDATE_PID" ] && kill -0 "$CANDIDATE_PID" 2>/dev/null; then
    kill -TERM "$CANDIDATE_PID" 2>/dev/null || true
    wait_count=0
    while kill -0 "$CANDIDATE_PID" 2>/dev/null && [ "$wait_count" -lt 20 ]; do
      sleep 0.1
      wait_count=$((wait_count + 1))
    done
    if kill -0 "$CANDIDATE_PID" 2>/dev/null; then
      kill -KILL "$CANDIDATE_PID" 2>/dev/null || true
    fi
    wait "$CANDIDATE_PID" 2>/dev/null || true
  fi
  [ -z "$TMP" ] || rm -rf -- "$TMP"
  if [ "$LOCK_HELD" = "1" ]; then
    current_owner="$(readlink "$LOCK" 2>/dev/null || true)"
    if [ "$current_owner" = "$$" ]; then
      rm -f -- "$LOCK"
    fi
  fi
}}
trap cleanup_candidate EXIT
trap 'cleanup_candidate; exit 129' HUP
trap 'cleanup_candidate; exit 130' INT
trap 'cleanup_candidate; exit 143' TERM

if ! command -v python3 >/dev/null 2>&1; then
  echo "远端缺少 python3，隔离候选严格验证不可用" >&2
  exit 11
fi
BINARY=""
if [ -x "$HOME/.local/bin/mihomo" ]; then
  BINARY="$HOME/.local/bin/mihomo"
else
  BINARY="$(command -v mihomo 2>/dev/null || command -v clash-meta 2>/dev/null || command -v clash 2>/dev/null || true)"
fi
if [ -z "$BINARY" ] || [ ! -x "$BINARY" ]; then
  echo "远端缺少可执行的 mihomo/clash，隔离候选验证已拒绝" >&2
  exit 12
fi

mkdir -p -- "$BASE"
chmod 700 "$BASE" 2>/dev/null || true
if ! ln -s "$$" "$LOCK" 2>/dev/null; then
  lock_owner="$(readlink "$LOCK" 2>/dev/null || true)"
  case "$lock_owner" in
    ''|*[!0-9]*) lock_owner="" ;;
  esac
  if [ -n "$lock_owner" ] && kill -0 "$lock_owner" 2>/dev/null; then
    echo "另一项远端候选验证正在运行，已拒绝并发修改" >&2
    exit 73
  fi
  current_owner="$(readlink "$LOCK" 2>/dev/null || true)"
  if [ "$current_owner" != "$lock_owner" ]; then
    echo "远端候选验证锁状态已变化，已拒绝并发修改" >&2
    exit 73
  fi
  if [ -d "$LOCK" ] && [ ! -L "$LOCK" ]; then
    rmdir -- "$LOCK" 2>/dev/null || true
  else
    rm -f -- "$LOCK"
  fi
  if ! ln -s "$$" "$LOCK" 2>/dev/null; then
    echo "无法取得远端候选验证锁，已拒绝并发修改" >&2
    exit 73
  fi
fi
LOCK_HELD=1

# A killed SSH channel may not run EXIT traps. Under the lock, remove only our
# private candidate directories; terminate a surviving process solely when its
# command line proves that it is using that exact directory.
for stale_dir in "$BASE"/candidate.*; do
  [ -d "$stale_dir" ] || continue
  stale_pid="$(head -n 1 "$stale_dir/mihomo.pid" 2>/dev/null || true)"
  case "$stale_pid" in
    ''|*[!0-9]*) stale_pid="" ;;
  esac
  if [ -n "$stale_pid" ] && kill -0 "$stale_pid" 2>/dev/null; then
    stale_cmd="$(ps -p "$stale_pid" -o args= 2>/dev/null || true)"
    if [ -z "$stale_cmd" ] && [ -r "/proc/$stale_pid/cmdline" ]; then
      stale_cmd="$(tr '\\0' ' ' < "/proc/$stale_pid/cmdline" 2>/dev/null || true)"
    fi
    case "$stale_cmd" in
      *mihomo*"$stale_dir"*|*clash*"$stale_dir"*|*"$stale_dir"*mihomo*|*"$stale_dir"*clash*)
        kill -TERM "$stale_pid" 2>/dev/null || true
        stale_wait=0
        while kill -0 "$stale_pid" 2>/dev/null && [ "$stale_wait" -lt 20 ]; do
          sleep 0.1
          stale_wait=$((stale_wait + 1))
        done
        if kill -0 "$stale_pid" 2>/dev/null; then
          kill -KILL "$stale_pid" 2>/dev/null || true
        fi
        ;;
    esac
  fi
  rm -rf -- "$stale_dir"
done
TMP="$(mktemp -d "$BASE/candidate.XXXXXX")"
chmod 700 "$TMP"

PORTS="$(python3 - <<'PY'
import socket

for _ in range(64):
    first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        first.bind(("127.0.0.1", 0))
        port = int(first.getsockname()[1])
        if port > 64535:
            continue
        second.bind(("127.0.0.1", port + 1000))
        print(f"{{port}} {{port + 1000}}")
        break
    except OSError:
        continue
    finally:
        first.close()
        second.close()
else:
    raise SystemExit("没有可用的隔离 loopback 端口")
PY
)"
set -- $PORTS
PORT="$1"
CONTROLLER_PORT="$2"
case "$PORT:$CONTROLLER_PORT" in
  *[!0-9:]*) echo "隔离端口分配结果无效" >&2; exit 13 ;;
esac

cat > "$TMP/config.template.yaml"
python3 - "$TMP/config.template.yaml" "$TMP/config.yaml" "$PORT" "$CONTROLLER_PORT" <<'PY'
import pathlib
import sys

source, target, port, controller_port = sys.argv[1:]
text = pathlib.Path(source).read_text(encoding="utf-8")
if text.count("__API_SWITCHER_CANDIDATE_PORT__") != 1:
    raise SystemExit("隔离配置 mixed-port 占位符无效")
if text.count("__API_SWITCHER_CANDIDATE_CONTROLLER_PORT__") != 1:
    raise SystemExit("隔离配置 controller 占位符无效")
text = text.replace("__API_SWITCHER_CANDIDATE_PORT__", port)
text = text.replace("__API_SWITCHER_CANDIDATE_CONTROLLER_PORT__", controller_port)
pathlib.Path(target).write_text(text, encoding="utf-8")
PY
chmod 600 "$TMP/config.yaml"
"$BINARY" -d "$TMP" -f "$TMP/config.yaml" >"$TMP/mihomo.log" 2>&1 &
CANDIDATE_PID=$!
printf '%s\n' "$CANDIDATE_PID" > "$TMP/mihomo.pid"
chmod 600 "$TMP/mihomo.pid"

python3 - "$PORT" "$CANDIDATE_PID" <<'PY'
import os
import socket
import sys
import time

port = int(sys.argv[1])
pid = int(sys.argv[2])
deadline = time.monotonic() + 8.0
while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
    except OSError:
        raise SystemExit("隔离候选进程启动失败")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(0)
    time.sleep(0.1)
raise SystemExit("隔离候选端口未在时限内监听")
PY

{strict_probe}
"""


def _probe_ai_proxy_candidate_isolated(
    client,
    home: str,
    proxy_node: dict,
    *,
    timeout: int = 8,
) -> tuple[RemoteAIProxyProbeResult, ...]:
    config = _build_isolated_candidate_config(proxy_node)
    try:
        timeout_value = max(1, min(60, int(timeout)))
    except (TypeError, ValueError):
        timeout_value = 8
    command = _build_isolated_candidate_probe_command(home, timeout_value)
    outer_timeout = min(
        210,
        max(40, (REMOTE_AI_STABILITY_ROUNDS + 1) * timeout_value + 24),
    )
    status, stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        timeout=outer_timeout,
        input_data=config,
        log_command=False,
    )
    if status != 0:
        detail = (stderr or stdout or "").strip().splitlines()
        clean_detail = detail[-1][:240] if detail else str(status)
        if status == 12:
            raise RemoteMihomoCoreMissingError(f"隔离候选验证失败: {clean_detail}")
        raise RuntimeError(f"隔离候选验证失败: {clean_detail}")
    results = _parse_remote_probe_output(stdout)
    expected = REMOTE_AI_STABILITY_EXPECTED_PROBES
    if any(proxy_region_is_hong_kong(item.detail) for item in results):
        raise RuntimeError("隔离候选实际出口为香港，已拒绝自动应用")
    expected_labels = {label for label, _url in REMOTE_AI_STABILITY_TARGETS}
    label_counts = {
        label: sum(1 for item in results if item.label == label)
        for label in expected_labels
    }
    complete_rounds = all(
        label_counts.get(label) == REMOTE_AI_STABILITY_ROUNDS
        for label in expected_labels
    )
    compact_count = sum(
        1 for item in results if item.label == REMOTE_CODEX_COMPACT_PROBE_LABEL
    )
    if (
        len(results) != expected
        or not complete_rounds
        or compact_count != 1
        or sum(1 for item in results if item.ok) != expected
    ):
        raise RuntimeError(
            f"隔离候选稳定性未通过: {sum(1 for item in results if item.ok)}/{expected} 可达"
        )
    return results


def probe_ai_proxy_candidate_isolated(
    ssh_name: str,
    proxy_text: str,
    timeout: int = 8,
) -> str:
    """Validate an automatic candidate without touching the managed proxy state."""

    proxy_node = parse_proxy_node(proxy_text)
    if (
        proxy_node_region(proxy_node) == "香港"
        or proxy_region_is_hong_kong(str(proxy_node.get("name") or ""))
    ):
        raise RuntimeError("香港节点仅允许手动选择，已拒绝自动候选验证")
    _ssh_profile, client = _connect_ssh(ssh_name)
    results = _probe_ai_proxy_candidate_isolated(
        client,
        remote_config._remote_home(client),
        proxy_node,
        timeout=timeout,
    )
    expected = REMOTE_AI_STABILITY_EXPECTED_PROBES
    return (
        f"{ssh_name}: 隔离候选 AI 稳定性 {expected}/{expected} 可达；"
        + "；".join(item.summary() for item in results)
    )


def _reconcile_dead_ai_proxy_runtime(
    client,
    home: str,
    mixed_port: int,
    *,
    conflict_action: str = "部署",
) -> dict[str, str]:
    """Repair or identify stale managed runtime state without touching live foreign proxies."""

    command = _build_dead_proxy_reconcile_command(home, mixed_port)
    status, stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        timeout=30,
        log_command=False,
    )
    if status != 0:
        detail = (stderr or stdout or str(status)).strip()
        raise RuntimeError(f"远端死代理识别失败: {detail}")
    values = _parse_key_values(stdout)
    if values.get("conflict") == "yes":
        reason = values.get("reason") or "unknown_owner"
        if reason == "managed_proxy_on_other_port":
            configured_port = values.get("configured_port") or "其他"
            raise RuntimeError(
                f"检测到本工具代理正在端口 {configured_port} 正常运行；"
                f"拒绝将同一受管配置目录覆盖到端口 {mixed_port}，正常代理未终止"
            )
        action_label = str(conflict_action or "操作").strip()[:20] or "操作"
        if reason in {"proxy_starting", "process_age_unknown"}:
            protection_reason = (
                "仍在启动保护期"
                if reason == "proxy_starting"
                else "启动时长无法确认"
            )
            raise RuntimeError(
                f"检测到本工具受管代理进程存在，但 {mixed_port} 端口尚未监听且{protection_reason}；"
                f"为避免中断正在建立的 Codex/Claude 连接，已停止{action_label}，请稍后重试"
            )
        listeners = values.get("listener_pids") or "无法读取"
        reason_labels = {
            "foreign_listener": "监听进程不属于本工具",
            "unowned_config": "监听进程使用的配置没有本工具标记",
            "multiple_managed_listeners": "检测到多个受管监听进程",
            "unknown_owner": "无法确认监听进程身份",
            "listener_check_unavailable": "远端缺少端口识别工具",
            "proxy_starting": "本工具受管代理仍在启动保护期",
            "process_age_unknown": "无法确认本工具受管代理的启动时长",
        }
        raise RuntimeError(
            f"远端端口 {mixed_port} 已占用且不能安全自动清理："
            f"{reason_labels.get(reason, reason)}（PID: {listeners}）。"
            f"为避免误杀，已停止{action_label}"
        )
    return values


def _ensure_remote_mihomo_core_on_client(
    client,
    home: str,
    *,
    force_check: bool = False,
) -> str:
    command = _build_ensure_mihomo_command(home, force_check=force_check)
    status, stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        timeout=300,
        log_command=False,
    )
    if status != 0:
        detail = (stderr or stdout or str(status)).strip().splitlines()
        raise RuntimeError(f"远端 mihomo 内核准备失败: {(detail[-1] if detail else status)[:300]}")
    values = _parse_key_values(stdout)
    return values.get("kernel_version") or "mihomo 内核已准备"


def ensure_remote_mihomo_core(ssh_name: str, *, force_check: bool = False) -> str:
    """Install or safely update only the tool-managed remote mihomo binary."""

    _ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    detail = _ensure_remote_mihomo_core_on_client(client, home, force_check=force_check)
    return f"{ssh_name}: {detail}"


def _probe_ai_proxy_candidate_with_core_bootstrap(
    ssh_name: str,
    proxy_text: str,
    timeout: int = 8,
) -> str:
    try:
        return probe_ai_proxy_candidate_isolated(ssh_name, proxy_text, timeout=timeout)
    except RemoteMihomoCoreMissingError:
        # A clean server cannot run an isolated candidate before the promised
        # automatic core installation.  Install only the managed binary, then
        # retry without touching proxy config, environment, or login state.
        ensure_remote_mihomo_core(ssh_name)
        return probe_ai_proxy_candidate_isolated(ssh_name, proxy_text, timeout=timeout)


def install_ai_proxy(
    ssh_name: str,
    proxy_text: str,
    mixed_port: int = 7890,
    *,
    strict_privacy: bool | None = None,
    fallback_nodes: tuple[dict, ...] | list[dict] | None = None,
) -> str:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    proxy_node = parse_proxy_node(proxy_text)
    ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    config_dir = posixpath.join(home, ".config", "mihomo")
    app_dir = posixpath.join(home, ".config", "api-switcher")
    local_bin_dir = posixpath.join(home, ".local", "bin")
    config_path = posixpath.join(config_dir, "config.yaml")
    env_path = posixpath.join(app_dir, "ai-proxy.env")
    start_path = posixpath.join(app_dir, "start-ai-proxy.sh")

    old_config = ssh_manager.read_remote_file(client, config_path) or ""
    effective_strict_privacy = _resolve_managed_strict_privacy(strict_privacy, old_config)
    reconcile = _reconcile_dead_ai_proxy_runtime(client, home, mixed_port)
    if reconcile.get("working") == "yes":
        reload_message = reload_ai_proxy(
            ssh_name,
            proxy_text,
            mixed_port,
            persist_selection=False,
            strict_privacy=effective_strict_privacy,
            fallback_nodes=fallback_nodes,
        )
        repair_note = (
            "；部署前已修复受管代理 PID 状态，正常工作的代理未终止"
            if reconcile.get("repaired_pid") == "yes"
            else "；部署前检测到正常工作的受管代理，已改用热更新且未终止进程"
        )
        return reload_message + repair_note

    cleanup_note = ""
    if reconcile.get("dirty") == "yes":
        cleanup_ai_proxy(ssh_name, mixed_port, include_legacy_config=False)
        cleanup_note = "；部署前已自动清理确认失效的受管代理与死代理环境"
    ssh_manager.write_remote_file(
        client,
        config_path,
        build_mihomo_config(
            proxy_node,
            mixed_port,
            fallback_proxy_nodes=fallback_nodes,
            health_checked_group=True,
            resilient_transport=True,
            mainland_dns=True,
            strict_privacy=effective_strict_privacy,
        ),
        file_mode=0o600,
    )
    ssh_manager.write_remote_file(client, env_path, _build_env_file(mixed_port), file_mode=0o600)
    ssh_manager.write_remote_file(
        client,
        start_path,
        _build_start_script(config_dir, app_dir, local_bin_dir, mixed_port),
        file_mode=0o700,
    )

    command = _build_install_command(home, config_dir, app_dir, local_bin_dir, start_path, mixed_port)
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, command, timeout=360, log_command=False)
    if status != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"远端 AI 代理配置失败: {detail or status}")
    _write_shell_profile_block(client, home, env_path, start_path, mixed_port)
    vscode_targets = _write_vscode_proxy_entrypoints(client, env_path, start_path, mixed_port)
    result = (stdout or "").strip().splitlines()
    suffix = f"；{result[-1]}" if result else ""
    return (
        f"AI 代理已部署到 {ssh_name}: http://127.0.0.1:{mixed_port}"
        f"{suffix}{cleanup_note}；内核故障切换池 {1 + len(fallback_nodes or ())} 个节点；"
        f"已写入 VS Code Remote/Codex/Claude Code 环境入口 {vscode_targets} 处；"
        + (
            "应用层严格隐私已开启（非 VPN/TUN）"
            if effective_strict_privacy
            else "兼容分流已保持（允许未命中流量 DIRECT）"
        )
    )


def install_ai_proxy_verified(
    ssh_name: str,
    proxy_text: str,
    candidate_nodes=None,
    mixed_port: int = 7890,
    max_candidates: int = 10,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
    strict_privacy: bool | None = None,
) -> str:
    proxy_status = inspect_ai_proxy(ssh_name, mixed_port)
    if proxy_status.running:
        # Never restart an existing working proxy through the deployment path.
        # Automatic reload probes in isolation, rechecks the current node for
        # races, and force-reloads the previous config if applying fails.
        return reload_ai_proxy_verified(
            ssh_name,
            proxy_text,
            candidate_nodes,
            mixed_port=mixed_port,
            max_candidates=max_candidates,
            quality_results=quality_results,
            automatic_update=True,
            **_strict_privacy_call_kwargs(strict_privacy),
        )
    requested_node = parse_proxy_node(proxy_text)
    requested_key = proxy_node_key(requested_node)
    requested_fallback_nodes = _remote_proxy_fallback_nodes(
        requested_node,
        candidate_nodes,
        quality_results,
    )
    tried = []
    try:
        requested_probe = _probe_ai_proxy_candidate_with_core_bootstrap(ssh_name, proxy_text)
    except Exception as exc:
        requested_probe = ""
        tried.append(f"{describe_proxy_node(requested_node)}: {exc}")
    if requested_probe and _probe_stability_summary_all_ok(requested_probe):
        install_message = install_ai_proxy(
            ssh_name,
            proxy_text,
            mixed_port,
            fallback_nodes=requested_fallback_nodes,
            **_strict_privacy_call_kwargs(strict_privacy),
        )
        return f"{install_message}；隔离验证通过: {_compact_probe_summary(requested_probe)}"

    candidates = automatic_proxy_subscription_nodes(candidate_nodes, quality_results)
    if not candidates:
        return f"{ssh_name}: 候选未通过隔离稳定性验证，未修改正式代理"

    try:
        latencies = measure_proxy_node_latencies_on_server(
            ssh_name,
            candidates,
            timeout=3.0,
            attempts=2,
            max_workers=20,
        )
    except Exception as exc:
        return f"{ssh_name}: 候选未通过隔离稳定性验证；自动换节点测速失败: {exc}；未修改正式代理"

    ranked = []
    for item in ranked_proxy_subscription_nodes_for_ai_probe(candidates, quality_results, latencies):
        key = proxy_subscription_node_key(item)
        if key == requested_key:
            continue
        result = latencies.get(key)
        latency = proxy_node_latency_ms(result)
        if latency is None or not proxy_node_latency_ok(result):
            continue
        ranked.append((latency, item, result))

    attempts = min(max(1, _int_or_default(max_candidates, 10)), len(ranked))
    for latency, item, result in ranked[:attempts]:
        node_summary = describe_proxy_node(item.node)
        latency_label = proxy_node_latency_label(result)
        try:
            candidate_probe = _probe_ai_proxy_candidate_with_core_bootstrap(
                ssh_name,
                format_proxy_node(item.node),
            )
        except Exception as exc:
            tried.append(f"{node_summary} {latency_label}: {exc}")
            continue
        if _probe_stability_summary_all_ok(candidate_probe):
            try:
                install_ai_proxy(
                    ssh_name,
                    format_proxy_node(item.node),
                    mixed_port,
                    fallback_nodes=_remote_proxy_fallback_nodes(
                        item.node,
                        candidate_nodes,
                        quality_results,
                    ),
                    **_strict_privacy_call_kwargs(strict_privacy),
                )
            except Exception as exc:
                tried.append(f"{node_summary} {latency_label}: 正式应用失败: {exc}")
                continue
            set_proxy_subscription_selected_node(item.node)
            return (
                f"{ssh_name}: 原节点验证失败，已自动切换到 {node_summary}（远端 TCP {latency_label}）；"
                f"验证通过: {_compact_probe_summary(candidate_probe)}"
            )
        tried.append(f"{node_summary} {latency_label}: {_probe_summary_counts(candidate_probe)}")

    tried_summary = "；".join(tried[:3])
    suffix = f"；尝试摘要: {tried_summary}" if tried_summary else ""
    return (
        f"{ssh_name}: 自动尝试 {1 + attempts} 个节点仍未通过隔离 3/3×4 服务 + compact 验证，"
        f"未修改正式代理{suffix}"
    )


def reload_ai_proxy(
    ssh_name: str,
    proxy_text: str,
    mixed_port: int = 7890,
    *,
    profile_id: str = "",
    persist_selection: bool = True,
    strict_privacy: bool | None = None,
    fallback_nodes: tuple[dict, ...] | list[dict] | None = None,
) -> str:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    proxy_node = parse_proxy_node(proxy_text)
    status = inspect_ai_proxy(ssh_name, mixed_port)
    if not status.running:
        return f"{ssh_name}: AI 代理未运行，已跳过热更新"

    _ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    config_path = posixpath.join(home, ".config", "mihomo", "config.yaml")
    old_config = ssh_manager.read_remote_file(client, config_path) or ""
    effective_strict_privacy = _resolve_managed_strict_privacy(strict_privacy, old_config)
    if fallback_nodes is None:
        fallback_nodes = _existing_remote_proxy_fallback_nodes(
            old_config,
            proxy_node,
        )
    new_config = build_mihomo_config(
        proxy_node,
        mixed_port,
        fallback_proxy_nodes=fallback_nodes,
        health_checked_group=True,
        resilient_transport=True,
        mainland_dns=True,
        strict_privacy=effective_strict_privacy,
    )
    if old_config.strip() == new_config.strip():
        if persist_selection:
            if profile_id:
                set_proxy_subscription_selected_node(proxy_node, profile_id=profile_id)
            else:
                set_proxy_subscription_selected_node(proxy_node)
        repair_suffix = _repair_remote_proxy_integrations(
            client,
            home,
            mixed_port,
            status,
        )
        return f"{ssh_name}: 运行节点已是最新配置，无需热更新{repair_suffix}"

    ssh_manager.write_remote_file(client, config_path, new_config, file_mode=0o600)
    command = _build_reload_command(config_path, mixed_port)
    status_code, stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        timeout=20,
        log_command=False,
    )
    if status_code != 0:
        restore_error = ""
        if old_config:
            ssh_manager.write_remote_file(client, config_path, old_config, file_mode=0o600)
            restore_status, restore_stdout, restore_stderr = ssh_manager.execute_command_with_status(
                client,
                command,
                timeout=20,
                log_command=False,
            )
            if restore_status != 0:
                restore_error = (restore_stderr or restore_stdout or str(restore_status)).strip()
        detail = (stderr or stdout or "").strip()
        if not old_config:
            restore_suffix = "；未读取到可恢复的旧配置"
        elif restore_error:
            restore_suffix = f"；旧配置已写回，但强制重载失败: {restore_error}"
        else:
            restore_suffix = "；已强制重载旧配置"
        raise RuntimeError(
            f"{ssh_name}: 当前远端代理不支持无感热更新或控制口不可用: "
            f"{detail or status_code}{restore_suffix}"
        )
    if persist_selection:
        if profile_id:
            set_proxy_subscription_selected_node(proxy_node, profile_id=profile_id)
        else:
            set_proxy_subscription_selected_node(proxy_node)
    repair_suffix = _repair_remote_proxy_integrations(
        client,
        home,
        mixed_port,
        status,
    )
    privacy_label = "应用层严格隐私" if effective_strict_privacy else "兼容分流"
    return (
        f"{ssh_name}: 已热更新远端 AI 代理节点为 {describe_proxy_node(proxy_node)}"
        f"；当前模式: {privacy_label}；内核故障切换池 {1 + len(fallback_nodes or ())} 个节点"
        f"{repair_suffix}"
    )


def _reload_ai_proxy_automatically_after_isolated_probe(
    ssh_name: str,
    requested_node: dict,
    candidate_nodes,
    mixed_port: int,
    max_candidates: int,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None,
    profile_id: str,
    persist_selection: bool,
    strict_privacy: bool | None,
) -> str:
    """Probe automatic candidates in isolation and mutate managed state at most once."""

    proxy_status = inspect_ai_proxy(ssh_name, mixed_port)
    if not proxy_status.running:
        return f"{ssh_name}: AI 代理未运行，已跳过热更新"

    try:
        original_node = _read_remote_managed_proxy_node(ssh_name, mixed_port)
    except Exception as exc:
        return f"{ssh_name}: 无法读取自动更新前节点，已保留当前运行节点: {exc}"
    if not original_node:
        return f"{ssh_name}: 未读取到自动更新前节点，已保留当前运行节点"
    original_key = proxy_node_key(original_node)
    requested_key = proxy_node_key(requested_node)
    tried: list[str] = []

    def validate_candidate(node: dict, detail: str = "") -> str | None:
        label = describe_proxy_node(node)
        try:
            summary = probe_ai_proxy_candidate_isolated(
                ssh_name,
                format_proxy_node(node),
            )
        except Exception as exc:
            tried.append(f"{label}{detail}: {exc}")
            return None
        if not _probe_stability_summary_all_ok(summary):
            tried.append(f"{label}{detail}: {_probe_summary_counts(summary)}")
            return None
        return summary

    def apply_candidate(node: dict) -> str:
        try:
            latest_node = _read_remote_managed_proxy_node(ssh_name, mixed_port)
        except Exception as exc:
            raise RuntimeError(f"正式应用前无法复核当前节点: {exc}") from exc
        if not latest_node:
            raise RuntimeError("正式应用前未读取到当前节点")
        if proxy_node_key(latest_node) != original_key:
            raise RuntimeError("隔离验证期间当前节点已变化，拒绝覆盖手动更新")
        text = format_proxy_node(node)
        fallback_nodes = _remote_proxy_fallback_nodes(
            node,
            candidate_nodes,
            quality_results,
        )
        if profile_id or not persist_selection:
            return reload_ai_proxy(
                ssh_name,
                text,
                mixed_port,
                profile_id=profile_id,
                persist_selection=persist_selection,
                fallback_nodes=fallback_nodes,
                **_strict_privacy_call_kwargs(strict_privacy),
            )
        return reload_ai_proxy(
            ssh_name,
            text,
            mixed_port,
            fallback_nodes=fallback_nodes,
            **_strict_privacy_call_kwargs(strict_privacy),
        )

    requested_probe = validate_candidate(requested_node)
    if requested_probe is not None:
        try:
            reload_message = apply_candidate(requested_node)
        except Exception as exc:
            return f"{ssh_name}: 候选隔离验证通过，但正式热更新失败: {exc}"
        return f"{reload_message}；隔离验证通过: {_compact_probe_summary(requested_probe)}"

    candidates = tuple(
        item
        for item in automatic_proxy_subscription_nodes(candidate_nodes, quality_results)
        if proxy_subscription_node_key(item) != requested_key
    )
    if not candidates:
        tried_summary = "；".join(tried[:3])
        suffix = f": {tried_summary}" if tried_summary else ""
        return f"{ssh_name}: 自动热更新候选未通过隔离验证，已保留当前运行节点{suffix}"

    try:
        latencies = measure_proxy_node_latencies_on_server(
            ssh_name,
            candidates,
            timeout=3.0,
            attempts=2,
            max_workers=20,
        )
    except Exception as exc:
        return f"{ssh_name}: 自动候选隔离验证未通过，后备节点测速失败，已保留当前运行节点: {exc}"

    ranked = []
    for item in ranked_proxy_subscription_nodes_for_ai_probe(candidates, quality_results, latencies):
        key = proxy_subscription_node_key(item)
        quality = (quality_results or {}).get(key)
        if (
            proxy_node_quality_decisive_for_ai_proxy(quality)
            and not proxy_node_quality_for_ai_proxy_ok(quality)
        ):
            continue
        latency_result = latencies.get(key)
        latency = proxy_node_latency_ms(latency_result)
        if latency is None or not proxy_node_latency_ok(latency_result):
            continue
        ranked.append((latency, item, latency_result))

    attempt_count = min(max(1, _int_or_default(max_candidates, 10)), len(ranked))
    for _latency, item, latency_result in ranked[:attempt_count]:
        latency_label = f"（远端 TCP {proxy_node_latency_label(latency_result)}）"
        candidate_probe = validate_candidate(item.node, latency_label)
        if candidate_probe is None:
            continue
        try:
            reload_message = apply_candidate(item.node)
        except Exception as exc:
            return f"{ssh_name}: 后备候选隔离验证通过，但正式热更新失败: {exc}"
        return (
            f"{reload_message}；后备候选隔离验证通过: "
            f"{_compact_probe_summary(candidate_probe)}"
        )

    tried_summary = "；".join(tried[:3])
    suffix = f"；尝试摘要: {tried_summary}" if tried_summary else ""
    return (
        f"{ssh_name}: 自动尝试 {1 + attempt_count} 个候选仍未通过隔离 3/3×4 服务 + compact 验证，"
        f"已保留当前运行节点{suffix}"
    )


def reload_ai_proxy_verified(
    ssh_name: str,
    proxy_text: str,
    candidate_nodes=None,
    mixed_port: int = 7890,
    max_candidates: int = 10,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
    profile_id: str = "",
    persist_selection: bool = True,
    *,
    automatic_update: bool = False,
    strict_privacy: bool | None = None,
) -> str:
    requested_node = parse_proxy_node(proxy_text)
    if automatic_update:
        return _reload_ai_proxy_automatically_after_isolated_probe(
            ssh_name,
            requested_node,
            candidate_nodes,
            mixed_port,
            max_candidates,
            quality_results,
            profile_id,
            persist_selection,
            strict_privacy,
        )
    requested_key = proxy_node_key(requested_node)
    requested_fallback_nodes = _remote_proxy_fallback_nodes(
        requested_node,
        candidate_nodes,
        quality_results,
    )
    try:
        original_node = _read_remote_managed_proxy_node(ssh_name, mixed_port)
    except Exception:
        original_node = None
    try:
        if profile_id or not persist_selection:
            reload_message = reload_ai_proxy(
                ssh_name,
                proxy_text,
                mixed_port,
                profile_id=profile_id,
                persist_selection=persist_selection,
                fallback_nodes=requested_fallback_nodes,
                **_strict_privacy_call_kwargs(strict_privacy),
            )
        else:
            reload_message = reload_ai_proxy(
                ssh_name,
                proxy_text,
                mixed_port,
                fallback_nodes=requested_fallback_nodes,
                **_strict_privacy_call_kwargs(strict_privacy),
            )
    except Exception as exc:
        return f"{ssh_name}: 自动更新跳过，{exc}"
    if "跳过" in reload_message:
        return reload_message

    try:
        probe_message = (
            probe_ai_proxy_stability(ssh_name, mixed_port)
            if automatic_update
            else probe_ai_proxy(ssh_name, mixed_port)
        )
    except Exception as exc:
        restore_suffix = _restore_remote_proxy_node_after_failed_update(
            ssh_name,
            original_node,
            requested_node,
            mixed_port,
            profile_id=profile_id,
            persist_selection=persist_selection,
            **_strict_privacy_call_kwargs(strict_privacy),
        )
        return f"{reload_message}；热更新后验证执行失败: {exc}{restore_suffix}"
    initial_probe_ok = (
        _probe_stability_summary_all_ok(probe_message)
        if automatic_update
        else _probe_summary_all_ok(probe_message)
    )
    if initial_probe_ok:
        return f"{reload_message}；验证通过: {_compact_probe_summary(probe_message)}"

    candidates = tuple(
        item
        for item in automatic_proxy_subscription_nodes(candidate_nodes, quality_results)
        if proxy_subscription_node_key(item) != requested_key
    )
    if not candidates:
        restore_suffix = _restore_remote_proxy_node_after_failed_update(
            ssh_name,
            original_node,
            requested_node,
            mixed_port,
            profile_id=profile_id,
            persist_selection=persist_selection,
            **_strict_privacy_call_kwargs(strict_privacy),
        )
        return f"{reload_message}；验证失败: {_compact_probe_summary(probe_message)}{restore_suffix}"

    try:
        latencies = measure_proxy_node_latencies_on_server(
            ssh_name,
            candidates,
            timeout=3.0,
            attempts=2,
            max_workers=20,
        )
    except Exception as exc:
        restore_suffix = _restore_remote_proxy_node_after_failed_update(
            ssh_name,
            original_node,
            requested_node,
            mixed_port,
            profile_id=profile_id,
            persist_selection=persist_selection,
            **_strict_privacy_call_kwargs(strict_privacy),
        )
        return f"{reload_message}；验证失败: {_compact_probe_summary(probe_message)}；自动换节点测速失败: {exc}{restore_suffix}"

    ranked = []
    for item in ranked_proxy_subscription_nodes_for_ai_probe(candidates, quality_results, latencies):
        key = proxy_subscription_node_key(item)
        quality = (quality_results or {}).get(key)
        if (
            proxy_node_quality_decisive_for_ai_proxy(quality)
            and not proxy_node_quality_for_ai_proxy_ok(quality)
        ):
            continue
        result = latencies.get(key)
        latency = proxy_node_latency_ms(result)
        if latency is None or not proxy_node_latency_ok(result):
            continue
        ranked.append((latency, item, result))

    attempts = min(max(1, _int_or_default(max_candidates, 10)), len(ranked))
    for _latency, item, result in ranked[:attempts]:
        try:
            if profile_id or not persist_selection:
                reload_ai_proxy(
                    ssh_name,
                    format_proxy_node(item.node),
                    mixed_port,
                    profile_id=profile_id,
                    persist_selection=persist_selection,
                    fallback_nodes=_remote_proxy_fallback_nodes(
                        item.node,
                        candidate_nodes,
                        quality_results,
                    ),
                    **_strict_privacy_call_kwargs(strict_privacy),
                )
            else:
                reload_ai_proxy(
                    ssh_name,
                    format_proxy_node(item.node),
                    mixed_port,
                    fallback_nodes=_remote_proxy_fallback_nodes(
                        item.node,
                        candidate_nodes,
                        quality_results,
                    ),
                    **_strict_privacy_call_kwargs(strict_privacy),
                )
            candidate_probe = probe_ai_proxy_stability(ssh_name, mixed_port)
        except Exception:
            continue
        if _probe_stability_summary_all_ok(candidate_probe):
            if persist_selection:
                if profile_id:
                    set_proxy_subscription_selected_node(item.node, profile_id=profile_id)
                else:
                    set_proxy_subscription_selected_node(item.node)
            return (
                f"{ssh_name}: 原热更新节点验证失败，已无重启切换到 {describe_proxy_node(item.node)}"
                f"（远端 TCP {proxy_node_latency_label(result)}）；"
                f"验证通过: {_compact_probe_summary(candidate_probe)}"
            )
    restore_suffix = _restore_remote_proxy_node_after_failed_update(
        ssh_name,
        original_node,
        requested_node,
        mixed_port,
        profile_id=profile_id,
        persist_selection=persist_selection,
        **_strict_privacy_call_kwargs(strict_privacy),
    )
    return f"{reload_message}；验证失败: {_compact_probe_summary(probe_message)}；自动尝试 {attempts} 个节点仍未 3/3 可达{restore_suffix}"


def refresh_running_ai_proxy_from_subscription(
    ssh_name: str,
    nodes,
    mixed_port: int = 7890,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
    profile_id: str = "",
    persist_selection: bool = True,
    strict_privacy: bool | None = None,
) -> str:
    status = inspect_ai_proxy(ssh_name, mixed_port)
    if not status.running:
        return f"{ssh_name}: AI 代理未运行，已跳过订阅热更新"
    candidates = tuple(item for item in (nodes or []) if isinstance(item, ProxySubscriptionNode))
    if not candidates:
        return f"{ssh_name}: 订阅里没有可用节点，已跳过热更新"
    current_node = _read_remote_managed_proxy_node(ssh_name, mixed_port)
    chosen = (
        _find_matching_subscription_node(candidates, current_node, quality_results)
        if current_node
        else None
    )
    if chosen is None:
        automatic_candidates = automatic_proxy_subscription_nodes(candidates, quality_results)
        if not automatic_candidates:
            return (
                f"{ssh_name}: 订阅已刷新，但仅有香港节点可候选；"
                "香港仅允许手动选择，已保留当前运行节点"
            )
        try:
            latencies = measure_proxy_node_latencies_on_server(
                ssh_name,
                automatic_candidates,
                timeout=3.0,
                attempts=2,
                max_workers=20,
            )
        except Exception as exc:
            return f"{ssh_name}: 订阅已刷新，但远端节点测速失败，已保留当前运行节点: {exc}"
        chosen, reason = best_proxy_subscription_node_for_hot_update(
            automatic_candidates,
            quality_results,
            latencies,
        )
        if chosen is None and reason == "quality_rejected":
            return f"{ssh_name}: 订阅已刷新，但所有可连节点都有明确的不合格质量证据，已保留当前运行节点"
        if chosen is None:
            return f"{ssh_name}: 订阅已刷新，但没有测到可连节点，已保留当前运行节点"
    return reload_ai_proxy_verified(
        ssh_name,
        format_proxy_node(chosen.node),
        candidates,
        mixed_port=mixed_port,
        quality_results=quality_results,
        profile_id=profile_id,
        persist_selection=persist_selection,
        automatic_update=True,
        **_strict_privacy_call_kwargs(strict_privacy),
    )


def inspect_ai_proxy(ssh_name: str, mixed_port: int = 7890) -> RemoteAIProxyStatus:
    _ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    config_path = posixpath.join(home, ".config", "mihomo", "config.yaml")
    env_path = posixpath.join(home, ".config", "api-switcher", "ai-proxy.env")
    start_path = posixpath.join(home, ".config", "api-switcher", "start-ai-proxy.sh")
    pid_path = posixpath.join(home, ".config", "api-switcher", "ai-proxy.pid")
    core_path = posixpath.join(home, ".local", "bin", "mihomo")
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    shell_paths = " ".join(shlex.quote(path) for path in _shell_proxy_profile_paths(home))
    vscode_paths = " ".join(
        shlex.quote(remote_config._expand_remote_path(client, path))
        for path in VSCODE_SERVER_ENV_SETUP_PATHS
    )
    command = f"""
CONFIG={shlex.quote(config_path)}
CONFIG_DIR={shlex.quote(posixpath.dirname(config_path))}
ENV_FILE={shlex.quote(env_path)}
START_SCRIPT={shlex.quote(start_path)}
PID_FILE={shlex.quote(pid_path)}
CORE_BIN={shlex.quote(core_path)}
PORT={mixed_port}
installed=no
running=no
pid_running=no
pid_managed=unknown
port_listening=unknown
config_present=no
config_owned=no
config_legacy=no
if [ -s "$CONFIG" ]; then
  config_present=yes
  if grep -q "{AI_PROXY_CONFIG_MARKER}" "$CONFIG" 2>/dev/null || (grep -q "AI-PROXY" "$CONFIG" 2>/dev/null && grep -q "chatgpt.com" "$CONFIG" 2>/dev/null); then
    config_owned=yes
    installed=yes
  elif grep -Eq "^[[:space:]]*(port|socks-port|mixed-port|proxies|proxy-groups|rules):" "$CONFIG" 2>/dev/null || grep -q "chatgpt.com" "$CONFIG" 2>/dev/null; then
    config_legacy=yes
  fi
fi
if [ -s "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*) pid_managed=no ;;
    *)
      if kill -0 "$pid" 2>/dev/null; then
        pid_running=yes
        if command -v ps >/dev/null 2>&1; then
          cmd="$(ps -p "$pid" -o comm= -o args= 2>/dev/null || true)"
          case "$cmd" in
            *mihomo*"$CONFIG_DIR"*|*clash*"$CONFIG_DIR"*) pid_managed=yes ;;
            *) pid_managed=no ;;
          esac
        fi
      fi
      ;;
  esac
fi
if command -v ss >/dev/null 2>&1; then
  port_listening=no
  ss -ltn 2>/dev/null | grep -q ":$PORT " && port_listening=yes || true
elif command -v netstat >/dev/null 2>&1; then
  port_listening=no
  netstat -ltn 2>/dev/null | grep -q ":$PORT " && port_listening=yes || true
fi
env_file=no
start_script=no
shell_entrypoints=0
vscode_entrypoints=0
kernel_version=""
kernel_source=""
running_kernel_version=""
running_kernel_source=""
[ -s "$ENV_FILE" ] && grep -q "HTTP_PROXY=http://127.0.0.1:$PORT" "$ENV_FILE" 2>/dev/null && env_file=yes
[ -x "$START_SCRIPT" ] && start_script=yes
for file in {shell_paths}; do
  [ -f "$file" ] && grep -q "# >>> API切换器 AI proxy >>>" "$file" 2>/dev/null && shell_entrypoints=$((shell_entrypoints + 1))
done
for file in {vscode_paths}; do
  [ -f "$file" ] && grep -q "{VSCODE_ENV_BLOCK_START}" "$file" 2>/dev/null && vscode_entrypoints=$((vscode_entrypoints + 1))
done
if [ -x "$CORE_BIN" ]; then
  kernel_source=managed
  kernel_version="$("$CORE_BIN" -v 2>&1 | head -n 1 | tr '\n\r' '  ' || true)"
else
  detected_core="$(command -v mihomo 2>/dev/null || command -v clash-meta 2>/dev/null || command -v clash 2>/dev/null || true)"
  if [ -n "$detected_core" ] && [ -x "$detected_core" ]; then
    kernel_source=system
    kernel_version="$("$detected_core" -v 2>&1 | head -n 1 | tr '\n\r' '  ' || true)"
  fi
fi
if [ "$pid_running" = "yes" ] && [ -n "${{pid:-}}" ] && [ -x "/proc/$pid/exe" ]; then
  running_kernel_version="$("/proc/$pid/exe" -v 2>&1 | head -n 1 | tr '\n\r' '  ' || true)"
  running_core_path="$(readlink "/proc/$pid/exe" 2>/dev/null || true)"
  case "$running_core_path" in
    "$CORE_BIN"|"$CORE_BIN (deleted)") running_kernel_source=managed ;;
    *) running_kernel_source=system ;;
  esac
fi
if [ "$config_owned" = "yes" ] && [ "$pid_running" = "yes" ] && [ "$pid_managed" = "yes" ] && [ "$port_listening" = "yes" ]; then
  running=yes
fi
printf 'installed=%s\\nrunning=%s\\npid_running=%s\\npid_managed=%s\\nport_listening=%s\\nenv_file=%s\\nstart_script=%s\\nshell_entrypoints=%s\\nvscode_entrypoints=%s\\nconfig_present=%s\\nconfig_owned=%s\\nconfig_legacy=%s\\nkernel_source=%s\\nkernel_version=%s\\nrunning_kernel_source=%s\\nrunning_kernel_version=%s\\nconfig=%s\\n' "$installed" "$running" "$pid_running" "$pid_managed" "$port_listening" "$env_file" "$start_script" "$shell_entrypoints" "$vscode_entrypoints" "$config_present" "$config_owned" "$config_legacy" "$kernel_source" "$kernel_version" "$running_kernel_source" "$running_kernel_version" "$CONFIG"
"""
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, command, timeout=20)
    if status != 0:
        raise RuntimeError((stderr or stdout or "远端 AI 代理状态检查失败").strip())
    values = _parse_key_values(stdout)
    managed_config = ""
    shell_entrypoint_count = _int_or_default(values.get("shell_entrypoints"), 0)
    vscode_entrypoint_count = _int_or_default(values.get("vscode_entrypoints"), 0)
    shell_entrypoints_ready = shell_entrypoint_count >= len(
        _shell_proxy_profile_paths(home)
    )
    vscode_entrypoints_ready = vscode_entrypoint_count >= len(
        VSCODE_SERVER_ENV_SETUP_PATHS
    )
    environment_ready = values.get("env_file") == "yes"
    start_script_ready = values.get("start_script") == "yes"
    if values.get("config_owned") == "yes":
        try:
            managed_config = ssh_manager.read_remote_file(client, config_path) or ""
        except Exception:
            managed_config = ""
    strict_marker_present = AI_PROXY_STRICT_PRIVACY_MARKER in managed_config
    strict_contract_valid = _managed_config_strict_privacy_enabled(managed_config)
    running_verified = all(
        (
            values.get("config_owned") == "yes",
            values.get("pid_running") == "yes",
            values.get("pid_managed") == "yes",
            values.get("port_listening") == "yes",
        )
    )
    detail_parts = []
    kernel_version = str(values.get("kernel_version") or "").strip()[:300]
    running_kernel_version = str(
        values.get("running_kernel_version") or ""
    ).strip()[:300]
    if running_kernel_version:
        running_source_label = (
            "受管"
            if values.get("running_kernel_source") == "managed"
            else "系统"
        )
        detail_parts.append(f"当前运行的{running_source_label}代理内核: {running_kernel_version}")
        if kernel_version and kernel_version != running_kernel_version:
            source_label = "受管" if values.get("kernel_source") == "managed" else "系统"
            detail_parts.append(
                f"已安装的{source_label}内核将在下次重启使用: {kernel_version}"
            )
    elif kernel_version:
        source_label = "受管" if values.get("kernel_source") == "managed" else "系统"
        detail_parts.append(f"{source_label}代理内核: {kernel_version}")
    else:
        detail_parts.append("未检测到可自检版本的 mihomo/clash 内核")
    if values.get("pid_running") == "yes" and values.get("pid_managed") == "no":
        detail_parts.append("pid 文件指向非本工具受管的 mihomo/clash 进程")
    elif values.get("pid_running") == "yes" and values.get("pid_managed") != "yes":
        detail_parts.append("无法确认 pid 进程是 mihomo/clash，已按未运行处理")
    if values.get("pid_running") == "yes" and values.get("port_listening") == "no":
        detail_parts.append("进程存在，但端口未监听")
    elif values.get("port_listening") not in {"yes", "no"}:
        detail_parts.append("远端缺少 ss/netstat，无法确认代理端口监听")
    elif values.get("pid_running") == "no" and values.get("port_listening") == "yes":
        detail_parts.append("端口已监听，但 pid 文件未更新")
    if values.get("config_owned") != "yes" and values.get("port_listening") == "yes":
        detail_parts.append("端口正在监听，但不是本工具配置")
    if values.get("config_present") == "yes" and values.get("config_owned") != "yes":
        if values.get("config_legacy") == "yes":
            detail_parts.append("检测到旧/非本工具 mihomo 配置，未计入 AI 代理")
        else:
            detail_parts.append("检测到非本工具 mihomo 配置，未计入 AI 代理")
    if values.get("config_owned") == "yes":
        if strict_contract_valid:
            detail_parts.append(
                "磁盘受管配置符合应用层严格隐私契约（非 VPN/TUN）；"
                "本次状态检查不能单独证明当前进程内存已加载，"
                "最近一次成功部署/热更新后生效"
            )
        elif strict_marker_present:
            detail_parts.append("严格隐私标记存在，但 fail-closed/DNS/IPv6 配置已漂移")
        else:
            detail_parts.append("兼容分流允许 DIRECT（非 VPN/TUN）")
        if not environment_ready:
            detail_parts.append("远端代理环境文件缺失或端口不匹配")
        if not start_script_ready:
            detail_parts.append("远端启动脚本缺失或不可执行")
        if shell_entrypoint_count <= 0:
            detail_parts.append("shell 启动入口未检测到")
        elif not shell_entrypoints_ready:
            detail_parts.append(
                f"shell 启动入口不完整（{shell_entrypoint_count}/{len(_shell_proxy_profile_paths(home))}）"
            )
        if vscode_entrypoint_count <= 0:
            detail_parts.append("VS Code Remote 启动入口未检测到")
        elif not vscode_entrypoints_ready:
            detail_parts.append(
                f"VS Code Remote 启动入口不完整（{vscode_entrypoint_count}/{len(VSCODE_SERVER_ENV_SETUP_PATHS)}）"
            )
    return RemoteAIProxyStatus(
        installed=values.get("config_owned") == "yes",
        running=running_verified,
        config_path=values.get("config") or config_path,
        proxy_url=f"http://127.0.0.1:{mixed_port}",
        detail="；".join(detail_parts),
        environment_ready=environment_ready,
        start_script_ready=start_script_ready,
        shell_entrypoints_ready=shell_entrypoints_ready,
        vscode_entrypoints_ready=vscode_entrypoints_ready,
    )


def _run_ai_proxy_probe(
    ssh_name: str,
    mixed_port: int,
    timeout: int,
    *,
    rounds: int,
    strict: bool,
) -> str:
    proxy_status = inspect_ai_proxy(ssh_name, mixed_port)
    if not proxy_status.running:
        return f"{ssh_name}: {proxy_status.summary()}；代理未运行，跳过 AI 连通性探测"

    _ssh_profile, client = _connect_ssh(ssh_name)
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    command = _build_probe_command(
        mixed_port,
        timeout,
        rounds=rounds,
        strict=strict,
    )
    target_count = len(REMOTE_AI_STABILITY_TARGETS if strict else REMOTE_AI_PROBE_TARGETS)
    expected_count = (
        REMOTE_AI_STABILITY_EXPECTED_PROBES
        if strict
        else rounds * target_count
    )
    exit_status, stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        # Each round executes its targets concurrently. Keep a hard outer
        # deadline even if a remote Python/socket implementation ignores an
        # individual request timeout.
        timeout=max(
            30,
            (rounds + (1 if strict else 0)) * max(1, timeout) + 20,
        ),
        log_command=False,
    )
    if exit_status != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"远端 AI 代理连通性测试失败: {detail or exit_status}")
    results = _parse_remote_probe_output(stdout)
    if not results:
        return f"{ssh_name}: 未得到连通性测试结果"
    ok_count = sum(1 for item in results if item.ok)
    metric = "AI 稳定性" if strict else "AI 连通性"
    message = (
        f"{ssh_name}: {proxy_status.summary()}；{metric} {ok_count}/{expected_count} 可达；"
        + "；".join(item.summary() for item in results)
    )
    if len(results) != expected_count:
        message += f"；探测结果不完整: 实收 {len(results)}/{expected_count}"
    if ok_count == 0:
        log_hint = _read_remote_ai_proxy_error_tail(client, remote_config._remote_home(client))
        if log_hint:
            message += f"；最近远端代理日志: {log_hint}"
    return message


def probe_ai_proxy(ssh_name: str, mixed_port: int = 7890, timeout: int = 8) -> str:
    return _run_ai_proxy_probe(
        ssh_name,
        mixed_port,
        timeout,
        rounds=1,
        strict=False,
    )


def probe_ai_proxy_stability(
    ssh_name: str,
    mixed_port: int = 7890,
    timeout: int = 8,
) -> str:
    """Fail-closed, credential-free gate for automatically selected nodes."""

    return _run_ai_proxy_probe(
        ssh_name,
        mixed_port,
        timeout,
        rounds=REMOTE_AI_STABILITY_ROUNDS,
        strict=True,
    )


def _execute_ai_proxy_cleanup(
    client,
    home: str,
    mixed_port: int,
    *,
    include_legacy_config: bool,
    stale_only: bool = False,
) -> dict[str, str]:
    command = _build_cleanup_command(
        home,
        mixed_port,
        include_legacy_config,
        stale_only=stale_only,
    )
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, command, timeout=180, log_command=False)
    if status != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"远端 AI 代理清理失败: {detail or status}")
    return _parse_key_values(stdout)


def _format_ai_proxy_cleanup_result(
    ssh_name: str,
    values: dict[str, str],
    *,
    completion_label: str = "AI 代理清理完成",
    initial_pieces: tuple[str, ...] = (),
) -> str:
    pieces = list(initial_pieces)
    stopped_pids = values.get("stopped_pids", "")
    if stopped_pids:
        pieces.append(f"已停止进程 {stopped_pids}")
    removed_files = _int_or_default(values.get("removed_files"), 0)
    removed_blocks = _int_or_default(values.get("removed_blocks"), 0)
    removed_settings = _int_or_default(values.get("removed_settings"), 0)
    removed_systemd_env = _int_or_default(values.get("removed_systemd_env"), 0)
    backed_up_configs = _int_or_default(values.get("backed_up_configs"), 0)
    if removed_files:
        pieces.append(f"移除受管文件 {removed_files} 个")
    if removed_blocks:
        pieces.append(f"移除 shell/VS Code Remote 入口 {removed_blocks} 处")
    if removed_settings:
        pieces.append(f"清理 VS Code settings {removed_settings} 处")
    if removed_systemd_env:
        pieces.append(f"清理 systemd 用户会话代理变量 {removed_systemd_env} 个")
    if backed_up_configs:
        backup_dir = values.get("backup_dir") or ""
        pieces.append(f"备份并移走旧代理配置 {backed_up_configs} 个" + (f"到 {backup_dir}" if backup_dir else ""))
    if values.get("still_listening") == "yes":
        pieces.append("清理后端口仍在监听，请检查非本工具代理进程")
    else:
        pieces.append("代理端口未监听")
    skipped_pids = values.get("skipped_pids", "")
    if skipped_pids:
        pieces.append(f"跳过无法确认属于本工具的进程 {skipped_pids}")
    notes = values.get("notes", "")
    if notes:
        pieces.append(notes)
    if any(
        (
            bool(stopped_pids),
            removed_files > 0,
            removed_blocks > 0,
            removed_settings > 0,
            removed_systemd_env > 0,
            backed_up_configs > 0,
        )
    ):
        pieces.append("已打开的远端终端、Codex/Claude 或 VS Code 会话需断开重连后才会丢弃已继承的旧变量")
    if not pieces:
        pieces.append("未发现需要清理的远端 AI 代理")
    return f"{ssh_name}: {completion_label}；" + "；".join(pieces)


def cleanup_stale_ai_proxy(ssh_name: str, mixed_port: int = 7890) -> str:
    """Remove only confirmed stale tool-managed proxy residue on one SSH host.

    A healthy listener, a foreign listener, an unknown owner, or a proxy still
    inside its startup grace period is never torn down.  The cleanup command
    also rechecks the port immediately before mutation to narrow the race with
    another login session starting the managed proxy.
    """

    _ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    reconcile = _reconcile_dead_ai_proxy_runtime(
        client,
        home,
        mixed_port,
        conflict_action="脏代理清理",
    )
    if reconcile.get("working") == "yes":
        repair_note = (
            "；已修复失配的受管 PID 记录"
            if reconcile.get("repaired_pid") == "yes"
            else ""
        )
        return (
            f"{ssh_name}: 受管 AI 代理正在 {mixed_port} 端口正常监听"
            f"{repair_note}；未清理健康代理或环境入口"
        )
    if reconcile.get("dirty") != "yes":
        return f"{ssh_name}: 未发现需要清理的本工具 SSH 脏代理残留"

    values = _execute_ai_proxy_cleanup(
        client,
        home,
        mixed_port,
        include_legacy_config=False,
        stale_only=True,
    )
    if values.get("protected_running") == "yes":
        protected_subject = (
            f"{mixed_port} 端口已恢复监听"
            if values.get("protected_reason") == "listener"
            else "代理相关进程重新活动"
        )
        return (
            f"{ssh_name}: 清理前复核发现{protected_subject}；"
            "为避免中断 Codex/Claude，未修改任何代理入口"
        )
    if values.get("protected_unknown") == "yes":
        return (
            f"{ssh_name}: 清理前无法再次确认 {mixed_port} 端口状态；"
            "为避免误清理，未修改任何代理入口"
        )

    reconcile_pieces: list[str] = []
    stopped_pid = reconcile.get("stopped_pid", "")
    if stopped_pid:
        reconcile_pieces.append(f"已停止无监听的受管进程 {stopped_pid}")
    if reconcile.get("removed_pid") == "yes":
        reconcile_pieces.append("已移除失效 PID 记录")
    skipped_pid = reconcile.get("skipped_pid", "")
    if skipped_pid:
        reconcile_pieces.append(f"未终止身份不明的进程 {skipped_pid}")
    return _format_ai_proxy_cleanup_result(
        ssh_name,
        values,
        completion_label="SSH 脏代理清理完成",
        initial_pieces=tuple(reconcile_pieces),
    )


def cleanup_ai_proxy(ssh_name: str, mixed_port: int = 7890, include_legacy_config: bool = True) -> str:
    _ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    values = _execute_ai_proxy_cleanup(
        client,
        home,
        mixed_port,
        include_legacy_config=include_legacy_config,
    )
    return _format_ai_proxy_cleanup_result(ssh_name, values)


def _connect_ssh(ssh_name: str):
    profiles = profile_manager.list_ssh_profiles()
    profile = next((item for item in profiles if item.name == ssh_name), None)
    if not profile:
        raise ValueError(f"未找到 SSH 服务器: {ssh_name}")
    return profile, ssh_manager.connect(profile)


def _parse_inline_map(text: str) -> dict:
    result = {}
    for part in _split_top_level(text, ","):
        if not part.strip():
            continue
        key, value = _split_key_value(part)
        result[key] = _coerce_scalar(value)
    return result


def _parse_block_map(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        if ":" not in line:
            continue
        key, value = _split_key_value(line)
        result[key] = _coerce_scalar(value)
    return result


def _parse_yaml_proxy_nodes(text: str) -> list[dict]:
    try:
        parsed = yaml.safe_load(text)
    except Exception:
        proxy_section = _extract_yaml_proxy_section(text)
        if not proxy_section:
            return []
        try:
            parsed = yaml.safe_load(proxy_section)
        except Exception:
            return []

    candidates = []
    if isinstance(parsed, dict):
        proxies = parsed.get("proxies")
        if isinstance(proxies, list):
            candidates = proxies
        elif isinstance(proxies, dict):
            candidates = _proxy_mapping_values(proxies)
        elif _looks_like_proxy_node(parsed):
            candidates = [parsed]
    elif isinstance(parsed, list):
        candidates = parsed

    nodes = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            nodes.append(_normalize_proxy_node(candidate))
        except ValueError:
            continue
    return nodes


def _extract_yaml_proxy_section(text: str) -> str:
    match = re.search(r"(?m)^[ \t]*proxies\s*:", text or "")
    if not match:
        return ""
    lines = text[match.start():].splitlines()
    if not lines:
        return ""
    base_indent = len(lines[0]) - len(lines[0].lstrip(" "))
    collected = [lines[0]]
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            collected.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= base_indent and re.match(r"[A-Za-z0-9_-]+\s*:", stripped):
            break
        collected.append(line)
    return "\n".join(collected).strip()


def _proxy_mapping_values(proxies: dict) -> list[dict]:
    candidates = []
    for name, value in proxies.items():
        if not isinstance(value, dict):
            continue
        candidate = dict(value)
        if not _has_value(candidate.get("name")):
            candidate["name"] = name
        candidates.append(candidate)
    return candidates


def _parse_yaml_proxy_node(text: str) -> dict | None:
    try:
        parsed = yaml.safe_load(text)
    except Exception:
        return None
    if not isinstance(parsed, dict) or not _looks_like_proxy_node(parsed):
        return None
    try:
        return _normalize_proxy_node(parsed)
    except ValueError:
        return None


def _parse_custom_proxy_nodes(text: str) -> list[dict]:
    entries = _extract_proxy_entries(text)
    if not entries:
        entries = _extract_standalone_proxy_entries(text)

    nodes = []
    for entry in entries:
        try:
            nodes.append(parse_proxy_node(entry))
        except ValueError:
            continue
    return nodes


def _parse_proxy_uri_lines(text: str) -> list[dict]:
    nodes = []
    for candidate in _iter_proxy_uri_candidates(text):
        try:
            node = _parse_proxy_uri(candidate)
        except ValueError:
            continue
        if node:
            nodes.append(node)
    return nodes


def _iter_proxy_uri_candidates(text: str) -> list[str]:
    pattern = re.compile(
        r"(?i)\b(?:vmess|vless|trojan|ssr?|hy2|hysteria2|tuic)://[^\s<>'\"]+"
    )
    candidates = []
    for match in pattern.finditer(text or ""):
        candidate = _clean_proxy_uri_candidate(match.group(0))
        if candidate:
            candidates.append(candidate)
    for line in (text or "").splitlines():
        candidate = _clean_proxy_uri_candidate(line.strip())
        if candidate and re.match(r"(?i)^(?:socks5?|https?)://", candidate):
            candidates.append(candidate)
    return candidates


def _clean_proxy_uri_candidate(candidate: str) -> str:
    value = (candidate or "").strip().strip(",;")
    while value and value[-1] in ")]}":
        opener = {"}": "{", "]": "[", ")": "("}[value[-1]]
        if value.count(opener) >= value.count(value[-1]):
            break
        value = value[:-1]
    return value.strip()


def _parse_proxy_uri(text: str) -> dict:
    scheme = text.split("://", 1)[0].lower() if "://" in text else ""
    if scheme == "vmess":
        return _parse_vmess_uri(text)
    if scheme == "vless":
        return _parse_vless_uri(text)
    if scheme == "trojan":
        return _parse_trojan_uri(text)
    if scheme == "ss":
        return _parse_ss_uri(text)
    if scheme == "ssr":
        return _parse_ssr_uri(text)
    if scheme in {"hy2", "hysteria2"}:
        return _parse_hysteria2_uri(text)
    if scheme == "tuic":
        return _parse_tuic_uri(text)
    if scheme in {"http", "https", "socks", "socks5"}:
        return _parse_basic_proxy_uri(text, scheme)
    raise ValueError("不支持的代理 URI")


def _parse_vmess_uri(text: str) -> dict:
    payload = _decode_base64_payload(text.split("://", 1)[1])
    if not payload:
        raise ValueError("vmess URI 不是有效的 base64 JSON")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("vmess URI 不是有效的 JSON") from exc

    name = str(data.get("ps") or data.get("add") or "vmess").strip()
    node = {
        "name": name,
        "type": "vmess",
        "server": data.get("add"),
        "port": data.get("port"),
        "uuid": data.get("id"),
        "alterId": _int_or_default(data.get("aid"), 0),
        "cipher": data.get("scy") or "auto",
        "network": data.get("net") or "tcp",
    }
    if str(data.get("tls") or "").lower() in {"tls", "true"}:
        node["tls"] = True
    if data.get("sni"):
        node["servername"] = data.get("sni")
    if node["network"] == "ws":
        ws_opts = {}
        if data.get("path"):
            ws_opts["path"] = data.get("path")
        if data.get("host"):
            ws_opts["headers"] = {"Host": data.get("host")}
        if ws_opts:
            node["ws-opts"] = ws_opts
    return _normalize_proxy_node(node)


def _parse_vless_uri(text: str) -> dict:
    parsed = urlparse.urlparse(text)
    query = _query_map(parsed.query)
    node = {
        "name": _uri_name(parsed, "vless"),
        "type": "vless",
        "server": parsed.hostname,
        "port": parsed.port,
        "uuid": urlparse.unquote(parsed.username or ""),
        "network": query.get("type") or query.get("network") or "tcp",
        "udp": True,
    }
    encryption = query.get("encryption")
    if encryption:
        node["encryption"] = encryption
    _apply_common_uri_options(node, query)
    return _normalize_proxy_node(node)


def _parse_trojan_uri(text: str) -> dict:
    parsed = urlparse.urlparse(text)
    query = _query_map(parsed.query)
    node = {
        "name": _uri_name(parsed, "trojan"),
        "type": "trojan",
        "server": parsed.hostname,
        "port": parsed.port,
        "password": urlparse.unquote(parsed.username or ""),
        "network": query.get("type") or query.get("network") or "tcp",
        "udp": True,
    }
    _apply_common_uri_options(node, query)
    return _normalize_proxy_node(node)


def _parse_ss_uri(text: str) -> dict:
    parsed = urlparse.urlparse(text)
    query = _query_map(parsed.query)
    name = _uri_name(parsed, "ss")
    if "@" in parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", 1)[0]
        if ":" in userinfo:
            cipher, password = userinfo.split(":", 1)
            cipher = urlparse.unquote(cipher)
            password = urlparse.unquote(password)
        else:
            decoded = _decode_base64_userinfo(userinfo)
            cipher, password = decoded.split(":", 1)
        server = parsed.hostname
        port = parsed.port
    else:
        decoded = _decode_base64_userinfo(parsed.netloc)
        userinfo, endpoint = decoded.rsplit("@", 1)
        cipher, password = userinfo.split(":", 1)
        server, port_text = endpoint.rsplit(":", 1)
        port = int(port_text)

    node = {
        "name": name,
        "type": "ss",
        "server": server,
        "port": port,
        "cipher": cipher,
        "password": password,
        "udp": True,
    }
    if query.get("plugin"):
        node["plugin"] = query["plugin"]
    return _normalize_proxy_node(node)


def _parse_ssr_uri(text: str) -> dict:
    payload = _decode_base64_payload(text.split("://", 1)[1]) or _decode_base64_component(text.split("://", 1)[1])
    if not payload:
        raise ValueError("ssr URI 不是有效的 base64 内容")
    main, _, query_text = payload.partition("/?")
    parts = main.split(":")
    if len(parts) < 6:
        raise ValueError("ssr URI 主体字段不足")
    server, port, protocol, method, obfs, password_encoded = parts[:6]
    query = _query_map(query_text)
    node = {
        "name": _decode_base64_component(query.get("remarks") or "") or server,
        "type": "ssr",
        "server": server,
        "port": port,
        "cipher": method,
        "password": _decode_base64_component(password_encoded),
        "protocol": protocol,
        "obfs": obfs,
    }
    if query.get("obfsparam"):
        node["obfs-param"] = _decode_base64_component(query["obfsparam"])
    if query.get("protoparam"):
        node["protocol-param"] = _decode_base64_component(query["protoparam"])
    return _normalize_proxy_node(node)


def _parse_hysteria2_uri(text: str) -> dict:
    parsed = urlparse.urlparse(text)
    query = _query_map(parsed.query)
    node = {
        "name": _uri_name(parsed, "hysteria2"),
        "type": "hysteria2",
        "server": parsed.hostname,
        "port": parsed.port,
        "password": urlparse.unquote(parsed.username or query.get("password") or ""),
    }
    if query.get("sni"):
        node["sni"] = query["sni"]
    if _truthy(query.get("insecure") or query.get("allowInsecure")):
        node["skip-cert-verify"] = True
    if query.get("alpn"):
        node["alpn"] = _split_csv(query["alpn"])
    for key in ("obfs", "obfs-password", "up", "down"):
        if query.get(key):
            node[key] = query[key]
    return _normalize_proxy_node(node)


def _parse_tuic_uri(text: str) -> dict:
    parsed = urlparse.urlparse(text)
    query = _query_map(parsed.query)
    username = urlparse.unquote(parsed.username or "")
    password = urlparse.unquote(parsed.password or "")
    if ":" in username and not password:
        username, password = username.split(":", 1)
    node = {
        "name": _uri_name(parsed, "tuic"),
        "type": "tuic",
        "server": parsed.hostname,
        "port": parsed.port,
        "uuid": username,
        "password": password or query.get("password") or "",
    }
    if query.get("sni"):
        node["sni"] = query["sni"]
    if query.get("alpn"):
        node["alpn"] = _split_csv(query["alpn"])
    if query.get("congestion_control"):
        node["congestion-controller"] = query["congestion_control"]
    if query.get("udp_relay_mode"):
        node["udp-relay-mode"] = query["udp_relay_mode"]
    if _truthy(query.get("allowInsecure") or query.get("insecure")):
        node["skip-cert-verify"] = True
    return _normalize_proxy_node(node)


def _parse_basic_proxy_uri(text: str, scheme: str) -> dict:
    parsed = urlparse.urlparse(text)
    node = {
        "name": _uri_name(parsed, scheme),
        "type": "socks5" if scheme in {"socks", "socks5"} else "http",
        "server": parsed.hostname,
        "port": parsed.port,
    }
    if parsed.username:
        node["username"] = urlparse.unquote(parsed.username)
    if parsed.password:
        node["password"] = urlparse.unquote(parsed.password)
    if scheme == "https":
        node["tls"] = True
    return _normalize_proxy_node(node)


def _apply_common_uri_options(node: dict, query: dict[str, str]) -> None:
    security = (query.get("security") or "").lower()
    if security in {"tls", "reality"} or query.get("tls", "").lower() in {"1", "true", "tls"}:
        node["tls"] = True
    servername = query.get("sni") or query.get("servername") or query.get("peer")
    if servername:
        node["servername"] = servername
    if query.get("flow"):
        node["flow"] = query["flow"]
    if query.get("fp"):
        node["client-fingerprint"] = query["fp"]
    if _truthy(query.get("allowInsecure") or query.get("insecure")):
        node["skip-cert-verify"] = True
    if query.get("alpn"):
        node["alpn"] = _split_csv(query["alpn"])

    network = str(node.get("network") or "").lower()
    if network == "ws":
        ws_opts = {}
        if query.get("path"):
            ws_opts["path"] = query["path"]
        if query.get("host"):
            ws_opts["headers"] = {"Host": query["host"]}
        if ws_opts:
            node["ws-opts"] = ws_opts
    elif network == "grpc":
        grpc_opts = {}
        service_name = query.get("serviceName") or query.get("service_name") or query.get("grpc-service-name")
        if service_name:
            grpc_opts["grpc-service-name"] = service_name
        if query.get("mode"):
            grpc_opts["grpc-mode"] = query["mode"]
        if grpc_opts:
            node["grpc-opts"] = grpc_opts
    elif network == "httpupgrade":
        httpupgrade_opts = {}
        if query.get("path"):
            httpupgrade_opts["path"] = query["path"]
        if query.get("host"):
            httpupgrade_opts["host"] = query["host"]
        if httpupgrade_opts:
            node["httpupgrade-opts"] = httpupgrade_opts
    elif network in {"http", "h2"}:
        http_opts = {}
        if query.get("path"):
            http_opts["path"] = [query["path"]]
        if query.get("host"):
            http_opts["headers"] = {"Host": _split_csv(query["host"])}
        if http_opts:
            node["h2-opts" if network == "h2" else "http-opts"] = http_opts
    if security == "reality":
        reality_opts = {}
        if query.get("pbk"):
            reality_opts["public-key"] = query["pbk"]
        if query.get("sid"):
            reality_opts["short-id"] = query["sid"]
        if query.get("spx"):
            reality_opts["spider-x"] = query["spx"]
        if reality_opts:
            node["reality-opts"] = reality_opts


def _query_map(query: str) -> dict[str, str]:
    values = {}
    for key, items in urlparse.parse_qs(query, keep_blank_values=True).items():
        values[key] = items[-1] if items else ""
    return values


def _uri_name(parsed, fallback: str) -> str:
    return urlparse.unquote(parsed.fragment or parsed.hostname or fallback).strip() or fallback


def _decode_base64_userinfo(value: str) -> str:
    decoded = _decode_base64_payload(value)
    if not decoded:
        decoded = _decode_base64_component(value)
        if not decoded:
            raise ValueError("SS URI 用户信息不是有效 base64")
    if ":" not in decoded:
        raise ValueError("SS URI 用户信息缺少加密方式或密码")
    return decoded


def _decode_base64_component(value: str) -> str:
    raw = urlparse.unquote(value or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"\s+", "", raw)
    padded = compact + ("=" * (-len(compact) % 4))
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace").strip()
    except (binascii.Error, ValueError):
        return ""


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _proxy_subscription_auto_refresh_key(scope: str = "") -> str:
    normalized = str(scope or "").strip().lower()
    if normalized in {"local", "win", "win11", "windows"}:
        return "local_auto_refresh_enabled"
    if normalized in {"ssh", "remote", "server"}:
        return "ssh_auto_refresh_enabled"
    return ""


def _int_or_default(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_timeout(value, default: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = default
    return min(max(timeout, 0.2), 15.0)


def _optional_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _diagnostic_settings_keys(settings, service: str) -> list[str]:
    if not settings:
        return []
    if hasattr(settings, "keys_for"):
        try:
            return list(settings.keys_for(service))
        except Exception:
            return []
    if isinstance(settings, dict):
        raw = settings.get(service) or settings.get(str(service))
        if isinstance(raw, dict):
            raw = raw.get("api_keys") or raw.get("keys")
        if isinstance(raw, str):
            return network_diagnostic_settings.parse_api_keys(raw)
        if isinstance(raw, (list, tuple, set)):
            return network_diagnostic_settings.parse_api_keys(list(raw))
    return []


def proxy_quality_settings_signature(settings=None, enabled_services=None) -> str:
    settings = settings or network_diagnostic_settings.load_settings()
    if enabled_services is not None:
        services = network_diagnostic_settings.normalize_services(enabled_services)
    elif hasattr(settings, "enabled_services"):
        services = settings.enabled_services()
    else:
        services = []
    payload = [{
        "cache_schema": PROXY_QUALITY_CACHE_SCHEMA_VERSION,
        "assessment_scope": PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER,
        "proxycheck_api_version": network_diagnostics.PROXYCHECK_API_VERSION,
    }]
    for service in services:
        key_hashes = [
            hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]
            for key in _diagnostic_settings_keys(settings, service)
            if str(key or "").strip()
        ]
        payload.append({"service": service, "keys": key_hashes})
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cached_proxy_node_quality_result(
    node_key: str,
    host: str,
    region: str,
    ip: str,
    quality_signature: str,
    cached_quality_results: dict[str, ProxyNodeQualityResult | dict] | None,
    cache_ttl_seconds: int,
    *,
    cache_index: dict[tuple[str, str], ProxyNodeQualityResult | dict] | None = None,
) -> ProxyNodeQualityResult | None:
    cache = cached_quality_results if isinstance(cached_quality_results, dict) else load_proxy_subscription_qualities()
    result = _find_cached_proxy_node_quality_result(
        cache,
        node_key,
        ip,
        quality_signature,
        cache_ttl_seconds,
        cache_index=cache_index,
    )
    if result is None:
        return None
    return ProxyNodeQualityResult(
        node_key=node_key,
        ok=True,
        host=str(host or proxy_node_quality_host(result)),
        ip=ip,
        region=str(proxy_node_quality_region(result) or region),
        ip_type=proxy_node_quality_ip_type(result),
        risk_score=proxy_node_quality_risk_score(result),
        risk_label=proxy_node_quality_risk_label(result),
        quality_score=proxy_node_quality_score(result),
        quality_label=proxy_node_quality_label(result),
        confidence=proxy_node_quality_confidence(result),
        detail=_cache_hit_detail(proxy_node_quality_detail(result)),
        checked_at=proxy_node_quality_checked_at(result),
        sources=proxy_node_quality_sources(result),
        attempted_sources=proxy_node_quality_attempted_sources(result),
        coverage_complete=proxy_node_quality_coverage_complete(result),
        assessment_scope=proxy_node_quality_assessment_scope(result),
        classification_basis=proxy_node_quality_classification_basis(result),
        quality_signature=quality_signature,
        cached=True,
    )


def _find_cached_proxy_node_quality_result(
    cache: dict[str, ProxyNodeQualityResult | dict] | None,
    node_key: str,
    ip: str,
    quality_signature: str,
    cache_ttl_seconds: int,
    *,
    cache_index: dict[tuple[str, str], ProxyNodeQualityResult | dict] | None = None,
) -> ProxyNodeQualityResult | dict | None:
    if not isinstance(cache, dict) or not ip or not quality_signature:
        return None
    direct = cache.get(node_key)
    if _cached_quality_matches(direct, ip, quality_signature, cache_ttl_seconds):
        return direct
    if cache_index is not None:
        indexed = cache_index.get((_canonical_ip_text(ip), quality_signature))
        return indexed if _cached_quality_matches(indexed, ip, quality_signature, cache_ttl_seconds) else None
    candidates = [
        result
        for key, result in cache.items()
        if key != node_key and _cached_quality_matches(result, ip, quality_signature, cache_ttl_seconds)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: proxy_node_quality_checked_at(item))


def _build_proxy_quality_cache_index(
    cache: dict[str, ProxyNodeQualityResult | dict] | None,
) -> dict[tuple[str, str], ProxyNodeQualityResult | dict]:
    index: dict[tuple[str, str], ProxyNodeQualityResult | dict] = {}
    if not isinstance(cache, dict):
        return index
    for result in cache.values():
        if not proxy_node_quality_cacheable(result):
            continue
        ip = _canonical_ip_text(proxy_node_quality_ip(result))
        signature = proxy_node_quality_signature(result)
        if not ip or not signature:
            continue
        key = (ip, signature)
        current = index.get(key)
        if current is None or proxy_node_quality_checked_at(result) > proxy_node_quality_checked_at(current):
            index[key] = result
    return index


def _cached_quality_matches(
    result: ProxyNodeQualityResult | dict | None,
    ip: str,
    quality_signature: str,
    cache_ttl_seconds: int,
) -> bool:
    if not proxy_node_quality_cacheable(result):
        return False
    if not _same_ip(proxy_node_quality_ip(result), ip):
        return False
    if proxy_node_quality_signature(result) != quality_signature:
        return False
    return _quality_checked_at_fresh(proxy_node_quality_checked_at(result), cache_ttl_seconds)


def _quality_checked_at_fresh(checked_at: str, cache_ttl_seconds: int) -> bool:
    if not checked_at:
        return False
    try:
        timestamp = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -300:
        return False
    return age <= max(0, _int_or_default(cache_ttl_seconds, PROXY_QUALITY_CACHE_TTL_SECONDS))


def _cache_hit_detail(detail: str) -> str:
    text = str(detail or "").strip()
    marker = "缓存命中，跳过重复评测"
    if marker in text:
        return text[:220]
    if not text:
        return marker
    return f"{text}；{marker}"[:220]


def _proxy_node_quality_error_result(
    node: dict,
    quality_label: str,
    detail: str,
    ip: str = "",
    sources=None,
) -> ProxyNodeQualityResult:
    normalized = _normalize_proxy_node(node)
    return ProxyNodeQualityResult(
        node_key=proxy_node_key(normalized),
        ok=False,
        host=str(normalized.get("server") or ""),
        ip=str(ip or ""),
        region=proxy_node_region(normalized),
        quality_label=str(quality_label or "检测失败")[:60],
        detail=str(detail or "节点服务器 IP 质量检测失败")[:220],
        checked_at=_now_iso(),
        attempted_sources=tuple(network_diagnostic_settings.normalize_services(sources or [])),
        assessment_scope=PROXY_QUALITY_ASSESSMENT_SCOPE_SERVER,
    )


def _resolve_proxy_node_ip(node: dict, resolver=None) -> str:
    normalized = _normalize_proxy_node(node)
    server = str(normalized.get("server") or "").strip().strip("[]")
    if not server:
        raise ValueError("代理节点缺少服务器地址")
    try:
        literal = _canonical_ip_text(server)
        if not _proxy_quality_ip_usable(literal):
            raise ValueError("代理节点服务器不是可用于外部质量检测的公网地址")
        return literal
    except ValueError:
        if _looks_like_ip_literal(server):
            raise

    resolver = resolver or socket.getaddrinfo
    try:
        infos = resolver(server, None, type=socket.SOCK_STREAM)
    except TypeError:
        infos = resolver(server)
    candidates: list[str] = []
    for info in infos or []:
        sockaddr = info[4] if isinstance(info, tuple) and len(info) >= 5 else None
        address = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else None
        if not address:
            continue
        text = str(address).strip().strip("[]")
        try:
            text = _canonical_ip_text(text)
        except ValueError:
            continue
        if text not in candidates:
            candidates.append(text)
    if not candidates:
        raise ValueError(f"无法解析节点服务器: {server}")

    usable_candidates = [value for value in candidates if _proxy_quality_ip_usable(value)]
    if not usable_candidates:
        raise ValueError(f"节点服务器未解析到可用于外部质量检测的公网地址: {server}")

    def rank(value: str) -> tuple[int, int]:
        parsed = ipaddress.ip_address(value)
        return (0 if parsed.is_global else 1, 0 if parsed.version == 4 else 1)

    return sorted(usable_candidates, key=rank)[0]


def _looks_like_ip_literal(value: str) -> bool:
    text = str(value or "").strip().strip("[]")
    return bool(":" in text or re.fullmatch(r"[\d.]+", text))


def _canonical_ip_text(value: str) -> str:
    return str(ipaddress.ip_address(str(value or "").strip().strip("[]")))


def _same_ip(left: str, right: str) -> bool:
    try:
        return _canonical_ip_text(left) == _canonical_ip_text(right)
    except ValueError:
        return False


def _proxy_quality_ip_usable(value: str) -> bool:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return False
    return parsed.is_global


def _proxy_quality_score(classification: network_diagnostics.IpClassification) -> int:
    risk = max(0, min(100, int(classification.risk_score)))
    score = 100 - risk
    ip_type = str(classification.ip_type or "")

    has_high_risk = "高风险" in ip_type
    has_anonymity = any(marker in ip_type for marker in ("代理", "VPN", "Tor", "匿名"))
    has_non_native = "非原生" in ip_type or "广播" in ip_type
    has_conflict = "冲突" in ip_type
    has_residential = any(
        marker in ip_type
        for marker in ("家庭宽带", "住宅", "家庭/非IDC", "运营商/宽带", "家宽")
    )
    has_mobile = "蜂窝" in ip_type or "移动网络" in ip_type
    has_business = "企业" in ip_type or "商宽" in ip_type
    idc_text = ip_type.replace("非IDC", "").replace("非 IDC", "")
    has_hosting = "IDC" in idc_text or "机房" in idc_text
    network_category_count = sum((has_residential, has_mobile, has_business, has_hosting))
    has_mixed_network = network_category_count >= 2

    # Risk markers are deliberately cumulative. A future classifier label that
    # combines categories must never receive a better score because an earlier
    # ``elif`` branch happened to mask a stronger adverse signal.
    if has_high_risk:
        score -= 35
    if has_anonymity:
        score -= 65
    if has_non_native:
        score -= 25
    if has_conflict or has_mixed_network:
        score -= 20
    if has_mobile:
        score -= 8
    if has_business:
        score -= 12
    if has_hosting:
        score -= 38
    if has_residential and not any(
        (has_high_risk, has_anonymity, has_non_native, has_conflict, has_mixed_network)
    ):
        score += 18
    confidence = str(classification.confidence or "").strip()
    if confidence == "低":
        score -= 30
    elif confidence == "中":
        score -= 5
    return max(0, min(100, score))


def _proxy_quality_label(classification: network_diagnostics.IpClassification, score: int) -> str:
    ip_type = str(classification.ip_type or "")
    risk = int(classification.risk_score)
    if "冲突" in ip_type:
        return "来源冲突"
    if "高风险" in ip_type:
        return "高风险"
    if "非原生" in ip_type or "广播" in ip_type:
        return "非原生待复核"
    if "代理" in ip_type or "VPN" in ip_type or "Tor" in ip_type or "匿名" in ip_type:
        return "代理风险"
    if classification.risk_label == "高风险":
        return "高风险"
    if "运营商/宽带" in ip_type:
        return "家宽待核验"
    if any(marker in ip_type for marker in ("家庭宽带", "住宅", "家庭/非IDC", "运营商/宽带")):
        if classification.confidence == "低":
            return "家宽待核验"
        if score >= 80 and risk <= 35:
            return "家宽高质"
        return "家宽可用"
    if "蜂窝" in ip_type or "移动网络" in ip_type:
        return "移动网络"
    if "企业" in ip_type or "商宽" in ip_type:
        return "商宽中等"
    if "IDC" in ip_type or "机房" in ip_type:
        return "机房风险"
    if score >= 75 and risk <= 35:
        return "低风险"
    return "质量未知"


def _download_proxy_subscription(
    *,
    request: urlrequest.Request,
    timeout: int,
    max_bytes: int,
    retries: int,
    retry_base_delay: float,
    allow_direct_fallback: bool = True,
    proxy_diagnostic: ProxyEnvironmentDiagnostic | None = None,
    recovery_proxy_provider: Callable[[float], object] | None = None,
    download_trace: _ProxySubscriptionDownloadTrace | None = None,
) -> tuple[bytes, str, str]:
    try:
        attempts = max(1, int(retries))
    except (TypeError, ValueError):
        attempts = len(PROXY_SUBSCRIPTION_USER_AGENTS)
    attempts = max(1, attempts)
    last_error: Exception | None = None
    proxy_diagnostic = proxy_diagnostic or _subscription_proxy_environment_diagnostic(
        request.full_url
    )
    force_direct = proxy_diagnostic.has_invalid_proxy and allow_direct_fallback
    timeout_seconds = _normalize_subscription_timeout(timeout)
    deadline = time.monotonic() + timeout_seconds
    proxy_configured = _subscription_proxy_is_configured() and not force_direct
    primary_is_direct = bool(force_direct or (allow_direct_fallback and not proxy_configured))
    direct_recovery_active = False
    trace = download_trace if download_trace is not None else _ProxySubscriptionDownloadTrace()
    recovery_discovery_attempted = False
    strict_proxy_map: dict[str, str] | None = None
    strict_proxy_error: RuntimeError | None = None
    if not allow_direct_fallback:
        try:
            strict_proxy_map = _strict_subscription_proxy_map(request)
        except RuntimeError as exc:
            # A verified isolated recovery session may still provide a
            # fail-closed route. Defer the original error until that lazy
            # provider has had one chance to start.
            strict_proxy_error = exc
    # A broken configured proxy is one of the main reasons subscription
    # refresh is needed.  Reserve part of the caller's total budget for the
    # direct recovery instead of letting the first urlopen consume the entire
    # deadline. Compatible signatures may reuse that download-only route, but
    # every request remains bounded by the same overall deadline and never
    # changes system/application proxy settings.
    direct_recovery_reserve = (
        min(15.0, max(0.25, timeout_seconds / 3.0))
        if allow_direct_fallback and proxy_configured and timeout_seconds >= 0.5
        else 0.0
    )
    recovery_proxy_reserve = (
        min(15.0, max(0.25, timeout_seconds / 3.0))
        if (
            callable(recovery_proxy_provider)
            and timeout_seconds >= 0.5
        )
        else 0.0
    )
    if direct_recovery_reserve and recovery_proxy_reserve:
        route_slice = min(15.0, max(0.05, timeout_seconds / 3.0))
        direct_recovery_reserve = route_slice
        recovery_proxy_reserve = route_slice

    for attempt in range(1, attempts + 1):
        attempt_request = _proxy_subscription_request_for_attempt(request, attempt)
        try:
            if strict_proxy_error is not None:
                raise strict_proxy_error
            result = _open_validated_proxy_subscription_request(
                attempt_request,
                timeout=_subscription_request_timeout(
                    deadline,
                    reserve_seconds=direct_recovery_reserve + recovery_proxy_reserve,
                ),
                deadline=deadline - direct_recovery_reserve - recovery_proxy_reserve,
                max_bytes=max_bytes,
                proxy_map=strict_proxy_map,
                direct=force_direct,
            )
            return result
        except HTTPError as exc:
            if exc.code in PROXY_SUBSCRIPTION_PERMANENT_HTTP_ERRORS:
                raise ValueError(f"订阅链接返回 HTTP {exc.code}，请检查订阅地址是否有效") from exc
            last_error = exc
        except _ProxySubscriptionPayloadError as exc:
            last_error = exc
        except ValueError:
            raise
        except Exception as exc:
            last_error = exc

        # A stopped/broken local proxy must not prevent the subscription from
        # being refreshed to recover service. This bypass is download-only and
        # never changes the user's proxy configuration or selected node.
        if (
            allow_direct_fallback
            and not primary_is_direct
            and (
                direct_recovery_active
                or _should_try_direct_subscription_download(last_error)
            )
        ):
            direct_recovery_active = True
            try:
                return _open_validated_proxy_subscription_request(
                    attempt_request,
                    timeout=_subscription_request_timeout(
                        deadline,
                        reserve_seconds=recovery_proxy_reserve,
                    ),
                    deadline=deadline - recovery_proxy_reserve,
                    max_bytes=max_bytes,
                    direct=True,
                )
            except HTTPError as exc:
                if exc.code in PROXY_SUBSCRIPTION_PERMANENT_HTTP_ERRORS:
                    raise ValueError(
                        f"订阅链接返回 HTTP {exc.code}，请检查订阅地址是否有效"
                    ) from exc
                last_error = exc
            except _ProxySubscriptionPayloadError as exc:
                last_error = exc
            except ValueError:
                raise
            except Exception as exc:
                last_error = exc

        if (
            not recovery_discovery_attempted
            and callable(recovery_proxy_provider)
            and (
                strict_proxy_error is not None
                or _should_try_recovery_proxy_subscription_download(last_error)
            )
        ):
            recovery_discovery_attempted = True
            original_error = last_error
            try:
                with _subscription_recovery_proxy_context(
                    recovery_proxy_provider,
                    timeout_seconds=max(0.001, deadline - time.monotonic()),
                ) as recovery_session:
                    if recovery_session is None:
                        trace.recovery_proxy_unavailable = True
                        continue
                    trace.recovery_proxy_attempted = True
                    result, recovery_error = _download_proxy_subscription_via_recovery(
                        request=attempt_request,
                        session=recovery_session,
                        deadline=deadline,
                        max_bytes=max_bytes,
                        trace=trace,
                    )
                    if result is None:
                        last_error = recovery_error or original_error
                    else:
                        # Do not report success until the disposable context
                        # has also stopped and cleaned its credential directory.
                        successful_result = result
                if result is not None:
                    trace.recovery_proxy_used = True
                    return successful_result
            except HTTPError as exc:
                if exc.code in PROXY_SUBSCRIPTION_PERMANENT_HTTP_ERRORS:
                    raise ValueError(
                        f"订阅链接返回 HTTP {exc.code}，请检查订阅地址是否有效"
                    ) from exc
                last_error = exc
            except _ProxySubscriptionPayloadError as exc:
                last_error = exc
            except ValueError:
                raise
            except Exception as exc:
                if trace.recovery_proxy_attempted:
                    last_error = exc
                else:
                    # Discovery/startup is best-effort and must not hide the
                    # original network or strict-privacy routing failure.
                    trace.recovery_proxy_unavailable = True
                    last_error = original_error

        if attempt < attempts:
            if _subscription_error_is_retryable(last_error):
                retry_after = _subscription_retry_after_seconds(last_error)
                delay = (
                    retry_after
                    if retry_after is not None
                    else _retry_delay_seconds(retry_base_delay, attempt)
                )
            else:
                delay = 0.0 if _subscription_error_allows_immediate_retry(last_error) else _retry_delay_seconds(
                    retry_base_delay,
                    attempt,
                )
            delay = min(delay, max(0.0, deadline - time.monotonic()))
            if delay > 0:
                time.sleep(delay)

        if time.monotonic() >= deadline:
            break

    recovery_suffix = trace.failure_suffix()
    if isinstance(last_error, HTTPError) and 400 <= last_error.code < 500:
        raise ValueError(
            f"订阅链接返回 HTTP {last_error.code}，"
            f"服务器可能拒绝了当前客户端或链接已失效{recovery_suffix}"
        ) from last_error
    if isinstance(last_error, _ProxySubscriptionPayloadError):
        raise ValueError(f"{last_error}{recovery_suffix}") from last_error
    suffix = (
        f"（最长等待 {timeout_seconds:g} 秒，最多重试 {attempts} 次）"
        if attempts > 1
        else ""
    )
    privacy_suffix = "；已禁止绕过代理直连回退" if not allow_direct_fallback else ""
    proxy_suffix = (
        f"；{proxy_diagnostic.warning(bypassed=allow_direct_fallback)}"
        if proxy_diagnostic.has_invalid_proxy
        else ""
    )
    raise RuntimeError(
        f"订阅下载失败{suffix}{privacy_suffix}{proxy_suffix}{recovery_suffix}: {last_error}"
    ) from last_error


def _open_proxy_subscription_request(
    request: urlrequest.Request,
    *,
    timeout: int | float,
    deadline: float | None = None,
    max_bytes: int,
    direct: bool = False,
    proxy_map: dict[str, str] | None = None,
) -> tuple[bytes, str, str]:
    if direct and proxy_map is not None:
        raise ValueError("订阅请求不能同时指定代理与直连")
    opener = None
    if direct:
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    elif proxy_map is not None:
        # The standard ProxyHandler still honors proxy_bypass/NO_PROXY even
        # with an explicit mapping.  This handler intentionally skips that
        # branch, so NO_PROXY='*' cannot turn strict mode into a direct call.
        opener = urlrequest.build_opener(_NoBypassProxyHandler(proxy_map))
    open_request = opener.open if opener is not None else urlrequest.urlopen
    with open_request(request, timeout=timeout) as response:
        status = _int_or_default(getattr(response, "status", getattr(response, "code", 200)), 200)
        if status >= 400:
            # Keep the status and response headers available to the retry layer.
            # In particular, 429 providers commonly return Retry-After and
            # should not be treated like a permanently invalid subscription.
            raise HTTPError(
                request.full_url,
                status,
                f"订阅服务器返回 HTTP {status}",
                getattr(response, "headers", None),
                None,
            )

        limit = _normalize_subscription_max_bytes(max_bytes)
        read_deadline = (
            float(deadline)
            if deadline is not None
            else time.monotonic() + max(0.25, float(timeout))
        )
        payload = bytearray()
        chunk_reader = getattr(response, "read1", None)
        supports_chunked_read = callable(chunk_reader)
        if not supports_chunked_read:
            # Small wrappers used by some URL handlers expose only ``read``.
            # Keep that bounded by the socket timeout and byte limit; standard
            # HTTPResponse/addinfourl objects expose read1 for the true loop.
            chunk_reader = response.read
        while len(payload) <= limit:
            remaining = read_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("订阅下载超过总等待时间")
            _tighten_subscription_response_timeout(response, remaining)
            chunk = chunk_reader(min(64 * 1024, limit + 1 - len(payload)))
            if time.monotonic() >= read_deadline:
                raise TimeoutError("订阅下载超过总等待时间")
            if not chunk:
                break
            payload.extend(chunk)
            if not supports_chunked_read:
                break
        if len(payload) > limit:
            raise ValueError(f"订阅内容超过 {_subscription_size_limit_label(limit)}，已停止读取")

        headers = getattr(response, "headers", {}) or {}
        payload = _decode_http_payload(
            bytes(payload),
            content_encoding=_header_value(headers, "Content-Encoding"),
            max_bytes=limit,
        )
        return bytes(payload), _response_content_type(headers), _response_charset(headers) or "utf-8"


def _open_validated_proxy_subscription_request(
    request: urlrequest.Request,
    *,
    timeout: int | float,
    deadline: float | None = None,
    max_bytes: int,
    direct: bool = False,
    proxy_map: dict[str, str] | None = None,
) -> tuple[bytes, str, str]:
    """Download and reject HTTP-200 block/error pages before route fallback ends."""

    result = _open_proxy_subscription_request(
        request,
        timeout=timeout,
        deadline=deadline,
        max_bytes=max_bytes,
        direct=direct,
        proxy_map=proxy_map,
    )
    payload, content_type, charset = result
    text = _decode_subscription_bytes(payload, charset)
    try:
        parse_proxy_subscription_content(text)
    except Exception as exc:
        normalized_type = str(content_type or "").casefold()
        prefix = text.lstrip()[:256].casefold()
        if "html" in normalized_type or prefix.startswith(("<!doctype html", "<html")):
            message = "订阅服务器返回了网页或拦截页，而不是节点配置"
        elif (
            "json" in normalized_type
            and re.search(r'^[\[{].*"(?:error|message|detail|code)"\s*:', prefix, flags=re.S)
        ):
            message = "订阅服务器返回了错误信息，而不是节点配置"
        else:
            message = str(exc).strip() or "订阅内容无法识别"
        raise _ProxySubscriptionPayloadError(message) from exc
    return result


def _tighten_subscription_response_timeout(response, remaining: float) -> None:
    """Best-effort socket timeout tightening; the monotonic checks stay authoritative."""

    timeout = max(0.001, min(60.0, float(remaining)))
    candidates = [
        response,
        getattr(response, "fp", None),
        getattr(getattr(response, "fp", None), "raw", None),
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
    ]
    for candidate in candidates:
        settimeout = getattr(candidate, "settimeout", None)
        if not callable(settimeout):
            continue
        try:
            settimeout(timeout)
            return
        except Exception:
            continue


def _proxy_subscription_request_for_attempt(
    request: urlrequest.Request,
    attempt: int,
) -> urlrequest.Request:
    index = (max(1, int(attempt)) - 1) % len(PROXY_SUBSCRIPTION_USER_AGENTS)
    return _proxy_subscription_request_with_user_agent(
        request,
        PROXY_SUBSCRIPTION_USER_AGENTS[index],
    )


def _proxy_subscription_request_with_user_agent(
    request: urlrequest.Request,
    user_agent: str,
) -> urlrequest.Request:
    headers = dict(request.header_items())
    headers["User-Agent"] = str(user_agent or PROXY_SUBSCRIPTION_USER_AGENTS[0])
    return urlrequest.Request(
        request.full_url,
        data=request.data,
        headers=headers,
        method=request.get_method(),
    )


def _proxy_subscription_user_agents_for_request(
    request: urlrequest.Request,
) -> tuple[str, ...]:
    current = str(request.get_header("User-agent") or "").strip()
    ordered = [current, *PROXY_SUBSCRIPTION_USER_AGENTS]
    seen = set()
    result = []
    for value in ordered:
        clean = str(value or "").strip()
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return tuple(result)


def _subscription_recovery_attempt_deadline(
    deadline: float,
    *,
    routes_after: int,
) -> float:
    """Reserve a useful slice for each later node in the disposable pool."""

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("订阅下载超过总等待时间")
    later = max(0, int(routes_after))
    if later <= 0:
        return deadline
    per_later_route = min(2.5, remaining / (later + 1))
    reserve = min(max(0.0, remaining - 0.25), later * per_later_route)
    return deadline - reserve


def _download_proxy_subscription_via_recovery(
    *,
    request: urlrequest.Request,
    session: _ProxySubscriptionRecoverySession,
    deadline: float,
    max_bytes: int,
    trace: _ProxySubscriptionDownloadTrace,
) -> tuple[tuple[bytes, str, str] | None, Exception | None]:
    """Try client signatures and deterministic existing-node routes, bounded by deadline."""

    last_error: Exception | None = None
    user_agents = _proxy_subscription_user_agents_for_request(request)
    route_count = max(1, min(AI_PROXY_FALLBACK_MAX_NODES, session.route_count))
    for route_index in range(route_count):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_error = TimeoutError("订阅下载超过总等待时间")
            break
        if route_index > 0:
            switch_timeout = min(
                1.5,
                max(0.05, remaining / (route_count - route_index + 1)),
            )
            try:
                session.select_route(route_index, switch_timeout)
            except Exception as exc:
                last_error = exc
                continue

        trace.recovery_routes_attempted += 1
        for user_agent in user_agents:
            if time.monotonic() >= deadline:
                last_error = TimeoutError("订阅下载超过总等待时间")
                break
            attempt_deadline = _subscription_recovery_attempt_deadline(
                deadline,
                routes_after=route_count - route_index - 1,
            )
            attempt_request = _proxy_subscription_request_with_user_agent(
                request,
                user_agent,
            )
            trace.recovery_signatures_attempted += 1
            try:
                result = _open_validated_proxy_subscription_request(
                    attempt_request,
                    timeout=_subscription_request_timeout(attempt_deadline),
                    deadline=attempt_deadline,
                    max_bytes=max_bytes,
                    proxy_map=session.proxy_map,
                )
                return result, None
            except HTTPError as exc:
                if exc.code in PROXY_SUBSCRIPTION_PERMANENT_HTTP_ERRORS:
                    raise ValueError(
                        f"订阅链接返回 HTTP {exc.code}，请检查订阅地址是否有效"
                    ) from exc
                last_error = exc
                # 403/406 are commonly selected by User-Agent. Other route or
                # origin failures are better served by changing the exit node.
                if exc.code in {403, 406}:
                    continue
                break
            except _ProxySubscriptionPayloadError as exc:
                # A 200 anti-bot/error page can depend on both the client
                # signature and exit IP, so exhaust signatures before routing
                # the same secret URL through the next local-only node.
                last_error = exc
                continue
            except ValueError:
                raise
            except Exception as exc:
                last_error = exc
                break
    return None, last_error


def _should_try_direct_subscription_download(error: Exception | None) -> bool:
    text = _exception_chain_text(error).casefold()
    if isinstance(error, HTTPError) and error.code == 407:
        return True
    if (
        isinstance(error, HTTPError)
        and error.code in {502, 503, 504}
        and _subscription_proxy_is_configured()
    ):
        # A broken local Clash/mihomo upstream commonly surfaces as a bare
        # gateway error. Use the already bounded, download-only direct recovery
        # so the subscription can repair that proxy state.
        return True
    if any(
        marker in text
        for marker in (
            "proxy",
            "tunnel",
            "10061",
            "actively refused",
            "connection refused",
        )
    ):
        return True
    return _subscription_error_is_timeout(error) and _subscription_proxy_is_configured()


def _should_try_recovery_proxy_subscription_download(error: Exception | None) -> bool:
    """Return whether a route change can plausibly recover the request."""

    if error is None:
        return False
    if isinstance(error, _ProxySubscriptionPayloadError):
        return True
    if isinstance(error, HTTPError):
        return error.code == 407 or error.code in PROXY_SUBSCRIPTION_RETRYABLE_HTTP_ERRORS
    return isinstance(error, OSError) or _subscription_error_is_timeout(error)


def _exception_chain_text(error: Exception | None) -> str:
    pieces = []
    current = error
    seen = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        pieces.append(str(current))
        current = current.__cause__ or current.__context__
    return " ".join(pieces)


def _subscription_error_allows_immediate_retry(error: Exception | None) -> bool:
    return isinstance(error, HTTPError) and error.code in {403, 406}


def _subscription_retry_after_seconds(error: Exception | None) -> float | None:
    """Read a bounded Retry-After delay from a transient HTTP response."""
    if not isinstance(error, HTTPError):
        return None
    headers = getattr(error, "headers", None)
    raw = _header_value(headers, "Retry-After")
    if not raw:
        return None
    value = str(raw).strip()
    try:
        return min(30.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        pass
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return min(30.0, max(0.0, target.timestamp() - time.time()))
    except (TypeError, ValueError, OverflowError):
        return None


def _subscription_error_is_retryable(error: Exception | None) -> bool:
    return isinstance(error, HTTPError) and error.code in PROXY_SUBSCRIPTION_RETRYABLE_HTTP_ERRORS


def _subscription_error_is_timeout(error: Exception | None) -> bool:
    current = error
    seen = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, socket.timeout)):
            return True
        if "timed out" in str(current).casefold():
            return True
        current = current.__cause__ or current.__context__
    return False


def _subscription_proxy_is_configured() -> bool:
    try:
        proxies = urlrequest.getproxies()
    except Exception:
        return False
    return any(str(proxies.get(key) or "").strip() for key in ("http", "https", "all"))


_SUBSCRIPTION_PROXY_ENV_KEYS = {
    "http": ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"),
    "https": ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"),
}
_LOOPBACK_PROXY_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _subscription_proxy_url_signature(value: str) -> tuple[str, str, int | None] | None:
    """Normalize a proxy value enough to compare it with an environment value."""

    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlparse.urlparse(candidate)
        host = str(parsed.hostname or "").strip().casefold()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if not host:
        return None
    if port is None:
        port = 443 if parsed.scheme.casefold() == "https" else 80
    return parsed.scheme.casefold(), host, int(port)


def _subscription_proxy_display_url(value: str) -> str:
    """Return a credential-free proxy URL for user-facing diagnostics."""

    signature = _subscription_proxy_url_signature(value)
    if signature is None:
        return "本机代理"
    scheme, host, port = signature
    display_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{display_host}:{port}"


def _subscription_recovery_proxy_map(value: str) -> dict[str, str] | None:
    """Validate a read-only recovery route to the app's local HTTP proxy.

    Recovery is intentionally restricted to an unauthenticated loopback HTTP
    endpoint. This prevents an internal callback mistake from forwarding a
    subscription URL (which may itself contain a secret) to an arbitrary
    third-party proxy.
    """

    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse.urlparse(raw)
        host = str(parsed.hostname or "").strip().casefold()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.casefold() != "http"
        or host not in _LOOPBACK_PROXY_HOSTS
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or str(parsed.path or "") not in {"", "/"}
    ):
        return None
    display_host = f"[{host}]" if ":" in host else host
    normalized = f"http://{display_host}:{int(port)}"
    return {"http": normalized, "https": normalized}


def _normalize_subscription_recovery_session(
    candidate: object,
) -> _ProxySubscriptionRecoverySession | None:
    """Validate the loopback endpoint and optional bounded route-switch API."""

    if isinstance(candidate, str):
        proxy_url = candidate
    else:
        proxy_url = str(getattr(candidate, "proxy_url", "") or "")
    proxy_map = _subscription_recovery_proxy_map(proxy_url)
    if proxy_map is None:
        return None

    route_selector = getattr(candidate, "select_route", None)
    try:
        route_count = int(getattr(candidate, "route_count", 1) or 1)
    except (TypeError, ValueError, OverflowError):
        route_count = 1
    route_count = max(1, min(AI_PROXY_FALLBACK_MAX_NODES, route_count))
    if route_count > 1 and not callable(route_selector):
        # A plain URL is still a valid one-route recovery endpoint. Never
        # assume that repeatedly using it selects a different upstream node.
        route_count = 1
    return _ProxySubscriptionRecoverySession(
        proxy_map=proxy_map,
        route_count=route_count,
        route_selector=route_selector if callable(route_selector) else None,
    )


@contextmanager
def _subscription_recovery_proxy_context(
    provider: Callable[[float], object] | None,
    *,
    timeout_seconds: float,
):
    """Resolve either a plain loopback URL or a scoped local proxy session."""

    if not callable(provider):
        yield None
        return
    candidate = provider(max(0.001, float(timeout_seconds)))
    enter = getattr(candidate, "__enter__", None)
    exit_context = getattr(candidate, "__exit__", None)
    if callable(enter) and callable(exit_context):
        with candidate as recovery_session:
            yield _normalize_subscription_recovery_session(recovery_session)
        return
    if callable(enter) or callable(exit_context):
        raise TypeError("订阅兜底代理会话接口不完整")
    yield _normalize_subscription_recovery_session(candidate)


def _subscription_proxy_environment_diagnostic(url: str) -> ProxyEnvironmentDiagnostic:
    """Detect an unreachable selected loopback proxy without changing settings.

    ``urllib.getproxies`` may select either an environment value or WinINET.
    Both can cause WinError 10061, so both must trigger an immediate bounded
    direct recovery.  Only matching environment names are eligible for the
    separately ownership-checked cleanup path; an unrelated WinINET proxy is
    reported and bypassed for this download but never changed automatically.
    """

    try:
        proxies = urlrequest.getproxies()
    except Exception:
        return ProxyEnvironmentDiagnostic()
    parsed_url = urlparse.urlparse(str(url or ""))
    scheme = parsed_url.scheme.casefold()
    if scheme not in _SUBSCRIPTION_PROXY_ENV_KEYS:
        return ProxyEnvironmentDiagnostic()
    candidate = str(
        proxies.get(scheme)
        or proxies.get("all")
        or ""
    ).strip()
    signature = _subscription_proxy_url_signature(candidate)
    if signature is None:
        return ProxyEnvironmentDiagnostic()
    _proxy_scheme, host, port = signature
    if host not in _LOOPBACK_PROXY_HOSTS or port is None:
        return ProxyEnvironmentDiagnostic()

    env_names = _SUBSCRIPTION_PROXY_ENV_KEYS[scheme]
    env_name_set = {name.casefold() for name in env_names}
    matching_names: list[str] = []
    for key, value in list(os.environ.items()):
        if str(key).casefold() not in env_name_set:
            continue
        if _subscription_proxy_url_signature(value) == signature:
            matching_names.append(key)
    try:
        connection = socket.create_connection((host, port), timeout=0.25)
        try:
            return ProxyEnvironmentDiagnostic()
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
    except (OSError, TimeoutError):
        # Preserve the original value in the message, but never expose
        # credentials or mutate the user's environment.
        return ProxyEnvironmentDiagnostic(
            invalid_variables=tuple(sorted(set(matching_names))),
            invalid_proxy_urls=(_subscription_proxy_display_url(candidate),),
            invalid_windows_proxy=not matching_names,
        )


def _reconcile_subscription_proxy_environment(
    url: str,
    diagnostic: ProxyEnvironmentDiagnostic,
    *,
    allow_direct_fallback: bool,
) -> ProxyEnvironmentDiagnostic:
    """Auto-clean only app-owned stale proxy residue before a link refresh."""

    if not diagnostic.has_invalid_proxy:
        return diagnostic

    request_action = (
        "本次订阅请求已临时绕过该代理并直连"
        if allow_direct_fallback
        else "严格隐私模式仍禁止直连"
    )
    try:
        # Import lazily: APITester itself imports local proxy helpers only when
        # ownership must be verified, avoiding a module-import cycle here.
        from core.api_tester import APITester

        warning = APITester.reconcile_invalid_local_proxy_for_request(
            url,
            request_action=request_action,
        )
    except Exception:
        # Cleanup diagnostics must never make a recoverable subscription fetch
        # fail. The original detector still forces a bounded direct recovery
        # (when allowed) and reports that no environment value was changed.
        return diagnostic

    if not warning:
        # The endpoint may have recovered during the lock-protected forced
        # recheck. Recompute so a healthy proxy is not bypassed unnecessarily.
        return _subscription_proxy_environment_diagnostic(url)
    return ProxyEnvironmentDiagnostic(
        invalid_variables=diagnostic.invalid_variables,
        invalid_proxy_urls=diagnostic.invalid_proxy_urls,
        invalid_windows_proxy=diagnostic.invalid_windows_proxy,
        reconciled_warning=warning,
    )


def _strict_subscription_proxy_map(request: urlrequest.Request) -> dict[str, str]:
    """Return an explicit loopback proxy for fail-closed subscription downloads."""

    try:
        proxies = urlrequest.getproxies()
    except Exception as exc:
        raise RuntimeError("严格隐私模式未读取到可验证代理，已拒绝直连") from exc
    scheme = str(urlparse.urlparse(request.full_url).scheme or "https").lower()
    candidate = str(
        proxies.get(scheme)
        or proxies.get("all")
        or proxies.get("https")
        or proxies.get("http")
        or ""
    ).strip()
    if not candidate:
        raise RuntimeError("严格隐私模式未配置代理，已拒绝直连订阅链接")
    parsed = urlparse.urlparse(candidate if "://" in candidate else f"http://{candidate}")
    host = str(parsed.hostname or "").strip().casefold()
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("严格隐私模式代理协议无法验证，已拒绝直连")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("严格隐私模式代理端口无效，已拒绝直连") from exc
    if host not in {"127.0.0.1", "localhost", "::1"} or port is None:
        raise RuntimeError("严格隐私仅信任本机 loopback 受管代理，已拒绝直连")
    normalized = candidate if "://" in candidate else f"http://{candidate}"
    return {"http": normalized, "https": normalized}


def _normalize_subscription_timeout(value: int | float) -> float:
    try:
        return min(120.0, max(0.25, float(value)))
    except (TypeError, ValueError):
        return 45.0


def _subscription_request_timeout(
    deadline: float,
    *,
    reserve_seconds: float = 0.0,
) -> float:
    remaining = deadline - time.monotonic() - max(0.0, float(reserve_seconds or 0.0))
    if remaining <= 0:
        raise TimeoutError("订阅下载超过总等待时间")
    # Reserve time for direct recovery and alternate client signatures.
    return max(0.25, min(15.0, remaining))


def _normalize_subscription_max_bytes(value: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return PROXY_SUBSCRIPTION_MAX_BYTES


def _subscription_size_limit_label(max_bytes: int) -> str:
    megabytes = max_bytes / (1024 * 1024)
    return f"{megabytes:g}MB"


def _decode_http_payload(payload: bytes, content_encoding: str, max_bytes: int) -> bytes:
    encoding = (content_encoding or "").lower()
    if "gzip" in encoding:
        decoded = _decompress_limited(payload, max_bytes, 16 + zlib.MAX_WBITS)
    elif "deflate" in encoding:
        try:
            decoded = _decompress_limited(payload, max_bytes, zlib.MAX_WBITS)
        except zlib.error:
            decoded = _decompress_limited(payload, max_bytes, -zlib.MAX_WBITS)
    else:
        return payload

    if len(decoded) > max_bytes:
        raise ValueError("订阅内容解压后超过 5MB，已停止读取")
    return decoded


def _decompress_limited(payload: bytes, max_bytes: int, wbits: int) -> bytes:
    decompressor = zlib.decompressobj(wbits)
    decoded = decompressor.decompress(payload, max_bytes + 1)
    if decompressor.unconsumed_tail or len(decoded) > max_bytes:
        raise ValueError("订阅内容解压后超过 5MB，已停止读取")
    remaining = max_bytes + 1 - len(decoded)
    decoded += decompressor.flush(remaining)
    if len(decoded) > max_bytes:
        raise ValueError("订阅内容解压后超过 5MB，已停止读取")
    return decoded


def _header_value(headers, name: str) -> str:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    for key in (name, name.lower(), name.title()):
        try:
            value = getter(key, "")
        except TypeError:
            value = getter(key)
        if value:
            return str(value)
    return ""


def _response_content_type(headers) -> str:
    getter = getattr(headers, "get_content_type", None)
    if callable(getter):
        try:
            content_type = getter()
        except Exception:
            content_type = ""
        if content_type:
            return str(content_type)
    content_type = _header_value(headers, "Content-Type").split(";", 1)[0].strip()
    return content_type or "text/plain"


def _response_charset(headers) -> str:
    getter = getattr(headers, "get_content_charset", None)
    if callable(getter):
        try:
            charset = getter()
        except Exception:
            charset = ""
        if charset:
            return str(charset)
    content_type = _header_value(headers, "Content-Type")
    match = re.search(r"charset\s*=\s*([^;\s]+)", content_type, flags=re.I)
    return match.group(1).strip("\"'") if match else ""


def _decode_subscription_bytes(payload: bytes, charset: str) -> str:
    seen = set()
    candidates = [charset, "utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"]
    for candidate in candidates:
        encoding = str(candidate or "").strip().strip("\"'")
        if not encoding or encoding.lower() in seen:
            continue
        seen.add(encoding.lower())
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _retry_delay_seconds(retry_base_delay: float, failed_attempt: int) -> float:
    try:
        base_delay = float(retry_base_delay)
    except (TypeError, ValueError):
        base_delay = 1.0
    return min(max(0.0, base_delay) * max(1, failed_attempt), 15.0)


def _looks_like_proxy_node(value: dict) -> bool:
    return all(key in value for key in ("name", "type", "server", "port"))


def _subscription_text_variants(text: str) -> list[str]:
    variants = [text]
    if "://" not in text and not re.search(r"(?m)^[ \t]*proxies\s*:", text):
        decoded = _decode_base64_text(text)
        if decoded and decoded not in variants:
            variants.append(decoded)
    return variants


def _decode_base64_text(text: str) -> str:
    decoded = _decode_base64_payload(text)
    if decoded and ("://" in decoded or re.search(r"(?m)^[ \t]*proxies\s*:", decoded)):
        return decoded
    return ""


def _decode_base64_payload(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 8 or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return ""
    padded = compact + ("=" * (-len(compact) % 4))
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            payload = decoder(padded)
            decoded = payload.decode("utf-8", errors="strict").strip()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded:
            return decoded
    return ""


def _dedupe_proxy_nodes(nodes: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for node in nodes:
        try:
            normalized = _normalize_proxy_node(node)
        except ValueError:
            continue
        if _is_subscription_metadata_node(normalized):
            continue
        key = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
    return unique


def _is_subscription_metadata_node(node: dict) -> bool:
    name = str(node.get("name") or "").strip()
    if not name:
        return False
    compact = re.sub(r"\s+", "", name).lower()
    return any(re.search(pattern, compact, flags=re.I) for pattern in SUBSCRIPTION_METADATA_NODE_NAME_PATTERNS)


def _proxy_subscription_dir() -> Path:
    return STORAGE_DIR / "proxy_subscriptions"


def _proxy_subscription_state_path() -> Path:
    return _proxy_subscription_dir() / "subscription_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _proxy_subscription_cache_path(url: str, payload: bytes, content_type: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    raw_start = payload.lstrip()[:80].lower()
    extension = ".yaml" if "yaml" in content_type or raw_start.startswith((b"proxies:", b"proxy-groups:")) else ".txt"
    return _proxy_subscription_dir() / f"subscription-{digest}{extension}"


def _prune_unreferenced_proxy_subscription_caches(state: dict) -> int:
    """Best-effort removal of managed node caches no profile references.

    Cached subscription documents can contain live node credentials.  Limit
    cleanup to direct children created by this module; never touch imported
    source paths or arbitrary paths supplied through state.
    """

    directory = _proxy_subscription_dir()
    profiles = state.get("profiles") if isinstance(state, dict) else None
    referenced: set[Path] = set()
    for profile in profiles.values() if isinstance(profiles, dict) else ():
        if not isinstance(profile, dict):
            continue
        saved_path = str(profile.get("saved_path") or "").strip()
        if not saved_path:
            continue
        try:
            referenced.add(Path(saved_path).resolve(strict=False))
        except (OSError, RuntimeError):
            continue

    removed = 0
    try:
        candidates = tuple(directory.glob("subscription-*"))
    except OSError:
        return 0
    for candidate in candidates:
        if candidate.suffix.casefold() not in {".yaml", ".yml", ".txt"}:
            continue
        try:
            if candidate.resolve(strict=False) in referenced:
                continue
            candidate.unlink(missing_ok=True)
            removed += 1
        except (OSError, RuntimeError):
            continue
    return removed


def _write_proxy_subscription_cache(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_bytes(payload)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _commit_proxy_subscription_cache_and_state(
    cache_path: Path,
    payload: bytes,
    state: dict,
) -> None:
    """Publish cache bytes and state together, restoring the prior cache on failure."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_existed = cache_path.is_file()
    previous_payload = cache_path.read_bytes() if cache_existed else None
    _write_proxy_subscription_cache(cache_path, payload)
    try:
        persisted_state = _persist_proxy_subscription_state(state)
        _prune_unreferenced_proxy_subscription_caches(persisted_state)
    except Exception as state_error:
        try:
            if previous_payload is None:
                cache_path.unlink(missing_ok=True)
            else:
                _write_proxy_subscription_cache(cache_path, previous_payload)
        except Exception as restore_error:
            raise RuntimeError(
                "订阅状态保存失败，且原缓存恢复失败: " + str(restore_error)
            ) from state_error
        raise


def _extract_proxy_entries(text: str) -> list[str]:
    lines = text.splitlines()
    in_proxies = False
    base_indent = 0
    proxy_indent = 0
    entries: list[str] = []
    collected: list[str] = []

    def flush_current():
        if collected:
            entries.append("\n".join(collected).strip())
            collected.clear()

    for raw_line in lines:
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if not in_proxies:
            inline_match = re.fullmatch(r"proxies\s*:\s*(.+)", stripped)
            if inline_match:
                inline_value = inline_match.group(1).strip()
                return _extract_inline_map_items(inline_value) or [inline_value]
            if re.fullmatch(r"proxies\s*:\s*", stripped):
                in_proxies = True
                base_indent = indent
            continue

        if indent <= base_indent and not stripped.startswith("-"):
            break
        if stripped.startswith("-") and (not collected or indent <= proxy_indent):
            flush_current()
            proxy_indent = indent
            item = stripped[1:].strip()
            if item:
                collected.append(item)
            continue
        if collected:
            collected.append(stripped)
    flush_current()
    return entries


def _extract_standalone_proxy_entries(text: str) -> list[str]:
    entries = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-") or stripped.startswith("{"):
            entries.append(stripped)
    return entries


def _extract_inline_map_items(text: str) -> list[str]:
    items = []
    quote = ""
    escape = False
    start = -1
    depth = 0
    for index, char in enumerate(text):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                items.append(text[start:index + 1].strip())
                start = -1
    return items


def _extract_first_proxy_entry(text: str) -> str:
    entries = _extract_proxy_entries(text)
    return entries[0] if entries else ""


def _extract_first_inline_map(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""
    quote = ""
    escape = False
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1].strip()
    return ""


def _split_top_level(text: str, delimiter: str) -> list[str]:
    parts = []
    current = []
    quote = ""
    escape = False
    depth = 0
    for char in text:
        if escape:
            current.append(char)
            escape = False
            continue
        if char == "\\":
            current.append(char)
            escape = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
        if char == delimiter and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _split_key_value(text: str) -> tuple[str, str]:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == ":":
            key = text[:index].strip().strip("\"'")
            value = text[index + 1:].strip()
            if not key:
                raise ValueError("代理节点包含空字段名")
            return key, value
    raise ValueError(f"代理节点字段缺少冒号: {text}")


def _coerce_scalar(value: str):
    value = value.strip()
    if not value:
        return ""
    if value.startswith("{") and value.endswith("}"):
        return _parse_inline_map(value[1:-1])
    if value.startswith("[") and value.endswith("]"):
        return [_coerce_scalar(part) for part in _split_top_level(value[1:-1], ",")]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in {"null", "none"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            pass
    return value


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _normalize_port(value, label: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label}必须是 1-65535 的整数")
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是 1-65535 的整数") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{label}必须在 1-65535 之间")
    return port


def _dump_yaml(value, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_dump_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{prefix}-")
                lines.append(_dump_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{prefix}{_yaml_scalar(value)}"


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _proxy_env_values(mixed_port: int) -> dict[str, str]:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    proxy_url = f"http://127.0.0.1:{mixed_port}"
    no_proxy = "127.0.0.1,localhost,::1,*.local"
    return {
        "API_SWITCHER_AI_PROXY_URL": proxy_url,
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "all_proxy": proxy_url,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }


def _build_env_file(mixed_port: int) -> str:
    env = _proxy_env_values(mixed_port)
    return "\n".join([
        "# Managed by API切换器. Non-AI domains are DIRECT in mihomo rules.",
        *(f"export {key}={shlex.quote(env[key])}" for key in PROXY_ENV_KEYS),
        "",
    ])


def _build_start_script(config_dir: str, app_dir: str, local_bin_dir: str, mixed_port: int) -> str:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    proxy_unset = "unset " + " ".join(PROXY_ENV_KEYS)
    return f"""#!/bin/sh
set -eu
CONFIG_DIR={shlex.quote(config_dir)}
APP_DIR={shlex.quote(app_dir)}
LOCAL_BIN_DIR={shlex.quote(local_bin_dir)}
PID_FILE="$APP_DIR/ai-proxy.pid"
LOG_FILE="$APP_DIR/ai-proxy.log"
PORT={mixed_port}
RESTART="${{1:-}}"
BIN="$LOCAL_BIN_DIR/mihomo"
if [ ! -x "$BIN" ]; then
  BIN="$(command -v mihomo 2>/dev/null || command -v clash-meta 2>/dev/null || command -v clash 2>/dev/null || true)"
fi
if [ -z "$BIN" ]; then
  echo "mihomo/clash-meta/clash not found" >&2
  exit 1
fi
pid_managed() {{
  pid="$1"
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  if ! command -v ps >/dev/null 2>&1; then
    # Never trust only a reusable PID number.  Without process identity
    # evidence, restart mode must not risk terminating an unrelated process.
    return 1
  fi
  cmd="$(ps -p "$pid" -o comm= -o args= 2>/dev/null || true)"
  case "$cmd" in
    *mihomo*"$CONFIG_DIR"*|*clash*"$CONFIG_DIR"*) return 0 ;;
    *) return 1 ;;
  esac
}}
port_listening() {{
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":$PORT "
    return $?
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -q ":$PORT "
    return $?
  fi
  return 2
}}
if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  case "$old_pid" in ''|*[!0-9]*) old_pid="" ;; esac
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    if pid_managed "$old_pid"; then
      if [ "$RESTART" = "restart" ]; then
        kill "$old_pid" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
          kill -0 "$old_pid" 2>/dev/null || break
          sleep 1
        done
        if kill -0 "$old_pid" 2>/dev/null; then
          kill -9 "$old_pid" 2>/dev/null || true
        fi
      else
        exit 0
      fi
    else
      rm -f "$PID_FILE"
      if port_listening; then
        echo "port $PORT is already listening, but pid file does not identify this tool's managed process $old_pid" >&2
        exit 5
      fi
    fi
  else
    rm -f "$PID_FILE"
  fi
fi
if port_listening; then
  echo "port $PORT is already listening before starting mihomo; please choose another port or stop the existing process" >&2
  exit 6
fi
mkdir -p "$APP_DIR"
if [ -f "$LOG_FILE" ]; then
  log_size="$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)"
  case "$log_size" in ''|*[!0-9]*) log_size=0 ;; esac
  if [ "$log_size" -gt 8388608 ]; then
    rm -f "$LOG_FILE.1"
    mv "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
  fi
fi
printf '\\n--- API-Switcher AI proxy start %s ---\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date 2>/dev/null || true)" >>"$LOG_FILE"
{proxy_unset}
nohup "$BIN" -d "$CONFIG_DIR" >>"$LOG_FILE" 2>&1 &
echo "$!" > "$PID_FILE"
sleep 2
new_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
cleanup_new_process() {{
  if [ -n "$new_pid" ] && kill -0 "$new_pid" 2>/dev/null; then
    kill "$new_pid" 2>/dev/null || true
    for _ in 1 2 3; do
      kill -0 "$new_pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$new_pid" 2>/dev/null; then
      kill -9 "$new_pid" 2>/dev/null || true
    fi
  fi
  rm -f "$PID_FILE"
}}
if [ -z "$new_pid" ] || ! kill -0 "$new_pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "mihomo failed to stay running; see $LOG_FILE" >&2
  exit 2
fi
if ! pid_managed "$new_pid"; then
  cleanup_new_process
  echo "started process is not recognized as this tool's managed mihomo/clash; see $LOG_FILE" >&2
  exit 7
fi
for _ in 1 2 3 4 5; do
  if port_listening; then
    exit 0
  fi
  sleep 1
done
if command -v ss >/dev/null 2>&1 || command -v netstat >/dev/null 2>&1; then
  cleanup_new_process
  echo "mihomo is running but port $PORT is not listening yet; see $LOG_FILE" >&2
  exit 3
fi
echo "mihomo is running; ss/netstat not found, skipped port listening verification"
exit 0
"""


def _build_dead_proxy_reconcile_command(home: str, mixed_port: int) -> str:
    """Build a fail-safe pre-deploy check for stale managed proxy state."""

    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    config_dir = posixpath.join(home, ".config", "mihomo")
    app_dir = posixpath.join(home, ".config", "api-switcher")
    shell_paths = " ".join(shlex.quote(path) for path in _shell_proxy_profile_paths(home))
    vscode_paths = " ".join(
        shlex.quote(posixpath.join(home, path[2:]) if path.startswith("~/") else path)
        for path in VSCODE_SERVER_ENV_SETUP_PATHS
    )
    vscode_settings_paths = " ".join(
        shlex.quote(posixpath.join(home, path[2:]) if path.startswith("~/") else path)
        for path in remote_config.REMOTE_VSCODE_SETTINGS_PATHS
    )
    legacy_config_paths = " ".join(
        shlex.quote(posixpath.join(home, ".config", "clash", filename))
        for filename in ("config.yaml", "config.yml")
    )
    proxy_url = _proxy_env_values(mixed_port)["API_SWITCHER_AI_PROXY_URL"]
    return fr"""set +e
CONFIG_DIR={shlex.quote(config_dir)}
CONFIG_FILE="$CONFIG_DIR/config.yaml"
APP_DIR={shlex.quote(app_dir)}
PID_FILE="$APP_DIR/ai-proxy.pid"
ENV_FILE="$APP_DIR/ai-proxy.env"
START_SCRIPT="$APP_DIR/start-ai-proxy.sh"
LOG_FILE="$APP_DIR/ai-proxy.log"
FISH_FILE={shlex.quote(posixpath.join(home, ".config", "fish", "conf.d", "api-switcher-ai-proxy.fish"))}
PORT={mixed_port}
STARTUP_GRACE={REMOTE_AI_PROXY_STARTUP_GRACE_SECONDS}
PROXY_URL={shlex.quote(proxy_url)}
CONFIG_MARKER={shlex.quote(AI_PROXY_CONFIG_MARKER)}
dirty=no
working=no
repaired_pid=no
removed_pid=no
stopped_pid=""
skipped_pid=""
conflict=no
reason=""
port_listening=unknown
listener_pids=""
managed_listener_pids=""
foreign_listener_pids=""
config_owned=no
configured_port=""

if [ -s "$CONFIG_FILE" ] && grep -qF "$CONFIG_MARKER" "$CONFIG_FILE" 2>/dev/null; then
  config_owned=yes
  configured_port="$(sed -n 's/^[[:space:]]*mixed-port:[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1/p' "$CONFIG_FILE" 2>/dev/null | head -n 1)"
fi

is_managed_pid() {{
  pid="$1"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2>/dev/null || return 1
  command -v ps >/dev/null 2>&1 || return 1
  cmd="$(ps -p "$pid" -o comm= -o args= 2>/dev/null || true)"
  case "$cmd" in
    *mihomo*"$CONFIG_DIR"*|*clash*"$CONFIG_DIR"*) return 0 ;;
    *) return 1 ;;
  esac
}}

stop_managed_pid() {{
  pid="$1"
  [ "$config_owned" = "yes" ] || return 1
  is_managed_pid "$pid" || return 1
  kill -TERM "$pid" 2>/dev/null || true
  wait_count=0
  while kill -0 "$pid" 2>/dev/null && [ "$wait_count" -lt 20 ]; do
    sleep 0.1
    wait_count=$((wait_count + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  stopped_pid="$pid"
  return 0
}}

if command -v ss >/dev/null 2>&1; then
  all_listener_lines="$(ss -ltnp 2>/dev/null)"
  listener_status=$?
  if [ "$listener_status" -eq 0 ]; then
    port_listening=no
    listener_lines="$(printf '%s\n' "$all_listener_lines" | awk -v suffix=":$PORT" '$4 ~ suffix "$" {{print}}')"
    [ -n "$listener_lines" ] && port_listening=yes
    listener_pids="$(printf '%s\n' "$listener_lines" | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u | xargs 2>/dev/null)"
  fi
elif command -v netstat >/dev/null 2>&1; then
  all_listener_lines="$(netstat -ltnp 2>/dev/null)"
  listener_status=$?
  if [ "$listener_status" -eq 0 ]; then
    port_listening=no
    listener_lines="$(printf '%s\n' "$all_listener_lines" | awk -v suffix=":$PORT" '$4 ~ suffix "$" {{print}}')"
    [ -n "$listener_lines" ] && port_listening=yes
    listener_pids="$(printf '%s\n' "$listener_lines" | awk '{{print $7}}' | sed -n 's#/.*##p' | sort -u | xargs 2>/dev/null)"
  fi
elif command -v python3 >/dev/null 2>&1; then
  port_listening=no
  python3 -c 'import socket,sys; sock=socket.socket(); sock.settimeout(0.3); sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$PORT" >/dev/null 2>&1 && port_listening=yes
fi
if [ -z "$listener_pids" ] && command -v lsof >/dev/null 2>&1; then
  listener_pids="$(lsof -nP -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null | sort -u | xargs 2>/dev/null)"
  [ -n "$listener_pids" ] && port_listening=yes
fi
if [ -z "$listener_pids" ] && command -v fuser >/dev/null 2>&1; then
  listener_pids="$(fuser -n tcp "$PORT" 2>/dev/null | tr ' ' '\n' | sed '/^$/d' | sort -u | xargs 2>/dev/null)"
  [ -n "$listener_pids" ] && port_listening=yes
fi

for pid in $listener_pids; do
  case "$pid" in ''|*[!0-9]*) continue ;; esac
  if is_managed_pid "$pid"; then
    managed_listener_pids="$managed_listener_pids $pid"
  else
    foreign_listener_pids="$foreign_listener_pids $pid"
  fi
done
managed_listener_pids="$(printf '%s' "$managed_listener_pids" | xargs 2>/dev/null)"
foreign_listener_pids="$(printf '%s' "$foreign_listener_pids" | xargs 2>/dev/null)"

if [ "$port_listening" = "yes" ]; then
  if [ -z "$listener_pids" ]; then
    conflict=yes
    reason=unknown_owner
  elif [ -n "$foreign_listener_pids" ]; then
    conflict=yes
    reason=foreign_listener
  elif [ "$config_owned" != "yes" ]; then
    conflict=yes
    reason=unowned_config
  else
    set -- $managed_listener_pids
    if [ "$#" -ne 1 ]; then
      conflict=yes
      reason=multiple_managed_listeners
    else
      live_pid="$1"
      saved_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
      if [ "$saved_pid" != "$live_pid" ]; then
        mkdir -p "$APP_DIR"
        printf '%s\n' "$live_pid" > "$PID_FILE"
        chmod 600 "$PID_FILE" 2>/dev/null || true
        repaired_pid=yes
      fi
      working=yes
    fi
  fi
elif [ "$port_listening" = "no" ]; then
  if [ -e "$PID_FILE" ]; then
    saved_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    preserve_pid=no
    case "$saved_pid" in
      ''|*[!0-9]*) ;;
      *)
        if kill -0 "$saved_pid" 2>/dev/null; then
          configured_port_listening=no
          case "$configured_port" in
            ''|*[!0-9]*|"$PORT") ;;
            *)
              if command -v ss >/dev/null 2>&1; then
                ss -ltnp 2>/dev/null | awk -v suffix=":$configured_port" '$4 ~ suffix "$" {{print}}' | grep -q "pid=$saved_pid," && configured_port_listening=yes
              elif command -v netstat >/dev/null 2>&1; then
                netstat -ltnp 2>/dev/null | awk -v suffix=":$configured_port" '$4 ~ suffix "$" {{print $7}}' | grep -q "^$saved_pid/" && configured_port_listening=yes
              fi
              ;;
          esac
          if [ "$configured_port_listening" = "yes" ] && is_managed_pid "$saved_pid"; then
            conflict=yes
            reason=managed_proxy_on_other_port
            preserve_pid=yes
          elif is_managed_pid "$saved_pid"; then
            process_age="$(ps -p "$saved_pid" -o etimes= 2>/dev/null | tr -d '[:space:]' || true)"
            case "$process_age" in
              ''|*[!0-9]*) process_age="" ;;
            esac
            if [ -z "$process_age" ]; then
              conflict=yes
              reason=process_age_unknown
              preserve_pid=yes
            elif [ "$process_age" -lt "$STARTUP_GRACE" ]; then
              conflict=yes
              reason=proxy_starting
              preserve_pid=yes
            elif ! stop_managed_pid "$saved_pid"; then
              skipped_pid="$saved_pid"
            fi
          elif ! stop_managed_pid "$saved_pid"; then
            skipped_pid="$saved_pid"
          fi
        fi
        ;;
    esac
    if [ "$preserve_pid" != "yes" ]; then
      rm -f "$PID_FILE"
      removed_pid=yes
      dirty=yes
    fi
  fi
  if [ "$config_owned" = "yes" ] || [ -e "$ENV_FILE" ] || [ -e "$START_SCRIPT" ] || [ -e "$LOG_FILE" ] || [ -e "$FISH_FILE" ]; then
    dirty=yes
  fi
  for file in {legacy_config_paths}; do
    if [ -f "$file" ] && (grep -qF "$CONFIG_MARKER" "$file" 2>/dev/null || (grep -q "AI-PROXY" "$file" 2>/dev/null && grep -q "chatgpt.com" "$file" 2>/dev/null)); then
      dirty=yes
    fi
  done
  for file in {shell_paths}; do
    [ -f "$file" ] && grep -qF "# >>> API切换器 AI proxy >>>" "$file" 2>/dev/null && dirty=yes
  done
  for file in {vscode_paths}; do
    [ -f "$file" ] && grep -qF {shlex.quote(VSCODE_ENV_BLOCK_START)} "$file" 2>/dev/null && dirty=yes
  done
  for file in {vscode_settings_paths}; do
    [ -f "$file" ] && grep -qF "$PROXY_URL" "$file" 2>/dev/null && dirty=yes
  done
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment 2>/dev/null | grep -Fx -- "API_SWITCHER_AI_PROXY_URL=$PROXY_URL" >/dev/null 2>&1; then
    dirty=yes
  fi
else
  conflict=yes
  reason=listener_check_unavailable
fi

printf 'dirty=%s\nworking=%s\nrepaired_pid=%s\nremoved_pid=%s\nstopped_pid=%s\nskipped_pid=%s\nconflict=%s\nreason=%s\nport_listening=%s\nlistener_pids=%s\nmanaged_listener_pids=%s\nforeign_listener_pids=%s\nconfig_owned=%s\nconfigured_port=%s\n' "$dirty" "$working" "$repaired_pid" "$removed_pid" "$stopped_pid" "$skipped_pid" "$conflict" "$reason" "$port_listening" "$listener_pids" "$managed_listener_pids" "$foreign_listener_pids" "$config_owned" "$configured_port"
"""


def _build_cleanup_command(
    home: str,
    mixed_port: int,
    include_legacy_config: bool = True,
    *,
    stale_only: bool = False,
) -> str:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    env = _proxy_env_values(mixed_port)
    template = r'''set +e
HOME_DIR=__HOME_DIR__
PORT=__PORT__
INCLUDE_LEGACY_CONFIG=__INCLUDE_LEGACY_CONFIG__
STALE_ONLY=__STALE_ONLY__
CONFIG_MARKER=__CONFIG_MARKER__
PROXY_URL=__PROXY_URL__
NO_PROXY_VALUE=__NO_PROXY_VALUE__
APP_DIR="$HOME_DIR/.config/api-switcher"
CONFIG_DIR="$HOME_DIR/.config/mihomo"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
ENV_FILE="$APP_DIR/ai-proxy.env"
START_SCRIPT="$APP_DIR/start-ai-proxy.sh"
PID_FILE="$APP_DIR/ai-proxy.pid"
LOG_FILE="$APP_DIR/ai-proxy.log"
removed_files=0
removed_blocks=0
removed_settings=0
removed_systemd_env=0
backed_up_configs=0
stopped_pids=""
skipped_pids=""
notes=""
backup_dir=""
config_owned=no
protected_running=no
protected_unknown=no
protected_reason=""

if [ -s "$CONFIG_FILE" ] && grep -qF "$CONFIG_MARKER" "$CONFIG_FILE" 2>/dev/null; then
  config_owned=yes
fi

refresh_current_port_state() {
  current_port_state=unknown
  if command -v ss >/dev/null 2>&1; then
    if current_listener_lines="$(ss -ltn 2>/dev/null)"; then
      current_port_state=no
      printf '%s\n' "$current_listener_lines" | awk -v port=":$PORT" '$4 ~ (port "$") {found=1} END {exit found ? 0 : 1}' && current_port_state=yes
    fi
  elif command -v netstat >/dev/null 2>&1; then
    if current_listener_lines="$(netstat -ltn 2>/dev/null)"; then
      current_port_state=no
      printf '%s\n' "$current_listener_lines" | awk -v port=":$PORT" '$4 ~ (port "$") {found=1} END {exit found ? 0 : 1}' && current_port_state=yes
    fi
  elif command -v python3 >/dev/null 2>&1; then
    current_port_state=no
    python3 -c 'import socket,sys; sock=socket.socket(); sock.settimeout(0.3); sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$PORT" >/dev/null 2>&1 && current_port_state=yes
  elif command -v lsof >/dev/null 2>&1; then
    current_port_state=no
    lsof -nP -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null | grep -q . && current_port_state=yes
  elif command -v fuser >/dev/null 2>&1; then
    current_port_state=no
    fuser -n tcp "$PORT" >/dev/null 2>&1 && current_port_state=yes
  fi
}

protect_stale_cleanup_if_needed() {
  if [ "$current_port_state" = "yes" ]; then
    printf 'protected_running=yes\nprotected_reason=listener\nstill_listening=yes\n'
    exit 0
  fi
  if [ "$current_port_state" != "no" ]; then
    printf 'protected_unknown=yes\nprotected_reason=listener_check\nstill_listening=unknown\n'
    exit 0
  fi
}

# A stale-only cleanup must never turn into a normal teardown because another
# SSH/VS Code login started the proxy after the first inspection. Recheck the
# requested port immediately before any process or file mutation and fail safe
# when the remote host cannot provide a listener check.
if [ "$STALE_ONLY" = "1" ]; then
  refresh_current_port_state
  protect_stale_cleanup_if_needed
fi

append_note() {
  if [ -n "$notes" ]; then
    notes="$notes; $1"
  else
    notes="$1"
  fi
}

is_managed_proxy_pid() {
  pid="$1"
  [ -n "$pid" ] || return 1
  [ "$config_owned" = "yes" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if ! command -v ps >/dev/null 2>&1; then
    return 1
  fi
  cmd="$(ps -p "$pid" -o comm= -o args= 2>/dev/null || true)"
  case "$cmd" in
    *mihomo*"$CONFIG_DIR"*|*clash*"$CONFIG_DIR"*) return 0 ;;
    *) return 1 ;;
  esac
}

stop_pid_if_proxy() {
  pid="$1"
  case "$pid" in ''|*[!0-9]*) return 0 ;; esac
  if [ "$STALE_ONLY" = "1" ] && kill -0 "$pid" 2>/dev/null; then
    protected_running=yes
    skipped_pids="$skipped_pids $pid"
    return 0
  fi
  if is_managed_proxy_pid "$pid"; then
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    stopped_pids="$stopped_pids $pid"
  elif kill -0 "$pid" 2>/dev/null; then
    skipped_pids="$skipped_pids $pid"
  fi
}

if [ -s "$PID_FILE" ]; then
  saved_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  case "$saved_pid" in ''|*[!0-9]*) saved_pid="" ;; esac
  [ -z "$saved_pid" ] || stop_pid_if_proxy "$saved_pid"
fi
if command -v lsof >/dev/null 2>&1; then
  for pid in $(lsof -nP -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null | sort -u); do
    stop_pid_if_proxy "$pid"
  done
fi
if command -v ss >/dev/null 2>&1; then
  for pid in $(ss -ltnp 2>/dev/null | awk -v port=":$PORT" '$4 ~ (port "$") {print $0}' | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u); do
    stop_pid_if_proxy "$pid"
  done
fi
if command -v netstat >/dev/null 2>&1; then
  for pid in $(netstat -ltnp 2>/dev/null | awk -v port=":$PORT" '$4 ~ (port "$") {print $7}' | sed -n 's#/.*##p' | sort -u); do
    stop_pid_if_proxy "$pid"
  done
fi
if command -v fuser >/dev/null 2>&1; then
  for pid in $(fuser -n tcp "$PORT" 2>/dev/null | tr ' ' '\n' | sort -u); do
    stop_pid_if_proxy "$pid"
  done
fi

if [ "$STALE_ONLY" = "1" ]; then
  if [ "$protected_running" = "yes" ]; then
    printf 'protected_running=yes\nprotected_reason=live_process\nstill_listening=unknown\nskipped_pids=%s\n' "$(echo "$skipped_pids" | xargs 2>/dev/null)"
    exit 0
  fi
  # Narrow the remaining race with a process that wrote no PID at the first
  # scan but began listening while ownership checks were running.
  refresh_current_port_state
  protect_stale_cleanup_if_needed
fi

for file in "$ENV_FILE" "$START_SCRIPT" "$PID_FILE" "$LOG_FILE"; do
  if [ -e "$file" ]; then
    rm -f "$file" && removed_files=$((removed_files + 1))
  fi
done

backup_file() {
  file="$1"
  [ -f "$file" ] || return 0
  stamp="$(date -u '+%Y%m%dT%H%M%SZ' 2>/dev/null || date '+%Y%m%d%H%M%S')"
  if [ -z "$backup_dir" ]; then
    backup_dir="$APP_DIR/proxy-cleanup-backup-$stamp"
  fi
  mkdir -p "$backup_dir" || return 1
  relative="$(printf '%s' "$file" | sed "s#^$HOME_DIR/##; s#/#_#g")"
  target="$backup_dir/$relative"
  mv "$file" "$target" && backed_up_configs=$((backed_up_configs + 1))
}

clean_config() {
  file="$1"
  [ -f "$file" ] || return 0
  if grep -q "$CONFIG_MARKER" "$file" 2>/dev/null || (grep -q "AI-PROXY" "$file" 2>/dev/null && grep -q "chatgpt.com" "$file" 2>/dev/null); then
    rm -f "$file" && removed_files=$((removed_files + 1))
    return 0
  fi
  if [ "$INCLUDE_LEGACY_CONFIG" = "1" ] && (grep -Eq '^[[:space:]]*(port|socks-port|mixed-port|proxies|proxy-groups|rules):' "$file" 2>/dev/null || grep -q "chatgpt.com" "$file" 2>/dev/null); then
    backup_file "$file"
  else
    append_note "保留 $file（不像本工具 AI 代理配置）"
  fi
}

clean_config "$CONFIG_FILE"
clean_config "$HOME_DIR/.config/clash/config.yaml"
clean_config "$HOME_DIR/.config/clash/config.yml"
rmdir "$CONFIG_DIR" "$HOME_DIR/.config/clash" "$APP_DIR" 2>/dev/null || true

remove_block() {
  file="$1"
  start="$2"
  end="$3"
  [ -f "$file" ] || return 0
  grep -qF "$start" "$file" 2>/dev/null || return 0
  tmp="$file.api-switcher-clean.$$"
  awk -v start="$start" -v end="$end" '
    $0 == start {skip=1; changed=1; next}
    $0 == end {skip=0; next}
    skip != 1 {print}
    END {if (skip == 1) exit 2; if (changed != 1) exit 3}
  ' "$file" > "$tmp"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    mv "$tmp" "$file" && removed_blocks=$((removed_blocks + 1))
  else
    rm -f "$tmp"
    append_note "未能安全移除 $file 的 managed block"
  fi
}

for file in "$HOME_DIR/.profile" "$HOME_DIR/.bashrc" "$HOME_DIR/.bash_profile" "$HOME_DIR/.bash_login" "$HOME_DIR/.zprofile" "$HOME_DIR/.zshrc"; do
  remove_block "$file" "# >>> API切换器 AI proxy >>>" "# <<< API切换器 AI proxy <<<"
done
if [ -e "$HOME_DIR/.config/fish/conf.d/api-switcher-ai-proxy.fish" ]; then
  rm -f "$HOME_DIR/.config/fish/conf.d/api-switcher-ai-proxy.fish" && removed_files=$((removed_files + 1))
fi
for file in "$HOME_DIR/.vscode-server/server-env-setup" "$HOME_DIR/.vscode-server-insiders/server-env-setup" "$HOME_DIR/.cursor-server/server-env-setup"; do
  remove_block "$file" "# >>> API切换器 AI proxy VS Code >>>" "# <<< API切换器 AI proxy VS Code <<<"
done

# Desktop sessions may import ~/.profile into the systemd user manager.  Clear
# only values that still point at this tool's loopback proxy; the managed marker
# proves ownership before NO_PROXY is removed as well.
if command -v systemctl >/dev/null 2>&1; then
  systemd_env="$(systemctl --user show-environment 2>/dev/null || true)"
  unset_keys=""
  managed_systemd_env=no
  for key in API_SWITCHER_AI_PROXY_URL HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
    if printf '%s\n' "$systemd_env" | grep -Fx -- "$key=$PROXY_URL" >/dev/null 2>&1; then
      unset_keys="$unset_keys $key"
      [ "$key" = "API_SWITCHER_AI_PROXY_URL" ] && managed_systemd_env=yes
    fi
  done
  if [ "$managed_systemd_env" = "yes" ]; then
    unset_keys="$unset_keys NO_PROXY no_proxy"
  fi
  if [ -n "$unset_keys" ]; then
    if systemctl --user unset-environment $unset_keys >/dev/null 2>&1; then
      set -- $unset_keys
      removed_systemd_env=$#
    else
      append_note "未能清理 systemd 用户会话中的代理变量"
    fi
  fi
fi

if command -v python3 >/dev/null 2>&1; then
  settings_count="$(python3 - "$PROXY_URL" "$NO_PROXY_VALUE" <<'PY'
import json
import os
import sys
import tempfile

proxy_url = sys.argv[1]
no_proxy = sys.argv[2]
paths = [
    "~/.vscode-server/data/Machine/settings.json",
    "~/.vscode-server-insiders/data/Machine/settings.json",
    "~/.cursor-server/data/Machine/settings.json",
]
proxy_keys = {
    "API_SWITCHER_AI_PROXY_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
}
changed_count = 0
for raw_path in paths:
    path = os.path.expanduser(raw_path)
    if not os.path.isfile(path):
        continue
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception:
        continue
    if not isinstance(data, dict):
        continue
    changed = False
    removed_http_proxy = False
    if data.get("http.proxy") == proxy_url:
        data.pop("http.proxy", None)
        changed = True
        removed_http_proxy = True
    if removed_http_proxy and data.get("http.proxySupport") == "override":
        data.pop("http.proxySupport", None)
        changed = True
    env = data.get("terminal.integrated.env.linux")
    if isinstance(env, dict):
        updated_env = dict(env)
        for key in proxy_keys:
            value = updated_env.get(key)
            if value == proxy_url or value == no_proxy:
                updated_env.pop(key, None)
                changed = True
        if changed:
            if updated_env:
                data["terminal.integrated.env.linux"] = updated_env
            else:
                data.pop("terminal.integrated.env.linux", None)
    if changed:
        directory = os.path.dirname(path)
        fd, temp_path = tempfile.mkstemp(prefix="settings.json.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_path, path)
            changed_count += 1
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
print(changed_count)
PY
)"
  case "$settings_count" in ''|*[!0-9]*) settings_count=0 ;; esac
  removed_settings="$settings_count"
else
  append_note "远端无 python3，跳过 VS Code settings JSON 清理"
fi

still_listening=no
listener_detail=""
if command -v ss >/dev/null 2>&1; then
  listener_detail="$(ss -ltnp 2>/dev/null | awk -v port=":$PORT" '$4 ~ (port "$") {print $0}' | head -n 3)"
  [ -n "$listener_detail" ] && still_listening=yes
elif command -v netstat >/dev/null 2>&1; then
  listener_detail="$(netstat -ltnp 2>/dev/null | awk -v port=":$PORT" '$4 ~ (port "$") {print $0}' | head -n 3)"
  [ -n "$listener_detail" ] && still_listening=yes
else
  still_listening=unknown
fi

printf 'removed_files=%s\nremoved_blocks=%s\nremoved_settings=%s\nremoved_systemd_env=%s\nbacked_up_configs=%s\nbackup_dir=%s\nstopped_pids=%s\nskipped_pids=%s\nstill_listening=%s\nprotected_running=%s\nprotected_unknown=%s\nprotected_reason=%s\n' "$removed_files" "$removed_blocks" "$removed_settings" "$removed_systemd_env" "$backed_up_configs" "$backup_dir" "$(echo "$stopped_pids" | xargs 2>/dev/null)" "$(echo "$skipped_pids" | xargs 2>/dev/null)" "$still_listening" "$protected_running" "$protected_unknown" "$protected_reason"
if [ -n "$listener_detail" ]; then
  printf 'listener_detail=%s\n' "$(echo "$listener_detail" | tr '\n' ' ' | cut -c 1-500)"
fi
if [ -n "$notes" ]; then
  printf 'notes=%s\n' "$notes"
fi
'''
    replacements = {
        "__HOME_DIR__": shlex.quote(home),
        "__PORT__": str(mixed_port),
        "__INCLUDE_LEGACY_CONFIG__": "1" if include_legacy_config else "0",
        "__STALE_ONLY__": "1" if stale_only else "0",
        "__CONFIG_MARKER__": shlex.quote(AI_PROXY_CONFIG_MARKER),
        "__PROXY_URL__": shlex.quote(env["API_SWITCHER_AI_PROXY_URL"]),
        "__NO_PROXY_VALUE__": shlex.quote(env["NO_PROXY"]),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def _build_probe_command(
    mixed_port: int,
    timeout: int = 8,
    *,
    rounds: int = 1,
    strict: bool = False,
) -> str:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    try:
        timeout = max(1, min(60, int(timeout)))
    except (TypeError, ValueError):
        timeout = 8
    try:
        rounds = max(1, min(3, int(rounds)))
    except (TypeError, ValueError):
        rounds = 1
    targets = REMOTE_AI_STABILITY_TARGETS if strict else REMOTE_AI_PROBE_TARGETS
    targets_json = json.dumps(targets, ensure_ascii=False)
    curl_probes = "\n".join(
        f"probe_curl {shlex.quote(label)} {shlex.quote(url)}"
        for label, url in targets
    )
    return f"""set -u
PROXY=http://127.0.0.1:{mixed_port}
TIMEOUT={timeout}
ROUNDS={rounds}
STRICT={1 if strict else 0}
TARGETS_JSON={shlex.quote(targets_json)}
if command -v python3 >/dev/null 2>&1; then
  python3 - "$PROXY" "$TIMEOUT" "$TARGETS_JSON" "$ROUNDS" "$STRICT" <<'PY'
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request

proxy_url = sys.argv[1]
timeout = float(sys.argv[2])
targets = json.loads(sys.argv[3])
rounds = max(1, min(3, int(sys.argv[4])))
strict = sys.argv[5] == "1"
compact_label = {REMOTE_CODEX_COMPACT_PROBE_LABEL!r}
compact_url = {REMOTE_CODEX_COMPACT_PROBE_URL!r}
compact_payload_bytes = {REMOTE_CODEX_COMPACT_PROBE_PAYLOAD_BYTES}
for key in ("NO_PROXY", "no_proxy"):
    os.environ.pop(key, None)

def clean(value):
    return str(value or "").replace("\\t", " ").replace("\\r", " ").replace("\\n", " ")[:180]

def parse_error(body):
    try:
        parsed = json.loads(body)
    except Exception:
        return {{}}
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return error if isinstance(error, dict) else {{}}

def read_compact_body(stream, deadline, max_bytes=65536):
    headers = getattr(stream, "headers", None)
    declared = None
    getter = getattr(headers, "get", None)
    if callable(getter):
        raw_length = str(getter("Content-Length", "") or "").strip()
        if raw_length:
            try:
                declared = int(raw_length)
            except ValueError as exc:
                raise ValueError("compact Content-Length 非整数") from exc
            if declared < 0 or declared > max_bytes:
                raise ValueError("compact Content-Length 超出限制")
    payload = bytearray()
    while len(payload) <= max_bytes:
        if time.monotonic() >= deadline:
            raise TimeoutError("compact 响应读取超过总截止时间")
        chunk = stream.read(min(16 * 1024, max_bytes + 1 - len(payload)))
        if time.monotonic() >= deadline:
            raise TimeoutError("compact 响应读取超过总截止时间")
        if not chunk:
            break
        payload.extend(chunk)
    if len(payload) > max_bytes:
        raise ValueError("compact 响应超出大小限制")
    if declared is not None and len(payload) != declared:
        raise ValueError(
            f"compact 响应截断: Content-Length={{declared}}，实收 {{len(payload)}}"
        )
    return bytes(payload).decode("utf-8", errors="replace")

def classify_compact(code, body):
    if code != 401:
        return False, f"HTTP {{code or '无状态'}}，未收到预期的无认证响应"
    error = parse_error(body)
    message = str(error.get("message") or "").casefold()
    error_type = str(error.get("type") or error.get("code") or "").casefold()
    challenge = any(
        token in f"{{message}} {{error_type}}"
        for token in ("api key", "authentication", "unauthorized", "invalid_api_key")
    )
    if not error or not challenge:
        return False, "HTTP 401，但缺少 OpenAI 结构化认证错误"
    return True, "HTTP 401 OpenAI 无认证结构完整（128KiB 固定预检）"

def classify(label, code, body):
    if not strict:
        return 0 < code < 500, f"HTTP {{code}}" if code else "no status"
    if label == "ChatGPT 出口":
        trace = {{}}
        for line in body.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip():
                trace[key.strip().lower()] = value.strip()
        loc = str(trace.get("loc") or "").upper()
        identified = code == 200 and bool(loc) and bool(trace.get("ip") or trace.get("colo"))
        if not identified:
            return False, f"HTTP {{code}}，未识别 ChatGPT trace"
        if loc == "HK":
            return False, "HTTP 200，ChatGPT 实际出口 loc=HK（香港）"
        return True, f"HTTP 200，ChatGPT 实际出口 loc={{loc}}"
    error = parse_error(body)
    message = str(error.get("message") or "").casefold()
    error_type = str(error.get("type") or error.get("status") or "").casefold()
    if label == "OpenAI API":
        auth_error = any(
            token in f"{{message}} {{error_type}}"
            for token in ("api key", "authentication", "unauthorized", "invalid_api_key")
        )
        ok = code == 401 and bool(error) and auth_error
        return ok, f"HTTP {{code}}" + ("，OpenAI API 身份已校验" if ok else "，OpenAI API 响应不合格")
    if label == "Claude/Anthropic":
        auth_error = any(
            token in f"{{message}} {{error_type}}"
            for token in ("api key", "authentication", "unauthorized", "x-api-key", "anthropic-version")
        )
        ok = code in {{400, 401}} and bool(error) and auth_error
        return ok, f"HTTP {{code}}" + ("，Anthropic 身份已校验" if ok else "，Anthropic 响应不合格")
    if label == "Gemini/Google AI":
        credential_error = any(
            token in message
            for token in ("api key", "credential", "unregistered caller")
        )
        policy_error = any(
            token in message
            for token in ("region", "country", "location", "not supported", "blocked")
        )
        ok = code in {{400, 401, 403}} and bool(error) and credential_error and not policy_error
        return ok, f"HTTP {{code}}" + ("，Google AI 身份已校验" if ok else "，Google AI 响应不合格")
    return False, "未知 AI 探测目标"

def probe(target):
    label, url = target
    started = time.monotonic()
    code = 0
    body = ""
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({{"http": proxy_url, "https": proxy_url}})
        )
        request = urllib.request.Request(
            url,
            headers={{"Accept": "application/json,text/plain,*/*", "User-Agent": "API-Switcher/1.0"}},
        )
        with opener.open(request, timeout=timeout) as response:
            code = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
            body = response.read(65536).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        try:
            body = exc.read(65536).decode("utf-8", errors="replace")
        except Exception:
            body = ""
    except Exception as exc:
        return label, False, clean(exc), max(0, int((time.monotonic() - started) * 1000))
    ok, detail = classify(label, code, body)
    return label, ok, detail, max(0, int((time.monotonic() - started) * 1000))

def probe_compact():
    started = time.monotonic()
    deadline = started + timeout
    prefix = b'{{"model":"api-switcher-network-probe-no-model","input":[{{"role":"user","content":"'
    suffix = b'"}}]}}'
    if compact_payload_bytes <= len(prefix) + len(suffix):
        return compact_label, False, "compact 固定请求体过小", 0
    payload = prefix + (b"x" * (compact_payload_bytes - len(prefix) - len(suffix))) + suffix
    code = 0
    body = ""
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({{"http": proxy_url, "https": proxy_url}})
        )
        request = urllib.request.Request(
            compact_url,
            data=payload,
            method="POST",
            headers={{
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "API-Switcher/1.0",
            }},
        )
        with opener.open(request, timeout=timeout) as response:
            code = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
            body = read_compact_body(response, deadline)
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        try:
            body = read_compact_body(exc, deadline)
        except Exception as body_exc:
            return compact_label, False, clean(body_exc), max(0, int((time.monotonic() - started) * 1000))
        finally:
            try:
                exc.close()
            except Exception:
                pass
    except Exception as exc:
        return compact_label, False, clean(exc), max(0, int((time.monotonic() - started) * 1000))
    ok, detail = classify_compact(code, body)
    return compact_label, ok, detail, max(0, int((time.monotonic() - started) * 1000))

for round_index in range(1, rounds + 1):
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as executor:
        futures = [executor.submit(probe, target) for target in targets]
        for future in concurrent.futures.as_completed(futures):
            label, ok, detail, elapsed = future.result()
            detail = f"第{{round_index}}/{{rounds}}轮 {{detail}}"
            print(
                f"probe\\t{{clean(label)}}\\t{{1 if ok else 0}}\\t{{clean(detail)}}\\t{{elapsed}}",
                flush=True,
            )
if strict:
    label, ok, detail, elapsed = probe_compact()
    print(
        f"probe\\t{{clean(label)}}\\t{{1 if ok else 0}}\\t{{clean(detail)}}\\t{{elapsed}}",
        flush=True,
    )
PY
  exit $?
fi
if [ "$STRICT" = "1" ]; then
  echo "远端未安装 python3，无法执行严格的多轮 AI 稳定性验证" >&2
  exit 11
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "远端未安装 python3/curl，无法测试代理连通性" >&2
  exit 11
fi
TMP_ERR="${{TMPDIR:-/tmp}}/api-switcher-ai-proxy-probe.$$.err"
cleanup() {{
  rm -f "$TMP_ERR"
}}
trap cleanup EXIT HUP INT TERM
probe_curl() {{
  label="$1"
  url="$2"
  http_code=""
  rc=0
  http_code="$(env -u NO_PROXY -u no_proxy curl --noproxy '' -x "$PROXY" -m "$TIMEOUT" -sS -o /dev/null -w "%{{http_code}}" "$url" 2>"$TMP_ERR")" || rc=$?
  ok=0
  detail=""
  case "$http_code" in
    ''|*[!0-9]*)
      detail="$(head -n 1 "$TMP_ERR" 2>/dev/null | tr '\\t\\r\\n' '   ' | cut -c 1-180)"
      [ -n "$detail" ] || detail="curl exit $rc"
      ;;
    *)
      detail="HTTP $http_code"
      if [ "$http_code" -gt 0 ] && [ "$http_code" -lt 500 ]; then
        ok=1
      elif [ "$rc" -ne 0 ]; then
        err="$(head -n 1 "$TMP_ERR" 2>/dev/null | tr '\\t\\r\\n' '   ' | cut -c 1-180)"
        [ -n "$err" ] && detail="$detail $err"
      fi
      ;;
  esac
  printf 'probe\\t%s\\t%s\\t%s\\t\\n' "$label" "$ok" "$detail"
}}
{curl_probes}
"""


def _build_remote_latency_command(
    timeout: float = 3.0,
    attempts: int = 2,
    max_workers: int = PROXY_LATENCY_DEFAULT_MAX_WORKERS,
) -> str:
    timeout = _normalize_timeout(timeout, 3.0)
    attempts = max(1, _int_or_default(attempts, 2))
    max_workers = max(
        1,
        min(
            _int_or_default(max_workers, PROXY_LATENCY_DEFAULT_MAX_WORKERS),
            PROXY_LATENCY_MAX_WORKERS,
        ),
    )
    return f"""set -u
if ! command -v python3 >/dev/null 2>&1; then
  echo "远端未安装 python3，无法批量测试节点延迟" >&2
  exit 11
fi
TMP_INPUT="${{TMPDIR:-/tmp}}/api-switcher-node-latency.$$.json"
cleanup_latency_input() {{
  rm -f "$TMP_INPUT"
}}
trap cleanup_latency_input EXIT HUP INT TERM
cat > "$TMP_INPUT"
python3 - "$TMP_INPUT" <<'PY'
import concurrent.futures
import json
import socket
import sys
import time

TIMEOUT = {timeout!r}
ATTEMPTS = {attempts}
MAX_WORKERS = {max_workers}

def clean(value):
    return str(value or "").replace("\\t", " ").replace("\\r", " ").replace("\\n", " ")[:180]

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        nodes = json.load(handle)
except Exception as exc:
    print(f"读取节点列表失败: {{clean(exc)}}", file=sys.stderr)
    sys.exit(12)

def measure(item):
    key = clean(item.get("key"))
    server = clean(item.get("server"))
    try:
        port = int(item.get("port"))
    except Exception:
        return key, 0, "", "端口无效"
    latencies = []
    detail = ""
    for _ in range(max(1, ATTEMPTS)):
        started = time.perf_counter()
        try:
            with socket.create_connection((server, port), timeout=TIMEOUT):
                latencies.append(max(1, int((time.perf_counter() - started) * 1000)))
        except Exception as exc:
            detail = clean(exc) or exc.__class__.__name__
    if latencies:
        return key, 1, str(min(latencies)), ""
    return key, 0, "", detail or "TCP 连接失败"

workers = max(1, min(MAX_WORKERS, len(nodes) or 1))
with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
    futures = {{
        executor.submit(measure, item): clean(item.get("key"))
        for item in nodes
        if isinstance(item, dict)
    }}
    for future in concurrent.futures.as_completed(futures):
        expected_key = futures[future]
        try:
            key, ok, latency, detail = future.result()
        except Exception as exc:
            key, ok, latency, detail = expected_key, 0, "", clean(exc) or exc.__class__.__name__
        if key:
            print(f"latency\\t{{key}}\\t{{ok}}\\t{{latency}}\\t{{clean(detail)}}\\t{{ATTEMPTS}}", flush=True)
PY
"""


def _build_reload_command(config_path: str, mixed_port: int) -> str:
    controller = f"http://127.0.0.1:{mihomo_controller_port(mixed_port)}"
    payload = json.dumps({"path": config_path}, ensure_ascii=False)
    return f"""set -eu
URL={shlex.quote(controller + "/configs?force=true")}
PAYLOAD={shlex.quote(payload)}
if command -v python3 >/dev/null 2>&1; then
  python3 - "$URL" "$PAYLOAD" <<'PY'
import sys
import urllib.request

url = sys.argv[1]
payload = sys.argv[2].encode("utf-8")
request = urllib.request.Request(
    url,
    data=payload,
    headers={{"Content-Type": "application/json"}},
    method="PUT",
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
with opener.open(request, timeout=8) as response:
    code = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
    if not (200 <= code < 300):
        raise SystemExit(f"mihomo reload HTTP {{code}}")
PY
  exit $?
fi
if command -v curl >/dev/null 2>&1; then
  curl --noproxy '*' -fsS -X PUT -H 'Content-Type: application/json' --data "$PAYLOAD" "$URL" >/dev/null
  exit $?
fi
echo "远端未安装 python3/curl，无法调用 mihomo 热更新接口" >&2
exit 12
"""


def _remote_latency_command_timeout(
    node_count: int,
    timeout: float,
    attempts: int,
    max_workers: int = PROXY_LATENCY_DEFAULT_MAX_WORKERS,
) -> int:
    workers = max(
        1,
        min(
            _int_or_default(max_workers, PROXY_LATENCY_DEFAULT_MAX_WORKERS),
            PROXY_LATENCY_MAX_WORKERS,
            max(1, int(node_count or 1)),
        ),
    )
    batches = (max(1, int(node_count or 1)) + workers - 1) // workers
    return max(45, min(300, int(batches * max(1, attempts) * _normalize_timeout(timeout, 3.0) + 30)))


def _build_ensure_mihomo_command(
    home: str,
    *,
    app_dir: str | None = None,
    local_bin_dir: str | None = None,
    force_check: bool = False,
) -> str:
    """Build an atomic, digest-checked managed mihomo installer/updater."""

    app_dir = app_dir or posixpath.join(home, ".config", "api-switcher")
    local_bin_dir = local_bin_dir or posixpath.join(home, ".local", "bin")
    template = r"""set -eu
HOME_DIR=__HOME_DIR__
APP_DIR=__APP_DIR__
LOCAL_BIN_DIR=__LOCAL_BIN_DIR__
FORCE_CHECK=__FORCE_CHECK__
BIN="$LOCAL_BIN_DIR/mihomo"
RELEASE_STATE="$APP_DIR/mihomo-release.json"
mkdir -p "$APP_DIR" "$LOCAL_BIN_DIR"
chmod 700 "$APP_DIR" 2>/dev/null || true
arch="$(uname -m 2>/dev/null || echo unknown)"
case "$arch" in
  x86_64|amd64) pattern="linux-amd64" ;;
  aarch64|arm64) pattern="linux-arm64" ;;
  armv7l|armv7*) pattern="linux-armv7" ;;
  *) echo "不支持的远端架构: $arch" >&2; exit 3 ;;
esac

if command -v python3 >/dev/null 2>&1; then
  if ! python3 - "$pattern" "$BIN" "$RELEASE_STATE" "$FORCE_CHECK" <<'PY'
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

pattern, target, state_path, force_value = sys.argv[1:]
force_check = force_value == "1"
release_url = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
metadata_limit = 4 * 1024 * 1024
asset_limit = 256 * 1024 * 1024
binary_limit = 256 * 1024 * 1024


def safe_error(error):
    text = str(error or "unknown error").strip() or "unknown error"
    text = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1***@", text)
    text = re.sub(r"(?i)([?&](?:token|sig|signature|key|auth)=)[^&\s]+", r"\1***", text)
    return text[:600]


def read_state():
    try:
        if os.path.getsize(state_path) > 1024 * 1024:
            return {}
        with open(state_path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def write_state(value):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".mihomo-release-", dir=os.path.dirname(state_path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, state_path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def version_from(value):
    match = re.search(r"(?<!\d)v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", str(value or ""))
    return match.group(1) if match else ""


def version_key(value):
    version = version_from(value)
    if not version:
        return (-1, -1, -1)
    return tuple(int(part) for part in version.split("-", 1)[0].split(".")[:3])


def probe_binary(path):
    try:
        completed = subprocess.run(
            [path, "-v"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return None
    detail = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), "")
    version = version_from(detail)
    if completed.returncode != 0 or not version or "mihomo" not in detail.casefold():
        return None
    return version, detail[:300]


def read_response(response, limit, label):
    content_length = response.headers.get("Content-Length")
    try:
        declared = int(content_length) if content_length else 0
    except (TypeError, ValueError):
        declared = 0
    if declared > limit:
        raise RuntimeError(f"{label} response too large: {declared}")
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise RuntimeError(f"{label} response exceeds {limit} bytes")
    return payload


def read_url(url, timeout, limit, label):
    last_error = None
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(1, 4):
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json, application/octet-stream, */*",
                "User-Agent": "API-Switcher/1.0",
            },
        )
        openers = [urllib.request.urlopen]
        if any(urllib.request.getproxies().get(key) for key in ("http", "https", "all")):
            openers.append(direct.open)
        errors = []
        for opener in openers:
            try:
                with opener(request, timeout=timeout) as response:
                    return read_response(response, limit, label)
            except Exception as exc:
                last_error = exc
                errors.append(safe_error(exc))
        if attempt < 3:
            time.sleep(attempt)
    detail = "; ".join(dict.fromkeys(errors)) if errors else safe_error(last_error)
    raise RuntimeError(f"{label} failed after 3 attempts: {detail}") from last_error


state = read_state()
current = probe_binary(target) if os.path.isfile(target) and os.access(target, os.X_OK) else None
now = time.time()
try:
    checked_at = float(state.get("checked_at_epoch") or 0)
except (TypeError, ValueError):
    checked_at = 0
ttl = 6 * 60 * 60 if state.get("last_check_success") is True else 15 * 60
cached_latest = version_from(state.get("latest_version"))
cache_current = current and (
    state.get("last_check_success") is not True
    or not cached_latest
    or version_key(current[0]) >= version_key(cached_latest)
)
if current and not force_check and checked_at > 0 and 0 <= now - checked_at < ttl and cache_current:
    print("kernel_cached=" + current[1])
    raise SystemExit(0)

try:
    data = json.loads(read_url(release_url, 45, metadata_limit, "release metadata").decode("utf-8"))
    tag = str(data.get("tag_name") or "").strip()
    latest_version = version_from(tag)
    assets = data.get("assets") or []
    if not latest_version or not isinstance(assets, list):
        raise RuntimeError("release metadata is incomplete")

    if current and version_key(current[0]) >= version_key(latest_version):
        state.update({
            "schema": 1,
            "checked_at_epoch": now,
            "last_check_success": True,
            "latest_tag": tag,
            "latest_version": latest_version,
            "installed_version": current[0],
            "installed_detail": current[1],
            "last_error": "",
        })
        write_state(state)
        print("kernel_current=" + current[1])
        raise SystemExit(0)

    def usable(asset):
        name = str(asset.get("name") or "").lower()
        return (
            pattern in name
            and name.endswith(".gz")
            and not any(token in name for token in ("deb", "rpm", "sha256", "checksums"))
        )

    candidates = [asset for asset in assets if isinstance(asset, dict) and usable(asset)]
    if not candidates:
        raise RuntimeError(f"no mihomo asset matched {pattern}")
    exact_name = f"mihomo-{pattern}-{tag}".lower() + ".gz"

    def asset_rank(asset):
        name = str(asset.get("name") or "").lower()
        if name == exact_name:
            return (0, name)
        if "compatible" in name:
            return (30, name)
        if re.search(r"-(?:go\d+|v[1-3])(?:-|\.)", name):
            return (20, name)
        return (10, name)

    asset = min(candidates, key=asset_rank)
    url = str(asset.get("browser_download_url") or "")
    if not url:
        raise RuntimeError("mihomo release asset has no download URL")
    compressed = read_url(url, 180, asset_limit, "release asset")
    try:
        expected_size = int(asset.get("size") or 0)
    except (TypeError, ValueError):
        expected_size = 0
    if expected_size and len(compressed) != expected_size:
        raise RuntimeError(f"release asset size mismatch: expected {expected_size}, got {len(compressed)}")
    digest = str(asset.get("digest") or "").strip().lower()
    if digest:
        algorithm, separator, expected_digest = digest.partition(":")
        if separator != ":" or algorithm != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise RuntimeError("release asset digest is unsupported")
        actual_digest = hashlib.sha256(compressed).hexdigest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise RuntimeError("release asset SHA-256 mismatch")

    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as handle:
        binary = handle.read(binary_limit + 1)
    if not binary or len(binary) > binary_limit or not binary.startswith(b"\x7fELF"):
        raise RuntimeError("downloaded mihomo binary is empty, oversized, or not ELF")

    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, candidate_path = tempfile.mkstemp(prefix=".mihomo-core-", dir=os.path.dirname(target))
    candidate = None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(binary)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(candidate_path, 0o755)
        candidate = probe_binary(candidate_path)
        if not candidate or version_key(candidate[0]) != version_key(latest_version):
            raise RuntimeError("downloaded mihomo binary version self-check failed")
        detail_lower = candidate[1].casefold()
        expected_arch = "armv7" if pattern.endswith("armv7") else pattern.rsplit("-", 1)[-1]
        if "linux" not in detail_lower or expected_arch not in detail_lower:
            raise RuntimeError("downloaded mihomo binary platform self-check failed")
        os.replace(candidate_path, target)
    finally:
        try:
            os.unlink(candidate_path)
        except FileNotFoundError:
            pass

    state.update({
        "schema": 1,
        "checked_at_epoch": now,
        "last_check_success": True,
        "latest_tag": tag,
        "latest_version": latest_version,
        "installed_version": candidate[0],
        "installed_detail": candidate[1],
        "installed_asset": str(asset.get("name") or ""),
        "installed_at_epoch": now,
        "last_error": "",
    })
    write_state(state)
    print("kernel_updated=" + candidate[1])
except SystemExit:
    raise
except Exception as exc:
    state.update({
        "schema": 1,
        "checked_at_epoch": time.time(),
        "last_check_success": False,
        "last_error": safe_error(exc),
    })
    if current:
        state["installed_version"] = current[0]
        state["installed_detail"] = current[1]
    try:
        write_state(state)
    except Exception:
        pass
    if current:
        print("mihomo update check failed; keeping current core: " + safe_error(exc), file=sys.stderr)
        raise SystemExit(0)
    raise
PY
  then
    if command -v mihomo >/dev/null 2>&1 || command -v clash-meta >/dev/null 2>&1 || command -v clash >/dev/null 2>&1; then
      echo "mihomo 下载/校验失败，回退使用远端已有兼容内核" >&2
    else
      exit 4
    fi
  fi
elif [ -x "$BIN" ]; then
  echo "远端未安装 python3，已复用受管 mihomo，但无法自动检查更新" >&2
elif command -v mihomo >/dev/null 2>&1 || command -v clash-meta >/dev/null 2>&1 || command -v clash >/dev/null 2>&1; then
  echo "远端未安装 python3，回退使用已有兼容内核" >&2
else
  echo "远端未安装 python3，且未找到 mihomo/clash-meta/clash，无法自动下载 mihomo" >&2
  exit 2
fi

ACTIVE_BIN="$BIN"
if [ ! -x "$ACTIVE_BIN" ]; then
  ACTIVE_BIN="$(command -v mihomo 2>/dev/null || command -v clash-meta 2>/dev/null || command -v clash 2>/dev/null || true)"
fi
if [ -z "$ACTIVE_BIN" ] || [ ! -x "$ACTIVE_BIN" ]; then
  echo "远端没有可执行的 mihomo/clash 兼容内核" >&2
  exit 2
fi
KERNEL_LINE="$("$ACTIVE_BIN" -v 2>&1 | head -n 1 | tr '\n\r' '  ' || true)"
if [ -z "$KERNEL_LINE" ]; then
  echo "远端代理内核版本自检失败" >&2
  exit 5
fi
printf 'kernel_path=%s\nkernel_version=%s\n' "$ACTIVE_BIN" "$KERNEL_LINE"
"""
    return (
        template.replace("__HOME_DIR__", shlex.quote(home))
        .replace("__APP_DIR__", shlex.quote(app_dir))
        .replace("__LOCAL_BIN_DIR__", shlex.quote(local_bin_dir))
        .replace("__FORCE_CHECK__", "1" if force_check else "0")
    )


def _build_install_command(
    home: str,
    config_dir: str,
    app_dir: str,
    local_bin_dir: str,
    start_path: str,
    mixed_port: int,
) -> str:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    ensure_command = _build_ensure_mihomo_command(
        home,
        app_dir=app_dir,
        local_bin_dir=local_bin_dir,
    )
    start_command = f"""
CONFIG_DIR={shlex.quote(config_dir)}
START_SCRIPT={shlex.quote(start_path)}
PORT={mixed_port}
mkdir -p "$CONFIG_DIR"
"$START_SCRIPT" restart
ACTIVE_BIN="$LOCAL_BIN_DIR/mihomo"
if [ ! -x "$ACTIVE_BIN" ]; then
  ACTIVE_BIN="$(command -v mihomo 2>/dev/null || command -v clash-meta 2>/dev/null || command -v clash 2>/dev/null || true)"
fi
KERNEL_LINE="$("$ACTIVE_BIN" -v 2>&1 | head -n 1 | tr '\n\r' '  ' || true)"
printf 'config=%s proxy=http://127.0.0.1:%s kernel=%s\n' "$CONFIG_DIR/config.yaml" "$PORT" "$KERNEL_LINE"
"""
    return ensure_command.rstrip() + "\n" + start_command.lstrip()


def _build_shell_profile_block(env_path: str, start_path: str) -> str:
    return "\n".join([
        "# >>> API切换器 AI proxy >>>",
        f"if [ -f {shlex.quote(env_path)} ]; then . {shlex.quote(env_path)}; fi",
        f"if [ -x {shlex.quote(start_path)} ]; then {shlex.quote(start_path)} >/dev/null 2>&1 & fi",
        "# <<< API切换器 AI proxy <<<",
    ])


def _build_fish_proxy_config(start_path: str, mixed_port: int) -> str:
    env = _proxy_env_values(mixed_port)
    return "\n".join([
        "# Managed by API切换器. Non-AI domains are DIRECT in mihomo rules.",
        f"if test -x {shlex.quote(start_path)}",
        f"    {shlex.quote(start_path)} >/dev/null 2>&1 &",
        "end",
        *(f"set -gx {key} {shlex.quote(env[key])}" for key in PROXY_ENV_KEYS),
        "",
    ])


def _shell_proxy_profile_paths(home: str) -> tuple[str, ...]:
    return (
        posixpath.join(home, ".profile"),
        posixpath.join(home, ".bashrc"),
        posixpath.join(home, ".bash_profile"),
        posixpath.join(home, ".bash_login"),
        posixpath.join(home, ".zprofile"),
        posixpath.join(home, ".zshrc"),
    )


def _write_shell_profile_block(client, home: str, env_path: str, start_path: str, mixed_port: int) -> None:
    block = _build_shell_profile_block(env_path, start_path)
    profile_paths = _shell_proxy_profile_paths(home)
    fish_path = posixpath.join(home, ".config", "fish", "conf.d", "api-switcher-ai-proxy.fish")
    ssh_manager.write_remote_file(client, fish_path, _build_fish_proxy_config(start_path, mixed_port), file_mode=0o600)
    quoted_paths = " ".join(shlex.quote(path) for path in profile_paths)
    script = f"""
set -eu
BLOCK_START="# >>> API切换器 AI proxy >>>"
BLOCK_END="# <<< API切换器 AI proxy <<<"
BLOCK={shlex.quote(block)}
for file in {quoted_paths}; do
  touch "$file"
  tmp="$file.tmp.$$"
  if ! awk -v start="$BLOCK_START" -v end="$BLOCK_END" '
    $0 == start {{skip=1; next}}
    $0 == end {{skip=0; next}}
    skip != 1 {{print}}
    END {{if (skip == 1) exit 2}}
  ' "$file" > "$tmp"; then
    rm -f "$tmp"
    echo "无法安全更新 $file：检测到未闭合的 API-Switcher 代理环境块" >&2
    exit 2
  fi
  printf "\\n%s\\n" "$BLOCK" >> "$tmp"
  mv "$tmp" "$file"
done
"""
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, script, timeout=30, log_command=False)
    if status != 0:
        raise RuntimeError((stderr or stdout or "写入 shell 代理环境失败").strip())


def _build_vscode_server_env_setup(env_path: str, start_path: str) -> str:
    return _merge_vscode_server_env_setup("", env_path, start_path)


def _build_vscode_server_env_block(env_path: str, start_path: str) -> str:
    return "\n".join([
        VSCODE_ENV_BLOCK_START,
        "# Managed by API切换器. Loaded by VS Code Remote Server when supported.",
        f"if [ -x {shlex.quote(start_path)} ]; then {shlex.quote(start_path)} >/dev/null 2>&1 & fi",
        f"if [ -f {shlex.quote(env_path)} ]; then . {shlex.quote(env_path)}; fi",
        VSCODE_ENV_BLOCK_END,
        "",
    ])


def _merge_vscode_server_env_setup(existing: str, env_path: str, start_path: str) -> str:
    block = _build_vscode_server_env_block(env_path, start_path).rstrip()
    existing = (existing or "").replace("\r\n", "\n")
    lines = existing.splitlines()
    output = []
    skipping = False
    for line in lines:
        if line.strip() == VSCODE_ENV_BLOCK_START:
            skipping = True
            continue
        if line.strip() == VSCODE_ENV_BLOCK_END:
            skipping = False
            continue
        if not skipping:
            output.append(line)

    while output and not output[-1].strip():
        output.pop()
    if output:
        return "\n".join(output) + "\n\n" + block + "\n"
    return "#!/bin/sh\n" + block + "\n"


def _write_vscode_proxy_entrypoints(
    client,
    env_path: str,
    start_path: str,
    mixed_port: int,
) -> int:
    written = 0
    for raw_path in VSCODE_SERVER_ENV_SETUP_PATHS:
        path = remote_config._expand_remote_path(client, raw_path)
        existing = ssh_manager.read_remote_file(client, path) or ""
        setup_content = _merge_vscode_server_env_setup(existing, env_path, start_path)
        ssh_manager.write_remote_file(client, path, setup_content, file_mode=0o700)
        written += 1
    written += _write_vscode_proxy_settings(client, mixed_port)
    return written


def _repair_remote_proxy_integrations(
    client,
    home: str,
    mixed_port: int,
    status: RemoteAIProxyStatus,
) -> str:
    """Repair drifted shell/VS Code entrypoints without restarting the proxy.

    The managed mihomo process and its active YAML are deliberately left
    untouched.  Each integration is repaired independently so one malformed
    shell profile does not prevent the safe environment file or VS Code entry
    from being restored.
    """

    if status.integrations_ready:
        return ""

    config_dir = posixpath.join(home, ".config", "mihomo")
    app_dir = posixpath.join(home, ".config", "api-switcher")
    local_bin_dir = posixpath.join(home, ".local", "bin")
    env_path = posixpath.join(app_dir, "ai-proxy.env")
    start_path = posixpath.join(app_dir, "start-ai-proxy.sh")
    repaired: list[str] = []
    errors: list[str] = []

    def attempt(label: str, action) -> None:
        try:
            action()
            repaired.append(label)
        except Exception as exc:
            detail = str(exc).strip().splitlines()[0][:240] or type(exc).__name__
            errors.append(f"{label}: {detail}")

    if not status.environment_ready:
        attempt(
            "代理环境文件",
            lambda: ssh_manager.write_remote_file(
                client,
                env_path,
                _build_env_file(mixed_port),
                file_mode=0o600,
            ),
        )
    if not status.start_script_ready:
        attempt(
            "启动脚本",
            lambda: ssh_manager.write_remote_file(
                client,
                start_path,
                _build_start_script(
                    config_dir,
                    app_dir,
                    local_bin_dir,
                    mixed_port,
                ),
                file_mode=0o700,
            ),
        )
    if not status.shell_entrypoints_ready:
        attempt(
            "shell 入口",
            lambda: _write_shell_profile_block(
                client,
                home,
                env_path,
                start_path,
                mixed_port,
            ),
        )
    if not status.vscode_entrypoints_ready:
        attempt(
            "VS Code Remote 入口",
            lambda: _write_vscode_proxy_entrypoints(
                client,
                env_path,
                start_path,
                mixed_port,
            ),
        )

    pieces = []
    if repaired:
        pieces.append("已无重启自愈 " + "、".join(repaired))
    if errors:
        pieces.append(
            "代理进程保持运行，但环境入口自愈未完成: " + "；".join(errors)
        )
    return "；" + "；".join(pieces) if pieces else ""


def _write_vscode_proxy_settings(client, mixed_port: int) -> int:
    targets = []
    for raw_path in remote_config.REMOTE_VSCODE_SETTINGS_PATHS:
        expanded = remote_config._expand_remote_path(client, raw_path)
        content = ssh_manager.read_remote_file(client, expanded)
        if content is not None:
            targets.append((expanded, content))

    if not targets:
        targets = [
            (remote_config._expand_remote_path(client, raw_path), "")
            for raw_path in remote_config.REMOTE_VSCODE_SETTINGS_PATHS
        ]

    written = 0
    for path, content in targets:
        settings = _parse_vscode_settings_for_proxy(content)
        if settings is None:
            continue
        updated, changed = _apply_vscode_proxy_settings(settings, mixed_port)
        if changed:
            remote_config.write_remote_json(client, path, updated, file_mode=0o600)
            written += 1
    return written


def _parse_vscode_settings_for_proxy(content: str) -> dict | None:
    if not (content or "").strip():
        return {}
    try:
        parsed = json.loads(content.lstrip("\ufeff"))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _apply_vscode_proxy_settings(settings: dict, mixed_port: int) -> tuple[dict, bool]:
    env = _proxy_env_values(mixed_port)
    updated = dict(settings or {})
    changed = False
    proxy_url = env["API_SWITCHER_AI_PROXY_URL"]

    if updated.get("http.proxy") != proxy_url:
        updated["http.proxy"] = proxy_url
        changed = True
    if updated.get("http.proxySupport") != "override":
        updated["http.proxySupport"] = "override"
        changed = True

    terminal_env = updated.get("terminal.integrated.env.linux")
    if not isinstance(terminal_env, dict):
        terminal_env = {}
    else:
        terminal_env = dict(terminal_env)

    for key in PROXY_ENV_KEYS:
        if terminal_env.get(key) != env[key]:
            terminal_env[key] = env[key]
            changed = True

    if updated.get("terminal.integrated.env.linux") != terminal_env:
        updated["terminal.integrated.env.linux"] = terminal_env
        changed = True
    return updated, changed


def _remove_vscode_proxy_settings(settings: dict, mixed_port: int) -> tuple[dict, bool]:
    env_values = _proxy_env_values(mixed_port)
    proxy_url = env_values["API_SWITCHER_AI_PROXY_URL"]
    no_proxy = env_values["NO_PROXY"]
    updated = dict(settings or {})
    changed = False
    removed_http_proxy = False

    if updated.get("http.proxy") == proxy_url:
        updated.pop("http.proxy", None)
        changed = True
        removed_http_proxy = True
    if removed_http_proxy and updated.get("http.proxySupport") == "override":
        updated.pop("http.proxySupport", None)
        changed = True

    terminal_env = updated.get("terminal.integrated.env.linux")
    if isinstance(terminal_env, dict):
        next_env = dict(terminal_env)
        for key in PROXY_ENV_KEYS:
            value = next_env.get(key)
            if value == proxy_url or value == no_proxy:
                next_env.pop(key, None)
                changed = True
        if next_env:
            if next_env != terminal_env:
                updated["terminal.integrated.env.linux"] = next_env
        elif "terminal.integrated.env.linux" in updated:
            updated.pop("terminal.integrated.env.linux", None)
            changed = True

    return updated, changed


def _parse_key_values(text: str) -> dict[str, str]:
    values = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _parse_remote_probe_output(text: str) -> tuple[RemoteAIProxyProbeResult, ...]:
    results: list[RemoteAIProxyProbeResult] = []
    for line in (text or "").splitlines():
        if not line.startswith("probe\t"):
            continue
        _prefix, label, ok, detail, elapsed = (line.split("\t", 4) + ["", "", "", "", ""])[:5]
        results.append(
            RemoteAIProxyProbeResult(
                label=(label or "unknown").strip(),
                ok=ok == "1",
                detail=(detail or "").strip(),
                elapsed_ms=_int_or_default((elapsed or "").strip(), 0),
            )
        )
    return tuple(results)


def _probe_summary_counts(summary: str) -> str:
    match = re.search(r"AI 连通性\s+(\d+)/(\d+)\s+可达", summary or "")
    if match:
        return f"{match.group(1)}/{match.group(2)} 可达"
    return str(summary or "").split("；", 1)[0][:160]


def _probe_summary_all_ok(summary: str) -> bool:
    match = re.search(r"AI 连通性\s+(\d+)/(\d+)\s+可达", summary or "")
    if not match:
        return False
    return _int_or_default(match.group(1), 0) >= _int_or_default(match.group(2), 1)


def _probe_stability_summary_all_ok(summary: str) -> bool:
    match = re.search(r"AI 稳定性\s+(\d+)/(\d+)\s+可达", summary or "")
    if not match:
        return False
    successes = _int_or_default(match.group(1), 0)
    attempts = _int_or_default(match.group(2), 1)
    expected = REMOTE_AI_STABILITY_EXPECTED_PROBES
    return (
        successes == expected
        and attempts == expected
        and "探测结果不完整" not in str(summary or "")
        and not proxy_region_is_hong_kong(summary)
    )


def _compact_probe_summary(summary: str) -> str:
    text = str(summary or "").strip()
    if not text:
        return ""
    parts = [part for part in text.split("；") if part]
    useful = [
        part
        for part in parts
        if "AI 连通性" in part
        or "AI 稳定性" in part
        or "OpenAI/ChatGPT" in part
        or "OpenAI API" in part
        or "ChatGPT 出口" in part
        or "Claude/Anthropic" in part
        or "Gemini/Google AI" in part
        or REMOTE_CODEX_COMPACT_PROBE_LABEL in part
    ]
    return "；".join(useful or parts[:2])[:900]


def _restore_remote_proxy_node_after_failed_update(
    ssh_name: str,
    original_node: dict | None,
    attempted_node: dict | None,
    mixed_port: int,
    *,
    profile_id: str = "",
    persist_selection: bool = True,
    strict_privacy: bool | None = None,
) -> str:
    if not original_node:
        return "；未读取到更新前节点，已保留最后一次热更新状态"
    try:
        original = _normalize_proxy_node(original_node)
    except Exception:
        return "；更新前节点格式不可恢复，已保留最后一次热更新状态"
    try:
        if profile_id or not persist_selection:
            reload_ai_proxy(
                ssh_name,
                format_proxy_node(original),
                mixed_port,
                profile_id=profile_id,
                persist_selection=persist_selection,
                **_strict_privacy_call_kwargs(strict_privacy),
            )
        else:
            reload_ai_proxy(
                ssh_name,
                format_proxy_node(original),
                mixed_port,
                **_strict_privacy_call_kwargs(strict_privacy),
            )
    except Exception as exc:
        attempted = describe_proxy_node(attempted_node or {}) if attempted_node else "当前节点"
        return f"；尝试从 {attempted} 恢复更新前节点失败: {exc}"
    try:
        restore_probe = probe_ai_proxy(ssh_name, mixed_port)
    except Exception as exc:
        return f"；已恢复更新前节点 {describe_proxy_node(original)}，但恢复后验证失败: {exc}"
    if _probe_summary_all_ok(restore_probe):
        return f"；已恢复更新前节点 {describe_proxy_node(original)}，验证通过: {_compact_probe_summary(restore_probe)}"
    return f"；已恢复更新前节点 {describe_proxy_node(original)}，但验证仍未完全通过: {_compact_probe_summary(restore_probe)}"


def _parse_remote_latency_output(text: str) -> dict[str, ProxyNodeLatencyResult]:
    results: dict[str, ProxyNodeLatencyResult] = {}
    for line in (text or "").splitlines():
        if not line.startswith("latency\t"):
            continue
        _prefix, node_key, ok, latency, detail, attempts = (line.split("\t", 5) + ["", "", "", "", "", ""])[:6]
        node_key = (node_key or "").strip()
        if not node_key:
            continue
        latency_ms = _int_or_default((latency or "").strip(), 0) if ok == "1" else 0
        results[node_key] = ProxyNodeLatencyResult(
            node_key=node_key,
            ok=ok == "1" and latency_ms > 0,
            latency_ms=latency_ms if ok == "1" and latency_ms > 0 else None,
            detail=(detail or "").strip()[:180],
            attempts=_int_or_default((attempts or "").strip(), 0),
            measured_at=_now_iso(),
        )
    return results


def _find_matching_subscription_node(
    nodes,
    current_node: dict | None,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
):
    """Preserve an exact node, but apply auto-selection policy to name-only migration."""

    if not current_node:
        return None
    try:
        current = _normalize_proxy_node(current_node)
    except Exception:
        return None
    current_key = proxy_node_key(current)
    for item in nodes or []:
        try:
            if proxy_subscription_node_key(item) == current_key:
                return item
        except Exception:
            continue
    current_name = str(current.get("name") or "").strip().lower()
    if current_name:
        for item in nodes or []:
            try:
                if (
                    proxy_subscription_node_auto_selectable(
                        item,
                        (quality_results or {}).get(proxy_subscription_node_key(item)),
                    )
                    and str(item.node.get("name") or "").strip().lower() == current_name
                ):
                    return item
            except Exception:
                continue
    return None


def _read_remote_managed_proxy_node(ssh_name: str, mixed_port: int = 7890) -> dict | None:
    _profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    config_path = posixpath.join(home, ".config", "mihomo", "config.yaml")
    content = ssh_manager.read_remote_file(client, config_path)
    if not content or AI_PROXY_CONFIG_MARKER not in content:
        return None
    try:
        parsed = yaml.safe_load(content)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    proxies = parsed.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        return None
    node = proxies[0]
    if not isinstance(node, dict):
        return None
    try:
        normalized = _normalize_proxy_node(node)
    except Exception:
        return None
    display_name = _managed_proxy_display_name(content)
    if display_name:
        normalized["name"] = display_name
    return normalized


def current_remote_ai_proxy_node_key(ssh_name: str, mixed_port: int = 7890) -> str:
    try:
        node = _read_remote_managed_proxy_node(ssh_name, mixed_port)
    except Exception:
        return ""
    if not node:
        return ""
    try:
        return proxy_node_key(node)
    except Exception:
        return ""


def _read_remote_ai_proxy_error_tail(client, home: str) -> str:
    log_path = posixpath.join(home, ".config", "api-switcher", "ai-proxy.log")
    command = f"""
LOG={shlex.quote(log_path)}
if [ -s "$LOG" ]; then
  tail -n 80 "$LOG" 2>/dev/null |
    grep -E 'level=(warning|error)| connect error| timeout| reset| refused' |
    tail -n 3 |
    sed -E 's/[[:space:]]+/ /g; s/(uuid: )[A-Za-z0-9_-]+/\\1***/g; s/(password: )[A-Za-z0-9._-]+/\\1***/g' |
    cut -c 1-260
fi
"""
    try:
        status, stdout, _stderr = ssh_manager.execute_command_with_status(
            client,
            command,
            timeout=10,
            log_command=False,
        )
    except Exception:
        return ""
    if status != 0:
        return ""
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    return " | ".join(lines)[-700:]
