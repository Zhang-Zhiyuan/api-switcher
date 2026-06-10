"""Persistent settings for public-network diagnostics."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from config.paths import STORAGE_DIR
from core import security
from core.atomic_io import atomic_write_text


SETTINGS_FILE = STORAGE_DIR / "network_diagnostics.json"

SERVICE_PING0 = "ping0"
SERVICE_PROXYCHECK = "proxycheck"
SERVICE_IPQS = "ipqs"
SERVICE_VPNAPI = "vpnapi"
SERVICE_ORDER = (SERVICE_PING0, SERVICE_PROXYCHECK, SERVICE_IPQS, SERVICE_VPNAPI)
SERVICE_SET = set(SERVICE_ORDER)
HIDDEN_SERVICES = {SERVICE_IPQS}
VISIBLE_SERVICE_ORDER = tuple(service for service in SERVICE_ORDER if service not in HIDDEN_SERVICES)

SERVICE_ALIASES = {
    "ping0": SERVICE_PING0,
    "ping0.cc": SERVICE_PING0,
    "ping0cc": SERVICE_PING0,
    "proxycheck": SERVICE_PROXYCHECK,
    "proxycheck.io": SERVICE_PROXYCHECK,
    "proxycheckio": SERVICE_PROXYCHECK,
    "ipqs": SERVICE_IPQS,
    "ipqualityscore": SERVICE_IPQS,
    "ipqualityscore.com": SERVICE_IPQS,
    "ipqualityscorecom": SERVICE_IPQS,
    "vpnapi": SERVICE_VPNAPI,
    "vpnapi.io": SERVICE_VPNAPI,
    "vpnapiio": SERVICE_VPNAPI,
}

SERVICE_LABELS = {
    SERVICE_PING0: "Ping0",
    SERVICE_PROXYCHECK: "ProxyCheck",
    SERVICE_IPQS: "IPQualityScore",
    SERVICE_VPNAPI: "VPNAPI.io",
}

DEFAULT_ENABLED = {
    SERVICE_PING0: True,
    SERVICE_PROXYCHECK: True,
    SERVICE_IPQS: False,
    SERVICE_VPNAPI: False,
}

ENV_KEYS = {
    SERVICE_PING0: ("PING0_API_KEY",),
    SERVICE_PROXYCHECK: ("PROXYCHECK_API_KEY",),
    SERVICE_IPQS: ("IPQS_API_KEY", "IPQUALITYSCORE_API_KEY"),
    SERVICE_VPNAPI: ("VPNAPI_KEY", "VPNAPI_API_KEY"),
}

KNOWN_API_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]{6}-){3}[A-Za-z0-9]{6}(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])[A-Za-z0-9]{24,96}(?![A-Za-z0-9])"
)


@dataclass
class DiagnosticServiceSettings:
    enabled: bool = False
    api_keys: list[str] = field(default_factory=list)


@dataclass
class NetworkDiagnosticSettings:
    services: dict[str, DiagnosticServiceSettings] = field(default_factory=dict)

    def enabled_services(self, include_hidden: bool = False) -> list[str]:
        order = SERVICE_ORDER if include_hidden else VISIBLE_SERVICE_ORDER
        return [service for service in order if self.service(service).enabled]

    def keys_for(self, service: str) -> list[str]:
        return list(self.service(service).api_keys)

    def service(self, service: str) -> DiagnosticServiceSettings:
        if service not in self.services:
            self.services[service] = DiagnosticServiceSettings(enabled=DEFAULT_ENABLED.get(service, False))
        return self.services[service]


def load_settings() -> NetworkDiagnosticSettings:
    """Load settings and decrypt saved API key pools."""

    data = _read_settings_file()
    raw_services = data.get("services") if isinstance(data.get("services"), dict) else {}
    settings = NetworkDiagnosticSettings()

    for service in SERVICE_ORDER:
        has_raw_service = isinstance(raw_services.get(service), dict)
        raw = raw_services.get(service) if has_raw_service else {}
        env_keys = _env_keys(service)
        default_enabled = DEFAULT_ENABLED.get(service, False)
        if not has_raw_service and env_keys and service in {SERVICE_IPQS, SERVICE_VPNAPI}:
            default_enabled = True
        enabled = _coerce_bool(raw.get("enabled"), default_enabled)
        key_refs = [str(item) for item in raw.get("key_refs", []) if str(item).strip()]
        keys = [value for ref in key_refs if (value := security.get_secret(ref))]
        if not keys and not has_raw_service:
            keys = env_keys
        settings.services[service] = DiagnosticServiceSettings(
            enabled=enabled,
            api_keys=_dedupe(keys),
        )

    return settings


def save_settings(settings: NetworkDiagnosticSettings) -> None:
    """Persist settings and store API keys in the app-managed secret store."""

    existing_refs = _collect_existing_refs()
    saved_refs: set[str] = set()
    payload: dict[str, Any] = {"version": 1, "services": {}}

    for service in SERVICE_ORDER:
        service_settings = settings.service(service)
        refs: list[str] = []
        for index, key in enumerate(_dedupe(service_settings.api_keys)):
            ref = f"network-diagnostics:{service}:{index}"
            security.set_secret(ref, key)
            refs.append(ref)
            saved_refs.add(ref)
        payload["services"][service] = {
            "enabled": bool(service_settings.enabled),
            "key_refs": refs,
        }

    for ref in existing_refs - saved_refs:
        security.delete_secret(ref)

    atomic_write_text(SETTINGS_FILE, json.dumps(payload, ensure_ascii=False, indent=2))


def settings_from_values(enabled_services: list[str] | set[str], api_keys: dict[str, list[str] | str]) -> NetworkDiagnosticSettings:
    enabled = set(normalize_services(enabled_services))
    settings = NetworkDiagnosticSettings()
    for service in SERVICE_ORDER:
        settings.services[service] = DiagnosticServiceSettings(
            enabled=service in enabled,
            api_keys=parse_api_keys(api_keys.get(service, [])),
        )
    return settings


def parse_api_keys(value: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return parse_api_keys(parsed)
        stripped = "\n".join(_clean_key_line(line) for line in stripped.splitlines())
        known_tokens = _extract_known_api_key_tokens(stripped)
        if known_tokens:
            return _dedupe(known_tokens)
        parts = re.split(r"[\s,;，；、|]+", stripped)
    else:
        parts: list[str] = []
        for item in value:
            parts.extend(parse_api_keys(str(item)))
    return _dedupe(parts)


def normalize_service(value: str) -> str:
    text = str(value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    return SERVICE_ALIASES.get(text, text if text in SERVICE_SET else "")


def normalize_services(values: list[str] | set[str] | tuple[str, ...] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        service = normalize_service(str(value))
        if not service or service in seen:
            continue
        seen.add(service)
        normalized.append(service)
    return normalized


def masked_key_summary(keys: list[str]) -> str:
    if not keys:
        return "未保存 Key"
    return "，".join(_mask_key(key) for key in keys)


def _read_settings_file() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _collect_existing_refs() -> set[str]:
    data = _read_settings_file()
    raw_services = data.get("services") if isinstance(data.get("services"), dict) else {}
    refs: set[str] = set()
    for raw in raw_services.values():
        if not isinstance(raw, dict):
            continue
        refs.update(str(item) for item in raw.get("key_refs", []) if str(item).strip())
    return refs


def _env_keys(service: str) -> list[str]:
    values = []
    for name in ENV_KEYS.get(service, ()):
        values.extend(parse_api_keys(os.environ.get(name, "")))
    return _dedupe(values)


def _dedupe(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_key_token(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return bool(default)


def _clean_key_token(value: str) -> str:
    text = _clean_key_line(str(value or ""))
    if not text:
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'", "`"}:
        text = text[1:-1].strip()
    return text.strip()


def _clean_key_line(value: str) -> str:
    return re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", str(value or "").strip())


def _extract_known_api_key_tokens(value: str) -> list[str]:
    return [match.group(0) for match in KNOWN_API_KEY_RE.finditer(value or "")]


def _mask_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"
