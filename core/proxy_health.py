"""Pure interpretation of mihomo's cached, per-test-URL health observations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math


HEALTH_TTL_SECONDS = 180
HEALTH_CLOCK_SKEW_SECONDS = 5


@dataclass(frozen=True)
class ProxyHealth:
    state: str
    specific: bool = False
    checked_at: datetime | None = None
    delay_ms: int | None = None

    @property
    def target_healthy(self) -> bool | None:
        # A generic connectivity record is useful feedback, not evidence that
        # this group's actual target is reachable (or that its target failed).
        if not self.specific:
            return None
        return {"passed": True, "failed": False}.get(self.state)


def _timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_proxy_health(group, node, now: datetime | None = None, *, ttl_seconds=HEALTH_TTL_SECONDS) -> ProxyHealth:
    """Prefer the exact group's test; never hide its failure behind a generic pass."""
    group = group if isinstance(group, dict) else {}
    node = node if isinstance(node, dict) else {}
    test_url = group.get("testUrl") or group.get("url")
    extra = node.get("extra")
    health = extra.get(test_url) if isinstance(extra, dict) and isinstance(test_url, str) and test_url else None
    specific = isinstance(health, dict)
    if not specific:
        health = node
        # Older controllers sometimes expose the generic history on the group
        # only. Keep this fallback explicitly generic in both consumers.
        if not isinstance(health.get("history"), list) or not health["history"]:
            health = group
    history = health.get("history")
    if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
        return ProxyHealth("no_history", specific)
    last = history[-1]
    checked = _timestamp(last.get("time"))
    if checked is None:
        return ProxyHealth("unknown_time", specific)
    try:
        current = now if now is not None else datetime.now(timezone.utc)
        age = (current - checked).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return ProxyHealth("unknown_time", specific)
    if age < -HEALTH_CLOCK_SKEW_SECONDS or age > ttl_seconds:
        return ProxyHealth("stale", specific, checked)
    delay = last.get("delay")
    try:
        # Controller delays are integer milliseconds. Reject booleans, negative
        # values, non-finite numbers and out-of-range/corrupt observations.
        valid_delay = (isinstance(delay, (int, float)) and not isinstance(delay, bool)
                       and math.isfinite(delay) and 0 <= delay <= 2**31 - 1 and int(delay) == delay)
    except (ValueError, OverflowError):
        valid_delay = False
    if not valid_delay:
        return ProxyHealth("invalid_delay", specific, checked)
    alive = health.get("alive")
    if alive is not None and not isinstance(alive, bool):
        return ProxyHealth("invalid_state", specific, checked)
    state = "failed" if delay == 0 or alive is False else "passed"
    return ProxyHealth(state, specific, checked, int(delay))


def proxy_health_summary(health: ProxyHealth) -> str:
    """Shared Chinese feedback; distinguish target evidence from generic history."""
    if health.state == "no_history":
        return "连通性未检测（无内核探针记录）"
    if health.state == "unknown_time":
        return "探针时间未知，不能认定当前可用"
    prefix = "此策略组探针" if health.specific else "通用探针（非此目标专测）"
    try:
        when = health.checked_at.astimezone().strftime("%m-%d %H:%M:%S")
    except (AttributeError, ValueError, OverflowError, OSError):
        when = "时间未知"
    if health.state == "stale":
        return f"{prefix}结果已过期 / 时钟不同步 · {when}"
    if health.state == "invalid_delay":
        return f"{prefix}延迟数据无效 · {when}"
    if health.state == "invalid_state":
        return f"{prefix}状态数据无效 · {when}"
    if health.state == "failed":
        return f"{prefix}失败 · {when}"
    return f"{prefix}通过 · {health.delay_ms} ms · {when}（不代表账号或长会话可用）"
