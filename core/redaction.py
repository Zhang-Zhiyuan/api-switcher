"""Shared credential redaction for logs and user-facing diagnostics."""

from __future__ import annotations

import re
from collections.abc import Iterable


_SECRET_REDACTION_RULES = (
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@"),
        r"\1[REDACTED]@",
    ),
    (
        re.compile(
            r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)"
            r"(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)((?:api[_ -]?key|(?:access|refresh|id|auth)[_ -]?token|"
            r"(?:github|gh)[_ -]?token|authorization|client[_ -]?secret|"
            r"secret|password|passphrase)"
            r"\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;；，。}\[\])>]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([?&](?:api[_-]?key|key|token|access[_-]?token|"
            r"refresh[_-]?token|id[_-]?token|auth|authorization|signature|"
            r"sig|secret|password)=)[^&#\s\"'；，。}\])>]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(https?://[^\s?#]*?/(?:sub|subscribe|subscription|link|clash)/)"
            r"[A-Za-z0-9._~%+=/-]{8,}"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_.-]{8,}\b", re.IGNORECASE),
        "[REDACTED]",
    ),
    (
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
        "[REDACTED]",
    ),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b"
        ),
        "[REDACTED]",
    ),
)


def redact_sensitive_text(
    value: object,
    *,
    secrets: Iterable[object] = (),
    max_length: int | None = None,
) -> str:
    """Redact exact credentials and common token shapes from diagnostic text."""

    text = str(value or "")
    exact_secrets = sorted(
        {str(secret) for secret in secrets if secret is not None and str(secret)},
        key=len,
        reverse=True,
    )
    for secret in exact_secrets:
        text = text.replace(secret, "[REDACTED]")
    for pattern, replacement in _SECRET_REDACTION_RULES:
        text = pattern.sub(replacement, text)
    if max_length is not None:
        try:
            limit = max(0, int(max_length))
        except (TypeError, ValueError, OverflowError):
            limit = 0
        text = text[:limit]
    return text
