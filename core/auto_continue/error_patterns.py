"""Shared regex patterns for auto-continue API error detection."""

CONTENT_LENGTH_PATTERNS = [
    r"CONTENT_LENGTH_EXCEEDS_THRESHOLD",
    r"content_length_exceeds_threshold",
    r"content[_\s-]?length.*(exceed|too|large|long|threshold)",
    r"context[_\s]?length[_\s]?exceeded",
    r"maximum context length",
    r"maximum[_\s-]?context[_\s-]?length",
    r"api error:.*context.*window.*limit",
    r"model.*reached.*context.*window.*limit",
    r"context.*window.*(limit|full|exceed|overflow)",
    r"(reached|hit|exceed(?:ed|s)?).*context.*window",
    r"context.*limit.*(reached|exceed(?:ed|s)?)",
    r"对话内容超出长度限制",
    r"内容.*超出.*长度",
    r"会话.*内容.*太长",
    r"上游服务.*处理能力",
    r"内容太长",
    r"tokens?.*exceed",
    r"context.*too.*long",
    r"(input|prompt|request|messages?).*(too\s*long|too\s*large|exceed(?:ed|s)?)",
    r"请求.*过长",
    r"上下文.*超出",
    r"提示词.*过长",
    r"输入.*过长",
]


TRANSPORT_RECOVERABLE_PATTERNS = [
    r"error running remote compact task",
    r"stream disconnected before completion",
    r"reconnecting\.\.\.\s*\d+/\d+",
    r"upstream connect error",
    r"disconnect/reset before headers",
    r"reset reason.*connection termination",
    r"error sending request for url",
    r"backend-api/codex/responses/compact",
    r"responses/compact",
]


RECOVERABLE_API_ERROR_PATTERNS = TRANSPORT_RECOVERABLE_PATTERNS + CONTENT_LENGTH_PATTERNS
