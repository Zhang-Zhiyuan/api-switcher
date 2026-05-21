"""Diagnostics helpers for auto-continue hooks and recovery logs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from core.auto_continue.manager import auto_continue_manager
from models.auto_continue import AutoContinueSettings, training_prompt_template_by_key


@dataclass
class AutoContinueLogEvent:
    provider: str
    source: str
    timestamp: str = ""
    session_id: str = ""
    hook_event: str = ""
    decision: str = ""
    reason: str = ""
    match: str = ""
    count: int | str = ""
    recovery_count: int | str = ""
    git_commit_hash: str = ""
    training_template: str = ""
    excerpt: str = ""
    raw: dict[str, Any] | None = None

    def summary_line(self) -> str:
        parts = [
            self.timestamp or "-",
            self.provider,
            self.source,
            self.decision or "-",
            self.reason or "-",
        ]
        if self.count not in ("", None, -1):
            parts.append(f"续跑次数={self.count}")
        if self.recovery_count not in ("", None):
            parts.append(f"恢复次数={self.recovery_count}")
        if self.match:
            parts.append(f"命中={self.match}")
        if self.git_commit_hash:
            parts.append(f"Git={self.git_commit_hash}")
        if self.training_template:
            parts.append(f"训练模板={self.training_template}")
        if self.excerpt:
            parts.append(f"摘要={self.excerpt}")
        return " | ".join(str(part) for part in parts)


def _jsonl_events(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []

    events: list[dict[str, Any]] = []
    for line in lines[-max(limit * 3, limit):]:
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events[-limit:]


def _provider_log_paths(provider_name: str) -> tuple[Path, list[tuple[str, Path]]]:
    provider = auto_continue_manager.get_provider(provider_name)
    config_dir = provider.get_config_dir()
    return config_dir, [
        ("Stop", config_dir / "tmp" / "auto_continue_stop_log.jsonl"),
        ("Stop", config_dir / "auto_continue_stop_log.jsonl"),
        ("API恢复", config_dir / "tmp" / "error_recovery_log.jsonl"),
        ("API恢复", config_dir / "error_recovery_log.jsonl"),
    ]


def load_auto_continue_events(provider_name: str, limit: int = 100) -> list[AutoContinueLogEvent]:
    """Load recent auto-continue and API recovery events for one provider."""
    provider_key = str(provider_name or "").strip().lower()
    _config_dir, paths = _provider_log_paths(provider_key)
    settings = auto_continue_manager.get_settings(provider_key) or AutoContinueSettings()
    training_template = training_prompt_template_by_key(settings.training_prompt_template_key)["name"]

    events: list[AutoContinueLogEvent] = []
    for source, path in paths:
        for raw in _jsonl_events(path, limit):
            if source == "API恢复":
                decision = str(raw.get("action") or "")
                reason = str(raw.get("error_type") or raw.get("error_code") or "")
                hook_event = "Error"
                recovery_count = raw.get("recovery_count", "")
                count = ""
                match = str(raw.get("recovery_strategy") or "")
                excerpt = str(raw.get("error_message") or "")[:500]
            else:
                decision = str(raw.get("decision") or "")
                reason = str(raw.get("reason") or "")
                hook_event = str(raw.get("hook_event") or "")
                recovery_count = ""
                count = raw.get("count", "")
                match = str(raw.get("match") or "")
                excerpt = str(raw.get("excerpt") or "")[:500]

            events.append(
                AutoContinueLogEvent(
                    provider=provider_key,
                    source=source,
                    timestamp=str(raw.get("timestamp") or ""),
                    session_id=str(raw.get("session_id") or ""),
                    hook_event=hook_event,
                    decision=decision,
                    reason=reason,
                    match=match,
                    count=count,
                    recovery_count=recovery_count,
                    git_commit_hash=str(
                        raw.get("git_commit_hash")
                        or raw.get("git_commit")
                        or raw.get("commit_hash")
                        or ""
                    ),
                    training_template=training_template if settings.training_auto_continue_enabled else "",
                    excerpt=excerpt,
                    raw=raw,
                )
            )

    events.sort(key=lambda event: event.timestamp, reverse=True)
    return events[:limit]


def format_auto_continue_diagnostics(provider_name: str, limit: int = 100) -> str:
    """Return a copy-friendly diagnostic report."""
    provider_key = str(provider_name or "").strip().lower()
    events = load_auto_continue_events(provider_key, limit=limit)
    settings = auto_continue_manager.get_settings(provider_key) or AutoContinueSettings()
    template = training_prompt_template_by_key(settings.training_prompt_template_key)["name"]
    block_count = sum(1 for event in events if event.decision == "block_stop")
    allow_count = sum(1 for event in events if event.decision == "allow_stop")
    recovery_count = sum(1 for event in events if event.source == "API恢复")

    lines = [
        f"Provider: {provider_key}",
        f"Stop续跑: {'ON' if settings.enabled else 'OFF'}",
        f"训练续跑: {'ON' if settings.training_auto_continue_enabled else 'OFF'} ({template})",
        f"API恢复: {'ON' if settings.error_recovery_enabled else 'OFF'}",
        f"Git快照: {'ON' if settings.git_auto_snapshot else 'OFF'}",
        f"Events: {len(events)} | block_stop={block_count} | allow_stop={allow_count} | API恢复={recovery_count}",
        "",
    ]
    if not events:
        lines.append("No auto-continue log events found.")
    else:
        lines.extend(event.summary_line() for event in events)
    return "\n".join(lines)
