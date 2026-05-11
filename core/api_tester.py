"""API connection and model-list utilities."""
from __future__ import annotations

import json
import logging
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
    model_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    response_time: Optional[float] = None
    status_code: Optional[int] = None
    error_details: Optional[str] = None


class APITester:
    """Test API connections and refresh model lists."""

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

        clean_name = re.sub(r"20\d{6}", "", name)
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
        start_time = time.time()
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                response_time = (time.time() - start_time) * 1000
                body = response.read().decode("utf-8", errors="replace")
                parsed = json.loads(body) if body else {}
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
        except urllib.error.URLError as e:
            return False, None, TestResult(
                success=False,
                message="网络错误，无法连接到服务器",
                error_details=str(e.reason)[:400],
            )
        except TimeoutError:
            return False, None, TestResult(
                success=False,
                message=f"连接超时，超过 {timeout} 秒",
                error_details="请检查网络连接或稍后重试",
            )
        except Exception as e:
            logger.error("API request failed: %s", e, exc_info=True)
            return False, None, TestResult(
                success=False,
                message=f"测试失败: {type(e).__name__}",
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
        recommended_model = APITester.recommend_best_model(models, model_metadata)
        return ModelListResult(
            success=bool(models),
            message=f"获取到 {len(models)} 个模型" if models else "接口返回中没有模型列表",
            models=models,
            recommended_model=recommended_model,
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
        recommended_model = APITester.recommend_best_model(models, model_metadata)
        return ModelListResult(
            success=bool(models),
            message=f"获取到 {len(models)} 个模型" if models else "接口返回中没有模型列表",
            models=models,
            recommended_model=recommended_model,
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
    def test_claude_api(api_key: str, base_url: str = "https://api.anthropic.com",
                        model: str = "claude-sonnet-4", timeout: int = 10) -> TestResult:
        """Test an Anthropic-compatible API by checking /v1/models, then fallback to /v1/messages."""
        if not api_key or not api_key.strip():
            return TestResult(success=False, message="API Key 为空")

        model = (model or "").strip()
        model_list = APITester.fetch_claude_models(api_key, base_url, timeout=timeout)
        if model_list.success:
            if model and model not in model_list.models:
                probe = APITester._probe_claude_message(api_key, base_url, model, timeout)
                if probe.success:
                    probe.message = "连接成功，模型别名可用"
                    return probe
                if probe.error_details:
                    probe.error_details = f"{probe.error_details}\n可用模型: {', '.join(model_list.models[:20])}"
                else:
                    probe.error_details = "可用模型: " + ", ".join(model_list.models[:20])
                return probe
            return TestResult(
                success=True,
                message="连接成功，模型可用" if model else "连接成功",
                response_time=model_list.response_time,
                status_code=model_list.status_code,
            )

        if model_list.status_code not in (404, 405):
            return TestResult(
                success=False,
                message=model_list.message,
                response_time=model_list.response_time,
                status_code=model_list.status_code,
                error_details=model_list.error_details,
            )

        return APITester._probe_claude_message(api_key, base_url, model, timeout)

    @staticmethod
    def test_openai_api(api_key: str, base_url: str = "https://api.openai.com/v1",
                        model: str = "gpt-5.5", timeout: int = 10) -> TestResult:
        """Test an OpenAI-compatible API by checking /models, then fallback to /chat/completions."""
        if not api_key or not api_key.strip():
            return TestResult(success=False, message="API Key 为空")

        model = (model or "").strip()
        model_list = APITester.fetch_openai_models(api_key, base_url, timeout=timeout)
        if model_list.success:
            if model and model not in model_list.models:
                probe = APITester._probe_openai_chat(api_key, base_url, model, timeout)
                if probe.success:
                    probe.message = "连接成功，模型别名可用"
                    return probe
                if probe.error_details:
                    probe.error_details = f"{probe.error_details}\n可用模型: {', '.join(model_list.models[:20])}"
                else:
                    probe.error_details = "可用模型: " + ", ".join(model_list.models[:20])
                return probe
            return TestResult(
                success=True,
                message="连接成功，模型可用" if model else "连接成功",
                response_time=model_list.response_time,
                status_code=model_list.status_code,
            )

        if model_list.status_code not in (404, 405):
            return TestResult(
                success=False,
                message=model_list.message,
                response_time=model_list.response_time,
                status_code=model_list.status_code,
                error_details=model_list.error_details,
            )

        return APITester._probe_openai_chat(api_key, base_url, model, timeout)

    @staticmethod
    def test_url_reachable(url: str, timeout: int = 5) -> TestResult:
        """Test if a URL is reachable."""
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
        except urllib.error.URLError as e:
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
