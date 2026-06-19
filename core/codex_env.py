"""Helpers for Codex's per-user .env file."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from config import paths
from core.atomic_io import atomic_write_text
from core.persistent_env import ENV_NAME_RE, normalize_env_names, normalize_env_updates

ENV_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _read_text() -> str:
    path = paths.CODEX_ENV
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _quote_dotenv_value(value: str) -> str:
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _assignment_line(name: str, value: str) -> str:
    return f"{name}={_quote_dotenv_value(value)}"


def _parse_dotenv_value(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        body = value[1:-1]
        result = []
        i = 0
        while i < len(body):
            char = body[i]
            if char == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if nxt == "n":
                    result.append("\n")
                elif nxt == "r":
                    result.append("\r")
                else:
                    result.append(nxt)
                i += 2
                continue
            result.append(char)
            i += 1
        return "".join(result)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _update_text(content: str, updates: Mapping[str, str], deletes: Iterable[str]) -> str:
    update_names = set(updates)
    delete_names = set(deletes)
    touched = set()
    output: list[str] = []

    for line in (content or "").splitlines():
        match = ENV_ASSIGNMENT_RE.match(line)
        name = match.group(1) if match else ""
        if name in delete_names and name not in update_names:
            continue
        if name in update_names:
            if name not in touched:
                output.append(_assignment_line(name, updates[name]))
                touched.add(name)
            continue
        output.append(line)

    if updates:
        if not output:
            output.extend([
                "# Managed by API 配置切换器.",
                "# Used by Codex desktop/VS Code when shell env is not inherited.",
            ])
        if output and output[-1].strip():
            output.append("")
        for name, value in updates.items():
            if name not in touched:
                output.append(_assignment_line(name, value))

    if not output:
        return ""
    return "\n".join(output).rstrip() + "\n"


def merge_codex_env_text(
    content: str | None,
    updates: Mapping[str, str] | None = None,
    deletes: Iterable[str] | str | None = None,
) -> str:
    normalized_updates = normalize_env_updates(updates or {}) if updates else {}
    if isinstance(deletes, str):
        normalized_deletes = normalize_env_names(deletes)
    elif deletes:
        normalized_deletes = normalize_env_names(deletes)
    else:
        normalized_deletes = []
    return _update_text(content or "", normalized_updates, normalized_deletes)


def parse_codex_env_text(content: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (content or "").splitlines():
        match = ENV_ASSIGNMENT_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        if "=" not in line:
            continue
        values[name] = _parse_dotenv_value(line.split("=", 1)[1])
    return values


def update_codex_env(updates: Mapping[str, str] | None = None, deletes: Iterable[str] | str | None = None) -> list[str]:
    """Upsert and delete Codex .env variables while preserving unrelated lines."""
    normalized_updates = normalize_env_updates(updates or {}) if updates else {}
    if isinstance(deletes, str):
        normalized_deletes = normalize_env_names(deletes)
    elif deletes:
        normalized_deletes = normalize_env_names(deletes)
    else:
        normalized_deletes = []

    if not normalized_updates and not normalized_deletes:
        return []

    for name in normalized_updates:
        if not ENV_NAME_RE.match(name):
            raise ValueError(f"环境变量名无效: {name}")

    updated = _update_text(_read_text(), normalized_updates, normalized_deletes)
    atomic_write_text(paths.CODEX_ENV, updated)
    return list(normalized_updates.keys())


def set_codex_env(updates: Mapping[str, str]) -> list[str]:
    return update_codex_env(updates=updates)


def delete_codex_env(names: Iterable[str] | str) -> list[str]:
    normalized = normalize_env_names(names)
    update_codex_env(deletes=normalized)
    return normalized


def read_codex_env_values() -> dict[str, str]:
    return parse_codex_env_text(_read_text())


def get_codex_env_value(name: str) -> str:
    normalized = normalize_env_names(name)[0]
    return read_codex_env_values().get(normalized, "")
