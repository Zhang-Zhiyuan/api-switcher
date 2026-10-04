"""Process-local download scheduling; never stores nodes or writes user files.

Downloads share freshness by source, while each caller supplies its own period
and independently acknowledges successful application. Callers serialize their
own application tasks and load/verify the returned revision from disk.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import threading
import time
from typing import Callable, Literal


Consumer = str | tuple[str, ...]


def make_source_key(
    profile_id: str,
    *,
    url: str = "",
    source_path: str = "",
    source_revision: str | int = "",
    privacy_policy=None,
) -> str:
    """Hash source identity without retaining subscription credentials.

    ``source_revision`` means source/editor identity, not last download time.
    Pass the cache fingerprint separately to ``finish_success``. The caller
    must include all privacy/transport policy affecting safe download reuse.
    """
    identity = [str(profile_id), str(url).strip(), str(source_path),
                str(source_revision), privacy_policy]
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RefreshToken:
    key: str
    serial: int


@dataclass(frozen=True)
class RefreshDecision:
    action: Literal["fetch", "cached", "wait", "busy"]
    revision: str | None = None
    version: int | None = None
    token: RefreshToken | None = None
    next_due: float | None = None
    retry_after: float = 0.0
    reason: str = ""


@dataclass
class _ConsumerState:
    applied: bool = False
    applied_at: float | None = None
    failures: int = 0
    retry_at: float = 0.0
    proven_current_key: str | None = None


@dataclass
class _SourceState:
    revision: str | None = None
    version: int | None = None
    downloaded_at: float | None = None
    failures: int = 0
    retry_at: float = 0.0
    reservation: RefreshToken | None = None
    consumers: OrderedDict[str, _ConsumerState] = field(default_factory=OrderedDict)


def _positive_seconds(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive finite number")
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return seconds


def _consumer_key(consumer: Consumer) -> str:
    if not (isinstance(consumer, str) or (
        isinstance(consumer, tuple) and all(isinstance(part, str) for part in consumer)
    )):
        raise TypeError("consumer must be a string or tuple of strings")
    encoded = json.dumps(consumer, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SubscriptionRefreshSchedule:
    """Bounded thread-safe scheduler, deliberately empty after process restart.

    In-flight sources are never LRU-evicted. If every slot is busy, a new
    source receives ``busy`` instead of starting an untracked second fetch.
    Evicted source/consumer acknowledgements may require a later idempotent
    reapply; no credential or full node payload is retained in memory here.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 128,
        max_consumers_per_source: int = 256,
        base_retry_seconds: float = 60,
        max_retry_seconds: float = 1800,
        busy_retry_seconds: float = 1,
    ):
        for value in (max_entries, max_consumers_per_source):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("schedule bounds must be positive integers")
        self._clock = clock
        self._max_entries = max_entries
        self._max_consumers = max_consumers_per_source
        self._base_retry = _positive_seconds(base_retry_seconds, "base retry")
        self._max_retry = _positive_seconds(max_retry_seconds, "max retry")
        self._busy_retry = _positive_seconds(busy_retry_seconds, "busy retry")
        if self._base_retry > self._max_retry:
            raise ValueError("base retry cannot exceed max retry")
        self._lock = threading.RLock()
        self._sources: OrderedDict[str, _SourceState] = OrderedDict()
        self._serial = 0

    def _next_serial(self) -> int:
        self._serial += 1
        return self._serial

    @staticmethod
    def _check_key(key: str):
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("source key must be a non-empty bounded string")

    def _get_or_create(self, key: str) -> _SourceState | None:
        state = self._sources.get(key)
        if state is not None:
            self._sources.move_to_end(key)
            return state
        if len(self._sources) >= self._max_entries:
            victim = next((name for name, item in self._sources.items()
                           if item.reservation is None), None)
            if victim is None:
                return None
            del self._sources[victim]
        state = _SourceState()
        self._sources[key] = state
        return state

    def begin(self, key: str, consumer: Consumer, interval_seconds: float) -> RefreshDecision:
        """Reserve one due fetch, reuse a cache revision, or return its deadline.

        ``next_due`` uses the injected monotonic clock, never wall time. A
        missing cache always yields ``fetch``, ``wait`` or ``busy`` rather
        than claiming that an unavailable cached result can be applied.
        """
        self._check_key(key)
        consumer_key = _consumer_key(consumer)
        interval = _positive_seconds(interval_seconds, "interval")
        with self._lock:
            now = self._clock()
            state = self._get_or_create(key)
            if state is None:
                return RefreshDecision("busy", next_due=now + self._busy_retry,
                                       retry_after=self._busy_retry, reason="capacity")
            if consumer_key in state.consumers:
                state.consumers.move_to_end(consumer_key)
            decision = self._decide(state, consumer_key, interval, now)
            if decision.action == "fetch":
                token = RefreshToken(key, self._next_serial())
                state.reservation = token
                return replace(decision, token=token)
            return decision

    def next_delay(self, key: str, consumer: Consumer, interval_seconds: float) -> float:
        """Read the next non-negative delay without reserving work or changing LRU.

        An unseen source is due immediately. Unlike ``begin``, this method
        neither allocates nor evicts entries, including when capacity is full.
        """
        self._check_key(key)
        consumer_key = _consumer_key(consumer)
        interval = _positive_seconds(interval_seconds, "interval")
        with self._lock:
            state = self._sources.get(key)
            if state is None:
                return 0.0
            return self._decide(state, consumer_key, interval, self._clock()).retry_after

    def _decide(self, state: _SourceState, consumer_key: str, interval: float, now: float) -> RefreshDecision:
        """Pure decision formula shared by reserving and read-only callers."""
        due = now if state.downloaded_at is None else state.downloaded_at + interval
        fetch_due = max(due, state.retry_at)
        consumer_state = state.consumers.get(consumer_key)
        cache_available = state.revision is not None
        if consumer_state is not None and consumer_state.applied and consumer_state.applied_at is not None:
            # Downloads by a faster scope must not postpone another scope's
            # periodic health verification of an unchanged node/config.
            apply_due = consumer_state.applied_at + interval
        else:
            apply_due = consumer_state.retry_at if consumer_state else now
        if state.reservation is None and now >= fetch_due:
            return RefreshDecision("fetch", state.revision, state.version,
                                   next_due=now, reason="due")
        if cache_available and now >= apply_due:
            return RefreshDecision("cached", state.revision, state.version,
                                   next_due=now, reason="pending_apply")
        if state.reservation is not None:
            next_due = now + self._busy_retry
            if cache_available:
                next_due = min(next_due, apply_due)
            return RefreshDecision("busy", state.revision, state.version,
                                   next_due=next_due,
                                   retry_after=max(0, next_due - now), reason="inflight")
        next_due = min(fetch_due, apply_due) if cache_available else fetch_due
        reason = ("apply_backoff" if cache_available and apply_due < fetch_due
                  and consumer_state is not None and not consumer_state.applied else
                  "backoff" if state.retry_at > due else "fresh")
        return RefreshDecision("wait", state.revision, state.version,
                               next_due=next_due, retry_after=max(0, next_due - now), reason=reason)

    def finish_success(self, token: RefreshToken, revision: str) -> int | None:
        """Accept the current fetch and return its cache version for acknowledgement.

        Repeated content fingerprints preserve version and consumer health
        check timestamps. Stale/cancelled/evicted reservations return ``None``.
        """
        if not isinstance(revision, str) or not revision or len(revision) > 4096:
            raise ValueError("cache revision must be a non-empty bounded string")
        with self._lock:
            state = self._reserved_state(token)
            if state is None:
                return None
            if revision != state.revision:
                state.revision = revision
                state.version = self._next_serial()
                state.consumers.clear()
            state.downloaded_at = self._clock()
            state.failures = 0
            state.retry_at = 0.0
            state.reservation = None
            self._sources.move_to_end(token.key)
            return state.version

    def finish_failure(self, token: RefreshToken) -> float | None:
        """Retain any usable cache and return retry deadline: 1/2/4/.../30 min."""
        with self._lock:
            state = self._reserved_state(token)
            if state is None:
                return None
            state.failures = min(state.failures + 1, 31)
            delay = min(self._max_retry, self._base_retry * 2 ** (state.failures - 1))
            state.retry_at = self._clock() + delay
            state.reservation = None
            self._sources.move_to_end(token.key)
            return state.retry_at

    def _reserved_state(self, token: RefreshToken) -> _SourceState | None:
        if not isinstance(token, RefreshToken):
            return None
        state = self._sources.get(token.key)
        return state if state is not None and state.reservation == token else None

    def mark_applied(self, key: str, consumer: Consumer, revision: str, *, version: int) -> bool:
        """Acknowledge only an exactly matching current cache incarnation.

        Apply failures deliberately do not call this method. An old apply
        completion cannot acknowledge a newer download, even after reset.
        """
        consumer_key = _consumer_key(consumer)
        with self._lock:
            state = self._current_revision_state(key, revision, version)
            if state is None:
                return False
            self._set_consumer(state, consumer_key, _ConsumerState(applied=True, applied_at=self._clock()))
            self._sources.move_to_end(key)
            return True

    def defer_apply(
        self, key: str, consumer: Consumer, revision: str, *, version: int,
        retryable: bool, interval_seconds: float,
    ) -> float | None:
        """Defer a failed/skipped apply without marking it as successful.

        Retryable failures use this consumer's independent 1/2/4/.../30 min
        backoff. Other skips wait its normal period. A new revision clears all
        such deferrals; periodic downloads remain eligible independently.
        """
        consumer_key = _consumer_key(consumer)
        interval = _positive_seconds(interval_seconds, "interval")
        with self._lock:
            state = self._current_revision_state(key, revision, version)
            if state is None:
                return None
            previous = state.consumers.get(consumer_key)
            failures = min((previous.failures if previous else 0) + 1, 31) if retryable else 0
            delay = min(self._max_retry, self._base_retry * 2 ** (failures - 1)) if retryable else interval
            retry_at = self._clock() + delay
            self._set_consumer(state, consumer_key, _ConsumerState(
                failures=failures, retry_at=retry_at,
                proven_current_key=previous.proven_current_key if previous else None,
            ))
            self._sources.move_to_end(key)
            return retry_at

    def remember_origin(
        self, key: str, consumer: Consumer, revision: str, *, version: int, current_key: str,
    ) -> bool:
        """Keep one caller-proven running-node fingerprint for same-content retry.

        The caller must first prove membership in this source's original cache;
        this method does not infer ownership. No node/config/credential is kept.
        A later retry must still compare the live fingerprint exactly and use
        the existing commit-time origin guard before applying any candidate.
        """
        if (not isinstance(current_key, str) or len(current_key) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in current_key)):
            return False
        consumer_key = _consumer_key(consumer)
        with self._lock:
            state = self._current_revision_state(key, revision, version)
            if state is None:
                return False
            previous = state.consumers.get(consumer_key) or _ConsumerState()
            self._set_consumer(state, consumer_key, replace(previous, proven_current_key=current_key))
            self._sources.move_to_end(key)
            return True

    def proven_origin(self, key: str, consumer: Consumer, revision: str, *, version: int) -> str | None:
        """Read same-consumer/version proof without extending its lifetime or LRU."""
        consumer_key = _consumer_key(consumer)
        with self._lock:
            state = self._current_revision_state(key, revision, version)
            if state is None:
                return None
            previous = state.consumers.get(consumer_key)
            return previous.proven_current_key if previous else None

    def _current_revision_state(self, key: str, revision: str, version: int) -> _SourceState | None:
        if not isinstance(revision, str) or not isinstance(version, int) or isinstance(version, bool):
            return None
        state = self._sources.get(key)
        if state is None or state.revision is None or state.revision != revision or state.version != version:
            return None
        return state

    def _set_consumer(self, state: _SourceState, consumer_key: str, consumer: _ConsumerState):
        state.consumers[consumer_key] = consumer
        state.consumers.move_to_end(consumer_key)
        while len(state.consumers) > self._max_consumers:
            state.consumers.popitem(last=False)

    def cancel(self, token: RefreshToken) -> bool:
        """Release an abandoned fetch without inventing a download failure."""
        with self._lock:
            state = self._reserved_state(token)
            if state is None:
                return False
            state.reservation = None
            return True

    def invalidate(self, key: str) -> None:
        """Forget one context and reject its stale completions; does not stop I/O."""
        with self._lock:
            self._sources.pop(key, None)

    def clear(self) -> None:
        """Reset process state without reusing old reservation/version numbers."""
        with self._lock:
            self._sources.clear()

    reset = clear

    def __len__(self) -> int:
        with self._lock:
            return len(self._sources)
