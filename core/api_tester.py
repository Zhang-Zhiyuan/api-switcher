"""API connection and model-list utilities."""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from core.redaction import redact_sensitive_text
from core.url_validation import validate_api_base_url

logger = logging.getLogger(__name__)


class _ResponseTooLargeError(ValueError):
    """Raised when a remote endpoint exceeds a bounded diagnostic response."""


@dataclass
class TestResult:
    """Result of an API connection test."""
    success: bool
    message: str
    response_time: Optional[float] = None
    status_code: Optional[int] = None
    error_details: Optional[str] = None
    selected_model: Optional[str] = None
    recommended_wire_api: Optional[str] = None
    proxy_warning: Optional[str] = None


@dataclass
class ModelInfo:
    """Normalized metadata for a model returned by a provider."""
    id: str
    display_name: str = ""
    created: int = 0


@dataclass
class ModelListResult:
    """Result of a remote model-list request."""
    success: bool
    message: str
    models: list[str] = field(default_factory=list)
    recommended_model: Optional[str] = None
    latest_model: Optional[str] = None
    model_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    response_time: Optional[float] = None
    status_code: Optional[int] = None
    error_details: Optional[str] = None
    proxy_warning: Optional[str] = None


@dataclass(frozen=True)
class InvalidProxySettingsInspection:
    """Credential-free snapshot shown before explicit stale-proxy cleanup."""

    environment_names: tuple[str, ...] = ()
    environment_endpoints: tuple[tuple[str, str, int], ...] = ()
    windows_system_endpoint: tuple[str, int] | None = None
    vscode_fields: tuple[str, ...] = ()
    vscode_endpoints: tuple[tuple[str, str, int], ...] = ()
    reconciliation_message: str = ""
    protected_message: str = ""

    @property
    def has_invalid_settings(self) -> bool:
        return bool(
            self.environment_names
            or self.windows_system_endpoint
            or self.vscode_fields
        )

    def confirmation_details(self) -> str:
        def endpoint_text(host: str, port: int) -> str:
            return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

        details: list[str] = []
        if self.environment_names:
            endpoints = {
                name.casefold(): endpoint_text(host, port)
                for name, host, port in self.environment_endpoints
            }
            labels = [
                f"{name} ({endpoints[name.casefold()]})"
                if name.casefold() in endpoints
                else name
                for name in self.environment_names
            ]
            details.append("Windows/进程环境变量：" + "、".join(labels))
        if self.windows_system_endpoint is not None:
            host, port = self.windows_system_endpoint
            details.append(
                "Windows 当前用户系统代理：" + endpoint_text(host, port)
            )
        if self.vscode_fields:
            endpoints = {
                name: endpoint_text(host, port)
                for name, host, port in self.vscode_endpoints
            }
            labels = [
                f"{name} ({endpoints[name]})" if name in endpoints else name
                for name in self.vscode_fields
            ]
            details.append("VS Code 设置：" + "、".join(labels))
        return "\n".join(details)

    def __str__(self) -> str:
        if self.protected_message:
            return self.protected_message
        pieces = []
        if self.reconciliation_message:
            pieces.append(self.reconciliation_message)
        if self.has_invalid_settings:
            pieces.append(
                "检测到来源无法确认的失效回环代理设置，等待用户确认："
                + self.confirmation_details().replace("\n", "；")
            )
        elif not pieces:
            pieces.append("未发现失效的回环代理环境变量、Windows 系统代理或 VS Code 残留")
        return "；".join(pieces)


@dataclass(frozen=True)
class InvalidProxyCleanupResult:
    """Non-secret summary of one revalidated explicit cleanup."""

    removed_environment_names: tuple[str, ...] = ()
    disabled_windows_endpoint: tuple[str, int] | None = None
    removed_vscode_fields: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(
            self.removed_environment_names
            or self.disabled_windows_endpoint
            or self.removed_vscode_fields
        )

    def __str__(self) -> str:
        actions: list[str] = []
        if self.removed_environment_names:
            actions.append(
                "已清理代理环境变量：" + "、".join(self.removed_environment_names)
            )
        if self.disabled_windows_endpoint is not None:
            host, port = self.disabled_windows_endpoint
            actions.append(f"已关闭失效 Windows 系统代理启用开关：{host}:{port}")
        if self.removed_vscode_fields:
            actions.append("已清理 VS Code 代理残留：" + "、".join(self.removed_vscode_fields))
        if not actions:
            actions.append("代理设置已变化或恢复可用，本次未清理任何项目")
        if self.errors:
            actions.append("部分项目清理失败：" + "；".join(self.errors))
        elif self.changed:
            actions.append("新终端和重开的 VS Code 窗口将读取最新设置")
        return "；".join(actions)


class APITester:
    """Test API connections and refresh model lists."""

    MAX_REQUEST_TIMEOUT = 30
    MAX_BENCHMARK_REPEAT = 5
    MAX_STREAM_EVENTS = 1200
    MAX_JSON_RESPONSE_BYTES = 8 * 1024 * 1024
    MAX_ERROR_RESPONSE_BYTES = 256 * 1024
    MAX_STREAM_LINE_BYTES = 256 * 1024
    MAX_STREAM_RESPONSE_BYTES = 8 * 1024 * 1024
    # Model-list and message/response probes can both incur gateway queueing;
    # keep the high-level UI test from reporting a healthy but slow relay as a
    # false timeout.  Low-level request helpers retain their shorter default.
    DEFAULT_API_TEST_TIMEOUT = 30
    INVALID_LOCAL_PROXY_CACHE_TTL = 15.0
    MAX_PROXY_CHECK_CACHE_ENTRIES = 64
    USER_AGENT = "API-Switcher/2.4.21"
    # Keep both common casings: Windows environment names are case-insensitive,
    # while copied shell variables on Unix often use lowercase names.
    LOCAL_PROXY_ENV_NAMES = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )

    _proxy_check_lock = threading.RLock()
    _proxy_check_cache: dict[tuple[str, str], tuple[float, bool]] = {}

    _SENSITIVE_HEADER_NAMES = {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
    }

    _NON_CHAT_MODEL_MARKERS = (
        "embedding",
        "embed",
        "rerank",
        "moderation",
        "image",
        "dall-e",
        "tts",
        "audio",
        "whisper",
        "transcrib",
        "speech",
        "realtime",
        "preview-image",
    )

    _MODEL_ALIAS_PRIORITY = {
        "opus[1m]": 1_000_000,
        "sonnet[1m]": 900_000,
        "opus": 800_000,
        "opusplan": 750_000,
        "sonnet": 700_000,
        "best": 650_000,
        "default": 50_000,
        "haiku": 100_000,
    }

    @staticmethod
    def _claude_headers(api_key: str, auth_scheme: str = "api_key") -> dict[str, str]:
        """Build the same auth header Claude Code uses for the profile."""
        scheme = str(auth_scheme or "api_key").strip().lower()
        headers = {
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }
        if scheme == "auth_token":
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["x-api-key"] = api_key
        return headers

    @staticmethod
    def _request_header_error(headers: dict[str, str]) -> TestResult | None:
        """Reject malformed header values before ``http.client`` encodes them.

        API keys copied with Chinese instructions, smart punctuation, or a
        trailing newline otherwise surface as a cryptic latin-1 error after the
        network operation starts.  Header control characters are rejected here
        as well so pasted multi-line text cannot become header injection.
        """

        for raw_name, raw_value in dict(headers or {}).items():
            name = str(raw_name)
            value = str(raw_value)
            sensitive = name.casefold() in APITester._SENSITIVE_HEADER_NAMES
            field_label = "API Key/Auth Token" if sensitive else f"请求头 {name}"
            if any(ord(char) < 32 and char != "\t" for char in value) or ord("\x7f") in map(ord, value):
                return TestResult(
                    success=False,
                    message=f"{field_label}格式无效",
                    error_details="检测到换行或控制字符；请只粘贴密钥本身，不要包含 export/set 命令或说明文字。",
                )
            try:
                name.encode("ascii")
                value.encode("latin-1")
            except UnicodeEncodeError:
                return TestResult(
                    success=False,
                    message=f"{field_label}格式无效",
                    error_details="检测到 HTTP 请求头不支持的字符；请去掉中文说明、全角引号或其他非密钥内容。",
                )
        return None

    @staticmethod
    def _read_response_bytes(response, max_bytes: int, label: str) -> bytes:
        """Read an HTTP response with declared-size and actual-size checks."""

        limit = max(1, int(max_bytes))
        headers = getattr(response, "headers", None)
        content_length = None
        if headers is not None:
            content_length = headers.get("Content-Length")
            if content_length is None:
                content_length = headers.get("content-length")
        try:
            declared = int(content_length or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > limit:
            raise _ResponseTooLargeError(f"{label}声明长度 {declared} 字节，超过 {limit} 字节上限")

        payload = response.read(limit + 1)
        if isinstance(payload, str):
            payload = payload.encode("utf-8", errors="replace")
        if len(payload) > limit:
            raise _ResponseTooLargeError(f"{label}超过 {limit} 字节上限")
        return bytes(payload)

    @staticmethod
    def _normalize_base_url(base_url: str, default: str) -> str:
        return validate_api_base_url(base_url, default=default)

    @staticmethod
    def _invalid_base_url_result(exc: ValueError) -> TestResult:
        return TestResult(
            success=False,
            message="API 端点无效",
            error_details=str(exc)[:400],
        )

    @staticmethod
    def _invalid_base_url_model_result(exc: ValueError) -> ModelListResult:
        return ModelListResult(
            success=False,
            message="API 端点无效",
            error_details=str(exc)[:400],
        )

    @staticmethod
    def _openai_url(base_url: str, resource: str) -> str:
        """Build a URL for OpenAI-compatible APIs without double-appending /v1."""
        base_url = APITester._normalize_base_url(base_url, "https://api.openai.com/v1")
        parsed = urllib.parse.urlparse(base_url)
        path = parsed.path.rstrip("/")
        resource = resource.strip("/")

        if path.endswith(("/v1", "/v4")):
            new_path = f"{path}/{resource}"
        elif parsed.netloc.lower() == "api.openai.com":
            new_path = f"{path}/v1/{resource}" if path else f"/v1/{resource}"
        else:
            new_path = f"{path}/{resource}" if path else f"/{resource}"

        return urllib.parse.urlunparse(parsed._replace(path=new_path))

    @staticmethod
    def _anthropic_url(base_url: str, resource: str) -> str:
        """Build a URL for Anthropic-compatible APIs."""
        base_url = APITester._normalize_base_url(base_url, "https://api.anthropic.com")
        parsed = urllib.parse.urlparse(base_url)
        path = parsed.path.rstrip("/")
        resource = resource.strip("/")

        if path.endswith("/v1"):
            new_path = f"{path}/{resource}"
        else:
            new_path = f"{path}/v1/{resource}" if path else f"/v1/{resource}"

        return urllib.parse.urlunparse(parsed._replace(path=new_path))

    @staticmethod
    def _extract_model_ids(data: Any) -> list[str]:
        """Extract model ids from common model-list response shapes."""
        return [model.id for model in APITester._extract_model_infos(data)]

    @staticmethod
    def _extract_model_infos(data: Any) -> list[ModelInfo]:
        """Extract normalized model metadata from common response shapes."""
        if isinstance(data, dict):
            candidates = data.get("data") or data.get("models") or data.get("items")
        else:
            candidates = data

        models: dict[str, ModelInfo] = {}
        if not isinstance(candidates, list):
            return []

        for item in candidates:
            model_id = None
            display_name = ""
            created = 0
            if isinstance(item, str):
                model_id = item
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("name") or item.get("model")
                display_name = str(item.get("display_name") or item.get("displayName") or "")
                created = APITester._parse_model_created(
                    item.get("created")
                    or item.get("created_at")
                    or item.get("createdAt")
                    or item.get("created_time")
                    or item.get("createdTime")
                )
            if model_id:
                model_id = str(model_id).strip()
                if not model_id:
                    continue
                info = ModelInfo(id=model_id, display_name=display_name, created=created)
                existing = models.get(model_id)
                if not existing or APITester._model_info_quality(info) > APITester._model_info_quality(existing):
                    models[model_id] = info

        return sorted(models.values(), key=lambda model: model.id.lower())

    @staticmethod
    def _model_info_quality(model: ModelInfo) -> tuple[int, int]:
        return (1 if model.display_name else 0, model.created)

    @staticmethod
    def _model_metadata_from_infos(infos: list[ModelInfo]) -> dict[str, dict[str, Any]]:
        return {
            info.id: {"display_name": info.display_name, "created": info.created}
            for info in infos
            if info.display_name or info.created
        }

    @staticmethod
    def _parse_model_created(value: Any) -> int:
        if value is None or isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            created = int(value)
            return created // 1000 if created > 10_000_000_000 else max(created, 0)
        if not isinstance(value, str):
            return 0

        text = value.strip()
        if not text:
            return 0
        if text.isdigit():
            if len(text) == 8 and text.startswith("20"):
                try:
                    return int(datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp())
                except ValueError:
                    return 0
            created = int(text)
            return created // 1000 if created > 10_000_000_000 else created

        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())

    @staticmethod
    def recommend_best_model(models: list[str],
                             model_metadata: Optional[dict[str, dict[str, Any]]] = None) -> Optional[str]:
        """Pick the strongest/latest chat model from a provider model list."""
        candidates = [str(model).strip() for model in models if str(model).strip()]
        if not candidates:
            return None
        return max(
            dict.fromkeys(candidates),
            key=lambda model: APITester._model_preference_score(model, model_metadata),
        )

    @staticmethod
    def recommend_latest_model(models: list[str],
                               model_metadata: Optional[dict[str, dict[str, Any]]] = None) -> Optional[str]:
        """Pick the newest chat-capable model from a provider model list."""
        candidates = [str(model).strip() for model in models if str(model).strip()]
        if not candidates:
            return None
        return max(
            dict.fromkeys(candidates),
            key=lambda model: APITester._model_latest_score(model, model_metadata),
        )

    @staticmethod
    def sort_models_by_preference(models: list[str],
                                  model_metadata: Optional[dict[str, dict[str, Any]]] = None) -> list[str]:
        """Return models in recommended order with duplicates removed."""
        unique = list(dict.fromkeys(str(model).strip() for model in models if str(model).strip()))
        return sorted(
            unique,
            key=lambda model: APITester._model_preference_score(model, model_metadata),
            reverse=True,
        )

    @staticmethod
    def _model_preference_score(model: str,
                                model_metadata: Optional[dict[str, dict[str, Any]]] = None) -> tuple[int, int, str]:
        name = model.lower()
        metadata = APITester._metadata_for_model(model, model_metadata)
        display_name = str(metadata.get("display_name") or metadata.get("displayName") or "")
        search_text = f"{name} {display_name.lower()}".strip()
        if any(marker in search_text for marker in APITester._NON_CHAT_MODEL_MARKERS):
            return (-1_000_000, APITester._metadata_created_score(metadata), name)

        if name in APITester._MODEL_ALIAS_PRIORITY:
            return (APITester._MODEL_ALIAS_PRIORITY[name], APITester._metadata_created_score(metadata), name)

        score = 0
        if "[1m]" in search_text or "1m" in search_text:
            score += 70_000

        # Most /models endpoints expose ids rather than capability metadata, so
        # this uses transparent naming heuristics and still leaves manual input.
        if "opus" in search_text:
            score += 600_000
        elif "sonnet" in search_text:
            score += 500_000
        elif "haiku" in search_text:
            score += 100_000
        elif "gpt-" in search_text:
            score += 450_000
        elif name.startswith("o") and len(name) > 1 and name[1].isdigit():
            score += 420_000
        elif "glm" in search_text:
            score += 380_000
        elif "kimi" in search_text or "moonshot" in search_text:
            score += 360_000
        elif "deepseek" in search_text:
            score += 340_000

        if any(token in search_text for token in ("pro", "max", "ultra")):
            score += 30_000
        if any(token in search_text for token in ("thinking", "reasoner", "reasoning")):
            score += 20_000
        if "turbo" in search_text:
            score += 5_000
        if any(token in search_text for token in ("mini", "nano", "flash", "lite", "air")):
            score -= 30_000

        score += APITester._version_score(search_text)
        score += APITester._date_score(search_text)
        return (score, APITester._metadata_created_score(metadata), name)

    @staticmethod
    def _model_latest_score(model: str,
                            model_metadata: Optional[dict[str, dict[str, Any]]] = None) -> tuple[int, int, int, int, str]:
        name = model.lower()
        metadata = APITester._metadata_for_model(model, model_metadata)
        display_name = str(metadata.get("display_name") or metadata.get("displayName") or "")
        search_text = f"{name} {display_name.lower()}".strip()
        if any(marker in search_text for marker in APITester._NON_CHAT_MODEL_MARKERS):
            return (-1_000_000, 0, 0, 0, name)

        family = 0
        if "opus" in search_text:
            family = 60
        elif "sonnet" in search_text:
            family = 55
        elif "gpt-" in search_text:
            family = 50
        elif name.startswith("o") and len(name) > 1 and name[1].isdigit():
            family = 48
        elif "glm" in search_text:
            family = 45
        elif "kimi" in search_text or "moonshot" in search_text:
            family = 43
        elif "deepseek" in search_text:
            family = 41

        size_adjust = 0
        if any(token in search_text for token in ("mini", "nano", "flash", "lite", "air")):
            size_adjust -= 1
        if any(token in search_text for token in ("pro", "max", "ultra", "opus")):
            size_adjust += 1

        return (
            APITester._version_score(search_text),
            APITester._metadata_created_score(metadata),
            APITester._date_score(search_text),
            family + size_adjust,
            name,
        )

    @staticmethod
    def _metadata_for_model(model: str,
                            model_metadata: Optional[dict[str, dict[str, Any]]]) -> dict[str, Any]:
        if not model_metadata:
            return {}
        metadata = model_metadata.get(model)
        if isinstance(metadata, dict):
            return metadata
        model_lower = model.lower()
        for key, value in model_metadata.items():
            if str(key).lower() == model_lower and isinstance(value, dict):
                return value
        return {}

    @staticmethod
    def _metadata_created_score(metadata: dict[str, Any]) -> int:
        created = APITester._parse_model_created(
            metadata.get("created")
            or metadata.get("created_at")
            or metadata.get("createdAt")
        )
        return created // 86_400 if created else 0

    @staticmethod
    def _version_score(name: str) -> int:
        import re

        clean_name = re.sub(r"20\d{2}[-.]?\d{2}[-.]?\d{2}", "", name)
        best = 0
        for match in re.finditer(r"(?<!\d)(\d{1,2}(?:[.-]\d{1,3}){0,3})(?!\d)", clean_name):
            token = match.group(1).replace("-", ".")
            value = 0
            for index, part in enumerate(token.split(".")[:4]):
                if part.isdigit():
                    value += int(part) * (1000 // (10 ** index))
            best = max(best, value)
        return best

    @staticmethod
    def _date_score(name: str) -> int:
        import re

        dates = [int(match.group(0)) for match in re.finditer(r"20\d{6}", name)]
        return (max(dates) - 20_000_000) // 10 if dates else 0

    @staticmethod
    def _sensitive_header_values(headers: dict[str, str]) -> tuple[str, ...]:
        """Return credentials from request headers without retaining other values."""
        values: list[str] = []
        for name, value in (headers or {}).items():
            if str(name).strip().lower() not in APITester._SENSITIVE_HEADER_NAMES:
                continue
            text = str(value or "").strip()
            if not text:
                continue
            values.append(text)
            if " " in text:
                _scheme, token = text.split(None, 1)
                if token:
                    values.append(token)
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _redact_sensitive_text(value: object, secrets: tuple[str, ...] = ()) -> str:
        """Redact credentials that a relay may echo in errors or response bodies."""
        return redact_sensitive_text(value, secrets=secrets, max_length=400)

    @staticmethod
    def _parse_error_body(error_body: str, secrets: tuple[str, ...] = ()) -> str:
        try:
            error_data = json.loads(error_body)
        except Exception:
            return APITester._redact_sensitive_text(error_body, secrets)

        error = error_data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("error_msg") or error.get("detail")
            if message:
                return APITester._redact_sensitive_text(message, secrets)
        if isinstance(error, str):
            return APITester._redact_sensitive_text(error, secrets)

        for key in ("message", "msg", "detail"):
            if error_data.get(key):
                return APITester._redact_sensitive_text(error_data[key], secrets)

        return APITester._redact_sensitive_text(error_body, secrets)

    @staticmethod
    def _coerce_timeout(timeout: object, default: int = 10, maximum: int | None = None) -> int:
        try:
            seconds = int(timeout or default)
        except (TypeError, ValueError):
            seconds = default
        return min(max(seconds, 1), maximum or APITester.MAX_REQUEST_TIMEOUT)

    @staticmethod
    def _coerce_repeat_count(repeat_count: object, default: int = 3) -> int:
        try:
            count = int(repeat_count or default)
        except (TypeError, ValueError):
            count = default
        return min(max(count, 1), APITester.MAX_BENCHMARK_REPEAT)

    @staticmethod
    def _timeout_result(timeout: int) -> TestResult:
        return TestResult(
            success=False,
            message=f"连接超时，超过 {timeout} 秒",
            error_details="请检查网络连接或稍后重试",
        )

    @staticmethod
    def _attach_proxy_warning(result: TestResult, warning: str) -> TestResult:
        """Attach a proxy diagnostic to every result, including failures."""
        if warning:
            result.proxy_warning = warning
        return result

    @staticmethod
    def _local_proxy_endpoint(proxy_url: object) -> tuple[str, int] | None:
        """Return a loopback proxy host/port, or ``None`` for other proxies."""

        text = str(proxy_url or "").strip()
        if not text or text.casefold() in {"direct", "none"}:
            return None
        if "://" not in text:
            text = f"http://{text}"
        try:
            parsed = urllib.parse.urlsplit(text)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError):
            return None
        if not hostname:
            return None
        normalized_host = hostname.casefold().rstrip(".")
        try:
            is_loopback = ipaddress.ip_address(normalized_host).is_loopback
        except ValueError:
            is_loopback = normalized_host == "localhost"
        if not is_loopback:
            return None
        if port is None:
            port = 443 if parsed.scheme.casefold() == "https" else 80
        if not 1 <= port <= 65535:
            return None
        return normalized_host, port

    @classmethod
    def _check_loopback_proxy(
        cls,
        proxy_url: object,
        *,
        force: bool = False,
    ) -> tuple[tuple[str, int] | None, bool]:
        """Return ``(endpoint, available)`` for a loopback proxy URL."""
        endpoint = cls._local_proxy_endpoint(proxy_url)
        if not endpoint:
            return None, False

        cache_key = ("loopback", str(proxy_url or "").strip().casefold())
        now = time.monotonic()
        with cls._proxy_check_lock:
            cached = cls._proxy_check_cache.get(cache_key)
            if not force and cached and now - cached[0] < cls.INVALID_LOCAL_PROXY_CACHE_TTL:
                return endpoint, cached[1]
            host, port = endpoint
            try:
                with socket.create_connection((host, port), timeout=0.25):
                    available = True
            except OSError:
                available = False
            cls._proxy_check_cache[cache_key] = (now, available)
            stale_before = now - cls.INVALID_LOCAL_PROXY_CACHE_TTL
            stale_keys = [
                key
                for key, (checked_at, _result) in cls._proxy_check_cache.items()
                if checked_at < stale_before
            ]
            for key in stale_keys:
                cls._proxy_check_cache.pop(key, None)
            overflow = len(cls._proxy_check_cache) - cls.MAX_PROXY_CHECK_CACHE_ENTRIES
            if overflow > 0:
                oldest = sorted(
                    cls._proxy_check_cache,
                    key=lambda key: cls._proxy_check_cache[key][0],
                )[:overflow]
                for key in oldest:
                    cls._proxy_check_cache.pop(key, None)
            return endpoint, available

    @classmethod
    def _local_proxy_env_values(cls) -> dict[str, str]:
        """Collect process and Windows-user proxy variables without values in logs."""
        names = {name.casefold() for name in cls.LOCAL_PROXY_ENV_NAMES}
        values: dict[str, str] = {}
        for name, value in os.environ.items():
            if name.casefold() in names and str(value or "").strip():
                values[name] = str(value).strip()

        # A stale value may only exist in HKCU\Environment and therefore not
        # be visible to this already-running process.  Read it only on Windows;
        # the helper safely returns None on other platforms.
        if os.name == "nt":
            try:
                from core import persistent_env

                present = {name.casefold() for name in values}
                for canonical in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                    if canonical.casefold() in present:
                        continue
                    value = persistent_env._local_user_env_value_strict(canonical)
                    if value and value.strip():
                        values[canonical] = value.strip()
            except Exception as error:
                logger.debug("Failed to read persistent proxy environment variables: %s", error)
        return values

    @classmethod
    def invalid_local_proxy_env_names(cls, *, force: bool = False) -> tuple[str, ...]:
        """Return proxy variables pointing at refused loopback endpoints."""
        invalid: list[str] = []
        checks: dict[str, tuple[tuple[str, int] | None, bool]] = {}
        for name, value in cls._local_proxy_env_values().items():
            signature = value.casefold()
            if signature not in checks:
                checks[signature] = cls._check_loopback_proxy(value, force=force)
            endpoint, available = checks[signature]
            if endpoint and not available:
                invalid.append(name)
        return tuple(invalid)

    @classmethod
    def invalid_windows_system_proxy_endpoint(
        cls,
        *,
        force: bool = False,
        include_environment_match: bool = False,
    ) -> tuple[str, int] | None:
        """Return a refused WinINET loopback endpoint, if selected.

        Ordinary request diagnostics leave an endpoint that is also present in
        proxy environment variables to the environment ownership path. An
        explicit cleanup preview sets ``include_environment_match`` so the
        confirmation list discloses every setting that may be changed.
        """

        if os.name != "nt":
            return None
        if include_environment_match:
            # ``urllib.getproxies`` gives environment variables precedence on
            # Windows, so it cannot accurately preview WinINET while a stale
            # environment proxy also exists. Read the enabled, simple
            # registry endpoint through the local-proxy registry helpers.
            try:
                from core import local_proxy

                enabled_exists, enabled_value, _enabled_type = (
                    local_proxy._read_windows_system_proxy_value("ProxyEnable")
                )
                server_exists, server_value, _server_type = (
                    local_proxy._read_windows_system_proxy_value("ProxyServer")
                )
                enabled = int(enabled_value or 0)
            except (OSError, TypeError, ValueError, OverflowError):
                return None
            raw_server = str(server_value or "").strip()
            if (
                not enabled_exists
                or enabled != 1
                or not server_exists
                or not raw_server
                or ";" in raw_server
                or "=" in raw_server
            ):
                return None
            proxy_url = raw_server
        else:
            try:
                proxies = urllib.request.getproxies()
            except (OSError, ValueError):
                return None
            proxy_url = str(
                proxies.get("https")
                or proxies.get("http")
                or proxies.get("all")
                or ""
            ).strip()
        endpoint, available = cls._check_loopback_proxy(proxy_url, force=force)
        if not endpoint or available:
            return None
        # Environment proxies have a separate cleanup path which can restore
        # the application's full checkpoint. This method is only for a
        # WinINET-selected endpoint with no matching environment value.
        if not include_environment_match and any(
            cls._local_proxy_endpoint(value) == endpoint
            for value in cls._local_proxy_env_values().values()
        ):
            return None
        return endpoint

    @classmethod
    def disable_invalid_windows_system_proxy(
        cls,
        *,
        expected_endpoint: tuple[str, int] | None = None,
    ) -> tuple[str, int] | None:
        """Disable only a user-confirmed, refused, simple WinINET loopback proxy.

        ``ProxyServer`` is preserved so another proxy application can re-enable
        it later. PAC, auto-detect and non-loopback/per-protocol proxy shapes
        are outside this narrow repair and are never changed here.
        """

        if os.name != "nt":
            return None
        from core import local_proxy

        with local_proxy._local_proxy_operation_lock("关闭失效 Windows 系统代理"):
            endpoint = cls.invalid_windows_system_proxy_endpoint(
                force=True,
                include_environment_match=True,
            )
            if endpoint is None:
                return None
            if expected_endpoint is not None and endpoint != expected_endpoint:
                return None
            enabled_exists, enabled_value, _enabled_type = (
                local_proxy._read_windows_system_proxy_value("ProxyEnable")
            )
            server_exists, server_value, _server_type = (
                local_proxy._read_windows_system_proxy_value("ProxyServer")
            )
            try:
                enabled = int(enabled_value or 0)
            except (TypeError, ValueError, OverflowError):
                return None
            if (
                not enabled_exists
                or enabled != 1
                or not server_exists
                or cls._local_proxy_endpoint(server_value) != endpoint
            ):
                return None

            import winreg

            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                local_proxy.WINDOWS_SYSTEM_PROXY_REG_PATH,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
            local_proxy._notify_windows_proxy_change()
            with cls._proxy_check_lock:
                cls._proxy_check_cache.clear()
            return endpoint

    @classmethod
    def _vscode_proxy_candidates(cls, settings: dict) -> dict[str, str]:
        candidates: dict[str, str] = {}
        proxy_url = str((settings or {}).get("http.proxy") or "").strip()
        if proxy_url:
            candidates["http.proxy"] = proxy_url

        terminal_key = "terminal.integrated.env.windows"
        terminal_env = (settings or {}).get(terminal_key)
        if not isinstance(terminal_env, dict):
            return candidates
        accepted_names = {
            *(name.casefold() for name in cls.LOCAL_PROXY_ENV_NAMES),
            "api_switcher_ai_proxy_url",
        }
        for name, value in terminal_env.items():
            clean_name = str(name or "").strip()
            clean_value = str(value or "").strip()
            if clean_name.casefold() in accepted_names and clean_value:
                candidates[f"{terminal_key}.{clean_name}"] = clean_value
        return candidates

    @classmethod
    def invalid_vscode_local_proxy_fields(
        cls,
        *,
        force: bool = False,
    ) -> tuple[str, ...]:
        """Return VS Code fields that still point to refused loopback ports."""

        if os.name != "nt":
            return ()
        try:
            from core import vscode_parser

            candidates = cls._vscode_proxy_candidates(
                vscode_parser.read_vscode_settings()
            )
        except Exception as error:
            logger.debug("Failed to inspect VS Code proxy settings: %s", error)
            return ()

        checks: dict[str, tuple[tuple[str, int] | None, bool]] = {}
        invalid = []
        for field_name, value in candidates.items():
            signature = value.casefold()
            if signature not in checks:
                checks[signature] = cls._check_loopback_proxy(value, force=force)
            endpoint, available = checks[signature]
            if endpoint and not available:
                invalid.append(field_name)
        return tuple(invalid)

    @classmethod
    def clear_invalid_vscode_local_proxy_fields(
        cls,
        fields: Iterable[str] | None = None,
        *,
        expected_endpoints: Iterable[tuple[str, str, int]] | None = None,
    ) -> tuple[str, ...]:
        """Remove only revalidated refused loopback values from VS Code."""

        if os.name != "nt":
            return ()
        from core import local_proxy, vscode_parser

        with local_proxy._local_proxy_operation_lock("清理 VS Code 失效代理设置"):
            settings = vscode_parser.read_vscode_settings()
            candidates = cls._vscode_proxy_candidates(settings)
            requested = set(candidates) if fields is None else {
                str(field or "").strip() for field in fields if str(field or "").strip()
            }
            endpoint_expectations_provided = expected_endpoints is not None
            expected_by_field = {
                str(field or "").strip(): (str(host or "").strip().casefold(), int(port))
                for field, host, port in (expected_endpoints or ())
                if str(field or "").strip()
            }
            removable = []
            checks: dict[str, tuple[tuple[str, int] | None, bool]] = {}
            for field_name, value in candidates.items():
                if field_name not in requested:
                    continue
                signature = value.casefold()
                if signature not in checks:
                    checks[signature] = cls._check_loopback_proxy(value, force=True)
                endpoint, available = checks[signature]
                expected_endpoint = expected_by_field.get(field_name)
                if (
                    endpoint
                    and not available
                    and (
                        not endpoint_expectations_provided
                        or endpoint == expected_endpoint
                    )
                ):
                    removable.append(field_name)
            if not removable:
                return ()

            updated = dict(settings or {})
            terminal_key = "terminal.integrated.env.windows"
            terminal_env = updated.get(terminal_key)
            terminal_env = dict(terminal_env) if isinstance(terminal_env, dict) else {}
            changed = False
            for field_name in removable:
                if field_name == "http.proxy":
                    if "http.proxy" in updated:
                        updated.pop("http.proxy", None)
                        changed = True
                    continue
                prefix = terminal_key + "."
                if field_name.startswith(prefix):
                    env_name = field_name[len(prefix):]
                    if env_name in terminal_env:
                        terminal_env.pop(env_name, None)
                        changed = True
            if terminal_env:
                updated[terminal_key] = terminal_env
            else:
                updated.pop(terminal_key, None)
            if changed:
                vscode_parser.write_vscode_settings(updated)
            with cls._proxy_check_lock:
                cls._proxy_check_cache.clear()
            return tuple(removable) if changed else ()

    @classmethod
    def inspect_invalid_proxy_settings_for_cleanup(
        cls,
    ) -> InvalidProxySettingsInspection:
        """Reconcile owned residue, then return explicit-cleanup candidates."""

        if os.name != "nt":
            return InvalidProxySettingsInspection(
                protected_message="显式脏代理清理目前仅支持 Windows"
            )
        from core import local_proxy

        reconciliation_messages: list[str] = []
        state = local_proxy._load_state()
        state_endpoint = cls._local_proxy_endpoint(state.get("proxy_url"))
        if state_endpoint and cls._program_owns_local_proxy_endpoint(
            state_endpoint,
            state=state,
        ):
            _endpoint, available = cls._check_loopback_proxy(
                state.get("proxy_url"),
                force=True,
            )
            if not available:
                reconciliation_message = (
                    local_proxy.reconcile_local_ai_proxy_startup_settings()
                )
                if any(
                    marker in reconciliation_message
                    for marker in (
                        "未自动改动",
                        "自动恢复本机设置未完成",
                    )
                ):
                    return InvalidProxySettingsInspection(
                        reconciliation_message=reconciliation_message,
                        protected_message=(
                            reconciliation_message
                            + "；为避免打断正在启动/切换的代理，本次未扫描或清理其他设置"
                        ),
                    )
                if reconciliation_message:
                    reconciliation_messages.append(reconciliation_message)

        # A missing/damaged state file can still leave enough independent
        # proof of ownership: the app-only environment marker plus the managed
        # mihomo config marker. Reuse the request-time cleanup authority so
        # these proven app residues do not require a confirmation dialog.
        environment_values = cls._local_proxy_env_values()
        initial_invalid_names = cls.invalid_local_proxy_env_names(force=True)
        checked_endpoints: set[tuple[str, int]] = set()
        values_by_name = {
            name.casefold(): value for name, value in environment_values.items()
        }
        for name in initial_invalid_names:
            proxy_url = values_by_name.get(name.casefold(), "")
            endpoint = cls._local_proxy_endpoint(proxy_url)
            if endpoint is None or endpoint in checked_endpoints:
                continue
            checked_endpoints.add(endpoint)
            owned_names = cls._program_owned_invalid_local_proxy_env_names(
                initial_invalid_names,
                environment_values,
                endpoint,
            )
            if not owned_names:
                continue
            try:
                removed, cleanup_state = cls._auto_clear_program_owned_invalid_proxy(
                    proxy_url,
                    endpoint,
                )
            except Exception as error:
                return InvalidProxySettingsInspection(
                    protected_message=(
                        "检测到本程序写入的失效代理，但自动清理失败："
                        f"{error}；为避免部分清理，本次未继续处理其他设置"
                    )
                )
            if cleanup_state in {"busy", "transitioning"}:
                return InvalidProxySettingsInspection(
                    protected_message=(
                        "本机代理正在启动或切换保护期，未清理任何设置；请稍后重试"
                    )
                )
            if cleanup_state == "restore_failed":
                return InvalidProxySettingsInspection(
                    protected_message=(
                        "检测到本程序写入的失效代理，但自动恢复 Windows 环境变量、"
                        "VS Code 或系统代理未完成；已保留恢复记录，请稍后重试"
                    )
                )
            if cleanup_state == "restored":
                reconciliation_messages.append(
                    "已自动恢复本程序启动前保存的 Windows 环境变量、VS Code 和系统代理设置"
                )
            elif removed:
                reconciliation_messages.append(
                    "已自动清理本程序写入的失效代理变量：" + "、".join(removed)
                )

        environment_names = cls.invalid_local_proxy_env_names(force=True)
        environment_values = cls._local_proxy_env_values()
        environment_endpoints = tuple(
            (name, endpoint[0], endpoint[1])
            for name in environment_names
            if (
                endpoint := cls._local_proxy_endpoint(
                    next(
                        (
                            value
                            for current_name, value in environment_values.items()
                            if current_name.casefold() == name.casefold()
                        ),
                        "",
                    )
                )
            )
        )
        vscode_fields = cls.invalid_vscode_local_proxy_fields(force=True)
        try:
            from core import vscode_parser

            vscode_values = cls._vscode_proxy_candidates(
                vscode_parser.read_vscode_settings()
            )
        except Exception:
            vscode_values = {}
        vscode_endpoints = tuple(
            (field, endpoint[0], endpoint[1])
            for field in vscode_fields
            if (endpoint := cls._local_proxy_endpoint(vscode_values.get(field)))
        )

        return InvalidProxySettingsInspection(
            environment_names=environment_names,
            environment_endpoints=environment_endpoints,
            windows_system_endpoint=cls.invalid_windows_system_proxy_endpoint(
                force=True,
                include_environment_match=True,
            ),
            vscode_fields=vscode_fields,
            vscode_endpoints=vscode_endpoints,
            reconciliation_message="；".join(dict.fromkeys(reconciliation_messages)),
        )

    @classmethod
    def clear_invalid_proxy_settings(
        cls,
        inspection: InvalidProxySettingsInspection,
    ) -> InvalidProxyCleanupResult:
        """Revalidate and clear exactly the user-confirmed stale settings."""

        if not isinstance(inspection, InvalidProxySettingsInspection):
            raise TypeError("失效代理清理清单无效")
        if inspection.protected_message:
            return InvalidProxyCleanupResult(errors=(inspection.protected_message,))
        from core import local_proxy

        removed_environment_names: tuple[str, ...] = ()
        disabled_windows_endpoint: tuple[str, int] | None = None
        removed_vscode_fields: tuple[str, ...] = ()
        errors: list[str] = []
        with local_proxy._local_proxy_operation_lock("显式清理失效代理设置"):
            if inspection.environment_names:
                try:
                    removed_environment_names = cls.clear_invalid_local_proxy_env(
                        inspection.environment_names,
                        expected_endpoints=inspection.environment_endpoints,
                    )
                except Exception as error:
                    errors.append(f"环境变量: {error}")
            if inspection.windows_system_endpoint is not None:
                try:
                    disabled_windows_endpoint = (
                        cls.disable_invalid_windows_system_proxy(
                            expected_endpoint=inspection.windows_system_endpoint,
                        )
                    )
                except Exception as error:
                    errors.append(f"Windows 系统代理: {error}")
            if inspection.vscode_fields:
                try:
                    removed_vscode_fields = (
                        cls.clear_invalid_vscode_local_proxy_fields(
                            inspection.vscode_fields,
                            expected_endpoints=inspection.vscode_endpoints,
                        )
                    )
                except Exception as error:
                    errors.append(f"VS Code: {error}")
        return InvalidProxyCleanupResult(
            removed_environment_names=removed_environment_names,
            disabled_windows_endpoint=disabled_windows_endpoint,
            removed_vscode_fields=removed_vscode_fields,
            errors=tuple(errors),
        )

    @classmethod
    def _program_owned_invalid_local_proxy_env_names(
        cls,
        invalid_names: Iterable[str],
        values: dict[str, str],
        endpoint: tuple[str, int],
    ) -> tuple[str, ...]:
        """Return invalid proxy variables proven to be written by this app.

        The local AI proxy keeps a restore checkpoint and, in newer state
        files, an explicit ownership marker.  Requiring that state plus an
        exact endpoint match lets us auto-clean only the proxy this program
        configured; an unrelated loopback proxy still requires confirmation.
        """
        if os.name != "nt":
            return ()

        try:
            from core import local_proxy

            state = local_proxy._load_state()
        except Exception as error:
            logger.debug("Failed to inspect managed local proxy state: %s", error)
            return ()

        if not isinstance(state, dict):
            return ()

        marker = state.get("managed_proxy_env")
        marker_owned = (
            isinstance(marker, dict)
            and str(marker.get("owner") or "").strip().casefold() == "api-switcher"
        )
        legacy_owned = isinstance(state.get("previous_env"), dict) and bool(
            str(state.get("proxy_url") or "").strip()
        )
        fallback_owned = False
        fallback_url = ""
        if not marker_owned and not legacy_owned:
            # A damaged/old state file can still be proven app-owned by the
            # app-specific environment marker plus the managed config marker.
            fallback_url = str(os.environ.get("API_SWITCHER_AI_PROXY_URL") or "").strip()
            fallback_endpoint = cls._local_proxy_endpoint(fallback_url)
            if fallback_endpoint:
                try:
                    config_path = Path(local_proxy.LOCAL_PROXY_CONFIG_DIR) / "config.yaml"
                    config_text = config_path.read_text(encoding="utf-8", errors="replace")
                    fallback_owned = local_proxy.remote_proxy.AI_PROXY_CONFIG_MARKER in config_text
                except OSError:
                    fallback_owned = False
            if not fallback_owned:
                return ()

        managed_url = ""
        if marker_owned:
            managed_url = str(marker.get("proxy_url") or "").strip()
        managed_url = managed_url or str(state.get("proxy_url") or "").strip() or fallback_url
        managed_endpoint = cls._local_proxy_endpoint(managed_url)
        if managed_endpoint != endpoint:
            return ()

        if marker_owned:
            raw_variables = marker.get("variables")
            if isinstance(raw_variables, (list, tuple, set)):
                managed_names = {str(name).casefold() for name in raw_variables}
            else:
                managed_names = set()
        else:
            # State written before the explicit marker was introduced still
            # has the complete restore checkpoint and exact proxy URL.
            managed_names = {name.casefold() for name in cls.LOCAL_PROXY_ENV_NAMES}

        owned: list[str] = []
        for name in invalid_names:
            normalized_name = str(name or "").strip()
            if not normalized_name or normalized_name.casefold() not in managed_names:
                continue
            if cls._local_proxy_endpoint(values.get(name)) != managed_endpoint:
                continue
            owned.append(normalized_name)
        return tuple(dict.fromkeys(owned))

    @classmethod
    def _program_owns_local_proxy_endpoint(
        cls,
        endpoint: tuple[str, int],
        *,
        state: dict | None = None,
    ) -> bool:
        """Return whether the full current endpoint checkpoint is app-owned."""

        if os.name != "nt":
            return False
        try:
            from core import local_proxy

            current_state = local_proxy._load_state() if state is None else state
            if not isinstance(current_state, dict):
                return False
            port = int(current_state.get("mixed_port") or 0)
            return bool(
                port > 0
                and cls._local_proxy_endpoint(current_state.get("proxy_url")) == endpoint
                and local_proxy._state_owns_local_proxy_settings(current_state, port)
            )
        except (OSError, RuntimeError, TypeError, ValueError, OverflowError):
            return False

    @classmethod
    def _clear_program_owned_proxy_marker(
        cls,
        endpoint: tuple[str, int],
    ) -> tuple[str, ...]:
        """Clear the app-only endpoint marker after ownership is proven."""

        marker_name = "API_SWITCHER_AI_PROXY_URL"
        removed: list[str] = []
        if cls._local_proxy_endpoint(os.environ.get(marker_name)) == endpoint:
            os.environ.pop(marker_name, None)
            removed.append(marker_name)
        if os.name == "nt":
            from core import persistent_env

            try:
                persistent_value = persistent_env._local_user_env_value_strict(
                    marker_name
                )
                if cls._local_proxy_endpoint(persistent_value) == endpoint:
                    persistent_env.delete_local_user_env((marker_name,))
                    if marker_name not in removed:
                        removed.append(marker_name)
            except Exception as error:
                logger.debug("Failed to clear the managed proxy marker: %s", error)
        return tuple(removed)

    @classmethod
    def clear_invalid_local_proxy_env(
        cls,
        names: Iterable[str] | None = None,
        *,
        expected_endpoints: Iterable[tuple[str, str, int]] | None = None,
    ) -> tuple[str, ...]:
        """Remove only currently invalid loopback proxy variables.

        The operation is intentionally narrow: non-loopback proxies, working
        loopback services, ``NO_PROXY``, system-wide settings, and unrelated
        environment variables are left untouched.
        """
        from core import local_proxy

        # A stale warning/button must not delete an environment variable while
        # this app is restarting or hot-updating its managed proxy. Revalidate
        # after acquiring the same cross-process lifecycle lock used by those
        # operations, bypassing the short availability cache.
        with local_proxy._local_proxy_operation_lock("清理失效代理环境变量"):
            invalid_names = cls.invalid_local_proxy_env_names(force=True)
            if names is None:
                names_to_clear = invalid_names
            else:
                invalid_by_fold = {name.casefold(): name for name in invalid_names}
                names_to_clear = tuple(
                    dict.fromkeys(
                        invalid_by_fold.get(str(name or "").strip().casefold())
                        for name in names
                        if invalid_by_fold.get(str(name or "").strip().casefold())
                    )
                )
            endpoint_expectations_provided = expected_endpoints is not None
            expected_by_name = {
                str(name or "").strip().casefold(): (
                    str(host or "").strip().casefold(),
                    int(port),
                )
                for name, host, port in (expected_endpoints or ())
                if str(name or "").strip()
            }
            if endpoint_expectations_provided:
                current_values = cls._local_proxy_env_values()
                current_by_name = {
                    name.casefold(): value for name, value in current_values.items()
                }
                names_to_clear = tuple(
                    name
                    for name in names_to_clear
                    if cls._local_proxy_endpoint(
                        current_by_name.get(name.casefold())
                    )
                    == expected_by_name.get(name.casefold())
                )
            if not names_to_clear:
                return ()

            if os.name == "nt":
                from core import persistent_env

                # The current process may still contain the app's old value
                # after the user changed HKCU\Environment elsewhere. Delete a
                # persistent value only when that registry value is itself a
                # currently refused loopback proxy; never erase the newer user
                # value merely because this process inherited an older one.
                persistent_names = []
                current_values = cls._local_proxy_env_values()
                for name in names_to_clear:
                    current_value = next(
                        (
                            value
                            for current_name, value in current_values.items()
                            if current_name.casefold() == name.casefold()
                        ),
                        "",
                    )
                    current_endpoint = cls._local_proxy_endpoint(current_value)
                    persistent_value = persistent_env._local_user_env_value_strict(name)
                    _persistent_endpoint, persistent_available = cls._check_loopback_proxy(
                        persistent_value,
                        force=True,
                    )
                    if (
                        _persistent_endpoint
                        and _persistent_endpoint == current_endpoint
                        and not persistent_available
                    ):
                        persistent_names.append(name)
                if persistent_names:
                    persistent_env.delete_local_user_env(persistent_names)
            # Keep the process view deterministic even when a platform adapter
            # only implements the registry part of persistent deletion.
            for name in names_to_clear:
                os.environ.pop(name, None)

            with cls._proxy_check_lock:
                cls._proxy_check_cache.clear()
            return names_to_clear

    @classmethod
    def _auto_clear_program_owned_invalid_proxy(
        cls,
        proxy_url: str,
        endpoint: tuple[str, int],
    ) -> tuple[tuple[str, ...], str]:
        """Auto-clean only after excluding a live or transitioning managed proxy."""

        from core import local_proxy

        entered_lock = False
        try:
            with local_proxy._local_proxy_operation_lock(
                "自动清理失效代理环境变量",
                timeout=0.0,
            ):
                entered_lock = True
                fresh_endpoint, fresh_available = cls._check_loopback_proxy(
                    proxy_url,
                    force=True,
                )
                if fresh_endpoint != endpoint:
                    return (), "changed"
                if fresh_available:
                    return (), "recovered"

                fresh_values = cls._local_proxy_env_values()
                fresh_invalid_names = cls.invalid_local_proxy_env_names(force=True)
                fresh_owned_names = cls._program_owned_invalid_local_proxy_env_names(
                    fresh_invalid_names,
                    fresh_values,
                    endpoint,
                )
                state = local_proxy._load_state()
                owns_endpoint = cls._program_owns_local_proxy_endpoint(
                    endpoint,
                    state=state,
                )
                if not fresh_owned_names and not owns_endpoint:
                    return (), "unowned"
                managed_running = local_proxy._managed_local_proxy_is_running(state)
                try:
                    state_pid = int(state.get("pid") or 0)
                except (TypeError, ValueError):
                    state_pid = 0
                if state_pid and not managed_running:
                    # The PID file itself may be momentarily unavailable or
                    # damaged. A state PID is not enough authority to stop a
                    # process, but a verified managed image is enough reason
                    # to defer destructive environment cleanup.
                    managed_running = bool(
                        local_proxy._is_pid_running(state_pid)
                        and local_proxy._is_managed_mihomo_pid(state_pid, state=state)
                    )
                recent_transition = bool(
                    (state.get("installing") or state.get("pid"))
                    and local_proxy._local_proxy_state_update_is_recent(state)
                )
                if managed_running or recent_transition:
                    return (), "transitioning"

                # A dead proxy written by this application owns more than the
                # environment variables: it also changed VS Code and WinINET.
                # Restore the complete checkpoint while the lifecycle lock is
                # still held.  This prevents a half-cleaned state where the
                # current request succeeds directly but the next Windows app
                # is sent back to the same refused system proxy.
                restore_message = local_proxy.reconcile_local_ai_proxy_startup_settings()
                if "已自动恢复" in restore_message:
                    return fresh_owned_names, "restored"
                if "自动恢复本机设置未完成" in restore_message:
                    return (), "restore_failed"
                if fresh_owned_names:
                    removed = cls.clear_invalid_local_proxy_env(fresh_owned_names)
                    marker_removed = cls._clear_program_owned_proxy_marker(endpoint)
                    return tuple(dict.fromkeys((*removed, *marker_removed))), ""
                # Ownership was proven from the full checkpoint, so an empty
                # reconciliation result means the state changed underneath us
                # or could not be safely restored. Never downgrade that to an
                # unowned automatic system-proxy mutation.
                return (), "restore_failed"
        except RuntimeError:
            if not entered_lock:
                return (), "busy"
            raise

    @classmethod
    def _invalid_local_proxy_warning(
        cls,
        url: str,
        *,
        request_action: str = "本次请求已临时直连",
    ) -> str:
        """Detect a refused loopback proxy and safely reconcile app-owned residue."""

        action = str(request_action or "本次请求已临时直连").strip()

        try:
            parsed_url = urllib.parse.urlsplit(str(url or ""))
            scheme = parsed_url.scheme.casefold() or "http"
            proxies = urllib.request.getproxies()
        except (TypeError, ValueError, OSError):
            return ""

        proxy_url = str(proxies.get(scheme) or proxies.get("all") or "").strip()
        endpoint, available = cls._check_loopback_proxy(proxy_url)
        if not endpoint:
            return ""
        host, port = endpoint
        if available:
            return ""

        values = cls._local_proxy_env_values()
        selected_from_environment = any(
            cls._local_proxy_endpoint(value) == endpoint
            for value in values.values()
        )

        invalid_names = cls.invalid_local_proxy_env_names()
        owned_names = cls._program_owned_invalid_local_proxy_env_names(
            invalid_names,
            values,
            endpoint,
        )
        owns_endpoint = cls._program_owns_local_proxy_endpoint(endpoint)
        if owned_names or owns_endpoint:
            try:
                removed, cleanup_state = cls._auto_clear_program_owned_invalid_proxy(
                    proxy_url,
                    endpoint,
                )
            except Exception as error:
                logger.warning("Failed to auto-clean managed invalid proxy variables: %s", error)
                managed_detail = (
                    f"变量（{ '、'.join(owned_names) }）"
                    if owned_names
                    else "Windows 系统代理设置"
                )
                return (
                    f"检测到本程序配置的失效本机代理 {host}:{port}，{action}；"
                    f"自动清理失败，请手动检查{managed_detail}"
                )
            if cleanup_state == "recovered":
                return ""
            if cleanup_state in {"busy", "transitioning"}:
                return (
                    f"检测到本程序的本机代理 {host}:{port} 暂时不可用，"
                    f"代理正在启动或切换保护期，未清理环境变量；{action}"
                )
            if cleanup_state == "restore_failed":
                return (
                    f"检测到本程序配置的失效本机代理 {host}:{port}，{action}；"
                    "自动恢复 Windows 环境变量、VS Code 或系统代理未完成，"
                    "已保留恢复记录供下次启动重试"
                )
            if cleanup_state == "restored":
                return (
                    f"检测到本程序配置的失效本机代理 {host}:{port}，"
                    "已自动恢复本程序启动前保存的 Windows 环境变量、"
                    f"VS Code 和系统代理设置；{action}"
                )
            if removed:
                remaining = tuple(
                    name for name in invalid_names if name.casefold() not in {item.casefold() for item in removed}
                )
                suffix = f"；另有来源不明变量待确认（{ '、'.join(remaining) }）" if remaining else ""
                return (
                    f"检测到本程序配置的失效本机代理 {host}:{port}，已自动清理变量"
                    f"（{ '、'.join(removed) }）；{action}{suffix}"
                )
        variable_detail = f"（{'、'.join(invalid_names)}）" if invalid_names else ""
        source = "本机代理" if selected_from_environment else "Windows 系统代理"
        untouched = "未修改环境变量" if selected_from_environment else "未修改该系统代理设置"
        return (
            f"检测到来源无法确认且失效的 {source} {host}:{port}{variable_detail}，"
            f"{untouched}；{action}"
        )

    @classmethod
    def reconcile_invalid_local_proxy_for_request(
        cls,
        url: str,
        *,
        request_action: str = "本次请求已临时直连",
    ) -> str:
        """Safely reconcile an invalid loopback proxy before a network request.

        This public entry point is shared by API probes and subscription
        downloads. Automatic deletion remains restricted to variables proven
        to be owned by this application; unrelated proxies are only reported.
        """

        action = str(request_action or "本次请求已临时直连").strip()
        if action == "本次请求已临时直连":
            # Preserve the original two-argument monkeypatch/extension
            # contract for ordinary API requests.
            return cls._invalid_local_proxy_warning(url)
        return cls._invalid_local_proxy_warning(url, request_action=action)

    @classmethod
    def _urlopen(
        cls,
        request: urllib.request.Request,
        timeout: int,
        *,
        known_proxy_warning: str = "",
    ):
        """Open a request, bypassing only a refused loopback proxy."""

        # High-level request helpers precompute this diagnostic so it survives
        # even when the direct request itself raises. Preserve that decision:
        # auto-cleaning the process env between two checks must not make this
        # same request fall back to a still-stale WinINET proxy.
        warning = str(
            known_proxy_warning or ""
        ) or cls.reconcile_invalid_local_proxy_for_request(request.full_url)
        if warning:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            return opener.open(request, timeout=timeout), warning
        return urllib.request.urlopen(request, timeout=timeout), ""

    @staticmethod
    def _is_timeout_error(error: object) -> bool:
        if isinstance(error, (TimeoutError, socket.timeout)):
            return True
        text = f"{type(error).__name__}: {error}".lower()
        return "timed out" in text or "timeout" in text

    @staticmethod
    def _is_stream_disconnect_error(error: object) -> bool:
        if isinstance(
            error,
            (
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                ConnectionAbortedError,
                BrokenPipeError,
                ssl.SSLError,
            ),
        ):
            return True
        text = f"{type(error).__name__}: {error}".lower()
        return any(
            marker in text
            for marker in (
                "incomplete read",
                "connection reset",
                "connection aborted",
                "broken pipe",
                "remote end closed",
                "server disconnected",
                "stream disconnected",
            )
        )

    @staticmethod
    def _is_network_transport_error(error: object) -> bool:
        return isinstance(
            error,
            (
                ConnectionError,
                OSError,
                ssl.SSLError,
                http.client.HTTPException,
            ),
        )

    @staticmethod
    def _network_error_result(
        error: object,
        message: str = "网络错误，无法连接到服务器",
        secrets: tuple[str, ...] = (),
    ) -> TestResult:
        return TestResult(
            success=False,
            message=message,
            error_details=APITester._redact_sensitive_text(error, secrets),
        )

    @staticmethod
    def _http_error_message(code: int, model_hint: bool = False) -> str:
        if code in (401, 403):
            return "认证失败或权限不足"
        if code == 404:
            return "端点不存在，检查 Base URL" + (" 或模型名称" if model_hint else "")
        if code == 429:
            return "速率限制，请稍后重试"
        if code >= 500:
            return f"服务器错误: HTTP {code}"
        return f"HTTP 错误: {code}"

    @staticmethod
    def _request_json(
        url: str,
        headers: dict[str, str],
        method: str = "GET",
        payload: Optional[dict[str, Any]] = None,
        timeout: int = 10,
    ) -> tuple[bool, Optional[Any], TestResult]:
        timeout = APITester._coerce_timeout(timeout)
        start_time = time.time()
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = dict(headers or {})
        request_headers.setdefault("User-Agent", APITester.USER_AGENT)
        header_error = APITester._request_header_error(request_headers)
        if header_error is not None:
            return False, None, header_error
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        request_secrets = APITester._sensitive_header_values(request_headers)

        # Compute before opening so the diagnostic is retained even when the
        # direct retry itself fails (``opener.open`` raises before returning).
        proxy_warning = APITester.reconcile_invalid_local_proxy_for_request(url)
        try:
            response, detected_proxy_warning = APITester._urlopen(
                req,
                timeout,
                known_proxy_warning=proxy_warning,
            )
            proxy_warning = detected_proxy_warning or proxy_warning
            with response:
                response_time = (time.time() - start_time) * 1000
                body = APITester._read_response_bytes(
                    response,
                    APITester.MAX_JSON_RESPONSE_BYTES,
                    "API 响应",
                ).decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    content_type = response.headers.get("Content-Type", "") or response.headers.get("content-type", "")
                    details = f"Content-Type: {content_type or 'unknown'}"
                    snippet = APITester._redact_sensitive_text(body.strip(), request_secrets)
                    if snippet:
                        details = f"{details}\nBody: {snippet}"
                    return False, None, APITester._attach_proxy_warning(TestResult(
                        success=False,
                        message="响应不是 JSON，可能 Base URL 指向了网页入口或路径不正确",
                        response_time=response_time,
                        status_code=response.getcode(),
                        error_details=details,
                    ), proxy_warning)
                return True, parsed, TestResult(
                    success=True,
                    message="连接成功",
                    response_time=response_time,
                    status_code=response.getcode(),
                    proxy_warning=proxy_warning or None,
                )
        except urllib.error.HTTPError as e:
            response_time = (time.time() - start_time) * 1000
            try:
                error_body = APITester._read_response_bytes(
                    e,
                    APITester.MAX_ERROR_RESPONSE_BYTES,
                    "API 错误响应",
                ).decode("utf-8", errors="replace")
                error_details = APITester._parse_error_body(error_body, request_secrets)
            except _ResponseTooLargeError as body_error:
                error_details = str(body_error)
            return False, None, APITester._attach_proxy_warning(TestResult(
                success=False,
                message=APITester._http_error_message(e.code, model_hint=True),
                response_time=response_time,
                status_code=e.code,
                error_details=error_details,
            ), proxy_warning)
        except _ResponseTooLargeError as e:
            return False, None, APITester._attach_proxy_warning(TestResult(
                success=False,
                message="API 响应过大，已停止读取",
                error_details=str(e),
            ), proxy_warning)
        except (TimeoutError, socket.timeout):
            return False, None, APITester._attach_proxy_warning(APITester._timeout_result(timeout), proxy_warning)
        except urllib.error.URLError as e:
            reason = e.reason
            if APITester._is_timeout_error(reason):
                return False, None, APITester._attach_proxy_warning(
                    APITester._timeout_result(timeout), proxy_warning
                )
            return False, None, APITester._attach_proxy_warning(TestResult(
                success=False,
                message="网络错误，无法连接到服务器",
                error_details=APITester._redact_sensitive_text(e.reason, request_secrets),
            ), proxy_warning)
        except Exception as e:
            if APITester._is_timeout_error(e):
                return False, None, APITester._attach_proxy_warning(APITester._timeout_result(timeout), proxy_warning)
            if APITester._is_network_transport_error(e):
                return False, None, APITester._attach_proxy_warning(
                    APITester._network_error_result(e, secrets=request_secrets), proxy_warning
                )
            safe_error = APITester._redact_sensitive_text(e, request_secrets)
            logger.error("API request failed (%s): %s", type(e).__name__, safe_error)
            return False, None, APITester._attach_proxy_warning(TestResult(
                success=False,
                message=f"测试失败: {type(e).__name__}",
                error_details=safe_error,
            ), proxy_warning)

    @staticmethod
    def _request_event_stream(
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: int = 10,
    ) -> TestResult:
        timeout = APITester._coerce_timeout(timeout)
        start_time = time.time()
        data = json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        request_headers.setdefault("User-Agent", APITester.USER_AGENT)
        header_error = APITester._request_header_error(request_headers)
        if header_error is not None:
            return header_error
        req = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
        request_secrets = APITester._sensitive_header_values(request_headers)

        proxy_warning = APITester.reconcile_invalid_local_proxy_for_request(url)
        try:
            response, detected_proxy_warning = APITester._urlopen(
                req,
                timeout,
                known_proxy_warning=proxy_warning,
            )
            proxy_warning = detected_proxy_warning or proxy_warning
            with response:
                status_code = response.getcode()
                content_type = response.headers.get("Content-Type", "") or response.headers.get("content-type", "")
                snippet_parts: list[str] = []
                snippet_len = 0
                rolling_text = ""
                event_count = 0
                stream_bytes = 0

                while True:
                    raw_line = response.readline(APITester.MAX_STREAM_LINE_BYTES + 1)
                    if not raw_line:
                        break
                    event_count += 1
                    stream_bytes += len(raw_line)
                    if len(raw_line) > APITester.MAX_STREAM_LINE_BYTES:
                        return APITester._attach_proxy_warning(TestResult(
                            success=False,
                            message="流式响应单行过大，已停止读取",
                            status_code=status_code,
                            error_details=(
                                f"单行超过 {APITester.MAX_STREAM_LINE_BYTES} 字节上限"
                            ),
                        ), proxy_warning)
                    if stream_bytes > APITester.MAX_STREAM_RESPONSE_BYTES:
                        return APITester._attach_proxy_warning(TestResult(
                            success=False,
                            message="流式响应过大，已停止读取",
                            status_code=status_code,
                            error_details=(
                                f"累计超过 {APITester.MAX_STREAM_RESPONSE_BYTES} 字节上限"
                            ),
                        ), proxy_warning)
                    line = raw_line.decode("utf-8", errors="replace")
                    if snippet_len < 400:
                        snippet_parts.append(line)
                        snippet_len += len(line)
                    rolling_text = (rolling_text + line)[-2000:]
                    lowered = rolling_text.lower()

                    if (
                        "event: error" in lowered
                        or "response.failed" in lowered
                        or "response.incomplete" in lowered
                        or re.search(r'"type"\s*:\s*"error"', lowered)
                    ):
                        response_time = (time.time() - start_time) * 1000
                        return APITester._attach_proxy_warning(TestResult(
                            success=False,
                            message="Streaming response returned an error",
                            response_time=response_time,
                            status_code=status_code,
                            error_details=APITester._redact_sensitive_text(
                                "".join(snippet_parts).strip(), request_secrets
                            ),
                        ), proxy_warning)

                    if (
                        "response.completed" in lowered
                        or "[done]" in lowered
                        or "event: done" in lowered
                    ):
                        response_time = (time.time() - start_time) * 1000
                        return TestResult(
                            success=True,
                            message="Streaming response completed",
                            response_time=response_time,
                            status_code=status_code,
                            proxy_warning=proxy_warning or None,
                        )

                    if time.time() - start_time >= timeout:
                        return APITester._attach_proxy_warning(APITester._timeout_result(timeout), proxy_warning)

                    if event_count >= APITester.MAX_STREAM_EVENTS:
                        response_time = (time.time() - start_time) * 1000
                        return APITester._attach_proxy_warning(TestResult(
                            success=False,
                            message="Streaming response exceeded event limit before completion",
                            response_time=response_time,
                            status_code=status_code,
                            error_details=APITester._redact_sensitive_text(
                                "".join(snippet_parts).strip(), request_secrets
                            ),
                        ), proxy_warning)

                response_time = (time.time() - start_time) * 1000
                snippet = APITester._redact_sensitive_text(
                    "".join(snippet_parts).strip(), request_secrets
                )
                if not snippet:
                    return APITester._attach_proxy_warning(TestResult(
                        success=False,
                        message="Streaming response was empty",
                        response_time=response_time,
                        status_code=status_code,
                    ), proxy_warning)

                return APITester._attach_proxy_warning(TestResult(
                    success=False,
                    message="Streaming response ended before completion",
                    response_time=response_time,
                    status_code=status_code,
                    error_details=f"Content-Type: {content_type or 'unknown'}\nBody: {snippet}",
                ), proxy_warning)
        except urllib.error.HTTPError as e:
            response_time = (time.time() - start_time) * 1000
            try:
                error_body = APITester._read_response_bytes(
                    e,
                    APITester.MAX_ERROR_RESPONSE_BYTES,
                    "API 错误响应",
                ).decode("utf-8", errors="replace")
                error_details = APITester._parse_error_body(error_body, request_secrets)
            except _ResponseTooLargeError as body_error:
                error_details = str(body_error)
            return APITester._attach_proxy_warning(TestResult(
                success=False,
                message=APITester._http_error_message(e.code, model_hint=True),
                response_time=response_time,
                status_code=e.code,
                error_details=error_details,
            ), proxy_warning)
        except (TimeoutError, socket.timeout):
            return APITester._attach_proxy_warning(APITester._timeout_result(timeout), proxy_warning)
        except urllib.error.URLError as e:
            reason = e.reason
            if APITester._is_timeout_error(reason):
                return APITester._attach_proxy_warning(APITester._timeout_result(timeout), proxy_warning)
            return APITester._attach_proxy_warning(TestResult(
                success=False,
                message="Network error: unable to connect to the server",
                error_details=APITester._redact_sensitive_text(e.reason, request_secrets),
            ), proxy_warning)
        except Exception as e:
            if APITester._is_timeout_error(e):
                return APITester._attach_proxy_warning(APITester._timeout_result(timeout), proxy_warning)
            if APITester._is_stream_disconnect_error(e):
                return APITester._attach_proxy_warning(TestResult(
                    success=False,
                    message="Streaming response disconnected before completion",
                    error_details=APITester._redact_sensitive_text(e, request_secrets),
                ), proxy_warning)
            safe_error = APITester._redact_sensitive_text(e, request_secrets)
            logger.error("API stream request failed (%s): %s", type(e).__name__, safe_error)
            return APITester._attach_proxy_warning(TestResult(
                success=False,
                message=f"Streaming test failed: {type(e).__name__}",
                error_details=safe_error,
            ), proxy_warning)

    @staticmethod
    def fetch_openai_models(api_key: str, base_url: str = "https://api.openai.com/v1",
                            timeout: int = DEFAULT_API_TEST_TIMEOUT) -> ModelListResult:
        """Fetch models from an OpenAI-compatible /models endpoint."""
        if not api_key or not api_key.strip():
            return ModelListResult(success=False, message="API Key 为空")

        try:
            url = APITester._openai_url(base_url, "models")
        except ValueError as exc:
            return APITester._invalid_base_url_model_result(exc)
        ok, data, result = APITester._request_json(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        )
        if not ok:
            return ModelListResult(
                success=False,
                message=result.message,
                response_time=result.response_time,
                status_code=result.status_code,
                error_details=result.error_details,
                proxy_warning=result.proxy_warning,
            )

        model_infos = APITester._extract_model_infos(data)
        models = [model.id for model in model_infos]
        model_metadata = APITester._model_metadata_from_infos(model_infos)
        latest_model = APITester.recommend_latest_model(models, model_metadata)
        recommended_model = latest_model or APITester.recommend_best_model(models, model_metadata)
        return ModelListResult(
            success=bool(models),
            message=f"获取到 {len(models)} 个模型" if models else "接口返回中没有模型列表",
            models=models,
            recommended_model=recommended_model,
            latest_model=latest_model,
            model_metadata=model_metadata,
            response_time=result.response_time,
            status_code=result.status_code,
            proxy_warning=result.proxy_warning,
        )

    @staticmethod
    def fetch_claude_models(api_key: str, base_url: str = "https://api.anthropic.com",
                            timeout: int = DEFAULT_API_TEST_TIMEOUT, auth_scheme: str = "api_key") -> ModelListResult:
        """Fetch models from an Anthropic-compatible /v1/models endpoint."""
        if not api_key or not api_key.strip():
            return ModelListResult(success=False, message="API Key 为空")

        try:
            url = APITester._anthropic_url(base_url, "models")
        except ValueError as exc:
            return APITester._invalid_base_url_model_result(exc)
        ok, data, result = APITester._request_json(
            url,
            headers=APITester._claude_headers(api_key, auth_scheme),
            timeout=timeout,
        )
        if not ok:
            return ModelListResult(
                success=False,
                message=result.message,
                response_time=result.response_time,
                status_code=result.status_code,
                error_details=result.error_details,
                proxy_warning=result.proxy_warning,
            )

        model_infos = APITester._extract_model_infos(data)
        models = [model.id for model in model_infos]
        model_metadata = APITester._model_metadata_from_infos(model_infos)
        latest_model = APITester.recommend_latest_model(models, model_metadata)
        recommended_model = latest_model or APITester.recommend_best_model(models, model_metadata)
        return ModelListResult(
            success=bool(models),
            message=f"获取到 {len(models)} 个模型" if models else "接口返回中没有模型列表",
            models=models,
            recommended_model=recommended_model,
            latest_model=latest_model,
            model_metadata=model_metadata,
            response_time=result.response_time,
            status_code=result.status_code,
            proxy_warning=result.proxy_warning,
        )

    @staticmethod
    def _probe_claude_message(api_key: str, base_url: str, model: str, timeout: int,
                              auth_scheme: str = "api_key") -> TestResult:
        url = APITester._anthropic_url(base_url, "messages")
        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        headers = APITester._claude_headers(api_key, auth_scheme)
        headers["content-type"] = "application/json"
        ok, _data, result = APITester._request_json(
            url,
            headers=headers,
            method="POST",
            payload=payload,
            timeout=timeout,
        )
        result.message = "连接成功，模型可用" if ok else result.message
        return result

    @staticmethod
    def _probe_openai_chat(api_key: str, base_url: str, model: str, timeout: int) -> TestResult:
        url = APITester._openai_url(base_url, "chat/completions")
        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        ok, _data, result = APITester._request_json(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
            payload=payload,
            timeout=timeout,
        )
        result.message = "连接成功，模型可用" if ok else result.message
        return result

    @staticmethod
    def _probe_openai_responses(api_key: str, base_url: str, model: str, timeout: int) -> TestResult:
        url = APITester._openai_url(base_url, "responses")
        payload = {
            "model": model,
            "max_output_tokens": 96,
            "input": "Write 40 short words about reliable coding workflows, then write DONE.",
            "stream": True,
        }
        result = APITester._request_event_stream(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            payload=payload,
            timeout=timeout,
        )
        ok = result.success
        result.message = "连接成功，模型可用" if ok else result.message
        return result

    @staticmethod
    def _probe_openai_wire_api(api_key: str, base_url: str, model: str, wire_api: str, timeout: int) -> TestResult:
        wire_api = (wire_api or "chat").strip().lower()
        if wire_api == "responses":
            return APITester._probe_openai_responses(api_key, base_url, model, timeout)
        if wire_api != "chat":
            return TestResult(success=False, message=f"不支持的 wire_api: {wire_api}")
        return APITester._probe_openai_chat(api_key, base_url, model, timeout)

    @staticmethod
    def _resolve_openai_model(api_key: str, base_url: str, model: str, timeout: int) -> tuple[str, ModelListResult]:
        model = (model or "").strip()
        model_list = APITester.fetch_openai_models(api_key, base_url, timeout=timeout)
        if model:
            return model, model_list
        if model_list.success:
            return (model_list.latest_model or model_list.recommended_model or ""), model_list
        return "", model_list

    @staticmethod
    def _resolve_claude_model(api_key: str, base_url: str, model: str, timeout: int,
                              auth_scheme: str = "api_key") -> tuple[str, ModelListResult]:
        model = (model or "").strip()
        model_list = APITester.fetch_claude_models(
            api_key, base_url, timeout=timeout, auth_scheme=auth_scheme
        )
        if model:
            return model, model_list
        if model_list.success:
            return (model_list.latest_model or model_list.recommended_model or ""), model_list
        return "", model_list

    @staticmethod
    def benchmark_openai_wire_apis(
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "",
        timeout: int = DEFAULT_API_TEST_TIMEOUT,
        repeat_count: int = 3,
        wire_apis: tuple[str, ...] = ("chat", "responses"),
    ) -> TestResult:
        """Probe multiple OpenAI-compatible wire APIs and recommend the most stable one."""
        if not api_key or not api_key.strip():
            return TestResult(success=False, message="API Key 为空")

        try:
            base_url = validate_api_base_url(base_url, default="https://api.openai.com/v1")
        except ValueError as exc:
            return APITester._invalid_base_url_result(exc)

        timeout = APITester._coerce_timeout(timeout)
        selected_model, model_list = APITester._resolve_openai_model(api_key, base_url, model, timeout=timeout)
        if not selected_model:
            return TestResult(
                success=False,
                message="无法自动选择模型",
                status_code=model_list.status_code,
                error_details=model_list.error_details or model_list.message,
                proxy_warning=model_list.proxy_warning,
            )

        repeat_count = APITester._coerce_repeat_count(repeat_count)
        summaries = []
        best_wire = None
        best_score = (-1, -1.0)
        best_avg = None
        best_status = None
        proxy_warnings: list[str] = []

        for wire_api in wire_apis:
            wire_api = (wire_api or "").strip().lower()
            if not wire_api:
                continue
            if wire_api not in {"chat", "responses"}:
                summaries.append(f"{wire_api}: 已跳过，不支持的 wire_api")
                continue
            successes = 0
            durations = []
            errors = []
            statuses = []
            for _index in range(repeat_count):
                result = APITester._probe_openai_wire_api(api_key, base_url, selected_model, wire_api, timeout)
                if result.proxy_warning:
                    proxy_warnings.append(result.proxy_warning)
                if result.status_code is not None:
                    statuses.append(str(result.status_code))
                if result.success:
                    successes += 1
                    if result.response_time is not None:
                        durations.append(result.response_time)
                else:
                    errors.append(result.error_details or result.message)

            avg_ms = sum(durations) / len(durations) if durations else None
            avg_for_score = avg_ms if avg_ms is not None else timeout * 1000
            score = (successes, -avg_for_score)
            if score > best_score:
                best_score = score
                best_wire = wire_api
                best_avg = avg_ms
                best_status = int(statuses[-1]) if statuses and statuses[-1].isdigit() else None

            status_text = ",".join(statuses) if statuses else "-"
            avg_text = f"{avg_ms:.0f} ms" if avg_ms is not None else "-"
            error_text = f"；最近错误: {errors[-1][:160]}" if errors else ""
            summaries.append(f"{wire_api}: {successes}/{repeat_count} 成功，平均 {avg_text}，HTTP {status_text}{error_text}")

        if not best_wire or best_score[0] <= 0:
            message = "没有可测试的 wire_api" if summaries and all("已跳过" in item for item in summaries) else "所有 wire_api 测试均失败"
            return TestResult(
                success=False,
                message=message,
                response_time=best_avg,
                status_code=best_status,
                error_details="\n".join(summaries),
                selected_model=selected_model,
                proxy_warning=proxy_warnings[-1] if proxy_warnings else model_list.proxy_warning,
            )

        return TestResult(
            success=True,
            message=f"推荐 wire_api: {best_wire}（{best_score[0]}/{repeat_count} 成功）",
            response_time=best_avg,
            status_code=best_status or 200,
            error_details="\n".join(summaries),
            selected_model=selected_model,
            recommended_wire_api=best_wire,
            proxy_warning=proxy_warnings[-1] if proxy_warnings else model_list.proxy_warning,
        )

    @staticmethod
    def test_claude_api(api_key: str, base_url: str = "https://api.anthropic.com",
                        model: str = "", timeout: int = DEFAULT_API_TEST_TIMEOUT,
                        auth_scheme: str = "api_key") -> TestResult:
        """Test an Anthropic-compatible API by checking /v1/models, then fallback to /v1/messages."""
        if not api_key or not api_key.strip():
            return TestResult(success=False, message="API Key 为空")

        try:
            base_url = validate_api_base_url(base_url, default="https://api.anthropic.com")
        except ValueError as exc:
            return APITester._invalid_base_url_result(exc)

        requested_model = (model or "").strip()
        model, model_list = APITester._resolve_claude_model(
            api_key, base_url, requested_model, timeout=timeout, auth_scheme=auth_scheme
        )
        if model_list.success:
            if not model:
                return TestResult(
                    success=False,
                    message="无法自动选择模型",
                    response_time=model_list.response_time,
                    status_code=model_list.status_code,
                    proxy_warning=model_list.proxy_warning,
                )
            probe = APITester._probe_claude_message(
                api_key, base_url, model, timeout, auth_scheme=auth_scheme
            )
            probe.selected_model = model
            if not probe.proxy_warning:
                probe.proxy_warning = model_list.proxy_warning
            if probe.success:
                probe.message = (
                    f"连接成功，已自动选择最新模型: {model}"
                    if not requested_model
                    else ("连接成功，模型别名可用" if model not in model_list.models else "连接成功，模型可用")
                )
                return probe
            if probe.error_details:
                probe.error_details = f"{probe.error_details}\n可用模型: {', '.join(model_list.models[:20])}"
            else:
                probe.error_details = "可用模型: " + ", ".join(model_list.models[:20])
            return probe

        if model_list.status_code not in (404, 405):
            return TestResult(
                success=False,
                message=model_list.message,
                response_time=model_list.response_time,
                status_code=model_list.status_code,
                error_details=model_list.error_details,
                proxy_warning=model_list.proxy_warning,
            )

        if not model:
            return TestResult(
                success=False,
                message="无法自动选择模型",
                response_time=model_list.response_time,
                status_code=model_list.status_code,
                error_details=model_list.error_details or model_list.message,
                proxy_warning=model_list.proxy_warning,
            )
        result = APITester._probe_claude_message(
            api_key, base_url, model, timeout, auth_scheme=auth_scheme
        )
        result.selected_model = model
        if not result.proxy_warning:
            result.proxy_warning = model_list.proxy_warning
        return result

    @staticmethod
    def test_openai_api(api_key: str, base_url: str = "https://api.openai.com/v1",
                        model: str = "", timeout: int = DEFAULT_API_TEST_TIMEOUT, wire_api: str = "chat") -> TestResult:
        """Test an OpenAI-compatible API by checking /models, then fallback to /chat/completions."""
        if not api_key or not api_key.strip():
            return TestResult(success=False, message="API Key 为空")

        try:
            base_url = validate_api_base_url(base_url, default="https://api.openai.com/v1")
        except ValueError as exc:
            return APITester._invalid_base_url_result(exc)

        requested_model = (model or "").strip()
        model, model_list = APITester._resolve_openai_model(api_key, base_url, requested_model, timeout=timeout)
        if model_list.success:
            if not model:
                return TestResult(
                    success=False,
                    message="无法自动选择模型",
                    response_time=model_list.response_time,
                    status_code=model_list.status_code,
                    proxy_warning=model_list.proxy_warning,
                )
            selected_wire_api = "responses" if (wire_api or "").strip().lower() == "responses" else "chat"
            probe = APITester._probe_openai_wire_api(api_key, base_url, model, selected_wire_api, timeout)
            probe.selected_model = model
            probe.recommended_wire_api = selected_wire_api
            if not probe.proxy_warning:
                probe.proxy_warning = model_list.proxy_warning
            if probe.success:
                probe.message = (
                    f"连接成功，已自动选择最新模型: {model}"
                    if not requested_model
                    else ("连接成功，模型别名可用" if model not in model_list.models else "连接成功，模型可用")
                )
                return probe
            if probe.error_details:
                probe.error_details = f"{probe.error_details}\n可用模型: {', '.join(model_list.models[:20])}"
            else:
                probe.error_details = "可用模型: " + ", ".join(model_list.models[:20])
            return probe

        if model_list.status_code not in (404, 405):
            return TestResult(
                success=False,
                message=model_list.message,
                response_time=model_list.response_time,
                status_code=model_list.status_code,
                error_details=model_list.error_details,
            )

        if not model:
            return TestResult(
                success=False,
                message="无法自动选择模型",
                response_time=model_list.response_time,
                status_code=model_list.status_code,
                error_details=model_list.error_details or model_list.message,
                proxy_warning=model_list.proxy_warning,
            )
        selected_wire_api = "responses" if (wire_api or "").strip().lower() == "responses" else "chat"
        result = APITester._probe_openai_wire_api(api_key, base_url, model, selected_wire_api, timeout)
        result.selected_model = model
        result.recommended_wire_api = selected_wire_api
        if not result.proxy_warning:
            result.proxy_warning = model_list.proxy_warning
        return result

    @staticmethod
    def test_url_reachable(url: str, timeout: int = 5) -> TestResult:
        """Test if a URL is reachable."""
        timeout = APITester._coerce_timeout(timeout, default=5)
        try:
            url = validate_api_base_url(url)
        except ValueError as exc:
            return APITester._invalid_base_url_result(exc)
        start_time = time.time()
        proxy_warning = APITester.reconcile_invalid_local_proxy_for_request(url)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": APITester.USER_AGENT}, method="HEAD")
            response, detected_proxy_warning = APITester._urlopen(
                req,
                timeout,
                known_proxy_warning=proxy_warning,
            )
            proxy_warning = detected_proxy_warning or proxy_warning
            with response:
                return TestResult(
                    success=True,
                    message=(
                        f"可访问 (HTTP {response.getcode()})"
                        + (f"；{proxy_warning}" if proxy_warning else "")
                    ),
                    response_time=(time.time() - start_time) * 1000,
                    status_code=response.getcode(),
                    proxy_warning=proxy_warning or None,
                )
        except urllib.error.HTTPError as e:
            return APITester._attach_proxy_warning(TestResult(
                success=False,
                message=f"HTTP {e.code}",
                response_time=(time.time() - start_time) * 1000,
                status_code=e.code,
            ), proxy_warning)
        except (TimeoutError, socket.timeout):
            return APITester._attach_proxy_warning(APITester._timeout_result(timeout), proxy_warning)
        except urllib.error.URLError as e:
            if APITester._is_timeout_error(e.reason):
                return APITester._attach_proxy_warning(APITester._timeout_result(timeout), proxy_warning)
            return APITester._attach_proxy_warning(TestResult(
                success=False,
                message="无法访问",
                error_details=str(e.reason)[:400],
            ), proxy_warning)
        except Exception as e:
            if APITester._is_timeout_error(e):
                return APITester._attach_proxy_warning(APITester._timeout_result(timeout), proxy_warning)
            if APITester._is_network_transport_error(e):
                return APITester._attach_proxy_warning(
                    APITester._network_error_result(e, message="无法访问"), proxy_warning
                )
            return APITester._attach_proxy_warning(TestResult(
                success=False,
                message="测试失败",
                error_details=str(e)[:400],
            ), proxy_warning)
