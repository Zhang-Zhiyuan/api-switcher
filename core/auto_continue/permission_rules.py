"""Helpers for managing Claude Code permission rules."""

from __future__ import annotations

from typing import Any


MANAGED_PERMISSION_RULES_VERSION = 1


def _rule_key(rule: object) -> str:
    return str(rule or "").strip().casefold()


def _clean_rules(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    rules: list[str] = []
    seen: set[str] = set()
    for item in values:
        rule = str(item or "").strip()
        key = _rule_key(rule)
        if rule and key not in seen:
            rules.append(rule)
            seen.add(key)
    return rules


def _rule_tool_name(rule: object) -> str:
    value = str(rule or "").strip()
    return value.split("(", 1)[0].strip().casefold()


def _is_bare_tool_rule(rule: object) -> bool:
    value = str(rule or "").strip()
    return bool(value) and "(" not in value


def permission_rule_covers(desired_rule: str, existing_rule: str) -> bool:
    """Return whether existing_rule grants at least desired_rule."""
    if _rule_key(desired_rule) == _rule_key(existing_rule):
        return True
    return _is_bare_tool_rule(existing_rule) and _rule_tool_name(existing_rule) == _rule_tool_name(desired_rule)


def permission_rule_conflicts(desired_rule: str, existing_rule: str) -> bool:
    """Return whether an ask/deny rule can still interrupt a desired allow rule."""
    if _rule_key(desired_rule) == _rule_key(existing_rule):
        return True
    if _rule_tool_name(desired_rule) != _rule_tool_name(existing_rule):
        return False
    return _is_bare_tool_rule(desired_rule) or _is_bare_tool_rule(existing_rule)


def missing_allow_rules(desired_rules: list[str], allow_rules: list[str]) -> list[str]:
    """Return desired rules that are not covered by allow rules."""
    clean_allow = _clean_rules(allow_rules)
    return [
        rule for rule in _clean_rules(desired_rules)
        if not any(permission_rule_covers(rule, allowed) for allowed in clean_allow)
    ]


def conflicting_permission_rules(desired_rules: list[str], rules: list[str]) -> list[str]:
    """Return ask/deny rules that conflict with desired auto-approve rules."""
    clean_desired = _clean_rules(desired_rules)
    return [
        rule for rule in _clean_rules(rules)
        if any(permission_rule_conflicts(desired, rule) for desired in clean_desired)
    ]


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
    return _clean_rules(raw_rules)


def ask_rules_from_payload(payload: Any) -> list[str]:
    """Parse previously removed ask rules from a managed-rule sidecar payload."""
    if isinstance(payload, dict):
        raw_rules = payload.get("ask_rules")
    else:
        raw_rules = []
    return _clean_rules(raw_rules)


def rules_payload(rules: list[str], ask_rules: list[str] | None = None) -> dict[str, object]:
    """Build the sidecar payload for managed permission rules."""
    payload: dict[str, object] = {
        "version": MANAGED_PERMISSION_RULES_VERSION,
        "rules": list(rules),
    }
    if ask_rules:
        payload["ask_rules"] = list(ask_rules)
    return payload


def apply_managed_permission_rules(
    claude_settings: dict,
    desired_rules: list[str],
    previous_managed_rules: list[str],
    previous_removed_ask_rules: list[str] | None = None,
) -> tuple[dict, list[str], list[str]]:
    """Apply desired rules while preserving user-managed permission rules.

    Only rules that this helper appends are returned as managed rules. This
    keeps pre-existing user allow rules out of the sidecar so disabling the
    feature does not remove permissions the user already had. Conflicting ask
    rules are temporarily removed and returned so they can be restored later.
    """
    settings = dict(claude_settings) if isinstance(claude_settings, dict) else {}
    permissions = settings.get("permissions")
    permissions = dict(permissions) if isinstance(permissions, dict) else {}

    desired_rules = _clean_rules(desired_rules)
    cleaned_allow = _clean_rules(permissions.get("allow"))
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

    current_ask = _clean_rules(permissions.get("ask"))
    restored_ask = list(current_ask)
    restored_ask_keys = {_rule_key(rule) for rule in restored_ask}
    for rule in previous_removed_ask_rules or []:
        key = _rule_key(rule)
        if rule and key not in restored_ask_keys:
            restored_ask.append(rule)
            restored_ask_keys.add(key)

    removed_ask_rules = conflicting_permission_rules(desired_rules, restored_ask)
    removed_ask_keys = {_rule_key(rule) for rule in removed_ask_rules}
    new_ask = [rule for rule in restored_ask if _rule_key(rule) not in removed_ask_keys]

    if new_allow:
        permissions["allow"] = new_allow
    else:
        permissions.pop("allow", None)
    if new_ask:
        permissions["ask"] = new_ask
    else:
        permissions.pop("ask", None)

    if permissions:
        settings["permissions"] = permissions
    else:
        settings.pop("permissions", None)

    return settings, managed_rules, removed_ask_rules
