"""API connection and model-list utilities."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
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
class ModelListResult:
    """Result of a remote model-list request."""
    success: bool
    message: str
    models: list[str] = field(default_factory=list)
    response_time: Optional[float] = None
    status_code: Optional[int] = None
    error_details: Optional[str] = None


class APITester:
    """Test API connections and refresh model lists."""

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
        if isinstance(data, dict):
            candidates = data.get("data") or data.get("models") or data.get("items")
        else:
            candidates = data

        models: list[str] = []
        if not isinstance(candidates, list):
            return models

        for item in candidates:
            model_id = None
            if isinstance(item, str):
                model_id = item
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("name") or item.get("model")
            if model_id:
                models.append(str(model_id))

        return sorted(dict.fromkeys(models), key=str.lower)

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

        models = APITester._extract_model_ids(data)
        return ModelListResult(
            success=bool(models),
            message=f"获取到 {len(models)} 个模型" if models else "接口返回中没有模型列表",
            models=models,
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

        models = APITester._extract_model_ids(data)
        return ModelListResult(
            success=bool(models),
            message=f"获取到 {len(models)} 个模型" if models else "接口返回中没有模型列表",
            models=models,
            response_time=result.response_time,
            status_code=result.status_code,
        )

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
                return TestResult(
                    success=False,
                    message="连接成功，但模型不在服务端列表中",
                    response_time=model_list.response_time,
                    status_code=model_list.status_code,
                    error_details="可用模型: " + ", ".join(model_list.models[:20]),
                )
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
    def test_openai_api(api_key: str, base_url: str = "https://api.openai.com/v1",
                        model: str = "gpt-5.5", timeout: int = 10) -> TestResult:
        """Test an OpenAI-compatible API by checking /models, then fallback to /chat/completions."""
        if not api_key or not api_key.strip():
            return TestResult(success=False, message="API Key 为空")

        model = (model or "").strip()
        model_list = APITester.fetch_openai_models(api_key, base_url, timeout=timeout)
        if model_list.success:
            if model and model not in model_list.models:
                return TestResult(
                    success=False,
                    message="连接成功，但模型不在服务端列表中",
                    response_time=model_list.response_time,
                    status_code=model_list.status_code,
                    error_details="可用模型: " + ", ".join(model_list.models[:20]),
                )
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
