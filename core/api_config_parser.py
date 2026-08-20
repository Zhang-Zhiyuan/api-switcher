"""Parse copied Claude Code/Codex API snippets without executing shell code.

The parser intentionally accepts several common export formats because users
often copy a whole setup block rather than one value. It only reads text and
returns normalized fields; it never evaluates, imports, or logs the supplied
secrets.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from core.url_validation import validate_api_base_url


_ASSIGNMENT_RE = re.compile(
    r"(?:^|[\r\n;&])\s*(?:(?:export|set|env)\s+)?"
    r"(?:\$env:)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"([^\r\n;&]*?)(?=\s*(?:[;&]|$))",
    re.MULTILINE,
)
_SETX_RE = re.compile(
    r"(?:^|[\r\n;&])\s*setx(?:\.exe)?\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\r\n;&]+)",
    re.IGNORECASE | re.MULTILINE,
)
_CMD_SET_QUOTED_RE = re.compile(
    r"(?:^|[\r\n;&])\s*set\s+\"([A-Za-z_][A-Za-z0-9_]*)\s*=([^\"]*)\"",
    re.IGNORECASE | re.MULTILINE,
)
_POWERSHELL_SET_RE = re.compile(
    r"(?i)\[Environment\]::SetEnvironmentVariable\(\s*"
    r"['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*,\s*"
    r"['\"]([^'\"]*)['\"]",
)
_POWER_SHELL_RE = re.compile(
    r"(?:^|[\r\n;&])\s*\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"([^\r\n;&]*?)(?=\s*(?:[;&]|$))",
    re.MULTILINE,
)
_URL_RE = re.compile(
    r"(?i)(?<![\w@])https?://[^\s\"'<>`]+|"
    r"(?<![\w@])(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?(?:/[^\s\"'<>`]*)?"
)
_HEADER_RE = re.compile(
    r"(?i)(?:x-api-key|authorization|api[-_ ]?key|auth[-_ ]?token)\s*:\s*"
    r"(?:bearer\s+)?([^\s\"']+)"
)

_URL_KEYS = {
    "ANTHROPIC_BASE_URL", "CLAUDE_BASE_URL", "CLAUDE_CODE_BASE_URL",
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_BASE_URL", "CODEX_API_BASE",
    "API_BASE_URL", "BASE_URL", "BASEURL", "ENDPOINT", "API_ENDPOINT",
}
_MODEL_KEYS = (
    "ANTHROPIC_MODEL", "CLAUDE_CODE_MODEL", "CLAUDE_MODEL", "OPENAI_MODEL",
    "CODEX_MODEL", "DEFAULT_MODEL", "MODEL",
)
_CLAUDE_TOKEN_KEYS = (
    "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY", "API_KEY", "APIKEY", "AUTH_TOKEN", "AUTHORIZATION",
)
_CODEX_TOKEN_KEYS = (
    "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_AUTH_TOKEN", "API_KEY",
    "APIKEY", "AUTH_TOKEN", "AUTHORIZATION",
)
_CLAUDE_URL_KEYS = ("ANTHROPIC_BASE_URL", "CLAUDE_BASE_URL", "CLAUDE_CODE_BASE_URL")
_CODEX_URL_KEYS = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_BASE_URL", "CODEX_API_BASE")

_PROVIDER_HOST_HINTS = (
    ("deepseek", ("deepseek.com",)),
    ("kimi", ("kimi.com", "moonshot.ai", "moonshot.cn")),
    ("minimax", ("minimax.io", "minimax.chat", "minimaxi.com")),
    ("qwen", ("dashscope.aliyuncs.com",)),
    ("gemini", ("generativelanguage.googleapis.com",)),
    ("glm", ("bigmodel.cn", "z.ai")),
)
_FALLBACK_URLS = {
    "claude": {
        "deepseek": "https://api.deepseek.com/anthropic",
        "kimi": "https://api.kimi.com/coding",
        "minimax": "https://api.minimax.io/anthropic",
        "qwen": "https://dashscope.aliyuncs.com/apps/anthropic",
    },
    "codex": {
        "deepseek": "https://api.deepseek.com",
        "kimi": "https://api.moonshot.ai/v1",
        "minimax": "https://api.minimax.chat/v1",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "glm": "https://open.bigmodel.cn/api/coding/paas/v4",
    },
}
_PROVIDER_KEY_HINTS = (
    ("deepseek", ("DEEPSEEK",)),
    ("kimi", ("KIMI", "MOONSHOT")),
    ("minimax", ("MINIMAX",)),
    ("qwen", ("QWEN", "DASHSCOPE")),
    ("gemini", ("GEMINI", "GOOGLE")),
    ("glm", ("GLM", "ZHIPU", "BIGMODEL")),
)

_NON_API_URL_HOST_HINTS = (
    "json.schemastore.org",
    "schemastore.org",
    "opencode.ai",
)


@dataclass(frozen=True)
class ParsedAPIConfig:
    """Normalized values suitable for either profile editor."""

    profile_type: str
    token: str = ""
    auth_scheme: str = "auth_token"
    base_url: str = ""
    model: str = ""
    provider_id: str = "custom"
    provider_name: str = ""
    env_key: str = ""
    name: str = ""
    matched_keys: tuple[str, ...] = field(default_factory=tuple)
    url_inferred: bool = False


def _clean_value(value: object) -> str:
    text = str(value or "").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    if text.endswith(","):
        text = text[:-1].rstrip()
    if len(text) >= 2 and text.startswith("\\\"") and text.endswith("\\\""):
        text = text[1:-1]
    if not (text.startswith(("'", '"', "`"))):
        text = re.split(r"\s+#", text, maxsplit=1)[0].rstrip()
    return text.strip()


def _flatten_json(value: object, prefix: str = "") -> dict[str, str]:
    flattened: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).strip()
            if not name:
                continue
            flattened.update(_flatten_json(item, f"{prefix}_{name}" if prefix else name))
    elif isinstance(value, (str, int, float, bool)):
        flattened[prefix.upper()] = _clean_value(value)
    return flattened


def _json_candidates(text: str) -> list[str]:
    """Find complete JSON objects in plain text or Markdown code fences."""

    raw = str(text or "").strip().strip("`").strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)

    lines = str(text or "").splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    fenced = "\n".join(lines).strip()
    if fenced and fenced not in candidates:
        candidates.append(fenced)

    # A copied config is often surrounded by a label or prose. JSONDecoder
    # can locate a nested object without evaluating any shell content.
    decoder = json.JSONDecoder()
    source = str(text or "")
    for index, character in enumerate(source):
        if character != "{":
            continue
        try:
            _parsed, end = decoder.raw_decode(source[index:])
        except (TypeError, ValueError):
            continue
        candidate = source[index : index + end].strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _extract_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in _ASSIGNMENT_RE.finditer(text):
        values[match.group(1).upper()] = _clean_value(match.group(2))
    for match in _POWER_SHELL_RE.finditer(text):
        values[match.group(1).upper()] = _clean_value(match.group(2))
    for match in _SETX_RE.finditer(text):
        values[match.group(1).upper()] = _clean_value(match.group(2))
    for match in _CMD_SET_QUOTED_RE.finditer(text):
        values[match.group(1).upper()] = _clean_value(match.group(2))
    for match in _POWERSHELL_SET_RE.finditer(text):
        values[match.group(1).upper()] = _clean_value(match.group(2))

    # JSON fragments are common in copied config/auth files. Try the complete
    # payload first for pretty-printed JSON, then compact JSON lines embedded
    # in surrounding prose.
    json_candidates = _json_candidates(text)
    json_candidates.extend(line.strip().strip("`;") for line in text.splitlines())
    for candidate in json_candidates:
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        values.update(_flatten_json(parsed))

    known_suffixes = _URL_KEYS | set(_MODEL_KEYS) | set(_CLAUDE_TOKEN_KEYS) | set(_CODEX_TOKEN_KEYS)
    for key, value in list(values.items()):
        for suffix in known_suffixes:
            if key.endswith("_" + suffix):
                values.setdefault(suffix, value)
        # OpenCode/AI SDK uses camelCase option names, which become
        # PROVIDER_ANTHROPIC_OPTIONS_BASEURL/APIKEY after flattening JSON.
        if key.endswith("_BASEURL"):
            values.setdefault("BASEURL", value)
        if key.endswith("_APIKEY"):
            values.setdefault("APIKEY", value)

    for match in _HEADER_RE.finditer(text):
        value = _clean_value(match.group(1))
        if value:
            lowered = match.group(0).lower()
            key = "ANTHROPIC_API_KEY" if "x-api-key" in lowered else "AUTHORIZATION"
            values.setdefault(key, value)
    return values


def _url_candidates(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in _URL_RE.finditer(text):
        value = match.group(0).rstrip(".,;:)]}\\")
        if value.lower() in {"http://", "https://"}:
            continue
        if "://" not in value:
            value = f"https://{value}"
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _choose_url_candidate(urls: list[str], profile_type: str) -> str:
    """Prefer an API-looking URL over schema/documentation links."""

    api_urls = [
        url
        for url in urls
        if not any(
            (urlsplit(url).hostname or "").casefold() == hint
            or (urlsplit(url).hostname or "").casefold().endswith("." + hint)
            for hint in _NON_API_URL_HOST_HINTS
        )
    ]
    if not api_urls:
        return ""
    urls = api_urls

    def score(url: str) -> tuple[int, int]:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        path = parsed.path.casefold()
        value = 0
        if any(host == hint or host.endswith("." + hint) for hint in _NON_API_URL_HOST_HINTS):
            value -= 100
        if path.endswith((".json", ".yaml", ".yml")):
            value -= 20
        if "/v1" in path or "api" in host or "endpoint" in path:
            value += 20
        if profile_type == "claude" and any(marker in host for marker in ("anthropic", "claude")):
            value += 15
        if profile_type == "codex" and any(marker in host for marker in ("openai", "codex")):
            value += 15
        return value, -len(url)

    return max(urls, key=score, default="")


def _normalize_url(value: str, *, profile_type: str, inferred: bool) -> str:
    raw = _clean_value(value)
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
        path = parsed.path.rstrip("/")
        lowered = path.lower()
        suffixes = (
            ("/v1/chat/completions", "/v1"), ("/chat/completions", ""),
            ("/v1/responses", "/v1"), ("/responses", ""),
            ("/v1/messages", "/v1"), ("/messages", ""),
            ("/v1/models", "/v1"), ("/models", ""),
        )
        for suffix, replacement in suffixes:
            if lowered.endswith(suffix):
                path = path[: -len(suffix)] + replacement
                break
        normalized = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        normalized = validate_api_base_url(normalized)
    except (TypeError, ValueError):
        return ""

    # OpenAI-compatible gateways conventionally expose /v1 when a bare URL
    # was sniffed from surrounding text. Explicit BASE_URL values are kept
    # exactly as supplied because some gateways intentionally use root paths.
    if profile_type == "codex" and inferred:
        parsed = urlsplit(normalized)
        if not parsed.path or parsed.path == "/":
            normalized = urlunsplit((parsed.scheme, parsed.netloc, "/v1", "", ""))
    return normalized.rstrip("/")


def _provider_for_url(url: str) -> str:
    host = urlsplit(url).hostname.casefold() if url else ""
    for provider, domains in _PROVIDER_HOST_HINTS:
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return provider
    return "custom"


def _provider_for_keys(keys: set[str]) -> str:
    for provider, hints in _PROVIDER_KEY_HINTS:
        if any(any(hint in key for hint in hints) for key in keys):
            return provider
    return "custom"


def _pick_value(values: dict[str, str], keys: tuple[str, ...]) -> tuple[str, str]:
    for key in keys:
        value = _clean_value(values.get(key))
        if value:
            return value, key
    return "", ""


def parse_api_config_text(text: str, profile_type: str | None = None) -> ParsedAPIConfig:
    """Parse copied environment/config text for a Claude or Codex profile."""

    raw = str(text or "").strip()
    if not raw:
        raise ValueError("剪贴板中没有可解析的 API 配置文本")
    values = _extract_values(raw)
    upper_keys = set(values)
    inferred_type = "claude" if any(k.startswith(("ANTHROPIC_", "CLAUDE_")) for k in upper_keys) else ""
    if not inferred_type and any(k.startswith(("OPENAI_", "CODEX_")) for k in upper_keys):
        inferred_type = "codex"
    target_type = str(profile_type or inferred_type or "claude").strip().lower()
    if target_type not in {"claude", "codex"}:
        raise ValueError("API 类型只能是 Claude 或 Codex")

    url_keys = _CLAUDE_URL_KEYS if target_type == "claude" else _CODEX_URL_KEYS
    explicit_url, explicit_url_key = _pick_value(values, url_keys)
    if not explicit_url:
        explicit_url, explicit_url_key = _pick_value(
            values,
            ("API_BASE_URL", "BASE_URL", "BASEURL", "ENDPOINT", "API_ENDPOINT"),
        )
    urls = _url_candidates(raw)
    url_inferred = not bool(explicit_url)
    url_value = explicit_url or _choose_url_candidate(urls, target_type)

    token_keys = _CLAUDE_TOKEN_KEYS if target_type == "claude" else _CODEX_TOKEN_KEYS
    token, token_key = _pick_value(values, token_keys)
    if token_key == "API_KEY":
        vendor_key = next(
            (key for key, value in values.items() if key.endswith("_API_KEY") and _clean_value(value)),
            "",
        )
        if vendor_key:
            token, token_key = _clean_value(values[vendor_key]), vendor_key
    if not token:
        # Vendor-specific keys such as DEEPSEEK_API_KEY are valid Codex keys.
        for key, value in values.items():
            if key.endswith(("_API_KEY", "_AUTH_TOKEN")) and _clean_value(value):
                token, token_key = _clean_value(value), key
                break
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    model, _model_key = _pick_value(values, _MODEL_KEYS)
    normalized_candidate = _normalize_url(
        url_value, profile_type=target_type, inferred=url_inferred
    )
    if not normalized_candidate and explicit_url and urls:
        normalized_candidate = _normalize_url(
            urls[0], profile_type=target_type, inferred=True
        )
        url_inferred = True
    provider_id = _provider_for_url(normalized_candidate)
    if provider_id == "custom":
        provider_id = _provider_for_keys(upper_keys)
    if provider_id == "custom":
        provider_hint = _clean_value(values.get("PROVIDER") or values.get("MODEL_PROVIDER"))
        if provider_hint:
            provider_id = provider_hint.casefold().replace(" ", "-")
    base_url = normalized_candidate
    fallback_urls = _FALLBACK_URLS[target_type]
    if not base_url and provider_id in fallback_urls:
        base_url = fallback_urls[provider_id]
        url_inferred = True
    if not token:
        raise ValueError("未找到 API Key/Auth Token；请复制完整的密钥环境变量")
    if not base_url:
        raise ValueError("未找到 API 端点；请同时复制 BASE_URL 或包含 http(s):// 的地址")

    host = urlsplit(base_url).hostname or "api"
    provider_name = _clean_value(values.get("PROVIDER_NAME") or values.get("CUSTOM_PROVIDER_NAME"))
    if not provider_name and provider_id == "custom":
        provider_name = host.split(".")[-2] if len(host.split(".")) >= 2 else host
    name = _clean_value(values.get("PROFILE_NAME") or values.get("API_NAME"))
    if not name:
        name = f"{provider_name or host} {'Claude' if target_type == 'claude' else 'Codex'}"
    auth_scheme = (
        "api_key"
        if target_type == "claude"
        and ("API_KEY" in token_key or token_key.endswith("APIKEY"))
        else "auth_token"
    )
    env_key = token_key if target_type == "codex" else ""
    matched = tuple(sorted({key for key in (explicit_url_key, token_key) if key}))
    return ParsedAPIConfig(
        profile_type=target_type,
        token=token,
        auth_scheme=auth_scheme,
        base_url=base_url,
        model=model,
        provider_id=provider_id,
        provider_name=provider_name,
        env_key=env_key,
        name=name,
        matched_keys=matched,
        url_inferred=url_inferred,
    )
