"""Helpers for managing Claude Code permission allow rules."""

from __future__ import annotations

from typing import Any


MANAGED_PERMISSION_RULES_VERSION = 1


def _rule_key(rule: object) -> str:
    return str(rule or "").strip().casefold()


def permission_rules_from_auto_settings(settings: object | None) -> list[str]:
    """Return Claude Code permission rules implied by auto-approve settings."""
    if not settings or not getattr(settings, "auto_approve_permission_requests", False):
        return []

    rules: list[str] = []
    seen: set[str] = set()
    for item in getattr(settings, "auto_approve_tools", []) or []:
        rule = str(item or "").strip()
        if not rule or rule == "*":
            continue
        key = _rule_key(rule)
        if key in seen:
            continue
        rules.append(rule)
        seen.add(key)
    return rules


def rules_from_payload(payload: Any) -> list[str]:
    """Parse a managed-rule sidecar payload."""
    if isinstance(payload, dict):
        raw_rules = payload.get("rules")
    elif isinstance(payload, list):
        raw_rules = payload
    else:
        raw_rules = []
    if not isinstance(raw_rules, list):
        return []

    rules: list[str] = []
    seen: set[str] = set()
    for item in raw_rules:
        rule = str(item or "").strip()
        key = _rule_key(rule)
        if rule and key not in seen:
            rules.append(rule)
            seen.add(key)
    return rules


def rules_payload(rules: list[str]) -> dict[str, object]:
    """Build the sidecar payload for managed permission rules."""
    return {
        "version": MANAGED_PERMISSION_RULES_VERSION,
        "rules": list(rules),
    }


def apply_managed_permission_rules(
    claude_settings: dict,
    desired_rules: list[str],
    previous_managed_rules: list[str],
) -> tuple[dict, list[str]]:
    """Apply desired rules while preserving user-managed allow rules.

    Only rules that this helper appends are returned as managed rules. This
    keeps pre-existing user allow rules out of the sidecar so disabling the
    feature does not remove permissions the user already had.
    """
    settings = dict(claude_settings) if isinstance(claude_settings, dict) else {}
    permissions = settings.get("permissions")
    permissions = dict(permissions) if isinstance(permissions, dict) else {}

    raw_allow = permissions.get("allow")
    current_allow = raw_allow if isinstance(raw_allow, list) else []
    cleaned_allow: list[str] = []
    seen_current: set[str] = set()
    for item in current_allow:
        rule = str(item or "").strip()
        key = _rule_key(rule)
        if rule and key not in seen_current:
            cleaned_allow.append(rule)
            seen_current.add(key)

    previous_keys = {_rule_key(rule) for rule in previous_managed_rules}
    base_allow = [rule for rule in cleaned_allow if _rule_key(rule) not in previous_keys]
    allow_keys = {_rule_key(rule) for rule in base_allow}

    managed_rules: list[str] = []
    new_allow = list(base_allow)
    for rule in desired_rules:
        rule = str(rule or "").strip()
        key = _rule_key(rule)
        if not rule or key in allow_keys:
            continue
        new_allow.append(rule)
        managed_rules.append(rule)
        allow_keys.add(key)

    if new_allow:
        permissions["allow"] = new_allow
    else:
        permissions.pop("allow", None)

    if permissions:
        settings["permissions"] = permissions
    else:
        settings.pop("permissions", None)

    return settings, managed_rules
