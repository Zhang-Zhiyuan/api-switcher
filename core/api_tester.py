"""API connection and model-list utilities."""
from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


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


class APITester:
    """Test API connections and refresh model lists."""

    MAX_REQUEST_TIMEOUT = 30
    MAX_BENCHMARK_REPEAT = 5
    MAX_STREAM_EVENTS = 1200

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
    def _normalize_base_url(base_url: str, default: str) -> str:
        base_url = (base_url or default).strip()
        if not base_url:
            base_url = default
        if "://" not in base_url:
            base_url = "https://" + base_url
        return base_url.rstrip("/")

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
    def _parse_error_body(error_body: str) -> str:
        try:
            error_data = json.loads(error_body)
        except Exception:
            return error_body[:400]

        error = error_data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("error_msg") or error.get("detail")
            if message:
                return str(message)[:400]
        if isinstance(error, str):
            return error[:400]

        for key in ("message", "msg", "detail"):
            if error_data.get(key):
                return str(error_data[key])[:400]

        return error_body[:400]

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
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                response_time = (time.time() - start_time) * 1000
                body = response.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    content_type = response.headers.get("Content-Type", "") or response.headers.get("content-type", "")
                    details = f"Content-Type: {content_type or 'unknown'}"
                    snippet = body.strip()[:400]
                    if snippet:
                        details = f"{details}\nBody: {snippet}"
                    return False, None, TestResult(
                        success=False,
                        message="响应不是 JSON，可能 Base URL 指向了网页入口或路径不正确",
                        response_time=response_time,
                        status_code=response.getcode(),
                        error_details=details,
                    )
                return True, parsed, TestResult(
                    success=True,
                    message="连接成功",
                    response_time=response_time,
                    status_code=response.getcode(),
                )
        except urllib.error.HTTPError as e:
            response_time = (time.time() - start_time) * 1000
            error_body = e.read().decode("utf-8", errors="replace")
            return False, None, TestResult(
                success=False,
                message=APITester._http_error_message(e.code, model_hint=True),
                response_time=response_time,
                status_code=e.code,
                error_details=APITester._parse_error_body(error_body),
            )
        except (TimeoutError, socket.timeout):
            return False, None, APITester._timeout_result(timeout)
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                return False, None, APITester._timeout_result(timeout)
            return False, None, TestResult(
                success=False,
                message="网络错误，无法连接到服务器",
                error_details=str(e.reason)[:400],
            )
        except Exception as e:
            logger.error("API request failed: %s", e, exc_info=True)
            return False, None, TestResult(
                success=False,
                message=f"测试失败: {type(e).__name__}",
                error_details=str(e)[:400],
            )

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
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status_code = response.getcode()
                content_type = response.headers.get("Content-Type", "") or response.headers.get("content-type", "")
                snippet_parts: list[str] = []
                snippet_len = 0
                rolling_text = ""
                event_count = 0

                while True:
                    raw_line = response.readline()
                    if not raw_line:
                        break
                    event_count += 1
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
                        return TestResult(
                            success=False,
                            message="Streaming response returned an error",
                            response_time=response_time,
                            status_code=status_code,
                            error_details="".join(snippet_parts).strip()[:400],
                        )

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
                        )

                    if time.time() - start_time >= timeout:
                        return APITester._timeout_result(timeout)

                    if event_count >= APITester.MAX_STREAM_EVENTS:
                        response_time = (time.time() - start_time) * 1000
                        return TestResult(
                            success=False,
                            message="Streaming response exceeded event limit before completion",
                            response_time=response_time,
                            status_code=status_code,
                            error_details="".join(snippet_parts).strip()[:400],
                        )

                response_time = (time.time() - start_time) * 1000
                snippet = "".join(snippet_parts).strip()[:400]
                if not snippet:
                    return TestResult(
                        success=False,
                        message="Streaming response was empty",
                        response_time=response_time,
                        status_code=status_code,
                    )

                return TestResult(
                    success=False,
                    message="Streaming response ended before completion",
                    response_time=response_time,
                    status_code=status_code,
                    error_details=f"Content-Type: {content_type or 'unknown'}\nBody: {snippet}",
                )
        except urllib.error.HTTPError as e:
            response_time = (time.time() - start_time) * 1000
            error_body = e.read().decode("utf-8", errors="replace")
            return TestResult(
                success=False,
                message=APITester._http_error_message(e.code, model_hint=True),
                response_time=response_time,
                status_code=e.code,
                error_details=APITester._parse_error_body(error_body),
            )
        except (TimeoutError, socket.timeout):
            return APITester._timeout_result(timeout)
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                return APITester._timeout_result(timeout)
            return TestResult(
                success=False,
                message="Network error: unable to connect to the server",
                error_details=str(e.reason)[:400],
            )
        except Exception as e:
            logger.error("API stream request failed: %s", e, exc_info=True)
            return TestResult(
                success=False,
                message=f"Streaming test failed: {type(e).__name__}",
                error_details=str(e)[:400],
            )

    @staticmethod
    def fetch_openai_models(api_key: str, base_url: str = "https://api.openai.com/v1",
                            timeout: int = 10) -> ModelListResult:
        """Fetch models from an OpenAI-compatible /models endpoint."""
        if not api_key or not api_key.strip():
            return ModelListResult(success=False, message="API Key 为空")

        url = APITester._openai_url(base_url, "models")
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
        )

    @staticmethod
    def fetch_claude_models(api_key: str, base_url: str = "https://api.anthropic.com",
                            timeout: int = 10) -> ModelListResult:
        """Fetch models from an Anthropic-compatible /v1/models endpoint."""
        if not api_key or not api_key.strip():
            return ModelListResult(success=False, message="API Key 为空")

        url = APITester._anthropic_url(base_url, "models")
        ok, data, result = APITester._request_json(
            url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        if not ok:
            return ModelListResult(
                success=False,
                message=result.message,
                response_time=result.response_time,
                status_code=result.status_code,
                error_details=result.error_details,
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
        )

    @staticmethod
    def _probe_claude_message(api_key: str, base_url: str, model: str, timeout: int) -> TestResult:
        url = APITester._anthropic_url(base_url, "messages")
        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        ok, _data, result = APITester._request_json(
            url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
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
    def _resolve_claude_model(api_key: str, base_url: str, model: str, timeout: int) -> tuple[str, ModelListResult]:
        model = (model or "").strip()
        model_list = APITester.fetch_claude_models(api_key, base_url, timeout=timeout)
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
        timeout: int = 10,
        repeat_count: int = 3,
        wire_apis: tuple[str, ...] = ("chat", "responses"),
    ) -> TestResult:
        """Probe multiple OpenAI-compatible wire APIs and recommend the most stable one."""
        if not api_key or not api_key.strip():
            return TestResult(success=False, message="API Key 为空")

        timeout = APITester._coerce_timeout(timeout)
        selected_model, model_list = APITester._resolve_openai_model(api_key, base_url, model, timeout=timeout)
        if not selected_model:
            return TestResult(
                success=False,
                message="无法自动选择模型",
                status_code=model_list.status_code,
                error_details=model_list.error_details or model_list.message,
            )

        repeat_count = APITester._coerce_repeat_count(repeat_count)
        summaries = []
        best_wire = None
        best_score = (-1, -1.0)
        best_avg = None
        best_status = None

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
            )

        return TestResult(
            success=True,
            message=f"推荐 wire_api: {best_wire}（{best_score[0]}/{repeat_count} 成功）",
            response_time=best_avg,
            status_code=best_status or 200,
            error_details="\n".join(summaries),
            selected_model=selected_model,
            recommended_wire_api=best_wire,
        )

    @staticmethod
    def test_claude_api(api_key: str, base_url: str = "https://api.anthropic.com",
                        model: str = "", timeout: int = 10) -> TestResult:
        """Test an Anthropic-compatible API by checking /v1/models, then fallback to /v1/messages."""
        if not api_key or not api_key.strip():
            return TestResult(success=False, message="API Key 为空")

        requested_model = (model or "").strip()
        model, model_list = APITester._resolve_claude_model(api_key, base_url, requested_model, timeout=timeout)
        if model_list.success:
            if not model:
                return TestResult(
                    success=False,
                    message="无法自动选择模型",
                    response_time=model_list.response_time,
                    status_code=model_list.status_code,
                )
            probe = APITester._probe_claude_message(api_key, base_url, model, timeout)
            probe.selected_model = model
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
            )
        result = APITester._probe_claude_message(api_key, base_url, model, timeout)
        result.selected_model = model
        return result

    @staticmethod
    def test_openai_api(api_key: str, base_url: str = "https://api.openai.com/v1",
                        model: str = "", timeout: int = 10, wire_api: str = "chat") -> TestResult:
        """Test an OpenAI-compatible API by checking /models, then fallback to /chat/completions."""
        if not api_key or not api_key.strip():
            return TestResult(success=False, message="API Key 为空")

        requested_model = (model or "").strip()
        model, model_list = APITester._resolve_openai_model(api_key, base_url, requested_model, timeout=timeout)
        if model_list.success:
            if not model:
                return TestResult(
                    success=False,
                    message="无法自动选择模型",
                    response_time=model_list.response_time,
                    status_code=model_list.status_code,
                )
            selected_wire_api = "responses" if (wire_api or "").strip().lower() == "responses" else "chat"
            probe = APITester._probe_openai_wire_api(api_key, base_url, model, selected_wire_api, timeout)
            probe.selected_model = model
            probe.recommended_wire_api = selected_wire_api
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
            )
        selected_wire_api = "responses" if (wire_api or "").strip().lower() == "responses" else "chat"
        result = APITester._probe_openai_wire_api(api_key, base_url, model, selected_wire_api, timeout)
        result.selected_model = model
        result.recommended_wire_api = selected_wire_api
        return result

    @staticmethod
    def test_url_reachable(url: str, timeout: int = 5) -> TestResult:
        """Test if a URL is reachable."""
        timeout = APITester._coerce_timeout(timeout, default=5)
        start_time = time.time()
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return TestResult(
                    success=True,
                    message=f"可访问 (HTTP {response.getcode()})",
                    response_time=(time.time() - start_time) * 1000,
                    status_code=response.getcode(),
                )
        except urllib.error.HTTPError as e:
            return TestResult(
                success=False,
                message=f"HTTP {e.code}",
                response_time=(time.time() - start_time) * 1000,
                status_code=e.code,
            )
        except (TimeoutError, socket.timeout):
            return APITester._timeout_result(timeout)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)) or "timed out" in str(e.reason).lower():
                return APITester._timeout_result(timeout)
            return TestResult(
                success=False,
                message="无法访问",
                error_details=str(e.reason)[:400],
            )
        except Exception as e:
            return TestResult(
                success=False,
                message="测试失败",
                error_details=str(e)[:400],
            )
