from __future__ import annotations

import base64
import binascii
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import ipaddress
import json
import posixpath
import re
import shlex
import socket
import threading
import time
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib import parse as urlparse
from urllib import request as urlrequest

import yaml

from config.paths import STORAGE_DIR
from core import network_diagnostic_settings, network_diagnostics, profile_manager, remote_config
from core.ssh_manager import ssh_manager


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

AI_PROXY_CONFIG_MARKER = "# Managed by API切换器 AI proxy"
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
_PROXY_SUBSCRIPTION_STATE_CACHE: dict | None = None
_PROXY_SUBSCRIPTION_STATE_CACHE_SIGNATURE: tuple[str, int | None, int | None] | None = None
_PROXY_SUBSCRIPTION_NODES_CACHE: tuple[ProxySubscriptionNode, ...] | None = None
_PROXY_SUBSCRIPTION_NODES_CACHE_SIGNATURE: tuple[str, int | None, int | None, str] | None = None

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
    ("香港", (r"香港", r"\bhk\b", r"hong\s*kong", r"🇭🇰")),
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

    def summary(self) -> str:
        state = "运行中" if self.running else "未运行"
        installed = "已配置" if self.installed else "未配置"
        detail = f"；{self.detail}" if self.detail else ""
        return f"AI 代理{installed}，{state}: {self.proxy_url}{detail}"


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

    def display_name(self) -> str:
        return f"{self.index}. {describe_proxy_node(self.node)}"


@dataclass(frozen=True)
class ProxyNodeLatencyResult:
    node_key: str
    ok: bool
    latency_ms: int | None = None
    detail: str = ""
    attempts: int = 0

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
    detail: str = ""
    checked_at: str = ""
    sources: tuple[str, ...] = ()

    def label(self) -> str:
        if not self.ok:
            return "质量未测"
        pieces = [self.quality_label or self.ip_type or "质量已测"]
        if self.risk_score is not None:
            pieces.append(f"风险{self.risk_score}%")
        if self.quality_score:
            pieces.append(f"评分{self.quality_score}")
        return " ".join(pieces)


@dataclass(frozen=True)
class ProxySubscriptionResult:
    nodes: tuple[ProxySubscriptionNode, ...]
    saved_path: str
    url: str = ""
    last_fetched_at: str = ""


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
        ProxySubscriptionNode(index=index, node=node, source="subscription")
        for index, node in enumerate(unique_nodes, 1)
    )


def fetch_proxy_subscription(
    url: str,
    timeout: int = 45,
    max_bytes: int = 5 * 1024 * 1024,
    persist: bool = True,
    retries: int = 3,
    retry_base_delay: float = 1.0,
) -> ProxySubscriptionResult:
    parsed_url = urlparse.urlparse((url or "").strip())
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("订阅链接必须是 http 或 https 地址")
    normalized_url = urlparse.urlunparse(parsed_url)

    request = urlrequest.Request(
        normalized_url,
        headers={
            "User-Agent": "API-Switcher/1.0",
            "Accept": "text/plain, application/yaml, application/json, */*",
            "Accept-Encoding": "gzip, deflate",
        },
    )

    payload, content_type, charset = _download_proxy_subscription(
        request=request,
        timeout=timeout,
        max_bytes=max_bytes,
        retries=retries,
        retry_base_delay=retry_base_delay,
    )
    text = _decode_subscription_bytes(payload, charset)
    nodes = parse_proxy_subscription_content(text)
    saved_path = _save_proxy_subscription(normalized_url, payload, content_type)
    fetched_at = _now_iso()
    if persist:
        save_proxy_subscription_state(
            url=normalized_url,
            saved_path=str(saved_path),
            last_fetched_at=fetched_at,
            node_count=len(nodes),
            content_type=content_type,
            charset=charset,
        )
    return ProxySubscriptionResult(
        nodes=nodes,
        saved_path=str(saved_path),
        url=normalized_url,
        last_fetched_at=fetched_at,
    )


def load_cached_proxy_subscription() -> ProxySubscriptionResult | None:
    state = load_proxy_subscription_state()
    saved_path = str(state.get("saved_path") or "").strip()
    if not saved_path:
        return None
    path = Path(saved_path)
    if not path.exists() or not path.is_file():
        return None
    charset = str(state.get("charset") or "utf-8-sig")
    signature = _proxy_subscription_nodes_signature(path, charset)
    try:
        nodes = _load_cached_proxy_subscription_nodes(path, charset, signature)
    except Exception:
        return None
    return ProxySubscriptionResult(
        nodes=nodes,
        saved_path=str(path),
        url=str(state.get("url") or ""),
        last_fetched_at=str(state.get("last_fetched_at") or ""),
    )


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
        except Exception:
            clear_proxy_subscription_state_cache()
            return {}
        state = data if isinstance(data, dict) else {}
        _cache_proxy_subscription_state(state, signature)
        return copy.deepcopy(state)


def save_proxy_subscription_state(**updates) -> dict:
    with _PROXY_SUBSCRIPTION_STATE_LOCK:
        directory = _proxy_subscription_dir()
        directory.mkdir(parents=True, exist_ok=True)
        state = load_proxy_subscription_state()
        for key, value in updates.items():
            if value is None:
                continue
            state[key] = value
        state["updated_at"] = _now_iso()
        path = _proxy_subscription_state_path()
        temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(path)
            _cache_proxy_subscription_state(state)
        finally:
            temp_path.unlink(missing_ok=True)
        return state


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
    except OSError:
        return (path_key, None, None)


def _cache_proxy_subscription_state(
    state: dict,
    signature: tuple[str, int | None, int | None] | None = None,
) -> None:
    global _PROXY_SUBSCRIPTION_STATE_CACHE, _PROXY_SUBSCRIPTION_STATE_CACHE_SIGNATURE
    _PROXY_SUBSCRIPTION_STATE_CACHE = copy.deepcopy(state)
    _PROXY_SUBSCRIPTION_STATE_CACHE_SIGNATURE = signature or _proxy_subscription_state_signature()


def _proxy_subscription_nodes_signature(path: Path, charset: str) -> tuple[str, int | None, int | None, str]:
    path_key = str(path.resolve(strict=False))
    charset_key = str(charset or "utf-8-sig")
    try:
        stat = path.stat()
        return (path_key, int(stat.st_mtime_ns), int(stat.st_size), charset_key)
    except OSError:
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
    text = _decode_subscription_bytes(path.read_bytes(), charset)
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


def set_proxy_subscription_selected_node(node: dict | None) -> dict:
    if not node:
        return save_proxy_subscription_state(selected_node_key="", selected_node_display="")
    normalized = _normalize_proxy_node(node)
    return save_proxy_subscription_state(
        selected_node_key=proxy_node_key(normalized),
        selected_node_display=describe_proxy_node(normalized),
    )


def save_proxy_subscription_latencies(latencies: dict[str, ProxyNodeLatencyResult | dict]) -> dict:
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
            "measured_at": measured_at,
        }
    return save_proxy_subscription_state(node_latencies=payload, node_latencies_updated_at=measured_at)


def save_proxy_subscription_qualities(qualities: dict[str, ProxyNodeQualityResult | dict]) -> dict:
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
            "detail": proxy_node_quality_detail(result),
            "checked_at": proxy_node_quality_checked_at(result) or _now_iso(),
            "sources": list(proxy_node_quality_sources(result)),
        }
    return save_proxy_subscription_state(node_qualities=payload, node_qualities_updated_at=_now_iso())


def load_proxy_subscription_latencies() -> dict[str, dict]:
    state = load_proxy_subscription_state()
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


def load_proxy_subscription_qualities() -> dict[str, dict]:
    state = load_proxy_subscription_state()
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
            "detail": str(value.get("detail") or "")[:220],
            "checked_at": str(value.get("checked_at") or "")[:40],
            "sources": list(network_diagnostic_settings.normalize_services(value.get("sources") or [])),
        }
    return results


def proxy_node_key(node: dict) -> str:
    normalized = _normalize_proxy_node(node)
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        node_key = proxy_node_key(item.node)
        region = proxy_node_region(item.node)
        region_index = PROXY_REGION_ORDER.index(region) if region in PROXY_REGION_ORDER else len(PROXY_REGION_ORDER)
        quality_result = qualities.get(node_key)
        quality_score = proxy_node_quality_score(quality_result)
        quality_measured_sort = 0 if proxy_node_quality_measured(quality_result) else 1
        quality_sort = -quality_score if prefer_quality else 0
        ai_proxy_sort = 0 if proxy_node_quality_for_ai_proxy_ok(quality_result) else 1
        latency_result = latencies.get(node_key)
        latency = proxy_node_latency_ms(latency_result)
        if proxy_node_latency_ok(latency_result):
            status_sort = 0
        elif latency_result is None:
            status_sort = 1
        else:
            status_sort = 2
        latency_sort = latency if latency is not None else 10**9
        display_name = str(item.node.get("name") or item.display_name()).lower()
        if prefer_quality:
            return (ai_proxy_sort, quality_measured_sort, quality_sort, status_sort, latency_sort, region_index, region, display_name)
        return (region_index, region, status_sort, latency_sort, display_name)

    return tuple(sorted(items, key=sort_key))


def best_proxy_subscription_node_for_ai_proxy(
    nodes,
    quality_results: dict[str, ProxyNodeQualityResult | dict],
    latency_results: dict[str, ProxyNodeLatencyResult | dict] | None = None,
) -> ProxySubscriptionNode | None:
    ranked = sort_proxy_subscription_nodes(
        nodes,
        latency_results=latency_results,
        quality_results=quality_results,
        prefer_quality=True,
    )
    for item in ranked:
        if proxy_node_quality_for_ai_proxy_ok(quality_results.get(proxy_node_key(item.node))):
            return item
    for item in ranked:
        if proxy_node_quality_measured(quality_results.get(proxy_node_key(item.node))):
            return item
    return None


def ranked_proxy_subscription_nodes_for_ai_probe(
    nodes,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
    latency_results: dict[str, ProxyNodeLatencyResult | dict] | None = None,
) -> tuple[ProxySubscriptionNode, ...]:
    """Rank candidates for AI proxy validation, preferring high-quality IPs first."""
    return sort_proxy_subscription_nodes(
        nodes,
        latency_results=latency_results,
        quality_results=quality_results or {},
        prefer_quality=bool(quality_results),
    )


def measure_proxy_node_latency(node: dict, timeout: float = 3.0, attempts: int = 2) -> ProxyNodeLatencyResult:
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

    if latencies:
        return ProxyNodeLatencyResult(
            node_key=node_key,
            ok=True,
            latency_ms=min(latencies),
            attempts=attempts,
        )
    return ProxyNodeLatencyResult(
        node_key=node_key,
        ok=False,
        latency_ms=None,
        detail=last_error or "TCP 连接失败",
        attempts=attempts,
    )


def measure_proxy_node_latencies(
    nodes,
    timeout: float = 3.0,
    attempts: int = 2,
    max_workers: int = 16,
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

    worker_count = min(max(1, _int_or_default(max_workers, 16)), len(items))
    results: dict[str, ProxyNodeLatencyResult] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(measure_proxy_node_latency, node, timeout, attempts) for node in items]
        for future in as_completed(futures):
            result = future.result()
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
) -> ProxyNodeQualityResult:
    normalized = _normalize_proxy_node(node)
    node_key = proxy_node_key(normalized)
    host = str(normalized.get("server") or "")
    region = proxy_node_region(normalized)
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

    try:
        settings = settings or network_diagnostic_settings.load_settings()
        if enabled_services is not None:
            services = network_diagnostic_settings.normalize_services(enabled_services)
        elif hasattr(settings, "enabled_services"):
            services = settings.enabled_services()
        else:
            services = []
        service_set = set(services)
        http_get = http_get or network_diagnostics._http_get
        if network_diagnostic_settings.SERVICE_PING0 in service_set:
            ping0 = network_diagnostics.lookup_ping0_quality(
                ip,
                "默认出口",
                timeout,
                http_get,
                api_keys=_diagnostic_settings_keys(settings, network_diagnostic_settings.SERVICE_PING0),
            )
        else:
            ping0 = network_diagnostics._disabled_ping0_quality(ip)
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
        geo = network_diagnostics.lookup_geo(ip, timeout, http_get)
        classification = network_diagnostics.classify_ip(geo, ping0=ping0, reputation=reputation)
    except Exception as exc:
        return _proxy_node_quality_error_result(
            normalized,
            "检测失败",
            str(exc).splitlines()[0][:180] or "节点服务器 IP 质量检测失败",
            ip=ip,
            sources=services,
        )
    quality_score = _proxy_quality_score(classification)
    quality_label = _proxy_quality_label(classification, quality_score)
    detail_parts = []
    if geo.ok and geo.owner_text() != "-":
        detail_parts.append(geo.owner_text())
    for signal in classification.signals:
        if signal.startswith("多源"):
            detail_parts.append(signal)
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
    return ProxyNodeQualityResult(
        node_key=node_key,
        ok=True,
        host=host,
        ip=ip,
        region=region,
        ip_type=classification.ip_type,
        risk_score=classification.risk_score,
        risk_label=classification.risk_label,
        quality_score=quality_score,
        quality_label=quality_label,
        detail="；".join(detail_parts[:3])[:220],
        checked_at=_now_iso(),
        sources=tuple(services),
    )


def assess_proxy_node_qualities(
    nodes,
    timeout: float = 5.0,
    max_workers: int = 8,
    *,
    http_get=None,
    resolver=None,
    settings=None,
    enabled_services=None,
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
    worker_count = min(max(1, _int_or_default(max_workers, 8)), len(items))
    results: dict[str, ProxyNodeQualityResult] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                assess_proxy_node_quality,
                node,
                timeout,
                http_get=http_get,
                resolver=resolver,
                settings=settings,
                enabled_services=enabled_services,
            ): node
            for node in items
        }
        for future in as_completed(futures):
            node = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = _proxy_node_quality_error_result(
                    node,
                    "检测失败",
                    str(exc).splitlines()[0][:180] or "节点服务器 IP 质量检测失败",
                )
            results[result.node_key] = result
    return results


def measure_proxy_node_latencies_on_server(
    ssh_name: str,
    nodes,
    timeout: float = 3.0,
    attempts: int = 2,
    max_workers: int = 16,
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
    workers_value = max(1, _int_or_default(max_workers, 16))
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
    latency = proxy_node_latency_ms(result)
    if latency is not None and proxy_node_latency_ok(result):
        return f"{latency}ms"
    if result is None:
        return "未测"
    return "不可连"


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


def proxy_node_quality_sources(result: ProxyNodeQualityResult | dict | None) -> tuple[str, ...]:
    if isinstance(result, ProxyNodeQualityResult):
        return tuple(network_diagnostic_settings.normalize_services(list(result.sources or ())))
    if isinstance(result, dict):
        return tuple(network_diagnostic_settings.normalize_services(result.get("sources") or []))
    return ()


def proxy_node_quality_source_label(result: ProxyNodeQualityResult | dict | None) -> str:
    sources = proxy_node_quality_sources(result)
    if not sources:
        return "未标明检测源"
    return " + ".join(network_diagnostic_settings.SERVICE_LABELS.get(source, source) for source in sources)


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
    residential = any(marker in ip_type or marker in label for marker in ("家庭", "住宅", "运营商/宽带", "家宽"))
    return residential and score >= 80 and (risk is None or risk <= 35)


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
    extra_proxy_domains: tuple[str, ...] | list[str] | None = None,
    extra_proxy_ip_cidrs: tuple[str, ...] | list[str] | None = None,
    proxy_non_cn: bool = False,
) -> str:
    node = dict(proxy_node)
    node["port"] = _normalize_port(node.get("port"), "代理节点端口")
    proxy_name = str(node.get("name") or "AI_PROXY").strip()
    node["name"] = proxy_name
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    rules = [
        *(f"DOMAIN-SUFFIX,{domain},AI-PROXY" for domain in _unique_clean_values(AI_PROXY_DOMAINS, extra_proxy_domains)),
        *(_ip_cidr_rule(cidr) for cidr in _unique_clean_values(extra_proxy_ip_cidrs)),
    ]
    if proxy_non_cn:
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
        "log-level": "warning",
        "ipv6": True,
        "proxies": [node],
        "proxy-groups": [
            {
                "name": "AI-PROXY",
                "type": "select",
                "proxies": [proxy_name],
            }
        ],
        "rules": rules,
    }
    return AI_PROXY_CONFIG_MARKER + "\n" + _dump_yaml(config)


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


def install_ai_proxy(ssh_name: str, proxy_text: str, mixed_port: int = 7890) -> str:
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

    ssh_manager.write_remote_file(client, config_path, build_mihomo_config(proxy_node, mixed_port), file_mode=0o600)
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
        f"{suffix}；已写入 VS Code Remote/Codex/Claude Code 环境入口 {vscode_targets} 处"
    )


def install_ai_proxy_verified(
    ssh_name: str,
    proxy_text: str,
    candidate_nodes=None,
    mixed_port: int = 7890,
    max_candidates: int = 10,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
) -> str:
    requested_node = parse_proxy_node(proxy_text)
    requested_key = proxy_node_key(requested_node)
    install_message = install_ai_proxy(ssh_name, proxy_text, mixed_port)
    probe_message = probe_ai_proxy(ssh_name, mixed_port)
    if _probe_summary_all_ok(probe_message):
        return f"{install_message}；验证通过: {_compact_probe_summary(probe_message)}"

    candidates = tuple(item for item in (candidate_nodes or []) if isinstance(item, ProxySubscriptionNode))
    if not candidates:
        return f"{install_message}；验证失败: {_compact_probe_summary(probe_message)}"

    try:
        latencies = measure_proxy_node_latencies_on_server(
            ssh_name,
            candidates,
            timeout=3.0,
            attempts=2,
            max_workers=20,
        )
    except Exception as exc:
        return f"{install_message}；验证失败: {_compact_probe_summary(probe_message)}；自动换节点测速失败: {exc}"

    ranked = []
    for item in ranked_proxy_subscription_nodes_for_ai_probe(candidates, quality_results, latencies):
        key = proxy_node_key(item.node)
        if key == requested_key:
            continue
        result = latencies.get(key)
        latency = proxy_node_latency_ms(result)
        if latency is None or not proxy_node_latency_ok(result):
            continue
        ranked.append((latency, item, result))

    attempts = max(1, min(_int_or_default(max_candidates, 10), len(ranked)))
    tried = []
    for latency, item, result in ranked[:attempts]:
        node_summary = describe_proxy_node(item.node)
        latency_label = proxy_node_latency_label(result)
        try:
            install_ai_proxy(ssh_name, format_proxy_node(item.node), mixed_port)
            candidate_probe = probe_ai_proxy(ssh_name, mixed_port)
        except Exception as exc:
            tried.append(f"{node_summary} {latency_label}: {exc}")
            continue
        if _probe_summary_all_ok(candidate_probe):
            set_proxy_subscription_selected_node(item.node)
            return (
                f"{ssh_name}: 原节点验证失败，已自动切换到 {node_summary}（远端 TCP {latency_label}）；"
                f"验证通过: {_compact_probe_summary(candidate_probe)}"
            )
        tried.append(f"{node_summary} {latency_label}: {_probe_summary_counts(candidate_probe)}")

    try:
        install_ai_proxy(ssh_name, format_proxy_node(requested_node), mixed_port)
    except Exception:
        pass
    tried_summary = "；".join(tried[:3])
    suffix = f"；尝试摘要: {tried_summary}" if tried_summary else ""
    return (
        f"{install_message}；验证失败: {_compact_probe_summary(probe_message)}；"
        f"自动尝试 {attempts} 个测速靠前节点仍未 3/3 可达，已恢复原节点{suffix}"
    )


def reload_ai_proxy(ssh_name: str, proxy_text: str, mixed_port: int = 7890) -> str:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    proxy_node = parse_proxy_node(proxy_text)
    status = inspect_ai_proxy(ssh_name, mixed_port)
    if not status.running:
        return f"{ssh_name}: AI 代理未运行，已跳过热更新"

    _ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    config_path = posixpath.join(home, ".config", "mihomo", "config.yaml")
    new_config = build_mihomo_config(proxy_node, mixed_port)
    old_config = ssh_manager.read_remote_file(client, config_path) or ""
    if old_config.strip() == new_config.strip():
        return f"{ssh_name}: 运行节点已是最新配置，无需热更新"

    ssh_manager.write_remote_file(client, config_path, new_config, file_mode=0o600)
    command = _build_reload_command(config_path, mixed_port)
    status_code, stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        timeout=20,
        log_command=False,
    )
    if status_code != 0:
        if old_config:
            ssh_manager.write_remote_file(client, config_path, old_config, file_mode=0o600)
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(
            f"{ssh_name}: 当前远端代理不支持无感热更新或控制口不可用: {detail or status_code}"
        )
    set_proxy_subscription_selected_node(proxy_node)
    return f"{ssh_name}: 已热更新远端 AI 代理节点为 {describe_proxy_node(proxy_node)}"


def reload_ai_proxy_verified(
    ssh_name: str,
    proxy_text: str,
    candidate_nodes=None,
    mixed_port: int = 7890,
    max_candidates: int = 10,
    quality_results: dict[str, ProxyNodeQualityResult | dict] | None = None,
) -> str:
    requested_node = parse_proxy_node(proxy_text)
    requested_key = proxy_node_key(requested_node)
    try:
        original_node = _read_remote_managed_proxy_node(ssh_name, mixed_port)
    except Exception:
        original_node = None
    try:
        reload_message = reload_ai_proxy(ssh_name, proxy_text, mixed_port)
    except Exception as exc:
        return f"{ssh_name}: 自动更新跳过，{exc}"
    if "跳过" in reload_message:
        return reload_message

    probe_message = probe_ai_proxy(ssh_name, mixed_port)
    if _probe_summary_all_ok(probe_message):
        return f"{reload_message}；验证通过: {_compact_probe_summary(probe_message)}"

    candidates = tuple(item for item in (candidate_nodes or []) if isinstance(item, ProxySubscriptionNode))
    if not candidates:
        restore_suffix = _restore_remote_proxy_node_after_failed_update(
            ssh_name,
            original_node,
            requested_node,
            mixed_port,
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
        )
        return f"{reload_message}；验证失败: {_compact_probe_summary(probe_message)}；自动换节点测速失败: {exc}{restore_suffix}"

    ranked = []
    for item in ranked_proxy_subscription_nodes_for_ai_probe(candidates, quality_results, latencies):
        key = proxy_node_key(item.node)
        if key == requested_key:
            continue
        result = latencies.get(key)
        latency = proxy_node_latency_ms(result)
        if latency is None or not proxy_node_latency_ok(result):
            continue
        ranked.append((latency, item, result))

    attempts = max(1, min(_int_or_default(max_candidates, 10), len(ranked)))
    for _latency, item, result in ranked[:attempts]:
        try:
            reload_ai_proxy(ssh_name, format_proxy_node(item.node), mixed_port)
            candidate_probe = probe_ai_proxy(ssh_name, mixed_port)
        except Exception:
            continue
        if _probe_summary_all_ok(candidate_probe):
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
    )
    return f"{reload_message}；验证失败: {_compact_probe_summary(probe_message)}；自动尝试 {attempts} 个节点仍未 3/3 可达{restore_suffix}"


def refresh_running_ai_proxy_from_subscription(
    ssh_name: str,
    nodes,
    mixed_port: int = 7890,
) -> str:
    status = inspect_ai_proxy(ssh_name, mixed_port)
    if not status.running:
        return f"{ssh_name}: AI 代理未运行，已跳过订阅自动热更新"
    candidates = tuple(item for item in (nodes or []) if isinstance(item, ProxySubscriptionNode))
    if not candidates:
        return f"{ssh_name}: 订阅里没有可用节点，已跳过热更新"
    current_node = _read_remote_managed_proxy_node(ssh_name, mixed_port)
    chosen = _find_matching_subscription_node(candidates, current_node) if current_node else None
    if chosen is None:
        try:
            latencies = measure_proxy_node_latencies_on_server(
                ssh_name,
                candidates,
                timeout=3.0,
                attempts=2,
                max_workers=20,
            )
        except Exception as exc:
            return f"{ssh_name}: 订阅已刷新，但远端节点测速失败，已保留当前运行节点: {exc}"
        ranked = [
            item
            for item in sort_proxy_subscription_nodes(candidates, latencies)
            if proxy_node_latency_ok(latencies.get(proxy_node_key(item.node)))
        ]
        if not ranked:
            return f"{ssh_name}: 订阅已刷新，但没有测到可连节点，已保留当前运行节点"
        chosen = ranked[0]
    return reload_ai_proxy_verified(
        ssh_name,
        format_proxy_node(chosen.node),
        candidates,
        mixed_port=mixed_port,
    )


def inspect_ai_proxy(ssh_name: str, mixed_port: int = 7890) -> RemoteAIProxyStatus:
    _ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    config_path = posixpath.join(home, ".config", "mihomo", "config.yaml")
    env_path = posixpath.join(home, ".config", "api-switcher", "ai-proxy.env")
    start_path = posixpath.join(home, ".config", "api-switcher", "start-ai-proxy.sh")
    pid_path = posixpath.join(home, ".config", "api-switcher", "ai-proxy.pid")
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    shell_paths = " ".join(shlex.quote(path) for path in _shell_proxy_profile_paths(home))
    vscode_paths = " ".join(
        shlex.quote(remote_config._expand_remote_path(client, path))
        for path in VSCODE_SERVER_ENV_SETUP_PATHS
    )
    command = f"""
CONFIG={shlex.quote(config_path)}
ENV_FILE={shlex.quote(env_path)}
START_SCRIPT={shlex.quote(start_path)}
PID_FILE={shlex.quote(pid_path)}
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
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    pid_running=yes
    if command -v ps >/dev/null 2>&1; then
      cmd="$(ps -p "$pid" -o comm= -o args= 2>/dev/null || true)"
      case "$cmd" in
        *mihomo*|*clash*) pid_managed=yes; running=yes ;;
        *) pid_managed=no ;;
      esac
    else
      running=yes
    fi
  fi
fi
if command -v ss >/dev/null 2>&1; then
  port_listening=no
  ss -ltn 2>/dev/null | grep -q ":$PORT " && port_listening=yes && running=yes || true
elif command -v netstat >/dev/null 2>&1; then
  port_listening=no
  netstat -ltn 2>/dev/null | grep -q ":$PORT " && port_listening=yes && running=yes || true
fi
env_file=no
start_script=no
shell_entrypoints=0
vscode_entrypoints=0
[ -s "$ENV_FILE" ] && grep -q "HTTP_PROXY=http://127.0.0.1:$PORT" "$ENV_FILE" 2>/dev/null && env_file=yes
[ -x "$START_SCRIPT" ] && start_script=yes
for file in {shell_paths}; do
  [ -f "$file" ] && grep -q "# >>> API切换器 AI proxy >>>" "$file" 2>/dev/null && shell_entrypoints=$((shell_entrypoints + 1))
done
for file in {vscode_paths}; do
  [ -f "$file" ] && grep -q "{VSCODE_ENV_BLOCK_START}" "$file" 2>/dev/null && vscode_entrypoints=$((vscode_entrypoints + 1))
done
printf 'installed=%s\\nrunning=%s\\npid_running=%s\\npid_managed=%s\\nport_listening=%s\\nenv_file=%s\\nstart_script=%s\\nshell_entrypoints=%s\\nvscode_entrypoints=%s\\nconfig_present=%s\\nconfig_owned=%s\\nconfig_legacy=%s\\nconfig=%s\\n' "$installed" "$running" "$pid_running" "$pid_managed" "$port_listening" "$env_file" "$start_script" "$shell_entrypoints" "$vscode_entrypoints" "$config_present" "$config_owned" "$config_legacy" "$CONFIG"
"""
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, command, timeout=20)
    if status != 0:
        raise RuntimeError((stderr or stdout or "远端 AI 代理状态检查失败").strip())
    values = _parse_key_values(stdout)
    detail_parts = []
    if values.get("pid_running") == "yes" and values.get("pid_managed") == "no":
        detail_parts.append("pid 文件指向非 mihomo/clash 进程")
    if values.get("pid_running") == "yes" and values.get("port_listening") == "no":
        detail_parts.append("进程存在，但端口未监听")
    elif values.get("pid_running") == "no" and values.get("port_listening") == "yes":
        detail_parts.append("端口已监听，但 pid 文件未更新")
    if values.get("installed") != "yes" and values.get("port_listening") == "yes":
        detail_parts.append("端口正在监听，但不是本工具配置")
    if values.get("config_present") == "yes" and values.get("config_owned") != "yes":
        if values.get("config_legacy") == "yes":
            detail_parts.append("检测到旧/非本工具 mihomo 配置，未计入 AI 代理")
        else:
            detail_parts.append("检测到非本工具 mihomo 配置，未计入 AI 代理")
    if values.get("installed") == "yes":
        if values.get("env_file") != "yes":
            detail_parts.append("远端代理环境文件缺失或端口不匹配")
        if values.get("start_script") != "yes":
            detail_parts.append("远端启动脚本缺失或不可执行")
        if _int_or_default(values.get("shell_entrypoints"), 0) <= 0:
            detail_parts.append("shell 启动入口未检测到")
        if _int_or_default(values.get("vscode_entrypoints"), 0) <= 0:
            detail_parts.append("VS Code Remote 启动入口未检测到")
    return RemoteAIProxyStatus(
        installed=values.get("installed") == "yes",
        running=values.get("running") == "yes",
        config_path=values.get("config") or config_path,
        proxy_url=f"http://127.0.0.1:{mixed_port}",
        detail="；".join(detail_parts),
    )


def probe_ai_proxy(ssh_name: str, mixed_port: int = 7890, timeout: int = 8) -> str:
    proxy_status = inspect_ai_proxy(ssh_name, mixed_port)
    if not proxy_status.running:
        return f"{ssh_name}: {proxy_status.summary()}；代理未运行，跳过 AI 连通性探测"

    _ssh_profile, client = _connect_ssh(ssh_name)
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    command = _build_probe_command(mixed_port, timeout)
    exit_status, stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        timeout=max(30, timeout * 5),
        log_command=False,
    )
    if exit_status != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"远端 AI 代理连通性测试失败: {detail or exit_status}")
    results = _parse_remote_probe_output(stdout)
    if not results:
        return f"{ssh_name}: 未得到连通性测试结果"
    ok_count = sum(1 for item in results if item.ok)
    message = (
        f"{ssh_name}: {proxy_status.summary()}；AI 连通性 {ok_count}/{len(results)} 可达；"
        + "；".join(item.summary() for item in results)
    )
    if ok_count == 0:
        log_hint = _read_remote_ai_proxy_error_tail(client, remote_config._remote_home(client))
        if log_hint:
            message += f"；最近远端代理日志: {log_hint}"
    return message


def cleanup_ai_proxy(ssh_name: str, mixed_port: int = 7890, include_legacy_config: bool = True) -> str:
    _ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    command = _build_cleanup_command(home, mixed_port, include_legacy_config)
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, command, timeout=180, log_command=False)
    if status != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"远端 AI 代理清理失败: {detail or status}")

    values = _parse_key_values(stdout)
    pieces = []
    stopped_pids = values.get("stopped_pids", "")
    if stopped_pids:
        pieces.append(f"已停止进程 {stopped_pids}")
    removed_files = _int_or_default(values.get("removed_files"), 0)
    removed_blocks = _int_or_default(values.get("removed_blocks"), 0)
    removed_settings = _int_or_default(values.get("removed_settings"), 0)
    backed_up_configs = _int_or_default(values.get("backed_up_configs"), 0)
    if removed_files:
        pieces.append(f"移除受管文件 {removed_files} 个")
    if removed_blocks:
        pieces.append(f"移除 shell/VS Code Remote 入口 {removed_blocks} 处")
    if removed_settings:
        pieces.append(f"清理 VS Code settings {removed_settings} 处")
    if backed_up_configs:
        backup_dir = values.get("backup_dir") or ""
        pieces.append(f"备份并移走旧代理配置 {backed_up_configs} 个" + (f"到 {backup_dir}" if backup_dir else ""))
    if values.get("still_listening") == "yes":
        pieces.append("清理后端口仍在监听，请检查非本工具代理进程")
    else:
        pieces.append("代理端口未监听")
    skipped_pids = values.get("skipped_pids", "")
    if skipped_pids:
        pieces.append(f"跳过非 mihomo/clash 进程 {skipped_pids}")
    notes = values.get("notes", "")
    if notes:
        pieces.append(notes)
    if not pieces:
        pieces.append("未发现需要清理的远端 AI 代理")
    return f"{ssh_name}: AI 代理清理完成；" + "；".join(pieces)


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
        sources=tuple(network_diagnostic_settings.normalize_services(sources or [])),
    )


def _resolve_proxy_node_ip(node: dict, resolver=None) -> str:
    normalized = _normalize_proxy_node(node)
    server = str(normalized.get("server") or "").strip().strip("[]")
    if not server:
        raise ValueError("代理节点缺少服务器地址")
    try:
        ipaddress.ip_address(server)
        return server
    except ValueError:
        pass

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
            ipaddress.ip_address(text)
        except ValueError:
            continue
        if text not in candidates:
            candidates.append(text)
    if not candidates:
        raise ValueError(f"无法解析节点服务器: {server}")

    def rank(value: str) -> tuple[int, int]:
        parsed = ipaddress.ip_address(value)
        return (0 if parsed.version == 4 else 1, 0 if parsed.is_global else 1)

    return sorted(candidates, key=rank)[0]


def _proxy_quality_score(classification: network_diagnostics.IpClassification) -> int:
    risk = max(0, min(100, int(classification.risk_score)))
    score = 100 - risk
    ip_type = str(classification.ip_type or "")
    if "高风险" in ip_type:
        score -= 35
    elif any(marker in ip_type for marker in ("家庭宽带", "住宅", "家庭/非IDC", "运营商/宽带")):
        score += 18
    elif "蜂窝" in ip_type or "移动网络" in ip_type:
        score -= 8
    elif "企业" in ip_type or "商宽" in ip_type:
        score -= 12
    elif "IDC" in ip_type or "机房" in ip_type:
        score -= 38
    elif "代理" in ip_type or "VPN" in ip_type or "Tor" in ip_type or "匿名" in ip_type:
        score -= 65
    return max(0, min(100, score))


def _proxy_quality_label(classification: network_diagnostics.IpClassification, score: int) -> str:
    ip_type = str(classification.ip_type or "")
    risk = int(classification.risk_score)
    if "冲突" in ip_type:
        return "来源冲突"
    if "高风险" in ip_type:
        return "高风险"
    if any(marker in ip_type for marker in ("家庭宽带", "住宅", "家庭/非IDC", "运营商/宽带")):
        if score >= 80 and risk <= 35:
            return "家宽高质"
        return "家宽可用"
    if "蜂窝" in ip_type or "移动网络" in ip_type:
        return "移动网络"
    if "企业" in ip_type or "商宽" in ip_type:
        return "商宽中等"
    if "IDC" in ip_type or "机房" in ip_type:
        return "机房风险"
    if "代理" in ip_type or "VPN" in ip_type or "Tor" in ip_type or "匿名" in ip_type:
        return "代理风险"
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
) -> tuple[bytes, str, str]:
    try:
        attempts = max(1, int(retries))
    except (TypeError, ValueError):
        attempts = 3
    attempts = max(1, attempts)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            with urlrequest.urlopen(request, timeout=timeout) as response:
                status = _int_or_default(getattr(response, "status", getattr(response, "code", 200)), 200)
                if 400 <= status < 500:
                    raise ValueError(f"订阅链接返回 HTTP {status}，请检查订阅地址是否有效")
                if status >= 500:
                    raise RuntimeError(f"订阅服务器返回 HTTP {status}")

                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise ValueError("订阅内容超过 5MB，已停止读取")

                headers = getattr(response, "headers", {}) or {}
                payload = _decode_http_payload(
                    payload,
                    content_encoding=_header_value(headers, "Content-Encoding"),
                    max_bytes=max_bytes,
                )
                return payload, _response_content_type(headers), _response_charset(headers) or "utf-8"
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                raise ValueError(f"订阅链接返回 HTTP {exc.code}，请检查订阅地址是否有效") from exc
            last_error = exc
        except ValueError:
            raise
        except Exception as exc:
            last_error = exc

        if attempt < attempts:
            delay = _retry_delay_seconds(retry_base_delay, attempt)
            if delay > 0:
                time.sleep(delay)

    suffix = f"（已重试 {attempts} 次）" if attempts > 1 else ""
    raise RuntimeError(f"订阅下载失败{suffix}: {last_error}") from last_error


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


def _save_proxy_subscription(url: str, payload: bytes, content_type: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    raw_start = payload.lstrip()[:80].lower()
    extension = ".yaml" if "yaml" in content_type or raw_start.startswith((b"proxies:", b"proxy-groups:")) else ".txt"
    directory = _proxy_subscription_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"subscription-{digest}{extension}"
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_bytes(payload)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return Path(path)


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
  BIN="$(command -v mihomo 2>/dev/null || command -v clash 2>/dev/null || true)"
fi
if [ -z "$BIN" ]; then
  echo "mihomo/clash not found" >&2
  exit 1
fi
pid_managed() {{
  pid="$1"
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  if ! command -v ps >/dev/null 2>&1; then
    return 0
  fi
  cmd="$(ps -p "$pid" -o comm= -o args= 2>/dev/null || true)"
  case "$cmd" in
    *mihomo*|*clash*) return 0 ;;
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
        echo "port $PORT is already listening, but pid file points to unmanaged process $old_pid" >&2
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
printf '\\n--- API-Switcher AI proxy start %s ---\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date 2>/dev/null || true)" >>"$LOG_FILE"
nohup "$BIN" -d "$CONFIG_DIR" >>"$LOG_FILE" 2>&1 &
echo "$!" > "$PID_FILE"
sleep 2
new_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -z "$new_pid" ] || ! kill -0 "$new_pid" 2>/dev/null; then
  echo "mihomo failed to stay running; see $LOG_FILE" >&2
  exit 2
fi
if ! pid_managed "$new_pid"; then
  echo "started process is not recognized as mihomo/clash; see $LOG_FILE" >&2
  exit 7
fi
for _ in 1 2 3 4 5; do
  if port_listening; then
    exit 0
  fi
  sleep 1
done
if command -v ss >/dev/null 2>&1 || command -v netstat >/dev/null 2>&1; then
  echo "mihomo is running but port $PORT is not listening yet; see $LOG_FILE" >&2
  exit 3
fi
echo "mihomo is running; ss/netstat not found, skipped port listening verification"
exit 0
"""


def _build_cleanup_command(home: str, mixed_port: int, include_legacy_config: bool = True) -> str:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    env = _proxy_env_values(mixed_port)
    template = r'''set +e
HOME_DIR=__HOME_DIR__
PORT=__PORT__
INCLUDE_LEGACY_CONFIG=__INCLUDE_LEGACY_CONFIG__
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
backed_up_configs=0
stopped_pids=""
skipped_pids=""
notes=""
backup_dir=""

append_note() {
  if [ -n "$notes" ]; then
    notes="$notes; $1"
  else
    notes="$1"
  fi
}

is_proxy_pid() {
  pid="$1"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if ! command -v ps >/dev/null 2>&1; then
    return 1
  fi
  cmd="$(ps -p "$pid" -o comm= -o args= 2>/dev/null || true)"
  case "$cmd" in
    *mihomo*|*clash*) return 0 ;;
    *) return 1 ;;
  esac
}

stop_pid_if_proxy() {
  pid="$1"
  case "$pid" in ''|*[!0-9]*) return 0 ;; esac
  if is_proxy_pid "$pid"; then
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
  stop_pid_if_proxy "$(cat "$PID_FILE" 2>/dev/null | tr -cd '0-9' | head -c 20)"
fi
if command -v lsof >/dev/null 2>&1; then
  for pid in $(lsof -nP -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null | sort -u); do
    stop_pid_if_proxy "$pid"
  done
fi
if command -v ss >/dev/null 2>&1; then
  for pid in $(ss -ltnp 2>/dev/null | awk -v port=":$PORT" '$4 ~ port {print $0}' | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u); do
    stop_pid_if_proxy "$pid"
  done
fi
if command -v netstat >/dev/null 2>&1; then
  for pid in $(netstat -ltnp 2>/dev/null | awk -v port=":$PORT" '$4 ~ port {print $7}' | sed -n 's#/.*##p' | sort -u); do
    stop_pid_if_proxy "$pid"
  done
fi
if command -v fuser >/dev/null 2>&1; then
  for pid in $(fuser -n tcp "$PORT" 2>/dev/null | tr ' ' '\n' | sort -u); do
    stop_pid_if_proxy "$pid"
  done
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
  listener_detail="$(ss -ltnp 2>/dev/null | awk -v port=":$PORT" '$4 ~ port {print $0}' | head -n 3)"
  [ -n "$listener_detail" ] && still_listening=yes
elif command -v netstat >/dev/null 2>&1; then
  listener_detail="$(netstat -ltnp 2>/dev/null | awk -v port=":$PORT" '$4 ~ port {print $0}' | head -n 3)"
  [ -n "$listener_detail" ] && still_listening=yes
else
  still_listening=unknown
fi

printf 'removed_files=%s\nremoved_blocks=%s\nremoved_settings=%s\nbacked_up_configs=%s\nbackup_dir=%s\nstopped_pids=%s\nskipped_pids=%s\nstill_listening=%s\n' "$removed_files" "$removed_blocks" "$removed_settings" "$backed_up_configs" "$backup_dir" "$(echo "$stopped_pids" | xargs 2>/dev/null)" "$(echo "$skipped_pids" | xargs 2>/dev/null)" "$still_listening"
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
        "__CONFIG_MARKER__": shlex.quote(AI_PROXY_CONFIG_MARKER),
        "__PROXY_URL__": shlex.quote(env["API_SWITCHER_AI_PROXY_URL"]),
        "__NO_PROXY_VALUE__": shlex.quote(env["NO_PROXY"]),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def _build_probe_command(mixed_port: int, timeout: int = 8) -> str:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    try:
        timeout = max(1, min(60, int(timeout)))
    except (TypeError, ValueError):
        timeout = 8
    targets_json = json.dumps(REMOTE_AI_PROBE_TARGETS, ensure_ascii=False)
    curl_probes = "\n".join(
        f"probe_curl {shlex.quote(label)} {shlex.quote(url)}"
        for label, url in REMOTE_AI_PROBE_TARGETS
    )
    return f"""set -u
PROXY=http://127.0.0.1:{mixed_port}
TIMEOUT={timeout}
TARGETS_JSON={shlex.quote(targets_json)}
if command -v python3 >/dev/null 2>&1; then
  python3 - "$PROXY" "$TIMEOUT" "$TARGETS_JSON" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

proxy_url = sys.argv[1]
timeout = float(sys.argv[2])
targets = json.loads(sys.argv[3])
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({{"http": proxy_url, "https": proxy_url}})
)

def clean(value):
    return str(value or "").replace("\\t", " ").replace("\\r", " ").replace("\\n", " ")[:180]

for label, url in targets:
    started = time.monotonic()
    ok = 0
    detail = ""
    try:
        request = urllib.request.Request(
            url,
            headers={{"Accept": "*/*", "User-Agent": "API-Switcher/1.0"}},
        )
        with opener.open(request, timeout=timeout) as response:
            code = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
            ok = 1 if 0 < code < 500 else 0
            detail = f"HTTP {{code}}" if code else "no status"
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        ok = 1 if 0 < code < 500 else 0
        detail = f"HTTP {{code}}" if code else clean(exc)
    except Exception as exc:
        detail = clean(exc)
    elapsed = max(0, int((time.monotonic() - started) * 1000))
    print(f"probe\\t{{clean(label)}}\\t{{ok}}\\t{{clean(detail)}}\\t{{elapsed}}", flush=True)
PY
  exit $?
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
  http_code="$(curl -x "$PROXY" -m "$TIMEOUT" -sS -o /dev/null -w "%{{http_code}}" "$url" 2>"$TMP_ERR")" || rc=$?
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


def _build_remote_latency_command(timeout: float = 3.0, attempts: int = 2, max_workers: int = 16) -> str:
    timeout = _normalize_timeout(timeout, 3.0)
    attempts = max(1, _int_or_default(attempts, 2))
    max_workers = max(1, min(_int_or_default(max_workers, 16), 64))
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
    futures = [executor.submit(measure, item) for item in nodes if isinstance(item, dict)]
    for future in concurrent.futures.as_completed(futures):
        key, ok, latency, detail = future.result()
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
with urllib.request.urlopen(request, timeout=8) as response:
    code = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
    if not (200 <= code < 300):
        raise SystemExit(f"mihomo reload HTTP {{code}}")
PY
  exit $?
fi
if command -v curl >/dev/null 2>&1; then
  curl -fsS -X PUT -H 'Content-Type: application/json' --data "$PAYLOAD" "$URL" >/dev/null
  exit $?
fi
echo "远端未安装 python3/curl，无法调用 mihomo 热更新接口" >&2
exit 12
"""


def _remote_latency_command_timeout(node_count: int, timeout: float, attempts: int, max_workers: int) -> int:
    workers = max(1, min(_int_or_default(max_workers, 16), max(1, int(node_count or 1))))
    batches = (max(1, int(node_count or 1)) + workers - 1) // workers
    return max(45, min(300, int(batches * max(1, attempts) * _normalize_timeout(timeout, 3.0) + 30)))


def _build_install_command(
    home: str,
    config_dir: str,
    app_dir: str,
    local_bin_dir: str,
    start_path: str,
    mixed_port: int,
) -> str:
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    return f"""set -eu
HOME_DIR={shlex.quote(home)}
CONFIG_DIR={shlex.quote(config_dir)}
APP_DIR={shlex.quote(app_dir)}
LOCAL_BIN_DIR={shlex.quote(local_bin_dir)}
START_SCRIPT={shlex.quote(start_path)}
PORT={mixed_port}
BIN="$LOCAL_BIN_DIR/mihomo"
mkdir -p "$CONFIG_DIR" "$APP_DIR" "$LOCAL_BIN_DIR"
if [ ! -x "$BIN" ] && ! command -v mihomo >/dev/null 2>&1; then
  arch="$(uname -m 2>/dev/null || echo unknown)"
  case "$arch" in
    x86_64|amd64) pattern="linux-amd64" ;;
    aarch64|arm64) pattern="linux-arm64" ;;
    armv7l|armv7*) pattern="linux-armv7" ;;
    *) echo "不支持的远端架构: $arch" >&2; exit 3 ;;
  esac
  if command -v python3 >/dev/null 2>&1; then
    if ! python3 - "$pattern" "$BIN" <<'PY'
import gzip
import json
import os
import sys
import time
import urllib.request

pattern, target = sys.argv[1], sys.argv[2]

def read_url(url, timeout):
    last_error = None
    for attempt in range(1, 4):
        try:
            request = urllib.request.Request(
                url,
                headers={{
                    "Accept": "application/vnd.github+json, application/octet-stream, */*",
                    "User-Agent": "API-Switcher/1.0",
                }},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt)
    raise RuntimeError(f"download failed after 3 attempts: {{last_error}}") from last_error

data = json.loads(read_url("https://api.github.com/repos/MetaCubeX/mihomo/releases/latest", 45).decode("utf-8"))
assets = data.get("assets") or []

def usable(asset):
    name = str(asset.get("name") or "").lower()
    if pattern not in name or not name.endswith(".gz"):
        return False
    return not any(token in name for token in ("deb", "rpm", "sha256", "checksums"))

candidates = [asset for asset in assets if usable(asset) and "compatible" not in str(asset.get("name", "")).lower()]
if not candidates:
    candidates = [asset for asset in assets if usable(asset)]
if not candidates:
    raise SystemExit(f"no mihomo asset matched {{pattern}}")
url = candidates[0]["browser_download_url"]
payload = read_url(url, 180)
if url.lower().endswith(".gz"):
    payload = gzip.decompress(payload)
with open(target, "wb") as handle:
    handle.write(payload)
os.chmod(target, 0o755)
print("downloaded=" + url)
PY
    then
      if command -v clash >/dev/null 2>&1; then
        echo "mihomo 下载失败，回退使用远端已有 clash" >&2
      else
        exit 4
      fi
    fi
  elif command -v clash >/dev/null 2>&1; then
    echo "远端未安装 python3，回退使用已有 clash" >&2
  else
    echo "远端未安装 python3，且未找到 mihomo/clash，无法自动下载 mihomo" >&2
    exit 2
  fi
fi
"$START_SCRIPT" restart
printf 'config=%s\\nproxy=http://127.0.0.1:%s\\n' "$CONFIG_DIR/config.yaml" "$PORT"
"""


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
  awk -v start="$BLOCK_START" -v end="$BLOCK_END" '
    $0 == start {{skip=1; next}}
    $0 == end {{skip=0; next}}
    skip != 1 {{print}}
  ' "$file" > "$tmp"
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


def _compact_probe_summary(summary: str) -> str:
    text = str(summary or "").strip()
    if not text:
        return ""
    parts = [part for part in text.split("；") if part]
    useful = [part for part in parts if "AI 连通性" in part or "OpenAI/ChatGPT" in part or "Claude/Anthropic" in part or "Gemini/Google AI" in part]
    return "；".join(useful or parts[:2])[:900]


def _restore_remote_proxy_node_after_failed_update(
    ssh_name: str,
    original_node: dict | None,
    attempted_node: dict | None,
    mixed_port: int,
) -> str:
    if not original_node:
        return "；未读取到更新前节点，已保留最后一次热更新状态"
    try:
        original = _normalize_proxy_node(original_node)
    except Exception:
        return "；更新前节点格式不可恢复，已保留最后一次热更新状态"
    try:
        reload_ai_proxy(ssh_name, format_proxy_node(original), mixed_port)
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
        )
    return results


def _find_matching_subscription_node(nodes, current_node: dict | None):
    if not current_node:
        return None
    try:
        current = _normalize_proxy_node(current_node)
    except Exception:
        return None
    current_key = proxy_node_key(current)
    for item in nodes or []:
        try:
            if proxy_node_key(item.node) == current_key:
                return item
        except Exception:
            continue
    current_name = str(current.get("name") or "").strip().lower()
    if current_name:
        for item in nodes or []:
            try:
                if str(item.node.get("name") or "").strip().lower() == current_name:
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
        return _normalize_proxy_node(node)
    except Exception:
        return None


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
