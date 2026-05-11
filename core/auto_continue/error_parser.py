"""
API 错误解析器
支持多种 API 提供商的错误格式，提取结构化错误信息
"""
import re
from dataclasses import dataclass
from typing import Optional, Dict, Any
from enum import Enum


class ErrorType(Enum):
    """错误类型枚举"""
    CONTENT_LENGTH_EXCEEDED = "content_length_exceeded"  # 内容超长
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"  # 速率限制
    AUTHENTICATION_ERROR = "authentication_error"  # 认证错误
    PERMISSION_DENIED = "permission_denied"  # 权限不足
    INVALID_REQUEST = "invalid_request"  # 请求无效
    SERVER_ERROR = "server_error"  # 服务器错误
    NETWORK_ERROR = "network_error"  # 网络错误
    TIMEOUT_ERROR = "timeout_error"  # 超时错误
    QUOTA_EXCEEDED = "quota_exceeded"  # 配额超限
    MODEL_OVERLOADED = "model_overloaded"  # 模型过载
    UNKNOWN = "unknown"  # 未知错误


class RecoveryStrategy(Enum):
    """恢复策略枚举"""
    COMPACT_AND_CONTINUE = "compact_and_continue"  # 压缩并继续
    RETRY_WITH_BACKOFF = "retry_with_backoff"  # 退避重试
    WAIT_AND_RETRY = "wait_and_retry"  # 等待后重试
    NOTIFY_USER = "notify_user"  # 通知用户
    ABORT = "abort"  # 中止执行
    NONE = "none"  # 无需恢复


@dataclass
class ParsedError:
    """解析后的错误信息"""
    error_type: ErrorType
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    http_status: Optional[int] = None
    provider: Optional[str] = None  # API 提供商
    retry_after: Optional[int] = None  # 建议重试等待时间（秒）
    details: Optional[Dict[str, Any]] = None  # 额外详情
    recovery_strategy: RecoveryStrategy = RecoveryStrategy.NONE
    user_message: Optional[str] = None  # 给用户的友好提示

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "error_type": self.error_type.value,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "http_status": self.http_status,
            "provider": self.provider,
            "retry_after": self.retry_after,
            "details": self.details,
            "recovery_strategy": self.recovery_strategy.value,
            "user_message": self.user_message
        }


class ErrorParser:
    """API 错误解析器"""

    # 内容超长错误模式
    CONTENT_LENGTH_PATTERNS = [
        r"CONTENT_LENGTH_EXCEEDS_THRESHOLD",
        r"context[_\s]?length[_\s]?exceeded",
        r"maximum context length",
        r"maximum[_\s-]?context[_\s-]?length",
        r"context.*window.*(limit|full|exceed|overflow)",
        r"(reached|hit|exceed(?:ed|s)?).*context.*window",
        r"context.*limit.*(reached|exceed(?:ed|s)?)",
        r"对话内容超出长度限制",
        r"内容太长",
        r"tokens?.*exceed",
        r"context.*too.*long",
        r"(input|prompt|request|messages?).*(too\s*long|too\s*large|exceed(?:ed|s)?)",
        r"请求.*过长",
        r"上下文.*超出",
        r"提示词.*过长",
        r"输入.*过长",
    ]

    # 速率限制错误模式
    RATE_LIMIT_PATTERNS = [
        r"rate[_\s]?limit[_\s]?exceeded",
        r"too[_\s]?many[_\s]?requests",
        r"请求过于频繁",
        r"速率限制",
        r"频率限制",
        r"429",
    ]

    # 认证错误模式
    AUTH_ERROR_PATTERNS = [
        r"authentication[_\s]?failed",
        r"invalid[_\s]?api[_\s]?key",
        r"unauthorized",
        r"认证失败",
        r"密钥无效",
        r"401",
    ]

    # 权限错误模式
    PERMISSION_PATTERNS = [
        r"permission[_\s]?denied",
        r"access[_\s]?denied",
        r"forbidden",
        r"权限不足",
        r"访问被拒绝",
        r"403",
    ]

    # 配额超限模式
    QUOTA_PATTERNS = [
        r"quota[_\s]?exceeded",
        r"insufficient[_\s]?quota",
        r"配额.*超出",
        r"额度.*不足",
        r"余额不足",
    ]

    # 模型过载模式
    OVERLOAD_PATTERNS = [
        r"model[_\s]?overloaded",
        r"server[_\s]?overloaded",
        r"capacity[_\s]?exceeded",
        r"模型.*过载",
        r"服务器.*繁忙",
        r"503",
    ]

    # 超时错误模式
    TIMEOUT_PATTERNS = [
        r"timeout",
        r"timed[_\s]?out",
        r"超时",
        r"504",
    ]

    # 网络错误模式
    NETWORK_PATTERNS = [
        r"network[_\s]?error",
        r"connection[_\s]?failed",
        r"connection[_\s]?refused",
        r"网络.*错误",
        r"连接.*失败",
    ]

    # 服务器错误模式
    SERVER_ERROR_PATTERNS = [
        r"internal[_\s]?server[_\s]?error",
        r"server[_\s]?error",
        r"服务器.*错误",
        r"500",
        r"502",
    ]

    def parse(self, error_data: Dict[str, Any]) -> ParsedError:
        """
        解析错误数据

        Args:
            error_data: 错误数据字典，可能包含：
                - error_code: 错误代码
                - error_message: 错误消息
                - error: 错误对象
                - status: HTTP 状态码
                - response: 响应内容
                等

        Returns:
            ParsedError: 解析后的错误信息
        """
        # 提取基本信息
        error_code = self._extract_error_code(error_data)
        error_message = self._extract_error_message(error_data)
        http_status = self._extract_http_status(error_data)
        provider = self._detect_provider(error_data)

        # 识别错误类型
        error_type = self._classify_error(error_code, error_message, http_status)

        # 提取额外信息
        retry_after = self._extract_retry_after(error_data)
        details = self._extract_details(error_data)

        # 确定恢复策略
        recovery_strategy = self._determine_recovery_strategy(error_type, error_data)

        # 生成用户友好消息
        user_message = self._generate_user_message(error_type, error_code, error_message)

        return ParsedError(
            error_type=error_type,
            error_code=error_code,
            error_message=error_message,
            http_status=http_status,
            provider=provider,
            retry_after=retry_after,
            details=details,
            recovery_strategy=recovery_strategy,
            user_message=user_message
        )

    def _extract_error_code(self, data: Dict[str, Any]) -> Optional[str]:
        """提取错误代码"""
        # 尝试多种可能的字段名
        for key in ["error_code", "code", "errorCode", "error_type", "type"]:
            if key in data and data[key]:
                return str(data[key])

        # 尝试从嵌套的 error 对象中提取
        if "error" in data and isinstance(data["error"], dict):
            for key in ["code", "type", "error_code"]:
                if key in data["error"]:
                    return str(data["error"][key])

        return None

    def _extract_error_message(self, data: Dict[str, Any]) -> Optional[str]:
        """提取错误消息"""
        # 尝试多种可能的字段名
        for key in [
            "error_message", "message", "error", "errorMessage", "hint", "detail",
            "text", "content",
            "response", "body", "data", "errors", "stderr", "stdout",
        ]:
            if key in data and data[key]:
                msg = data[key]
                if isinstance(msg, str):
                    return msg
                elif isinstance(msg, dict):
                    nested = self._extract_error_message(msg)
                    if nested:
                        return nested
                elif isinstance(msg, list):
                    for item in msg:
                        if isinstance(item, str) and item:
                            return item
                        if isinstance(item, dict):
                            nested = self._extract_error_message(item)
                            if nested:
                                return nested

        # 尝试从嵌套的 error 对象中提取
        if "error" in data and isinstance(data["error"], dict):
            for key in ["message", "detail", "hint", "error_message", "errorMessage"]:
                if key in data["error"]:
                    return str(data["error"][key])

        return None

    def _extract_http_status(self, data: Dict[str, Any]) -> Optional[int]:
        """提取 HTTP 状态码"""
        for key in ["status", "http_status", "status_code", "statusCode"]:
            if key in data and data[key]:
                try:
                    return int(data[key])
                except (ValueError, TypeError):
                    pass

        # 尝试从错误消息中提取
        error_message = self._extract_error_message(data)
        if error_message:
            match = re.search(r'\b([45]\d{2})\b', error_message)
            if match:
                return int(match.group(1))

        return None

    def _detect_provider(self, data: Dict[str, Any]) -> Optional[str]:
        """检测 API 提供商"""
        # 从数据中提取
        if "provider" in data:
            return str(data["provider"])

        # 从错误消息中推断
        error_message = self._extract_error_message(data) or ""
        error_code = self._extract_error_code(data) or ""
        combined = f"{error_code} {error_message}".lower()

        if "anthropic" in combined or "claude" in combined:
            return "anthropic"
        elif "openai" in combined or "gpt" in combined:
            return "openai"
        elif "google" in combined or "gemini" in combined:
            return "google"
        elif "deepseek" in combined:
            return "deepseek"
        elif "zhipu" in combined or "glm" in combined:
            return "zhipu"

        return None

    def _classify_error(self, error_code: Optional[str],
                       error_message: Optional[str],
                       http_status: Optional[int]) -> ErrorType:
        """分类错误类型"""
        combined = f"{error_code or ''} {error_message or ''}".lower()

        # 按优先级检查各种错误类型
        if self._matches_patterns(combined, self.CONTENT_LENGTH_PATTERNS):
            return ErrorType.CONTENT_LENGTH_EXCEEDED

        if self._matches_patterns(combined, self.RATE_LIMIT_PATTERNS) or http_status == 429:
            return ErrorType.RATE_LIMIT_EXCEEDED

        if self._matches_patterns(combined, self.AUTH_ERROR_PATTERNS) or http_status == 401:
            return ErrorType.AUTHENTICATION_ERROR

        if self._matches_patterns(combined, self.PERMISSION_PATTERNS) or http_status == 403:
            return ErrorType.PERMISSION_DENIED

        if self._matches_patterns(combined, self.QUOTA_PATTERNS):
            return ErrorType.QUOTA_EXCEEDED

        if self._matches_patterns(combined, self.OVERLOAD_PATTERNS) or http_status == 503:
            return ErrorType.MODEL_OVERLOADED

        if self._matches_patterns(combined, self.TIMEOUT_PATTERNS) or http_status == 504:
            return ErrorType.TIMEOUT_ERROR

        if self._matches_patterns(combined, self.NETWORK_PATTERNS):
            return ErrorType.NETWORK_ERROR

        if self._matches_patterns(combined, self.SERVER_ERROR_PATTERNS) or (http_status and 500 <= http_status < 600):
            return ErrorType.SERVER_ERROR

        if http_status and 400 <= http_status < 500:
            return ErrorType.INVALID_REQUEST

        return ErrorType.UNKNOWN

    def _matches_patterns(self, text: str, patterns: list) -> bool:
        """检查文本是否匹配任一模式"""
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def _extract_retry_after(self, data: Dict[str, Any]) -> Optional[int]:
        """提取建议的重试等待时间"""
        # 从 Retry-After 头部提取
        if "retry_after" in data:
            try:
                return int(data["retry_after"])
            except (ValueError, TypeError):
                pass

        # 从错误消息中提取
        error_message = self._extract_error_message(data) or ""

        # 匹配 "请在 X 秒后重试" 或 "retry after X seconds"
        patterns = [
            r'(\d+)\s*秒后.*重试',
            r'retry.*?(\d+)\s*second',
            r'wait.*?(\d+)\s*second',
            r'(\d+)s\s*后',
        ]

        for pattern in patterns:
            match = re.search(pattern, error_message, re.IGNORECASE)
            if match:
                try:
                    return int(match.group(1))
                except (ValueError, IndexError):
                    pass

        return None

    def _extract_details(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """提取额外的错误详情"""
        details = {}

        # 提取所有非标准字段
        standard_fields = {"error_code", "code", "error_message", "message", "error",
                          "status", "http_status", "provider"}

        for key, value in data.items():
            if key not in standard_fields:
                details[key] = value

        return details if details else None

    def _determine_recovery_strategy(self, error_type: ErrorType,
                                    data: Dict[str, Any]) -> RecoveryStrategy:
        """确定恢复策略"""
        strategy_map = {
            ErrorType.CONTENT_LENGTH_EXCEEDED: RecoveryStrategy.COMPACT_AND_CONTINUE,
            ErrorType.RATE_LIMIT_EXCEEDED: RecoveryStrategy.WAIT_AND_RETRY,
            ErrorType.MODEL_OVERLOADED: RecoveryStrategy.RETRY_WITH_BACKOFF,
            ErrorType.TIMEOUT_ERROR: RecoveryStrategy.RETRY_WITH_BACKOFF,
            ErrorType.NETWORK_ERROR: RecoveryStrategy.RETRY_WITH_BACKOFF,
            ErrorType.SERVER_ERROR: RecoveryStrategy.RETRY_WITH_BACKOFF,
            ErrorType.AUTHENTICATION_ERROR: RecoveryStrategy.NOTIFY_USER,
            ErrorType.PERMISSION_DENIED: RecoveryStrategy.NOTIFY_USER,
            ErrorType.QUOTA_EXCEEDED: RecoveryStrategy.NOTIFY_USER,
            ErrorType.INVALID_REQUEST: RecoveryStrategy.ABORT,
            ErrorType.UNKNOWN: RecoveryStrategy.NONE,
        }

        return strategy_map.get(error_type, RecoveryStrategy.NONE)

    def _generate_user_message(self, error_type: ErrorType,
                              error_code: Optional[str],
                              error_message: Optional[str]) -> str:
        """生成用户友好的错误消息"""
        messages = {
            ErrorType.CONTENT_LENGTH_EXCEEDED: "对话内容过长，正在自动压缩并继续...",
            ErrorType.RATE_LIMIT_EXCEEDED: "请求过于频繁，正在等待后重试...",
            ErrorType.MODEL_OVERLOADED: "服务器繁忙，正在重试...",
            ErrorType.TIMEOUT_ERROR: "请求超时，正在重试...",
            ErrorType.NETWORK_ERROR: "网络错误，正在重试...",
            ErrorType.SERVER_ERROR: "服务器错误，正在重试...",
            ErrorType.AUTHENTICATION_ERROR: "认证失败，请检查 API 密钥",
            ErrorType.PERMISSION_DENIED: "权限不足，请检查账户权限",
            ErrorType.QUOTA_EXCEEDED: "配额已用完，请充值或等待配额重置",
            ErrorType.INVALID_REQUEST: "请求无效，无法自动恢复",
            ErrorType.UNKNOWN: f"未知错误: {error_message or error_code or '无详细信息'}",
        }

        return messages.get(error_type, "发生错误")


# 全局实例
error_parser = ErrorParser()
