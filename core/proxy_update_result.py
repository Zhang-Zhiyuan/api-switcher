"""Typed proxy-update outcomes while preserving existing string consumers.

Presentation text is not a protocol. Background retry/severity decisions must
use these fields, never search localized messages for failure keywords.
"""
from __future__ import annotations


OUTCOMES = frozenset({"applied", "unchanged", "retained", "skipped", "failed", "unknown"})


class ProxyUpdateResult(str):
    def __new__(cls, message, outcome="unknown", *, retryable=False, warning=False):
        if outcome not in OUTCOMES:
            raise ValueError("Unknown proxy update outcome")
        instance = super().__new__(cls, str(message))
        instance.outcome = outcome
        instance.retryable = bool(retryable)
        instance.warning = bool(warning)
        return instance

    @property
    def applied(self):
        return self.outcome in {"applied", "unchanged"}


def update_result(message, outcome, *, retryable=False, warning=False):
    return ProxyUpdateResult(message, outcome, retryable=retryable, warning=warning)


def with_update_message(value, message):
    """Add context without losing the underlying result's machine-readable state."""
    if isinstance(value, ProxyUpdateResult):
        return update_result(message, value.outcome, retryable=value.retryable, warning=value.warning)
    # Old integrations may still return a plain string. Do not invent success
    # or retry authority from their wording; surface the uncertainty instead.
    return update_result(message, "unknown", warning=True)
