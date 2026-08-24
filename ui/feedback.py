"""Shared helpers for concise, safe, and semantically correct UI feedback."""

from __future__ import annotations

import re


FEEDBACK_SEVERITIES = frozenset({"info", "busy", "success", "warning", "error"})

_ERROR_MARKERS = (
    "失败",
    "错误",
    "异常",
    "损坏",
    "不可恢复",
    "未完成",
    "无法",
)
_CRITICAL_ERROR_PATTERNS = (
    re.compile(r"(?:恢复|回滚|还原)[^；。\n]{0,36}失败"),
    re.compile(r"失败[^；。\n]{0,36}(?:未恢复|未完成|可能仍|请重新检查)"),
)
_SAFE_FALLBACK_MARKERS = (
    "部分失败",
    "已回滚",
    "已恢复原",
    "已恢复旧",
    "已强制恢复",
    "已安全保留",
    "已保留原",
    "继续使用已有",
    "未改动",
    "未修改",
    "已跳过",
    "自动更新跳过",
    "已使用内置",
)
_WARNING_MARKERS = (
    "警告",
    "注意",
    "部分",
    "取消",
    "跳过",
    "暂无",
    "未找到",
    "未运行",
    "未配置",
    "未完全",
    "不可用",
    "不能",
    "请先",
    "请稍",
    "请勿",
    "待确认",
    "已保留",
    "无需",
    "失效",
    "暂未",
    "未监听",
    "未清理",
    "未改动",
    "未修改",
    "请检查",
)
_BUSY_MARKERS = (
    "正在",
    "加载中",
    "启动中",
    "读取中",
    "生成中",
    "处理中",
    "检测中",
    "刷新中",
    "连接中",
)
_SUCCESS_MARKERS = (
    "完成",
    "就绪",
    "成功",
    "已保存",
    "已加载",
    "已打开",
    "已创建",
    "已切换",
    "已清理",
    "已恢复",
    "已启动",
    "已停止",
    "已更新",
    "已同步",
    "已通过",
)


def safe_feedback_text(message: object) -> str:
    """Redact common credential shapes before text reaches a visible widget."""

    text = str(message or "")
    text = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{4,}",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)((?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|authorization|secret|password)"
        r"\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]{4,}",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:api[_-]?key|key|token|access_token)=)[^&#\s]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_.-]{8,}\b",
        "[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b",
        "[REDACTED]",
        text,
    )
    return text


def infer_feedback_severity(message: object) -> str:
    """Infer a visual state while distinguishing safe fallback from hard failure."""

    text = str(message or "").strip()
    if not text:
        return "info"

    has_error = any(marker in text for marker in _ERROR_MARKERS)
    if has_error:
        critical = any(pattern.search(text) for pattern in _CRITICAL_ERROR_PATTERNS)
        safely_contained = any(marker in text for marker in _SAFE_FALLBACK_MARKERS)
        if safely_contained and not critical:
            return "warning"
        return "error"
    if any(marker in text for marker in _WARNING_MARKERS):
        return "warning"
    if any(marker in text for marker in _BUSY_MARKERS):
        return "busy"
    if any(marker in text for marker in _SUCCESS_MARKERS) or text.startswith("已"):
        return "success"
    return "info"


def resolve_feedback_severity(
    message: object,
    *,
    severity: str | None = None,
    is_error: bool = False,
) -> str:
    """Resolve explicit and legacy error flags without painting warnings as errors."""

    explicit = str(severity or "").strip().lower()
    if explicit in FEEDBACK_SEVERITIES:
        return explicit
    inferred = infer_feedback_severity(message)
    if not is_error:
        return inferred
    # Existing callers historically use ``is_error=True`` for validation and
    # busy warnings too. Preserve an inferred warning, but retain the legacy
    # hard-error fallback when the text itself has no semantic marker.
    return inferred if inferred in {"warning", "error"} else "error"


def feedback_title(severity: str) -> str:
    return {
        "success": "操作完成",
        "warning": "需要注意",
        "error": "操作未完成",
        "busy": "正在处理",
        "info": "提示",
    }.get(str(severity or "").lower(), "提示")


def feedback_duration_ms(message: object, severity: str) -> int:
    """Give long/important feedback enough reading time without lingering forever."""

    text = str(message or "")
    base = {
        "success": 3200,
        "info": 3800,
        "busy": 3600,
        "warning": 5600,
        "error": 7200,
    }.get(str(severity or "").lower(), 3800)
    reading_time = 1900 + min(len(text), 180) * 42 + min(text.count("\n"), 6) * 280
    return max(base, min(reading_time, 11_000))
