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

from core.url_validation import normalize_claude_base_url, validate_api_base_url


_MAX_CONFIG_TEXT_CHARS = 2_000_000
_ASSIGNMENT_RE = re.compile(
    r"(?:^|[\r\n;&])\s*(?:(?:export|set|env)\s+)?"
    r"(?:\$env:)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`[^`]*`|[^\r\n;&]*?)"
    r"(?=\s*(?:\#.*)?(?:[;&]|$))",
    re.IGNORECASE | re.MULTILINE,
)
_SETX_RE = re.compile(
    r"(?:^|[\r\n;&])\s*setx(?:\.exe)?\s+(?:/m\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\r\n;&]+)",
    re.IGNORECASE | re.MULTILINE,
)
_SETX_EQUALS_RE = re.compile(
    r"(?:^|[\r\n;&])\s*setx(?:\.exe)?\s+(?:/m\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\r\n;&]+)",
    re.IGNORECASE | re.MULTILINE,
)
_CMD_SET_QUOTED_RE = re.compile(
    r"(?:^|[\r\n;&])\s*set\s+\"([A-Za-z_][A-Za-z0-9_]*)\s*=([^\"]*)\"",
    re.IGNORECASE | re.MULTILINE,
)
_POWERSHELL_SET_RE = re.compile(
    r"(?i)\[(?:System\.)?Environment\]::SetEnvironmentVariable\(\s*"
    r"['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*,\s*"
    r"['\"]([^'\"]*)['\"]",
)
_POWER_SHELL_RE = re.compile(
    r"(?:^|[\r\n;&])\s*\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`[^`]*`|[^\r\n;&]*?)"
    r"(?=\s*(?:\#.*)?(?:[;&]|$))",
    re.IGNORECASE | re.MULTILINE,
)
_URL_RE = re.compile(
    r"(?i)(?<![\w@])https?://[^\s\"'<>`]+|"
    r"(?<![\w@])(?:localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:/[^\s\"'<>`]*)?|"
    r"(?<![\w@])(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?(?:/[^\s\"'<>`]*)?"
)
_HEADER_RE = re.compile(
    r"(?i)(?:x-api-key|authorization|api[-_ ]?key|auth[-_ ]?token)\s*:\s*"
    r"(?:bearer\s+)?(\"[^\"]+\"|'[^']+'|[^\s,}\]]+)"
)
_CLI_OPTION_RE = re.compile(
    r"(?i)(?:^|\s)--?(base[-_]?url|endpoint|api[-_]?key|auth[-_]?token|model)"
    r"(?:\s*=\s*|\s+)(\"[^\"]*\"|'[^']*'|[^\s;&]+)"
)
_INLINE_OPTION_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_?&])(base[ _-]?url|endpoint|api[ _-]?key|auth[ _-]?token|model)"
    r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)"
)
_FISH_SET_RE = re.compile(
    r"(?:^|[\r\n;&])\s*set\s+(?:-[A-Za-z]+\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(\"[^\"]*\"|'[^']*'|[^\r\n;&]+)",
    re.IGNORECASE | re.MULTILINE,
)
_COMMON_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(sk-(?:ant-|proj-)?[A-Za-z0-9_.-]{12,})(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
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
    "CLAUDE_API_KEY", "API_KEY", "APIKEY", "API_TOKEN", "AUTH_TOKEN",
    "BEARER_TOKEN", "AUTHORIZATION",
)
_CODEX_TOKEN_KEYS = (
    "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_AUTH_TOKEN", "API_KEY",
    "APIKEY", "API_TOKEN", "AUTH_TOKEN", "BEARER_TOKEN", "AUTHORIZATION",
)
_CLAUDE_URL_KEYS = ("ANTHROPIC_BASE_URL", "CLAUDE_BASE_URL", "CLAUDE_CODE_BASE_URL")
_CODEX_URL_KEYS = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_BASE_URL", "CODEX_API_BASE")

_JSON_PROVIDER_CONTAINER_KEYS = {
    "provider",
    "providers",
    "modelprovider",
    "modelproviders",
}
_JSON_URL_OPTION_KEYS = {"baseurl", "endpoint", "apiurl", "apiendpoint"}
_JSON_TOKEN_OPTION_KEYS = {"apikey", "authtoken", "token", "bearertoken"}
_JSON_ENV_OPTION_KEYS = {"envkey", "keyenv"}
_JSON_MODEL_OPTION_KEYS = {"model", "defaultmodel"}
_PROVIDER_ENV_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "glm": "ZHIPUAI_API_KEY",
    "zai": "ZAI_API_KEY",
}

_PROVIDER_HOST_HINTS = (
    ("deepseek", ("deepseek.com",)),
    ("kimi", ("kimi.com", "moonshot.ai", "moonshot.cn")),
    ("minimax", ("minimax.io", "minimax.chat", "minimaxi.com")),
    ("qwen", ("dashscope.aliyuncs.com",)),
    ("gemini", ("generativelanguage.googleapis.com",)),
    ("glm", ("bigmodel.cn",)),
    ("zai", ("z.ai",)),
)
_FALLBACK_URLS = {
    "claude": {
        "deepseek": "https://api.deepseek.com/anthropic",
        "kimi": "https://api.kimi.com/coding",
        "minimax": "https://api.minimax.io/anthropic",
        "qwen": "https://dashscope.aliyuncs.com/apps/anthropic",
        "glm": "https://open.bigmodel.cn/api/anthropic",
        "zai": "https://api.z.ai/api/anthropic",
    },
    "codex": {
        "deepseek": "https://api.deepseek.com",
        "kimi": "https://api.moonshot.ai/v1",
        "minimax": "https://api.minimax.chat/v1",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "glm": "https://open.bigmodel.cn/api/coding/paas/v4",
        "zai": "https://api.z.ai/api/coding/paas/v4",
    },
}
_PROVIDER_KEY_HINTS = (
    ("deepseek", ("DEEPSEEK",)),
    ("kimi", ("KIMI", "MOONSHOT")),
    ("minimax", ("MINIMAX",)),
    ("qwen", ("QWEN", "DASHSCOPE")),
    ("gemini", ("GEMINI", "GOOGLE")),
    ("glm", ("GLM", "ZHIPU", "BIGMODEL")),
    ("zai", ("ZAI",)),
)

_NON_API_URL_HOST_HINTS = (
    "docs.anthropic.com",
    "docs.openai.com",
    "github.com",
    "githubusercontent.com",
    "json.schemastore.org",
    "npmjs.com",
    "platform.openai.com",
    "pypi.org",
    "schemastore.org",
    "support.anthropic.com",
    "support.openai.com",
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
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flattened.update(_flatten_json(item, f"{prefix}_{index}" if prefix else str(index)))
    elif isinstance(value, (str, int, float, bool)):
        flattened[prefix.upper()] = _clean_value(value)
    return flattened


def _compact_name(value: object) -> str:
    """Normalize JSON/TOML-style key spellings without weakening env keys."""

    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _json_scalar(value: object, aliases: set[str]) -> tuple[str, str]:
    """Return the first matching scalar from a provider object, breadth-first."""

    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, dict):
            for key, item in current.items():
                compact = _compact_name(key)
                if compact in aliases and isinstance(item, (str, int, float, bool)):
                    cleaned = _clean_value(item)
                    if cleaned:
                        return cleaned, compact
            pending.extend(item for item in current.values() if isinstance(item, (dict, list)))
        elif isinstance(current, list):
            pending.extend(item for item in current if isinstance(item, (dict, list)))
    return "", ""


def _json_provider_entries(value: object):
    """Yield provider-name/config pairs from OpenCode and Codex-like JSON."""

    if isinstance(value, dict):
        for key, item in value.items():
            if _compact_name(key) in _JSON_PROVIDER_CONTAINER_KEYS and isinstance(item, dict):
                for provider_name, provider_config in item.items():
                    if isinstance(provider_config, dict):
                        yield str(provider_name), provider_config
            yield from _json_provider_entries(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_provider_entries(item)


def _provider_id_from_label(value: object) -> str:
    compact = _compact_name(value)
    aliases = {
        "anthropic": "anthropic",
        "claude": "anthropic",
        "claudecode": "anthropic",
        "openai": "openai",
        "codex": "openai",
        "deepseek": "deepseek",
        "kimi": "kimi",
        "moonshot": "kimi",
        "minimax": "minimax",
        "qwen": "qwen",
        "dashscope": "qwen",
        "gemini": "gemini",
        "google": "gemini",
        "glm": "glm",
        "zhipu": "glm",
        "zhipuai": "glm",
        "bigmodel": "glm",
        "zai": "zai",
    }
    return aliases.get(compact, "custom")


def _add_structured_json_values(values: dict[str, str], parsed: object) -> None:
    """Add unambiguous aliases for nested provider configuration objects."""

    if not isinstance(parsed, (dict, list)):
        return

    if isinstance(parsed, dict):
        schema = _clean_value(parsed.get("$schema"))
        if "claude-code" in schema.casefold():
            values.setdefault("__JSON_CLAUDE_HINT", "1")

        for key, item in parsed.items():
            compact = _compact_name(key)
            if compact in {"profilename", "apiname"} and not isinstance(item, (dict, list)):
                values.setdefault("PROFILE_NAME", _clean_value(item))
            elif compact == "modelprovider" and not isinstance(item, (dict, list)):
                provider_label = _clean_value(item)
                values.setdefault("MODEL_PROVIDER", provider_label)
                values.setdefault("__JSON_CODEX_HINT", "1")

    for provider_label, provider_config in _json_provider_entries(parsed):
        provider_id = _provider_id_from_label(provider_label)
        compact_label = _compact_name(provider_label)
        package, _ = _json_scalar(provider_config, {"npm", "package", "adapter"})
        package_lower = package.casefold()
        if provider_id == "anthropic" or "anthropic" in package_lower:
            profile_hint = "claude"
        elif provider_id == "openai" or "openai" in package_lower:
            profile_hint = "codex"
        else:
            profile_hint = ""

        if profile_hint:
            values.setdefault(f"__JSON_{profile_hint.upper()}_HINT", "1")
        if provider_id != "custom":
            values.setdefault("PROVIDER", provider_id)
        elif compact_label not in {"custom", "default"}:
            values.setdefault("CUSTOM_PROVIDER_NAME", _clean_value(provider_label))

        base_url, _ = _json_scalar(provider_config, _JSON_URL_OPTION_KEYS)
        token, token_option = _json_scalar(provider_config, _JSON_TOKEN_OPTION_KEYS)
        env_key, _ = _json_scalar(provider_config, _JSON_ENV_OPTION_KEYS)
        model, _ = _json_scalar(provider_config, _JSON_MODEL_OPTION_KEYS)
        display_name, _ = _json_scalar(provider_config, {"name", "displayname"})

        if display_name and provider_id == "custom":
            values.setdefault("CUSTOM_PROVIDER_NAME", display_name)
        if env_key:
            values.setdefault("CODEX_ENV_KEY", env_key)
        if model:
            model_key = (
                "ANTHROPIC_MODEL" if profile_hint == "claude"
                else "OPENAI_MODEL" if profile_hint == "codex"
                else "MODEL"
            )
            values.setdefault(model_key, model)
        if base_url:
            url_key = (
                "ANTHROPIC_BASE_URL" if profile_hint == "claude"
                else "OPENAI_BASE_URL" if profile_hint == "codex"
                else "BASEURL"
            )
            values.setdefault(url_key, base_url)
        if token:
            if profile_hint == "claude":
                token_key = (
                    "ANTHROPIC_AUTH_TOKEN"
                    if token_option in {"authtoken", "bearertoken", "token"}
                    else "ANTHROPIC_API_KEY"
                )
            elif profile_hint == "codex":
                token_key = "OPENAI_API_KEY"
            else:
                token_key = _PROVIDER_ENV_KEYS.get(provider_id, "APIKEY")
            values.setdefault(token_key, token)


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
    cursor = 0
    while cursor < len(source):
        starts = [
            position
            for position in (source.find("{", cursor), source.find("[", cursor))
            if position >= 0
        ]
        if not starts:
            break
        index = min(starts)
        try:
            _parsed, end = decoder.raw_decode(source[index:])
        except (TypeError, ValueError, RecursionError):
            cursor = index + 1
            continue
        candidate = source[index : index + end].strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)
        # A complete outer object already contains its nested objects. Skip
        # over it to avoid quadratic work and ambiguous aliases from every
        # nested brace in a large copied configuration.
        cursor = index + max(end, 1)
    return candidates


def _clean_setx_value(value: object) -> str:
    cleaned = _clean_value(value)
    return re.sub(r"\s+/m\s*$", "", cleaned, flags=re.IGNORECASE).strip()


def _extract_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in _ASSIGNMENT_RE.finditer(text):
        values[match.group(1).upper()] = _clean_value(match.group(2))
    for match in _POWER_SHELL_RE.finditer(text):
        values[match.group(1).upper()] = _clean_value(match.group(2))
    for match in _SETX_EQUALS_RE.finditer(text):
        values[match.group(1).upper()] = _clean_setx_value(match.group(2))
    for match in _SETX_RE.finditer(text):
        values[match.group(1).upper()] = _clean_setx_value(match.group(2))
    for match in _CMD_SET_QUOTED_RE.finditer(text):
        values[match.group(1).upper()] = _clean_value(match.group(2))
    for match in _POWERSHELL_SET_RE.finditer(text):
        values[match.group(1).upper()] = _clean_value(match.group(2))
    for match in _FISH_SET_RE.finditer(text):
        values[match.group(1).upper()] = _clean_value(match.group(2))
    for pattern in (_CLI_OPTION_RE, _INLINE_OPTION_RE):
        for match in pattern.finditer(text):
            option = _compact_name(match.group(1))
            if pattern is _INLINE_OPTION_RE and option in {"baseurl", "endpoint"}:
                # A URL embedded in Python/JS/YAML-like text is a sniffed
                # candidate. Keeping it out of the explicit-key map lets URL
                # scoring reject documentation links and add /v1 for Codex.
                continue
            key = {
                "baseurl": "BASE_URL",
                "endpoint": "ENDPOINT",
                "apikey": "API_KEY",
                "authtoken": "AUTH_TOKEN",
                "model": "MODEL",
            }.get(option)
            if key:
                values.setdefault(key, _clean_value(match.group(2)))

    # JSON fragments are common in copied config/auth files. Try the complete
    # payload first for pretty-printed JSON, then compact JSON lines embedded
    # in surrounding prose.
    json_candidates = _json_candidates(text)
    json_candidates.extend(line.strip().strip("`;") for line in text.splitlines())
    for candidate in json_candidates:
        is_object = candidate.startswith("{") and candidate.endswith("}")
        is_array = candidate.startswith("[") and candidate.endswith("]")
        if not (is_object or is_array):
            continue
        try:
            parsed = json.loads(candidate)
            flattened = _flatten_json(parsed)
            _add_structured_json_values(values, parsed)
        except (TypeError, ValueError, RecursionError):
            continue
        for key, value in flattened.items():
            values.setdefault(key, value)

    known_suffixes = _URL_KEYS | set(_MODEL_KEYS) | set(_CLAUDE_TOKEN_KEYS) | set(_CODEX_TOKEN_KEYS)
    for key, value in list(values.items()):
        env_marker = key.rfind("_ENV_")
        env_name = key[env_marker + 5:] if env_marker >= 0 else (key[4:] if key.startswith("ENV_") else "")
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name or ""):
            values.setdefault(env_name, value)
        for suffix in known_suffixes:
            if key.endswith("_" + suffix):
                values.setdefault(suffix, value)
        # OpenCode/AI SDK uses camelCase option names, which become
        # PROVIDER_ANTHROPIC_OPTIONS_BASEURL/APIKEY after flattening JSON.
        if key.endswith("_BASEURL"):
            values.setdefault("BASEURL", value)
        if key.endswith("_APIKEY"):
            values.setdefault("APIKEY", value)
        compact = _compact_name(key)
        if compact.endswith(("apiurl", "apiendpoint")):
            values.setdefault("API_BASE_URL", value)
        if compact.endswith(("authtoken", "bearertoken")):
            values.setdefault("AUTH_TOKEN", value)
        if compact.endswith("envkey"):
            values.setdefault("CODEX_ENV_KEY", value)
        if compact.endswith("defaultmodel"):
            values.setdefault("MODEL", value)

    for match in _HEADER_RE.finditer(text):
        value = _clean_value(match.group(1))
        if value:
            lowered = match.group(0).lower()
            if "x-api-key" in lowered:
                key = "ANTHROPIC_API_KEY"
            elif re.search(r"api[-_ ]?key", lowered):
                key = "API_KEY"
            elif re.search(r"auth[-_ ]?token", lowered):
                key = "AUTH_TOKEN"
            else:
                key = "AUTHORIZATION"
            values.setdefault(key, value)

    has_token = any(
        _clean_value(value)
        and (
            key in set(_CLAUDE_TOKEN_KEYS) | set(_CODEX_TOKEN_KEYS)
            or key.endswith(("_API_KEY", "_AUTH_TOKEN"))
        )
        for key, value in values.items()
    )
    if not has_token:
        secret_match = _COMMON_SECRET_RE.search(text)
        if secret_match:
            values["API_KEY"] = secret_match.group(1)
    return values


def _url_candidates(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in _URL_RE.finditer(text):
        value = match.group(0).rstrip(".,;:\\")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while value.endswith(closing) and value.count(closing) > value.count(opening):
                value = value[:-1]
        if value.lower() in {"http://", "https://"}:
            continue
        if "://" not in value:
            local = value.casefold().startswith(("localhost", "127.", "[::1]"))
            value = f"{'http' if local else 'https'}://{value}"
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _choose_url_candidate(urls: list[str], profile_type: str) -> str:
    """Prefer an API-looking URL over schema/documentation links."""

    api_urls = []
    for url in urls:
        try:
            host = (urlsplit(url).hostname or "").casefold()
        except (TypeError, ValueError):
            continue
        if not any(host == hint or host.endswith("." + hint) for hint in _NON_API_URL_HOST_HINTS):
            api_urls.append(url)
    if not api_urls:
        return ""
    urls = api_urls

    def score(url: str) -> tuple[int, int]:
        try:
            parsed = urlsplit(url)
        except (TypeError, ValueError):
            return -1000, -len(url)
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


def _normalize_url_candidates(urls: list[str], profile_type: str) -> str:
    """Try sniffed candidates by relevance until one passes URL validation."""

    remaining = list(urls)
    while remaining:
        candidate = _choose_url_candidate(remaining, profile_type)
        if not candidate:
            return ""
        normalized = _normalize_url(candidate, profile_type=profile_type, inferred=True)
        if normalized:
            return normalized
        remaining.remove(candidate)
    return ""


def _url_candidate_is_invalid_explicit_fragment(candidate: str, explicit: str) -> bool:
    """Avoid silently repairing an invalid scheme/port by taking its host substring."""

    raw = _clean_value(explicit).casefold().rstrip("/")
    sniffed = str(candidate or "").casefold().rstrip("/")
    raw_body = raw.split("://", 1)[-1]
    sniffed_body = sniffed.split("://", 1)[-1]
    return raw_body == sniffed_body or raw_body.startswith(sniffed_body + ":")


def _normalize_url(value: str, *, profile_type: str, inferred: bool) -> str:
    raw = _clean_value(value)
    if not raw:
        return ""
    if "://" not in raw:
        local = raw.casefold().startswith(("localhost", "127.", "[::1]"))
        raw = f"{'http' if local else 'https'}://{raw}"
    try:
        parsed = urlsplit(raw)
        path = parsed.path.rstrip("/")
        lowered = path.lower()
        if profile_type == "claude":
            # A copied Anthropic request URL may include a provider prefix,
            # for example ``/gateway/anthropic/v1/messages``. Claude Code
            # appends ``/v1/messages`` itself, so remove the complete resource
            # suffix here. Keeping the intermediate ``/v1`` would otherwise
            # produce ``.../v1/v1/messages``. An explicit base ending only in
            # ``/api/v1`` is still preserved by normalize_claude_base_url().
            suffixes = (
                ("/v1/messages", ""), ("/messages", ""),
                ("/v1/models", ""), ("/models", ""),
            )
        else:
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
        normalized = urlunsplit(
            (parsed.scheme, parsed.netloc, path, parsed.query, "")
        )
        if profile_type == "claude":
            normalized = normalize_claude_base_url(normalized)
        else:
            normalized = validate_api_base_url(normalized)
    except (TypeError, ValueError):
        return ""

    # OpenAI-compatible gateways conventionally expose /v1 when a bare URL
    # was sniffed from surrounding text. Explicit BASE_URL values are kept
    # exactly as supplied because some gateways intentionally use root paths.
    if profile_type == "codex" and inferred:
        parsed = urlsplit(normalized)
        if not parsed.path or parsed.path == "/":
            normalized = urlunsplit(
                (parsed.scheme, parsed.netloc, "/v1", parsed.query, "")
            )
    # The shared URL validator already removes a trailing slash from the path.
    # Calling ``rstrip('/')`` on the whole URL would corrupt a legitimate query
    # value that happens to end in a slash.
    return normalized


def _provider_for_url(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").casefold() if url else ""
    except (TypeError, ValueError):
        host = ""
    for provider, domains in _PROVIDER_HOST_HINTS:
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return provider
    return "custom"


def _provider_for_keys(keys: set[str]) -> str:
    for provider, hints in _PROVIDER_KEY_HINTS:
        if any(any(hint in key for hint in hints) for key in keys):
            return provider
    return "custom"


def _profile_type_hints(values: dict[str, str]) -> set[str]:
    hints: set[str] = set()
    for key in values:
        if key == "__JSON_CLAUDE_HINT" or key.startswith(("ANTHROPIC_", "CLAUDE_")):
            hints.add("claude")
        if key == "__JSON_CODEX_HINT" or key.startswith(("OPENAI_", "CODEX_")):
            hints.add("codex")
        if key in {"MODEL_PROVIDER", "WIRE_API", "REQUIRES_OPENAI_AUTH"}:
            hints.add("codex")
    return hints


def _codex_env_key(values: dict[str, str], token_key: str, provider_id: str) -> tuple[str, str]:
    """Resolve a safe, credential-shaped env key without leaking JSON aliases."""

    from core.env_validation import validate_codex_env_key

    explicit, explicit_key = _pick_value(
        values,
        ("CODEX_ENV_KEY", "OPENAI_ENV_KEY", "ENV_KEY", "ENVKEY"),
    )
    candidates: list[tuple[str, str]] = []
    if explicit:
        candidates.append((explicit, explicit_key))

    generic_aliases = {
        "APIKEY", "API_KEY", "API_TOKEN", "AUTH_TOKEN", "BEARER_TOKEN", "AUTHORIZATION"
    }
    if token_key and token_key not in generic_aliases:
        candidates.append((token_key, token_key))
    provider_env_key = _PROVIDER_ENV_KEYS.get(provider_id)
    if provider_env_key:
        candidates.append((provider_env_key, ""))
    candidates.append(("OPENAI_API_KEY", ""))

    for candidate, matched_key in candidates:
        try:
            return validate_codex_env_key(candidate), matched_key
        except ValueError:
            continue
    return "OPENAI_API_KEY", ""


def _claude_auth_scheme(token_key: str, provider_id: str) -> str:
    normalized_key = str(token_key or "").upper()
    if normalized_key in {"ANTHROPIC_API_KEY", "CLAUDE_API_KEY"}:
        return "api_key"
    if normalized_key in {
        "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_AUTH_TOKEN", "AUTH_TOKEN",
        "BEARER_TOKEN", "AUTHORIZATION",
    } or normalized_key.endswith("_AUTH_TOKEN"):
        return "auth_token"

    provider_defaults = {
        "anthropic": "api_key",
        "kimi": "api_key",
        "deepseek": "auth_token",
        "minimax": "auth_token",
        "qwen": "auth_token",
        "glm": "auth_token",
        "zai": "auth_token",
    }
    if provider_id in provider_defaults:
        return provider_defaults[provider_id]
    return "api_key" if "API_KEY" in normalized_key or normalized_key.endswith("APIKEY") else "auth_token"


def _vendor_token_keys(values: dict[str, str], generic_keys: set[str]) -> list[str]:
    return [
        key
        for key, value in values.items()
        if key not in generic_keys
        and not re.search(r"(?:^|_)(?:CONFIG|ENV|OPTION|OPTIONS|SETTINGS)(?:_|$)", key)
        and not key.startswith(("PROVIDER_", "PROVIDERS_"))
        and key.endswith(("_API_KEY", "_AUTH_TOKEN"))
        and _clean_value(value)
    ]


def _pick_value(values: dict[str, str], keys: tuple[str, ...]) -> tuple[str, str]:
    for key in keys:
        value = _clean_value(values.get(key))
        if value:
            return value, key
    return "", ""


def _provider_name_from_host(host: str) -> str:
    parts = [part for part in str(host or "").split(".") if part]
    if len(parts) < 2:
        return str(host or "")
    common_second_level_suffixes = {"ac", "co", "com", "edu", "gov", "net", "org"}
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2].casefold() in common_second_level_suffixes:
        return parts[-3]
    return parts[-2]


def parse_api_config_text(text: str, profile_type: str | None = None) -> ParsedAPIConfig:
    """Parse copied environment/config text for a Claude or Codex profile."""

    raw = str(text or "").strip()
    if not raw:
        raise ValueError("剪贴板中没有可解析的 API 配置文本")
    if len(raw) > _MAX_CONFIG_TEXT_CHARS:
        raise ValueError("API 配置文本过大；请只复制包含端点、密钥和模型的配置片段")
    values = _extract_values(raw)
    upper_keys = set(values)
    type_hints = _profile_type_hints(values)
    requested_type = str(profile_type or "").strip().lower()
    if requested_type and requested_type not in {"claude", "codex"}:
        raise ValueError("API 类型只能是 Claude 或 Codex")
    if requested_type:
        if len(type_hints) == 1 and requested_type not in type_hints:
            detected = next(iter(type_hints))
            raise ValueError(
                f"检测到 {detected.title()} API 配置，但当前是 {requested_type.title()} 编辑器；"
                "请在对应类型的新建 API 窗口中粘贴"
            )
        target_type = requested_type
    else:
        if len(type_hints) > 1:
            raise ValueError("同时检测到 Claude 和 Codex 配置；请在对应 API 编辑器中粘贴解析")
        target_type = next(iter(type_hints), "claude")

    url_keys = _CLAUDE_URL_KEYS if target_type == "claude" else _CODEX_URL_KEYS
    explicit_url, explicit_url_key = _pick_value(values, url_keys)
    if not explicit_url:
        explicit_url, explicit_url_key = _pick_value(
            values,
            ("API_BASE_URL", "BASE_URL", "BASEURL", "ENDPOINT", "API_ENDPOINT"),
        )
    urls = _url_candidates(raw)
    if explicit_url:
        normalized_candidate = _normalize_url(
            explicit_url,
            profile_type=target_type,
            inferred=False,
        )
        url_inferred = False
        if not normalized_candidate:
            fallback_candidates = [
                url
                for url in urls
                if not _url_candidate_is_invalid_explicit_fragment(url, explicit_url)
            ]
            normalized_candidate = _normalize_url_candidates(fallback_candidates, target_type)
            url_inferred = bool(normalized_candidate)
    else:
        normalized_candidate = _normalize_url_candidates(urls, target_type)
        url_inferred = True

    token_keys = _CLAUDE_TOKEN_KEYS if target_type == "claude" else _CODEX_TOKEN_KEYS
    token, token_key = _pick_value(values, token_keys)
    generic_token_keys = {
        "APIKEY", "API_KEY", "API_TOKEN", "AUTH_TOKEN", "BEARER_TOKEN", "AUTHORIZATION"
    }
    if token_key in generic_token_keys:
        vendor_keys = _vendor_token_keys(values, generic_token_keys)
        vendor_key = min(vendor_keys, key=lambda key: (key.count("_"), len(key)), default="")
        if vendor_key:
            token, token_key = _clean_value(values[vendor_key]), vendor_key
    if not token:
        # Vendor-specific keys such as DEEPSEEK_API_KEY are valid Codex keys.
        vendor_keys = _vendor_token_keys(values, generic_token_keys)
        token_key = min(vendor_keys, key=lambda key: (key.count("_"), len(key)), default="")
        token = _clean_value(values.get(token_key))
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    model, model_key = _pick_value(values, _MODEL_KEYS)
    provider_id = _provider_for_url(normalized_candidate)
    if provider_id == "custom":
        provider_id = _provider_for_keys(upper_keys)
    provider_hint_name = ""
    if provider_id == "custom":
        provider_hint = _clean_value(values.get("PROVIDER") or values.get("MODEL_PROVIDER"))
        if provider_hint:
            hinted_provider = _provider_id_from_label(provider_hint)
            if hinted_provider != "custom":
                provider_id = hinted_provider
            elif _compact_name(provider_hint) not in {"custom", "default"}:
                provider_hint_name = provider_hint
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
    if not provider_name:
        provider_name = provider_hint_name
    if not provider_name and provider_id in {"custom", "anthropic", "openai"}:
        provider_name = _provider_name_from_host(host)
    name = _clean_value(values.get("PROFILE_NAME") or values.get("API_NAME"))
    if not name:
        name = f"{provider_name or host} {'Claude' if target_type == 'claude' else 'Codex'}"
    auth_scheme = _claude_auth_scheme(token_key, provider_id) if target_type == "claude" else "auth_token"
    env_key, env_key_match = (
        _codex_env_key(values, token_key, provider_id)
        if target_type == "codex"
        else ("", "")
    )
    matched = tuple(
        sorted({key for key in (explicit_url_key, token_key, model_key, env_key_match) if key})
    )
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
