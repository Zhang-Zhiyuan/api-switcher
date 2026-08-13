import json
import logging
import ntpath
import os
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from core.atomic_io import atomic_write_bytes, atomic_write_text
from core.auto_continue.base import AutoContinueProvider
from core.auto_continue.script_generator import generate_hook_script
from core.auto_continue.error_recovery_script import generate_codex_error_recovery_script
from models.auto_continue import AutoContinueSettings

logger = logging.getLogger(__name__)
AUTO_CONTINUE_HOOK_TIMEOUT_SECONDS = 30
ERROR_RECOVERY_HOOK_TIMEOUT_SECONDS = 30
CODEX_HOOKS_FEATURE_STATE_FILE = "auto_continue_codex_hooks_feature_state.json"
MANAGED_HOOK_PROCESS_QUERY_TIMEOUT_SECONDS = 5.0
MANAGED_HOOK_TASKKILL_TIMEOUT_SECONDS = 2.0


def _codex_hooks_enabled_from_config(config: dict) -> bool:
    """Read the hook feature flag, preferring the current Codex key."""
    if not isinstance(config, dict):
        return False

    features = config.get("features")
    if isinstance(features, dict):
        if "hooks" in features:
            return bool(features.get("hooks"))
        if "codex_hooks" in features:
            return bool(features.get("codex_hooks"))
    return bool(config.get("codex_hooks"))


def _toml_table_name(line: str) -> str | None:
    array_match = re.match(r"^\s*\[\[([^\[\]]+)\]\]\s*(?:#.*)?$", line)
    if array_match:
        return f"[]{array_match.group(1).strip()}"
    match = re.match(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$", line)
    return match.group(1).strip() if match else None


def _toml_assignment_matches(line: str, key: str) -> bool:
    return bool(re.match(rf"^\s*{re.escape(key)}\s*=", line))


def _toml_comment_suffix(value: str) -> str:
    """Return an assignment's unquoted inline comment, including spacing."""
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "#":
            prefix = value[:index]
            spacing = prefix[len(prefix.rstrip()):]
            return spacing + value[index:]
    return ""


def _set_toml_bool_assignment(line: str, key: str, enabled: bool) -> str:
    match = re.match(rf"^(\s*{re.escape(key)}\s*=\s*)(.*)$", line)
    if not match:
        return line
    suffix = _toml_comment_suffix(match.group(2))
    return f"{match.group(1)}{'true' if enabled else 'false'}{suffix}"


def _split_toml_inline_items(value: str) -> list[str] | None:
    """Split an inline-table body without disturbing nested values or strings."""
    items: list[str] = []
    start = 0
    quote = ""
    escaped = False
    depth = 0
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char in "[{(":
            depth += 1
            continue
        if char in "]})":
            if depth <= 0:
                return None
            depth -= 1
            continue
        if char == "," and depth == 0:
            items.append(value[start:index])
            start = index + 1
    if quote or depth:
        return None
    items.append(value[start:])
    return items


def _inline_toml_key(item: str) -> str:
    match = re.match(r"^\s*(?:([A-Za-z0-9_-]+)|\"([^\"]+)\"|'([^']+)')\s*=", item)
    if not match:
        return ""
    return next((part for part in match.groups() if part is not None), "")


def _set_inline_features_assignment(line: str, enabled: bool) -> str | None:
    """Update a root ``features = {...}`` assignment while preserving its layout."""
    match = re.match(r"^(\s*features\s*=\s*)\{(.*)\}(\s*(?:#.*)?)$", line)
    if not match:
        return None
    items = _split_toml_inline_items(match.group(2))
    if items is None:
        return None

    value = "true" if enabled else "false"
    found_hooks = False
    updated_items: list[str] = []
    for item in items:
        key = _inline_toml_key(item)
        if key in {"hooks", "codex_hooks"}:
            assignment = re.match(r"^(\s*(?:[A-Za-z0-9_-]+|\"[^\"]+\"|'[^']+')\s*=\s*)(.*?)(\s*)$", item)
            if not assignment:
                return None
            item = f"{assignment.group(1)}{value}{assignment.group(3)}"
            found_hooks = found_hooks or key == "hooks"
        updated_items.append(item)

    if not found_hooks:
        body = ",".join(updated_items)
        if body.strip():
            trailing = body[len(body.rstrip()):]
            body = body.rstrip() + f", hooks = {value}" + trailing
        else:
            body = f" hooks = {value} "
    else:
        body = ",".join(updated_items)
    return f"{match.group(1)}{{{body}}}{match.group(3)}"


def _set_codex_hooks_feature_lines(lines: list[str], enabled: bool) -> tuple[list[str], bool]:
    """Update Codex's hook flag while preserving legacy aliases if present."""
    updated = list(lines)
    value = "true" if enabled else "false"
    table_indexes = [index for index, line in enumerate(updated) if _toml_table_name(line) is not None]
    root_end = table_indexes[0] if table_indexes else len(updated)

    root_alias_indexes = [
        index
        for index in range(root_end)
        if _toml_assignment_matches(updated[index], "codex_hooks")
    ]
    dotted_alias_indexes = [
        index
        for index in range(root_end)
        if _toml_assignment_matches(updated[index], "features.codex_hooks")
    ]
    dotted_canonical_indexes = [
        index
        for index in range(root_end)
        if _toml_assignment_matches(updated[index], "features.hooks")
    ]
    inline_features_indexes = [
        index
        for index in range(root_end)
        if _toml_assignment_matches(updated[index], "features")
    ]

    features_index = next(
        (index for index, line in enumerate(updated) if _toml_table_name(line) == "features"),
        -1,
    )
    features_end = len(updated)
    feature_alias_indexes: list[int] = []
    feature_canonical_indexes: list[int] = []
    if features_index >= 0:
        features_end = next(
            (
                index
                for index in range(features_index + 1, len(updated))
                if _toml_table_name(updated[index]) is not None
            ),
            len(updated),
        )
        feature_alias_indexes = [
            index
            for index in range(features_index + 1, features_end)
            if _toml_assignment_matches(updated[index], "codex_hooks")
        ]
        feature_canonical_indexes = [
            index
            for index in range(features_index + 1, features_end)
            if _toml_assignment_matches(updated[index], "hooks")
        ]

    legacy_indexes = root_alias_indexes + dotted_alias_indexes + feature_alias_indexes
    canonical_indexes = dotted_canonical_indexes + feature_canonical_indexes
    should_have_canonical = bool(enabled or legacy_indexes or canonical_indexes)
    changed = False

    for index in root_alias_indexes:
        replacement = _set_toml_bool_assignment(updated[index], "codex_hooks", enabled)
        changed = changed or replacement != updated[index]
        updated[index] = replacement
    for index in dotted_alias_indexes:
        replacement = _set_toml_bool_assignment(updated[index], "features.codex_hooks", enabled)
        changed = changed or replacement != updated[index]
        updated[index] = replacement
    for index in dotted_canonical_indexes:
        replacement = _set_toml_bool_assignment(updated[index], "features.hooks", enabled)
        changed = changed or replacement != updated[index]
        updated[index] = replacement
    for index in feature_alias_indexes:
        replacement = _set_toml_bool_assignment(updated[index], "codex_hooks", enabled)
        changed = changed or replacement != updated[index]
        updated[index] = replacement
    for index in feature_canonical_indexes:
        replacement = _set_toml_bool_assignment(updated[index], "hooks", enabled)
        changed = changed or replacement != updated[index]
        updated[index] = replacement

    if inline_features_indexes:
        if len(inline_features_indexes) != 1:
            return list(lines), False
        index = inline_features_indexes[0]
        replacement = _set_inline_features_assignment(updated[index], enabled)
        if replacement is None:
            return list(lines), False
        changed = changed or replacement != updated[index]
        updated[index] = replacement

    if should_have_canonical and not canonical_indexes:
        if features_index >= 0:
            updated.insert(features_index + 1, f"hooks = {value}")
        elif dotted_alias_indexes:
            updated.insert(dotted_alias_indexes[-1] + 1, f"features.hooks = {value}")
        elif inline_features_indexes:
            # The canonical key was inserted into the existing inline table.
            pass
        else:
            block = ["[features]", f"hooks = {value}", ""]
            if root_end > 0 and updated[root_end - 1].strip():
                block.insert(0, "")
            updated = updated[:root_end] + block + updated[root_end:]
        changed = True

    return updated, changed


def _powershell_file_target(command: str) -> str | None:
    """Return the script passed to PowerShell's ``-File`` option, if any."""
    if not isinstance(command, str):
        return None

    try:
        arguments = shlex.split(command, posix=False)
    except ValueError:
        return None

    def unquote(value: str) -> str:
        value = str(value).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value

    if not arguments:
        return None
    executable = ntpath.basename(unquote(arguments[0])).casefold()
    if executable not in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return None

    # Do not search raw command text: ``-File`` inside a quoted ``-Command``
    # payload is data, not the PowerShell entry script. Token-level matching is
    # important because this helper also gates process termination.
    for index, argument in enumerate(arguments[1:], start=1):
        option = unquote(argument).casefold()
        if option in {"-command", "-c", "-encodedcommand", "-enc", "-e"}:
            return None
        if option == "-file":
            if index + 1 >= len(arguments):
                return None
            return unquote(arguments[index + 1])
    return None


def _normalize_windows_hook_path(value: str) -> str:
    # Older releases escaped backslashes before json.dumps, leaving doubled
    # separators in the parsed command. Windows accepts those paths, so treat
    # them as the same owned target while still rewriting the command itself.
    collapsed = re.sub(r"[\\/]+", r"\\", str(value).strip())
    return ntpath.normcase(ntpath.normpath(collapsed))


def _command_targets_script(
    command: str,
    script_path: Path,
) -> bool:
    target = _powershell_file_target(command)
    if not target:
        return False
    normalized_target = _normalize_windows_hook_path(target)
    normalized_expected = _normalize_windows_hook_path(str(script_path))
    # API Switcher has always emitted an absolute script path. A relative path
    # with the same basename can belong to the user and must not be claimed.
    return normalized_target == normalized_expected


def _is_managed_codex_hook(command: str) -> bool:
    """Legacy fallback used only when no provider-specific owner is supplied."""
    target = _powershell_file_target(command)
    return bool(
        target
        and not ntpath.dirname(_normalize_windows_hook_path(target))
        and ntpath.basename(target).casefold()
        in {"auto_continue_stop.ps1", "error_recovery.ps1"}
    )


def _codex_event_hooks(value) -> list[dict]:
    """Normalize supported Codex hook event shapes to a list of hook dicts."""
    if isinstance(value, dict):
        hooks = []
        if value.get("command"):
            hooks.append(dict(value))
        nested = value.get("hooks")
        if isinstance(nested, list):
            hooks.extend(dict(hook) for hook in nested if isinstance(hook, dict) and hook.get("command"))
        return hooks
    if isinstance(value, list):
        hooks = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("command"):
                hooks.append(dict(item))
            nested = item.get("hooks")
            if isinstance(nested, list):
                hooks.extend(dict(hook) for hook in nested if isinstance(hook, dict) and hook.get("command"))
        return hooks
    return []


def _partition_codex_event_value(value, is_managed) -> tuple[object | None, list[dict], bool]:
    """Remove managed hooks while preserving every user-owned group field."""
    managed: list[dict] = []

    def clean_hook_container(container):
        removed = False
        if isinstance(container, dict):
            if container.get("command") and is_managed(str(container.get("command", ""))):
                managed.append(dict(container))
                return None, True
            return dict(container), False
        if isinstance(container, list):
            remaining = []
            for hook in container:
                if (
                    isinstance(hook, dict)
                    and hook.get("command")
                    and is_managed(str(hook.get("command", "")))
                ):
                    managed.append(dict(hook))
                    removed = True
                else:
                    remaining.append(dict(hook) if isinstance(hook, dict) else hook)
            return remaining, removed
        return container, False

    def clean_item(item):
        if not isinstance(item, dict):
            return item, False
        if item.get("command"):
            if is_managed(str(item.get("command", ""))):
                managed.append(dict(item))
                return None, True
            return dict(item), False
        if "hooks" not in item:
            return dict(item), False

        cleaned_hooks, removed = clean_hook_container(item.get("hooks"))
        if not removed:
            return dict(item), False
        if cleaned_hooks is None or cleaned_hooks == []:
            return None, True
        cleaned = dict(item)
        cleaned["hooks"] = cleaned_hooks
        return cleaned, True

    if isinstance(value, list):
        remaining = []
        removed = False
        for item in value:
            cleaned, item_removed = clean_item(item)
            removed = removed or item_removed
            if cleaned is not None:
                remaining.append(cleaned)
        return (remaining if remaining else None), managed, removed

    cleaned, removed = clean_item(value)
    return cleaned, managed, removed


def _canonical_codex_event_items(value) -> list:
    """Convert supported singleton shapes without flattening user hook groups."""
    if value is None:
        return []
    source = value if isinstance(value, list) else [value]
    items = []
    for item in source:
        if isinstance(item, dict) and item.get("command"):
            items.append({"hooks": [dict(item)]})
        else:
            items.append(dict(item) if isinstance(item, dict) else item)
    return items


def _codex_hooks_container(
    data: dict,
    *,
    migrate_legacy: bool = False,
    is_managed=_is_managed_codex_hook,
) -> dict:
    """Return the pphoto/Codex hook container, migrating legacy top-level events."""
    if not isinstance(data, dict):
        return {}

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks

    if migrate_legacy:
        for event_name in list(data.keys()):
            if event_name == "hooks":
                continue
            remaining, managed_hooks, removed = _partition_codex_event_value(
                data.get(event_name),
                is_managed,
            )
            if not removed:
                continue
            event_items = _canonical_codex_event_items(hooks.get(event_name))
            event_items.extend({"hooks": [hook]} for hook in managed_hooks)
            hooks[event_name] = event_items

            if remaining is None:
                data.pop(event_name, None)
            else:
                data[event_name] = remaining

    return hooks


def _codex_event_has_command(data: dict, event_name: str, marker: str) -> bool:
    hooks = _codex_hooks_container(data)
    candidates = _codex_event_hooks(hooks.get(event_name))
    candidates.extend(_codex_event_hooks(data.get(event_name)))
    return any(marker in str(hook.get("command", "")) for hook in candidates)


def _codex_events_have_command(data: dict, event_names: tuple[str, ...], marker: str) -> bool:
    return all(_codex_event_has_command(data, event_name, marker) for event_name in event_names)


def _codex_hooks_has_entries(hooks: dict) -> bool:
    if not isinstance(hooks, dict):
        return False
    return any(_codex_event_hooks(value) for value in hooks.values())


def _codex_data_has_entries(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    hooks = _codex_hooks_container(data)
    if _codex_hooks_has_entries(hooks):
        return True
    return any(
        _codex_event_hooks(value)
        for key, value in data.items()
        if key != "hooks"
    )


def _codex_data_has_managed_entries(data: dict, is_managed=_is_managed_codex_hook) -> bool:
    if not isinstance(data, dict):
        return False
    hooks = _codex_hooks_container(data)
    values = list(hooks.values())
    values.extend(value for key, value in data.items() if key != "hooks")
    return any(
        is_managed(str(hook.get("command", "")))
        for value in values
        for hook in _codex_event_hooks(value)
    )


def _codex_owned_event_names(data: dict, is_owned) -> set[str]:
    """Return every container or legacy event containing an owned command."""
    if not isinstance(data, dict):
        return set()
    event_values = []
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        event_values.extend(hooks.items())
    event_values.extend(
        (event_name, value)
        for event_name, value in data.items()
        if event_name != "hooks"
    )
    return {
        str(event_name)
        for event_name, value in event_values
        if any(
            is_owned(str(hook.get("command", "")))
            for hook in _codex_event_hooks(value)
        )
    }


def _remove_owned_codex_hooks_from_all_events(data: dict, is_owned) -> bool:
    """Remove owned hooks from all event names without flattening user groups."""
    if not isinstance(data, dict):
        return False
    changed = False
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event_name in list(hooks):
            changed = _remove_codex_event_hook(
                hooks,
                event_name,
                is_owned,
            ) or changed

    for event_name in list(data):
        if event_name == "hooks":
            continue
        remaining, _managed, removed = _partition_codex_event_value(
            data.get(event_name),
            is_owned,
        )
        if not removed:
            continue
        changed = True
        if remaining is None:
            data.pop(event_name, None)
        else:
            data[event_name] = remaining
    return changed


def _format_codex_event_hooks(hook_list: list[dict]):
    if not hook_list:
        return None
    return [{"hooks": hook_list}]


def _format_legacy_codex_event_hooks(hook_list: list[dict]):
    if not hook_list:
        return None
    if len(hook_list) == 1:
        return hook_list[0]
    return {"hooks": hook_list}


def _upsert_codex_event_hook(hooks: dict, event_name: str, hook_def: dict, is_managed) -> None:
    remaining, _managed, _removed = _partition_codex_event_value(
        hooks.get(event_name),
        is_managed,
    )
    items = _canonical_codex_event_items(remaining)
    items.append({"hooks": [hook_def]})
    hooks[event_name] = items


def _remove_codex_event_hook(hooks: dict, event_name: str, is_managed) -> bool:
    if event_name not in hooks:
        return False
    remaining, _managed, removed = _partition_codex_event_value(
        hooks.get(event_name),
        is_managed,
    )
    if not removed:
        return False
    items = _canonical_codex_event_items(remaining)
    if not items:
        hooks.pop(event_name, None)
    else:
        hooks[event_name] = items
    return True


def _backup_codex_hooks_file(path: Path, reason: str) -> Path | None:
    if not path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for suffix in [""] + [f".{i}" for i in range(1, 100)]:
        backup_path = path.with_name(f"{path.name}.bak-{timestamp}{suffix}")
        if backup_path.exists():
            continue
        try:
            atomic_write_bytes(backup_path, path.read_bytes())
            logger.warning(f"Backed up Codex hooks.json to {backup_path}: {reason}")
            return backup_path
        except Exception as e:
            logger.warning(f"Failed to back up Codex hooks.json {path}: {e}")
            return None
    return None


def _snapshot_local_files(paths: list[Path]) -> dict[Path, bytes | None]:
    snapshots = {}
    for path in dict.fromkeys(paths):
        try:
            snapshots[path] = path.read_bytes()
        except FileNotFoundError:
            # The file may disappear between discovery and the transaction's
            # snapshot. That is the only condition equivalent to "missing".
            snapshots[path] = None
    return snapshots


def _restore_local_files(snapshots: dict[Path, bytes | None]) -> str:
    errors = []
    for path, content in snapshots.items():
        try:
            if content is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                # Avoid replacing an untouched user file merely because an
                # operation failed before its first write (notably malformed
                # hooks.json during disable/unregister).
                if path.exists() and path.read_bytes() == content:
                    continue
                atomic_write_bytes(path, content)
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    return "; ".join(errors)


def _read_codex_hooks_json(path: Path, *, recover: bool = False) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as e:
        logger.error(f"Failed to read hooks.json: {e}")
        return None
    except UnicodeError as e:
        if recover:
            backup_path = _backup_codex_hooks_file(path, f"invalid text encoding: {e}")
            if backup_path is not None:
                return {}
        logger.error(f"Invalid hooks.json text encoding: {e}")
        return None

    try:
        if not raw.strip():
            return {}
        data = json.loads(raw)
    except Exception as e:
        if recover:
            backup_path = _backup_codex_hooks_file(path, f"invalid JSON: {e}")
            if backup_path is not None:
                return {}
        logger.error(f"Failed to read hooks.json: {e}")
        return None

    if not isinstance(data, dict):
        reason = f"expected object, got {type(data).__name__}"
        if recover:
            backup_path = _backup_codex_hooks_file(path, reason)
            if backup_path is not None:
                return {}
        logger.error(f"Invalid hooks.json: {reason}")
        return None

    return data


class CodexProvider(AutoContinueProvider):
    """Auto-continue provider for Codex CLI."""

    AUTO_CONTINUE_EVENTS = ("Stop", "UserPromptSubmit", "SessionStart")
    ERROR_RECOVERY_EVENTS = ("Error", "ResponseError")

    def __init__(
        self,
        *,
        process_inventory=None,
        process_verifier=None,
        process_tree_terminator=None,
    ):
        super().__init__("codex")
        # Injection points keep startup cleanup fully testable without listing
        # or terminating processes on the developer/user machine.
        self._process_inventory = process_inventory
        self._process_verifier = process_verifier
        self._process_tree_terminator = process_tree_terminator

    @staticmethod
    def _query_windows_process_inventory() -> list[dict]:
        """Return a bounded Win32 process snapshot used for orphan detection."""
        if os.name != "nt":
            return []

        query = (
            "$ErrorActionPreference='Stop';"
            "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false);"
            "$items=@(Get-CimInstance Win32_Process | ForEach-Object {"
            "[pscustomobject]@{"
            "pid=[int]$_.ProcessId;"
            "parent_pid=[int]$_.ParentProcessId;"
            "name=[string]$_.Name;"
            "command_line=[string]$_.CommandLine;"
            "creation_time=if($null -eq $_.CreationDate){''}else{"
            "([datetime]$_.CreationDate).ToUniversalTime().ToString('o')}"
            "}});"
            "ConvertTo-Json -InputObject $items -Compress"
        )
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    query,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=MANAGED_HOOK_PROCESS_QUERY_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "timed out while listing Windows processes for Codex hook cleanup"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"could not list Windows processes for Codex hook cleanup: {exc}"
            ) from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(f"Windows process inventory failed: {detail}")
        raw = result.stdout.lstrip("\ufeff").strip()
        if not raw:
            return []
        try:
            records = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Windows process inventory returned invalid JSON") from exc
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            raise RuntimeError("Windows process inventory returned an invalid shape")
        return [record for record in records if isinstance(record, dict)]

    @staticmethod
    def _query_windows_process_by_id(process_id: int) -> dict | None:
        """Re-read one PID with a hard timeout immediately before taskkill."""
        if os.name != "nt":
            return None
        try:
            normalized_process_id = int(process_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid process ID for Codex hook verification: {process_id}") from exc
        if normalized_process_id <= 0:
            raise RuntimeError(f"invalid process ID for Codex hook verification: {process_id}")

        query = (
            "$ErrorActionPreference='Stop';"
            "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false);"
            f"$item=Get-CimInstance Win32_Process -Filter \"ProcessId = {normalized_process_id}\" "
            "| Select-Object -First 1;"
            "if($null -eq $item){'null'}else{"
            "$record=[pscustomobject]@{"
            "pid=[int]$item.ProcessId;"
            "parent_pid=[int]$item.ParentProcessId;"
            "name=[string]$item.Name;"
            "command_line=[string]$item.CommandLine;"
            "creation_time=if($null -eq $item.CreationDate){''}else{"
            "([datetime]$item.CreationDate).ToUniversalTime().ToString('o')}"
            "};"
            "ConvertTo-Json -InputObject $record -Compress}"
        )
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    query,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=MANAGED_HOOK_PROCESS_QUERY_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"timed out while verifying Codex hook PID {normalized_process_id}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"could not verify Codex hook PID {normalized_process_id}: {exc}"
            ) from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(
                f"Windows process verification failed for PID "
                f"{normalized_process_id}: {detail}"
            )
        raw = result.stdout.lstrip("\ufeff").strip()
        if not raw or raw == "null":
            return None
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Windows process verification returned invalid JSON for PID "
                f"{normalized_process_id}"
            ) from exc
        return record if isinstance(record, dict) else None

    @staticmethod
    def _terminate_windows_process_tree(process_id: int) -> bool:
        """Run ``taskkill /T /F`` with a hard timeout for one verified PID."""
        try:
            result = subprocess.run(
                ["taskkill.exe", "/PID", str(int(process_id)), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=MANAGED_HOOK_TASKKILL_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "taskkill timed out while cleaning orphaned Codex hook PID %s",
                process_id,
            )
            return False
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not terminate orphaned Codex hook PID %s: %s",
                process_id,
                exc,
            )
            return False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            logger.warning(
                "taskkill failed for orphaned Codex hook PID %s: %s",
                process_id,
                detail,
            )
            return False
        return True

    @staticmethod
    def _normalize_process_record(record) -> dict | None:
        if not isinstance(record, dict):
            return None
        try:
            process_id = int(record.get("pid", record.get("ProcessId", 0)))
            parent_id = int(record.get("parent_pid", record.get("ParentProcessId", 0)))
        except (TypeError, ValueError):
            return None
        if process_id <= 0:
            return None
        name = ntpath.basename(
            str(record.get("name", record.get("Name", "")) or "")
        ).casefold()
        command_line = str(
            record.get("command_line", record.get("CommandLine", "")) or ""
        )
        creation_time = str(
            record.get("creation_time", record.get("CreationTime", "")) or ""
        ).strip()
        return {
            "pid": process_id,
            "parent_pid": parent_id,
            "name": name,
            "command_line": command_line,
            "creation_time": creation_time,
        }

    def cleanup_orphaned_managed_hook_processes(self) -> list[int]:
        """Terminate only orphaned PowerShell trees running our exact scripts.

        A matching process is considered orphaned only when its recorded parent
        is absent from the same process snapshot. Active hooks whose ``cmd.exe``
        wrapper is still alive, interactive user PowerShell sessions, relative
        script paths, and same-named scripts elsewhere are deliberately ignored.
        """
        inventory = self._process_inventory
        if inventory is None:
            if os.name != "nt":
                return []
            inventory = self._query_windows_process_inventory
        verifier = self._process_verifier
        if verifier is None and self._process_inventory is None:
            verifier = self._query_windows_process_by_id
        terminator = (
            self._process_tree_terminator
            or self._terminate_windows_process_tree
        )

        try:
            records = inventory()
        except Exception as exc:
            logger.warning(
                "Could not inspect orphaned Codex hook processes: %s; "
                "continuing hook migration without orphan cleanup",
                exc,
            )
            return []
        if not isinstance(records, (list, tuple)):
            logger.warning(
                "Codex hook process inventory returned an invalid shape; "
                "continuing hook migration without orphan cleanup"
            )
            return []

        normalized: list[dict] = []
        for record in records:
            normalized_record = self._normalize_process_record(record)
            if normalized_record is not None:
                normalized.append(normalized_record)

        live_process_ids = {record["pid"] for record in normalized}
        candidates: list[dict] = []
        for record in normalized:
            process_id = record["pid"]
            parent_id = record["parent_pid"]
            name = record["name"]
            command_line = record["command_line"]
            if process_id == os.getpid():
                continue
            if name not in {
                "powershell",
                "powershell.exe",
                "pwsh",
                "pwsh.exe",
            }:
                continue
            if not self._is_owned_hook_command(command_line):
                continue
            if parent_id > 0 and parent_id in live_process_ids:
                continue
            target = _powershell_file_target(command_line)
            if not target or not record["creation_time"]:
                logger.warning(
                    "Skipping unverifiable orphaned Codex hook PID %s",
                    process_id,
                )
                continue
            candidates.append({
                **record,
                "target": _normalize_windows_hook_path(target),
            })

        terminated: list[int] = []
        seen_process_ids = set()
        for candidate in sorted(candidates, key=lambda value: value["pid"]):
            process_id = candidate["pid"]
            if process_id in seen_process_ids:
                continue
            seen_process_ids.add(process_id)

            if verifier is None:
                logger.warning(
                    "Skipping orphaned Codex hook PID %s because no independent "
                    "process verifier is available",
                    process_id,
                )
                continue
            try:
                verified = self._normalize_process_record(verifier(process_id))
            except Exception as exc:
                logger.warning(
                    "Could not re-verify orphaned Codex hook PID %s: %s",
                    process_id,
                    exc,
                )
                continue
            if verified is None:
                logger.info(
                    "Orphaned Codex hook PID %s exited before cleanup",
                    process_id,
                )
                continue
            verified_target = _powershell_file_target(verified["command_line"])
            identity_is_current = bool(
                verified["pid"] == process_id
                and verified["name"] == candidate["name"]
                and verified["creation_time"] == candidate["creation_time"]
                and verified_target
                and self._is_owned_hook_command(verified["command_line"])
                and _normalize_windows_hook_path(verified_target) == candidate["target"]
            )
            if not identity_is_current:
                logger.warning(
                    "Skipping orphaned Codex hook PID %s because its identity changed",
                    process_id,
                )
                continue
            try:
                succeeded = bool(terminator(process_id))
            except Exception as exc:
                logger.warning(
                    "Could not terminate orphaned Codex hook PID %s: %s",
                    process_id,
                    exc,
                )
                continue
            if succeeded:
                terminated.append(process_id)
                logger.info("Terminated orphaned managed Codex hook PID %s", process_id)
            else:
                logger.warning(
                    "Orphaned managed Codex hook PID %s could not be terminated",
                    process_id,
                )
        return terminated

    def get_config_dir(self) -> Path:
        """Get Codex config directory."""
        import os
        codex_home = os.environ.get("CODEX_HOME")
        if codex_home:
            return Path(codex_home)
        return Path.home() / ".codex"

    def get_hook_script_path(self) -> Path:
        return self.get_config_dir() / "hooks" / "auto_continue_stop.ps1"

    def get_error_recovery_script_path(self) -> Path:
        """获取错误恢复脚本路径"""
        return self.get_config_dir() / "hooks" / "error_recovery.ps1"

    def get_settings_path(self) -> Path:
        return self.get_config_dir() / "auto_continue_settings.json"

    def get_hooks_json_path(self) -> Path:
        return self.get_config_dir() / "hooks.json"

    def get_config_toml_path(self) -> Path:
        return self.get_config_dir() / "config.toml"

    def get_hooks_feature_state_path(self) -> Path:
        return self.get_config_dir() / CODEX_HOOKS_FEATURE_STATE_FILE

    def get_agents_md_path(self) -> Path:
        return self.get_config_dir() / "AGENTS.md"

    @staticmethod
    def _powershell_hook_command(script_path: Path) -> str:
        # json.dumps performs JSON escaping. Keeping the in-memory path native
        # avoids the doubled separators written by older API Switcher builds.
        return (
            'powershell.exe -NoProfile -ExecutionPolicy Bypass '
            f'-File "{script_path}"'
        )

    def _is_owned_auto_continue_command(self, command: str) -> bool:
        return _command_targets_script(command, self.get_hook_script_path())

    def _is_owned_error_recovery_command(self, command: str) -> bool:
        return _command_targets_script(command, self.get_error_recovery_script_path())

    def _is_owned_hook_command(self, command: str) -> bool:
        return (
            self._is_owned_auto_continue_command(command)
            or self._is_owned_error_recovery_command(command)
        )

    @staticmethod
    def _required_auto_continue_events(
        settings: AutoContinueSettings | None,
    ) -> tuple[str, ...]:
        if settings is None:
            # Preserve register_hook()'s historical explicit-install behavior.
            return CodexProvider.AUTO_CONTINUE_EVENTS
        needs_stop = bool(settings.enabled or settings.training_auto_continue_enabled)
        needs_prompt = bool(
            needs_stop
            or (settings.git_auto_snapshot and settings.git_snapshot_on_start)
        )
        required = []
        if needs_stop:
            required.append("Stop")
        if needs_prompt:
            required.extend(["UserPromptSubmit", "SessionStart"])
        return tuple(required)

    @staticmethod
    def _event_hook_definitions(data: dict, event_name: str) -> list[dict]:
        hooks = _codex_hooks_container(data)
        candidates = _codex_event_hooks(hooks.get(event_name))
        candidates.extend(_codex_event_hooks(data.get(event_name)))
        return candidates

    def _event_registration_is_current(
        self,
        data: dict,
        event_name: str,
        expected: dict | None,
        is_owned,
    ) -> bool:
        owned = [
            hook
            for hook in self._event_hook_definitions(data, event_name)
            if is_owned(str(hook.get("command", "")))
        ]
        if expected is None:
            return not owned
        return len(owned) == 1 and all(
            owned[0].get(key) == expected[key]
            for key in ("type", "command", "timeout")
        )

    def _auto_continue_registration_definitions_are_current(
        self,
        data: dict,
        settings: AutoContinueSettings | None,
    ) -> bool:
        required_events = set(self._required_auto_continue_events(settings))
        expected = self._auto_continue_hook_definition()
        expected_events_are_current = all(
            self._event_registration_is_current(
                data,
                event_name,
                expected if event_name in required_events else None,
                self._is_owned_auto_continue_command,
            )
            for event_name in self.AUTO_CONTINUE_EVENTS
        )
        return bool(
            expected_events_are_current
            and not (
                _codex_owned_event_names(
                    data,
                    self._is_owned_auto_continue_command,
                )
                - required_events
            )
        )

    def _error_recovery_registration_definitions_are_current(
        self,
        data: dict,
        *,
        enabled: bool,
    ) -> bool:
        expected = self._error_recovery_hook_definition() if enabled else None
        expected_events_are_current = all(
            self._event_registration_is_current(
                data,
                event_name,
                expected,
                self._is_owned_error_recovery_command,
            )
            for event_name in self.ERROR_RECOVERY_EVENTS
        )
        expected_events = set(self.ERROR_RECOVERY_EVENTS) if enabled else set()
        return bool(
            expected_events_are_current
            and not (
                _codex_owned_event_names(
                    data,
                    self._is_owned_error_recovery_command,
                )
                - expected_events
            )
        )

    def _auto_continue_hook_definition(self) -> dict:
        return {
            "type": "command",
            "command": self._powershell_hook_command(self.get_hook_script_path()),
            "timeout": AUTO_CONTINUE_HOOK_TIMEOUT_SECONDS,
        }

    def _error_recovery_hook_definition(self) -> dict:
        return {
            "type": "command",
            "command": self._powershell_hook_command(self.get_error_recovery_script_path()),
            "timeout": ERROR_RECOVERY_HOOK_TIMEOUT_SECONDS,
        }

    def is_hook_registered(self) -> bool:
        """Return true only for a complete, settings-matched installation."""
        if not self._codex_hooks_feature_enabled():
            return False
        settings = self.load_settings()
        if settings is None or not self._settings_require_hook(settings):
            return False
        hooks_path = self.get_hooks_json_path()
        if not hooks_path.exists():
            return False
        data = _read_codex_hooks_json(hooks_path)
        if not isinstance(data, dict):
            return False

        registration_is_current = (
            self._auto_continue_registration_definitions_are_current(data, settings)
        )
        if not registration_is_current:
            return False
        script_path = self.get_hook_script_path()
        return self._script_content_is_current(
            script_path,
            self._render_hook_script(settings),
        )

    def register_hook(self, settings=None) -> None:
        """Register hook in hooks.json."""
        hooks_path = self.get_hooks_json_path()
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        snapshots = _snapshot_local_files([
            hooks_path,
            self.get_config_toml_path(),
            self.get_hooks_feature_state_path(),
        ])

        try:
            self._validate_codex_hooks_config()

            # Read existing hooks. If the file is corrupt, keep a backup before
            # rebuilding it so repair never destroys the only copy.
            data = _read_codex_hooks_json(hooks_path, recover=True)
            if not isinstance(data, dict):
                raise RuntimeError("Codex hooks.json could not be read safely")
            hooks = _codex_hooks_container(
                data,
                migrate_legacy=True,
                is_managed=self._is_owned_hook_command,
            )
            _remove_owned_codex_hooks_from_all_events(
                data,
                self._is_owned_auto_continue_command,
            )

            hook_def = {
                **self._auto_continue_hook_definition(),
                "statusMessage": "Checking whether Codex should continue",
            }
            git_snapshot_on_start = (
                True
                if settings is None
                else bool(settings.git_auto_snapshot and settings.git_snapshot_on_start)
            )
            required_events = set(self._required_auto_continue_events(settings))
            needs_stop_hook = "Stop" in required_events
            needs_prompt_hooks = "UserPromptSubmit" in required_events
            if needs_stop_hook:
                _upsert_codex_event_hook(
                    hooks,
                    "Stop",
                    hook_def,
                    self._is_owned_auto_continue_command,
                )
            else:
                _remove_codex_event_hook(
                    hooks,
                    "Stop",
                    self._is_owned_auto_continue_command,
                )

            if needs_prompt_hooks:
                prompt_hook = dict(hook_def)
                prompt_hook["statusMessage"] = (
                    "Creating Git snapshot before Codex starts work"
                    if git_snapshot_on_start
                    else "Starting a new Codex auto-continue chain"
                )
                _upsert_codex_event_hook(
                    hooks,
                    "UserPromptSubmit",
                    prompt_hook,
                    self._is_owned_auto_continue_command,
                )
                session_hook = dict(hook_def)
                session_hook["statusMessage"] = (
                    "Creating Git snapshot when Codex session starts"
                    if git_snapshot_on_start
                    else "Resetting Codex auto-continue state for this session"
                )
                _upsert_codex_event_hook(
                    hooks,
                    "SessionStart",
                    session_hook,
                    self._is_owned_auto_continue_command,
                )
            else:
                _remove_codex_event_hook(
                    hooks,
                    "UserPromptSubmit",
                    self._is_owned_auto_continue_command,
                )
                _remove_codex_event_hook(
                    hooks,
                    "SessionStart",
                    self._is_owned_auto_continue_command,
                )

            atomic_write_text(hooks_path, json.dumps(data, indent=2, ensure_ascii=False))
            self._enable_codex_hooks()
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            if rollback_error:
                raise RuntimeError(
                    f"Failed to register Codex hooks: {exc}; rollback failed: {rollback_error}"
                ) from exc
            raise

    def unregister_hook(self) -> None:
        """Unregister hook from hooks.json."""
        hooks_path = self.get_hooks_json_path()
        snapshots = _snapshot_local_files([
            hooks_path,
            self.get_config_toml_path(),
            self.get_hooks_feature_state_path(),
        ])
        if not hooks_path.exists():
            if self.get_hooks_feature_state_path().exists():
                self._set_codex_hooks_enabled(False)
            return

        try:
            data = _read_codex_hooks_json(hooks_path)
            if not isinstance(data, dict):
                raise RuntimeError(
                    "Codex hooks.json is invalid; managed hooks were not removed"
                )
            _codex_hooks_container(
                data,
                migrate_legacy=True,
                is_managed=self._is_owned_hook_command,
            )

            changed = _remove_owned_codex_hooks_from_all_events(
                data,
                self._is_owned_auto_continue_command,
            )

            if changed:
                atomic_write_text(hooks_path, json.dumps(data, indent=2, ensure_ascii=False))

            if not _codex_data_has_managed_entries(data, self._is_owned_hook_command):
                if _codex_data_has_entries(data):
                    self._release_codex_hooks_feature_ownership()
                else:
                    self._set_codex_hooks_enabled(False)
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(f"Failed to unregister Codex hooks: {exc}{detail}") from exc

    def _render_hook_script(self, settings: AutoContinueSettings | None) -> str:
        enable_git = (
            bool(settings.git_auto_snapshot and settings.git_snapshot_on_start)
            if settings else True
        )
        settings_path = str(self.get_settings_path()).replace("\\", "\\\\")
        return generate_hook_script(settings_path, enable_git, provider_name="codex")

    def _render_error_recovery_script(
        self,
        settings: AutoContinueSettings | None,
    ) -> str:
        enable_git = (
            bool(settings.git_auto_snapshot and settings.git_snapshot_on_recovery)
            if settings else True
        )
        settings_path = str(self.get_settings_path()).replace("\\", "\\\\")
        return generate_codex_error_recovery_script(settings_path, enable_git)

    def _orphan_scan_reasons(
        self,
        settings: AutoContinueSettings,
    ) -> list[str]:
        """Return local drift signals that justify the expensive process scan."""
        reasons = []
        main_script_path = self.get_hook_script_path()
        if (
            main_script_path.exists()
            and not self._script_content_is_current(
                main_script_path,
                self._render_hook_script(settings),
            )
        ):
            reasons.append("stale auto-continue script")

        recovery_script_path = self.get_error_recovery_script_path()
        if recovery_script_path.exists():
            if not settings.error_recovery_enabled:
                reasons.append("unexpected error-recovery script")
            elif not self._script_content_is_current(
                recovery_script_path,
                self._render_error_recovery_script(settings),
            ):
                reasons.append("stale error-recovery script")

        hooks_path = self.get_hooks_json_path()
        data = _read_codex_hooks_json(hooks_path)
        if isinstance(data, dict):
            has_auto_entries = _codex_data_has_managed_entries(
                data,
                self._is_owned_auto_continue_command,
            )
            if (
                has_auto_entries
                and not self._auto_continue_registration_definitions_are_current(
                    data,
                    settings,
                )
            ):
                reasons.append("stale auto-continue registration")

            has_recovery_entries = _codex_data_has_managed_entries(
                data,
                self._is_owned_error_recovery_command,
            )
            if (
                has_recovery_entries
                and not self._error_recovery_registration_definitions_are_current(
                    data,
                    enabled=bool(settings.error_recovery_enabled),
                )
            ):
                reasons.append("stale error-recovery registration")
        return reasons

    @staticmethod
    def _script_content_is_current(path: Path, expected: str) -> bool:
        try:
            return path.exists() and path.read_text(encoding="utf-8-sig") == expected
        except OSError:
            return False

    def _has_owned_hook_entries(self, is_owned) -> bool:
        hooks_path = self.get_hooks_json_path()
        data = _read_codex_hooks_json(hooks_path)
        if not isinstance(data, dict):
            raise RuntimeError(
                "Codex hooks.json is invalid; managed hooks were not inspected or removed"
            )
        return _codex_data_has_managed_entries(data, is_owned)

    def install_hook_script(self, settings: AutoContinueSettings | None = None) -> None:
        """Install the hook script."""
        script_path = self.get_hook_script_path()
        script_path.parent.mkdir(parents=True, exist_ok=True)

        resolved_settings = settings if settings is not None else self.load_settings()
        script_content = self._render_hook_script(resolved_settings)

        atomic_write_text(script_path, script_content, encoding='utf-8-sig')

        logger.info(f"Installed hook script: {script_path}")

    def ensure_current_installation(self, settings: AutoContinueSettings) -> bool:
        """Idempotently reconcile installed Codex hooks with persisted settings.

        All local files touched by either hook family are snapshotted as one
        transaction. This lets startup upgrade an old script or hook definition
        without leaving the other family partially updated if any write fails.
        """
        # A full Win32 process inventory is comparatively expensive. Only pay
        # that cost when local evidence proves an older managed installation is
        # about to be migrated; a fully current daily startup performs no scan.
        orphan_scan_reasons = self._orphan_scan_reasons(settings)
        if orphan_scan_reasons:
            logger.info(
                "Scanning for orphaned Codex hooks before migration: %s",
                ", ".join(orphan_scan_reasons),
            )
            terminated = self.cleanup_orphaned_managed_hook_processes()
            logger.info(
                "Codex orphan scan finished; terminated %s process tree(s)",
                len(terminated),
            )

        paths = [
            self.get_hook_script_path(),
            self.get_error_recovery_script_path(),
            self.get_hooks_json_path(),
            self.get_config_toml_path(),
            self.get_hooks_feature_state_path(),
        ]
        snapshots = _snapshot_local_files(paths)
        try:
            needs_auto_hook = self._settings_require_hook(settings)
            expected_script = self._render_hook_script(settings)
            script_is_current = self._script_content_is_current(
                self.get_hook_script_path(),
                expected_script,
            )
            if needs_auto_hook:
                if not script_is_current:
                    self.install_hook_script(settings=settings)
                if not self.is_hook_registered():
                    self.register_hook_for_settings(settings)
            else:
                # Pause/disable keeps the script on disk. Refresh an existing
                # old copy once so subsequent daily startups remain scan-free.
                if self.get_hook_script_path().exists() and not script_is_current:
                    self.install_hook_script(settings=settings)
                if (
                    self._has_owned_hook_entries(self._is_owned_auto_continue_command)
                    or self.get_hooks_feature_state_path().exists()
                ):
                    self.unregister_hook()

            if settings.error_recovery_enabled:
                expected_recovery_script = self._render_error_recovery_script(settings)
                if (
                    not self._script_content_is_current(
                        self.get_error_recovery_script_path(),
                        expected_recovery_script,
                    )
                    or not self.is_error_recovery_installed()
                ):
                    self.install_error_recovery()
            elif (
                self.get_error_recovery_script_path().exists()
                or self._has_owned_hook_entries(self._is_owned_error_recovery_command)
            ):
                self.uninstall_error_recovery()
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(
                f"Failed to reconcile Codex hook installation: {exc}{detail}"
            ) from exc

        return snapshots != _snapshot_local_files(paths)

    def disable_managed_hooks_for_invalid_settings(self) -> bool:
        """Stop managed hooks from running when persisted settings are invalid."""
        safe_settings = AutoContinueSettings(
            enabled=False,
            training_auto_continue_enabled=False,
            git_auto_snapshot=False,
            git_snapshot_on_start=False,
            error_recovery_enabled=False,
        )
        return self.ensure_current_installation(safe_settings)

    def uninstall_hook_script(self) -> None:
        """Remove the hook script."""
        script_path = self.get_hook_script_path()
        if script_path.exists():
            script_path.unlink()

    def _enable_codex_hooks(self) -> None:
        """Enable hooks in config.toml."""
        self._set_codex_hooks_enabled(True)

    @staticmethod
    def _parse_codex_toml(content: str) -> dict:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
            import tomli as tomllib
        parsed = tomllib.loads(content) if content.strip() else {}
        if not isinstance(parsed, dict):
            raise ValueError("Codex config.toml root must be a table")
        return parsed

    def _validate_codex_hooks_config(self) -> dict:
        config_path = self.get_config_toml_path()
        if not config_path.exists():
            return {}
        try:
            return self._parse_codex_toml(config_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise RuntimeError(f"Invalid Codex config.toml; hooks were not changed: {exc}") from exc

    def _load_codex_hooks_feature_ownership(self) -> dict | None:
        state_path = self.get_hooks_feature_state_path()
        if not state_path.exists():
            return None
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or not isinstance(payload.get("original_enabled"), bool):
                raise ValueError("missing boolean original_enabled")
            return payload
        except Exception as exc:
            raise RuntimeError(f"Invalid Codex hooks feature ownership state: {exc}") from exc

    def _release_codex_hooks_feature_ownership(self) -> None:
        try:
            self.get_hooks_feature_state_path().unlink()
        except FileNotFoundError:
            pass

    def _codex_hooks_feature_enabled(self) -> bool:
        """Read canonical and legacy Codex hook feature flags."""
        config_path = self.get_config_toml_path()
        if not config_path.exists():
            return False
        try:
            config = self._parse_codex_toml(config_path.read_text(encoding="utf-8-sig"))
            return _codex_hooks_enabled_from_config(config)
        except Exception as e:
            logger.error(f"Failed to read Codex hooks feature flag: {e}")
            return False

    def _set_codex_hooks_enabled(self, enabled: bool) -> None:
        """Set hooks while retaining the feature state that existed before install."""
        config_path = self.get_config_toml_path()
        state_path = self.get_hooks_feature_state_path()
        if not enabled and not state_path.exists():
            return

        snapshots = _snapshot_local_files([config_path, state_path])
        try:
            config = self._validate_codex_hooks_config()
            ownership = self._load_codex_hooks_feature_ownership()
            if enabled and ownership is None:
                ownership = {
                    "version": 1,
                    "original_enabled": _codex_hooks_enabled_from_config(config),
                }
                atomic_write_text(
                    state_path,
                    json.dumps(ownership, indent=2, ensure_ascii=False),
                )
            elif not enabled and ownership is None:
                return

            target = True if enabled else bool(ownership["original_enabled"])
            if not config_path.exists() and not enabled:
                self._release_codex_hooks_feature_ownership()
                return

            original_text = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else ""
            lines, changed = _set_codex_hooks_feature_lines(original_text.splitlines(), target)
            candidate = "\n".join(lines).rstrip() + "\n"
            parsed_candidate = self._parse_codex_toml(candidate)
            if _codex_hooks_enabled_from_config(parsed_candidate) is not target:
                raise RuntimeError("Could not safely update the canonical [features].hooks flag")
            if changed:
                atomic_write_text(config_path, candidate)
            if not enabled:
                self._release_codex_hooks_feature_ownership()
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(f"Failed to update Codex hooks feature flag: {exc}{detail}") from exc

    def install_guidance(self) -> None:
        """Install guidance in AGENTS.md."""
        agents_md = self.get_agents_md_path()
        agents_md.parent.mkdir(parents=True, exist_ok=True)

        guidance = """
# Auto-Continue Guidance

Before providing your final response, check if the task is truly complete:
- Are there any remaining TODOs or unfinished work?
- Have all tests been run and passed?
- Has verification been completed?
- Are there any follow-up steps mentioned?

If work remains incomplete, continue working on it rather than stopping.
Only stop when you encounter a genuine blocker that requires user input or decision.
"""

        # Read existing content
        existing = ""
        if agents_md.exists():
            existing = agents_md.read_text(encoding='utf-8')

        # Check if guidance already exists
        if "Auto-Continue Guidance" not in existing:
            content = existing
            if content and not content.endswith('\n'):
                content += '\n\n'
            content += guidance
            atomic_write_text(agents_md, content)

    def uninstall_guidance(self) -> None:
        """Remove guidance from AGENTS.md."""
        agents_md = self.get_agents_md_path()
        if not agents_md.exists():
            return

        content = agents_md.read_text(encoding='utf-8')
        # Remove the guidance section
        lines = content.split('\n')
        filtered = []
        skip = False
        for line in lines:
            if "Auto-Continue Guidance" in line:
                skip = True
            elif skip and line.startswith('#'):
                skip = False
            if not skip:
                filtered.append(line)

        atomic_write_text(agents_md, '\n'.join(filtered))

    def install_error_recovery(self) -> None:
        """安装错误恢复 Hook"""
        script_path = self.get_error_recovery_script_path()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        snapshots = _snapshot_local_files([
            script_path,
            self.get_hooks_json_path(),
            self.get_config_toml_path(),
            self.get_hooks_feature_state_path(),
        ])

        try:
            settings = self.load_settings()
            script_content = self._render_error_recovery_script(settings)
            atomic_write_text(script_path, script_content, encoding='utf-8-sig')
            logger.info(f"Installed Codex error recovery script: {script_path}")
            self._register_error_recovery_hook()
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(f"Failed to install Codex error recovery: {exc}{detail}") from exc

    def _register_error_recovery_hook(self) -> None:
        """注册错误恢复 Hook 到 hooks.json"""
        hooks_path = self.get_hooks_json_path()
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        snapshots = _snapshot_local_files([
            hooks_path,
            self.get_config_toml_path(),
            self.get_hooks_feature_state_path(),
        ])

        try:
            self._validate_codex_hooks_config()
            data = _read_codex_hooks_json(hooks_path, recover=True)
            if not isinstance(data, dict):
                raise RuntimeError("Codex hooks.json could not be read safely")
            hooks = _codex_hooks_container(
                data,
                migrate_legacy=True,
                is_managed=self._is_owned_hook_command,
            )
            _remove_owned_codex_hooks_from_all_events(
                data,
                self._is_owned_error_recovery_command,
            )

            # Register both event names. Older Codex builds used Error, while newer
            # hook payloads and the settings UI refer to ResponseError.
            hook_def = {
                **self._error_recovery_hook_definition(),
                "statusMessage": "Checking for Codex API errors and auto-recovery",
            }
            for event_name in self.ERROR_RECOVERY_EVENTS:
                _upsert_codex_event_hook(
                    hooks,
                    event_name,
                    hook_def,
                    self._is_owned_error_recovery_command,
                )

            atomic_write_text(hooks_path, json.dumps(data, indent=2, ensure_ascii=False))
            self._enable_codex_hooks()
            logger.info("Registered Codex error recovery hook")
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            if rollback_error:
                raise RuntimeError(
                    f"Failed to register Codex error recovery hook: {exc}; "
                    f"rollback failed: {rollback_error}"
                ) from exc
            raise

    def uninstall_error_recovery(self) -> None:
        """卸载错误恢复功能"""
        script_path = self.get_error_recovery_script_path()
        hooks_path = self.get_hooks_json_path()
        snapshots = _snapshot_local_files([
            script_path,
            hooks_path,
            self.get_config_toml_path(),
            self.get_hooks_feature_state_path(),
        ])

        try:
            if script_path.exists():
                script_path.unlink()
            if not hooks_path.exists():
                if self.get_hooks_feature_state_path().exists():
                    self._set_codex_hooks_enabled(False)
                return

            data = _read_codex_hooks_json(hooks_path)
            if not isinstance(data, dict):
                raise RuntimeError("Codex hooks.json is invalid; recovery hook was not removed")
            _codex_hooks_container(
                data,
                migrate_legacy=True,
                is_managed=self._is_owned_hook_command,
            )

            changed = _remove_owned_codex_hooks_from_all_events(
                data,
                self._is_owned_error_recovery_command,
            )

            if changed:
                atomic_write_text(hooks_path, json.dumps(data, indent=2, ensure_ascii=False))

            if not _codex_data_has_managed_entries(data, self._is_owned_hook_command):
                if _codex_data_has_entries(data):
                    self._release_codex_hooks_feature_ownership()
                else:
                    self._set_codex_hooks_enabled(False)

            logger.info("Uninstalled Codex error recovery hook")
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(f"Failed to uninstall Codex error recovery: {exc}{detail}") from exc

    def is_error_recovery_installed(self) -> bool:
        """Return true only for settings-enabled, exact recovery installation."""
        if not self._codex_hooks_feature_enabled():
            return False
        settings = self.load_settings()
        if settings is None or not settings.error_recovery_enabled:
            return False
        script_path = self.get_error_recovery_script_path()
        if not self._script_content_is_current(
            script_path,
            self._render_error_recovery_script(settings),
        ):
            return False

        hooks_path = self.get_hooks_json_path()
        if not hooks_path.exists():
            return False

        try:
            data = _read_codex_hooks_json(hooks_path)
            if not isinstance(data, dict):
                return False

            return self._error_recovery_registration_definitions_are_current(
                data,
                enabled=True,
            )
        except Exception:
            return False

    def get_status(self):
        """Get status with error recovery check."""
        status = super().get_status()
        settings = self.load_settings()
        issues = []

        if settings is not None and self._settings_require_hook(settings):
            expected_script = self._render_hook_script(settings)
            if not self._script_content_is_current(
                self.get_hook_script_path(),
                expected_script,
            ):
                status.hook_registered = False
                issues.append("Codex auto-continue hook script is missing or stale")

        status.error_recovery_installed = self.is_error_recovery_installed()
        if settings is not None and settings.error_recovery_enabled:
            expected_recovery_script = self._render_error_recovery_script(settings)
            if not self._script_content_is_current(
                self.get_error_recovery_script_path(),
                expected_recovery_script,
            ):
                status.error_recovery_installed = False
                issues.append("Codex error-recovery hook script is missing or stale")

        if issues:
            if status.last_error:
                issues.insert(0, status.last_error)
            status.last_error = "; ".join(issues)
        return status
