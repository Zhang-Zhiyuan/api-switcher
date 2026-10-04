"""Typed proxy-update outcomes while preserving existing string consumers.

Presentation text is not a protocol. Background retry/severity decisions must
use these fields, never search localized messages for failure keywords.
"""
from __future__ import annotations


OUTCOMES = frozenset({"applied", "unchanged", "retained", "skipped", "failed", "unknown"})


class ProxyUpdateResult(str):
    def __new__(cls, message, outcome="unknown", *, retryable=False, warning=False, route_verification=()):
        if outcome not in OUTCOMES:
            raise ValueError("Unknown proxy update outcome")
        instance = super().__new__(cls, str(message))
        instance.outcome = outcome
        instance.retryable = bool(retryable)
        instance.warning = bool(warning)
        from types import MappingProxyType
        instance.route_verification = tuple(MappingProxyType(dict(row)) for row in route_verification)
        return instance

    @property
    def applied(self):
        return self.outcome in {"applied", "unchanged"}

    def __reduce_ex__(self, protocol):
        return (_restore_result, (str(self), self.outcome, self.retryable, self.warning,
                                  tuple(dict(row) for row in self.route_verification)))


def _restore_result(message, outcome, retryable, warning, records):
    return ProxyUpdateResult(message, outcome, retryable=retryable, warning=warning, route_verification=records)


def update_result(message, outcome, *, retryable=False, warning=False, route_verification=None):
    records = getattr(message, "route_verification", ()) if route_verification is None else route_verification
    return ProxyUpdateResult(message, outcome, retryable=retryable, warning=warning, route_verification=records)


def with_update_message(value, message):
    """Add context without losing the underlying result's machine-readable state."""
    if isinstance(value, ProxyUpdateResult):
        return update_result(message, value.outcome, retryable=value.retryable, warning=value.warning,
                             route_verification=value.route_verification)
    # Old integrations may still return a plain string. Do not invent success
    # or retry authority from their wording; surface the uncertainty instead.
    return update_result(message, "unknown", warning=True)
