import json
import logging
import re
from datetime import datetime
from pathlib import Path
from core.atomic_io import atomic_write_bytes, atomic_write_text
from core.auto_continue.base import AutoContinueProvider
from core.auto_continue.script_generator import generate_hook_script
from core.auto_continue.error_recovery_script import generate_codex_error_recovery_script
from models.auto_continue import AutoContinueSettings

logger = logging.getLogger(__name__)
AUTO_CONTINUE_HOOK_TIMEOUT_SECONDS = 30
CODEX_HOOKS_FEATURE_STATE_FILE = "auto_continue_codex_hooks_feature_state.json"


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


def _is_managed_codex_hook(command: str) -> bool:
    return "auto_continue_stop.ps1" in command or "error_recovery.ps1" in command


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


def _codex_hooks_container(data: dict, *, migrate_legacy: bool = False) -> dict:
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
                _is_managed_codex_hook,
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


def _codex_data_has_managed_entries(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    hooks = _codex_hooks_container(data)
    values = list(hooks.values())
    values.extend(value for key, value in data.items() if key != "hooks")
    return any(
        _is_managed_codex_hook(str(hook.get("command", "")))
        for value in values
        for hook in _codex_event_hooks(value)
    )


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


def _upsert_codex_event_hook(hooks: dict, event_name: str, hook_def: dict, marker: str) -> None:
    remaining, _managed, _removed = _partition_codex_event_value(
        hooks.get(event_name),
        lambda command: marker in command,
    )
    items = _canonical_codex_event_items(remaining)
    items.append({"hooks": [hook_def]})
    hooks[event_name] = items


def _remove_codex_event_hook(hooks: dict, event_name: str, marker: str) -> bool:
    if event_name not in hooks:
        return False
    remaining, _managed, removed = _partition_codex_event_value(
        hooks.get(event_name),
        lambda command: marker in command,
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
    return {
        path: path.read_bytes() if path.exists() else None
        for path in dict.fromkeys(paths)
    }


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
                atomic_write_bytes(path, content)
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    return "; ".join(errors)


def _read_codex_hooks_json(path: Path, *, recover: bool = False) -> dict | None:
    if not path.exists():
        return {}

    try:
        raw = path.read_text(encoding="utf-8-sig")
        if not raw.strip():
            return {}
        data = json.loads(raw)
    except Exception as e:
        if recover:
            _backup_codex_hooks_file(path, f"invalid JSON: {e}")
            return {}
        logger.error(f"Failed to read hooks.json: {e}")
        return None

    if not isinstance(data, dict):
        reason = f"expected object, got {type(data).__name__}"
        if recover:
            _backup_codex_hooks_file(path, reason)
            return {}
        logger.error(f"Invalid hooks.json: {reason}")
        return None

    return data


class CodexProvider(AutoContinueProvider):
    """Auto-continue provider for Codex CLI."""

    ERROR_RECOVERY_EVENTS = ("Error", "ResponseError")

    def __init__(self):
        super().__init__("codex")

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

    def is_hook_registered(self) -> bool:
        """Check if hook is registered in hooks.json."""
        if not self._codex_hooks_feature_enabled():
            return False
        hooks_path = self.get_hooks_json_path()
        if not hooks_path.exists():
            return False
        data = _read_codex_hooks_json(hooks_path)
        if not isinstance(data, dict):
            return False

        settings = self.load_settings() or AutoContinueSettings()
        git_snapshot_on_start = bool(settings.git_auto_snapshot and settings.git_snapshot_on_start)
        needs_stop_hook = bool(settings.enabled or settings.training_auto_continue_enabled or git_snapshot_on_start)
        needs_prompt_hooks = bool(
            git_snapshot_on_start
            or settings.enabled
            or settings.training_auto_continue_enabled
        )

        required_events = []
        if needs_stop_hook:
            required_events.append("Stop")
        if needs_prompt_hooks:
            required_events.extend(["UserPromptSubmit", "SessionStart"])

        if required_events:
            return all(
                _codex_event_has_command(data, event_name, "auto_continue_stop.ps1")
                for event_name in required_events
            )

        return any(
            _codex_event_has_command(data, event_name, "auto_continue_stop.ps1")
            for event_name in ("Stop", "UserPromptSubmit", "SessionStart")
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
            data = _read_codex_hooks_json(hooks_path, recover=True) or {}
            hooks = _codex_hooks_container(data, migrate_legacy=True)

            script_path = str(self.get_hook_script_path()).replace("\\", "\\\\")
            hook_def = {
                "type": "command",
                "command": f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
                "timeout": AUTO_CONTINUE_HOOK_TIMEOUT_SECONDS,
                "statusMessage": "Checking whether Codex should continue"
            }
            git_snapshot_on_start = (
                True
                if settings is None
                else bool(settings.git_auto_snapshot and settings.git_snapshot_on_start)
            )
            needs_stop_hook = (
                True
                if settings is None
                else bool(settings.enabled or settings.training_auto_continue_enabled or git_snapshot_on_start)
            )
            needs_prompt_hooks = bool(
                git_snapshot_on_start
                or settings is None
                or settings.enabled
                or settings.training_auto_continue_enabled
            )
            if needs_stop_hook:
                _upsert_codex_event_hook(hooks, "Stop", hook_def, "auto_continue_stop.ps1")
            else:
                _remove_codex_event_hook(hooks, "Stop", "auto_continue_stop.ps1")

            if needs_prompt_hooks:
                prompt_hook = dict(hook_def)
                prompt_hook["statusMessage"] = (
                    "Creating Git snapshot before Codex starts work"
                    if git_snapshot_on_start
                    else "Starting a new Codex auto-continue chain"
                )
                _upsert_codex_event_hook(hooks, "UserPromptSubmit", prompt_hook, "auto_continue_stop.ps1")
                session_hook = dict(hook_def)
                session_hook["statusMessage"] = (
                    "Creating Git snapshot when Codex session starts"
                    if git_snapshot_on_start
                    else "Resetting Codex auto-continue state for this session"
                )
                _upsert_codex_event_hook(hooks, "SessionStart", session_hook, "auto_continue_stop.ps1")
            else:
                _remove_codex_event_hook(hooks, "UserPromptSubmit", "auto_continue_stop.ps1")
                _remove_codex_event_hook(hooks, "SessionStart", "auto_continue_stop.ps1")

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
                return
            hooks = _codex_hooks_container(data, migrate_legacy=True)

            # Remove stop/prompt hooks if they are ours.
            changed = _remove_codex_event_hook(hooks, "Stop", "auto_continue_stop.ps1")
            changed = _remove_codex_event_hook(hooks, "UserPromptSubmit", "auto_continue_stop.ps1") or changed
            changed = _remove_codex_event_hook(hooks, "SessionStart", "auto_continue_stop.ps1") or changed

            if changed:
                atomic_write_text(hooks_path, json.dumps(data, indent=2, ensure_ascii=False))

            if not _codex_data_has_managed_entries(data):
                if _codex_data_has_entries(data):
                    self._release_codex_hooks_feature_ownership()
                else:
                    self._set_codex_hooks_enabled(False)
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(f"Failed to unregister Codex hooks: {exc}{detail}") from exc

    def install_hook_script(self) -> None:
        """Install the hook script."""
        script_path = self.get_hook_script_path()
        script_path.parent.mkdir(parents=True, exist_ok=True)

        # 加载设置以检查是否启用git
        settings = self.load_settings()
        enable_git = (
            bool(settings.git_auto_snapshot and settings.git_snapshot_on_start)
            if settings else True
        )

        settings_path = str(self.get_settings_path()).replace("\\", "\\\\")
        script_content = generate_hook_script(settings_path, enable_git, provider_name="codex")

        atomic_write_text(script_path, script_content, encoding='utf-8-sig')

        logger.info(f"Installed hook script: {script_path}")

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
            enable_git = (
                bool(settings.git_auto_snapshot and settings.git_snapshot_on_recovery)
                if settings else True
            )
            settings_path = str(self.get_settings_path()).replace("\\", "\\\\")
            script_content = generate_codex_error_recovery_script(settings_path, enable_git)
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
            data = _read_codex_hooks_json(hooks_path, recover=True) or {}
            hooks = _codex_hooks_container(data, migrate_legacy=True)

            # Register both event names. Older Codex builds used Error, while newer
            # hook payloads and the settings UI refer to ResponseError.
            script_path = str(self.get_error_recovery_script_path()).replace("\\", "\\\\")
            hook_def = {
                "type": "command",
                "command": f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
                "timeout": 10,
                "statusMessage": "Checking for Codex API errors and auto-recovery"
            }
            for event_name in self.ERROR_RECOVERY_EVENTS:
                _upsert_codex_event_hook(hooks, event_name, hook_def, "error_recovery.ps1")

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
            hooks = _codex_hooks_container(data, migrate_legacy=True)

            changed = False
            for event_name in self.ERROR_RECOVERY_EVENTS:
                changed = _remove_codex_event_hook(hooks, event_name, "error_recovery.ps1") or changed

            if changed:
                atomic_write_text(hooks_path, json.dumps(data, indent=2, ensure_ascii=False))

            if not _codex_data_has_managed_entries(data):
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
        """检查错误恢复是否已安装"""
        if not self._codex_hooks_feature_enabled():
            return False
        script_path = self.get_error_recovery_script_path()
        if not script_path.exists():
            return False

        hooks_path = self.get_hooks_json_path()
        if not hooks_path.exists():
            return False

        try:
            data = _read_codex_hooks_json(hooks_path)
            if not isinstance(data, dict):
                return False

            return _codex_events_have_command(data, self.ERROR_RECOVERY_EVENTS, "error_recovery.ps1")
        except Exception:
            return False

    def get_status(self):
        """Get status with error recovery check."""
        status = super().get_status()
        status.error_recovery_installed = self.is_error_recovery_installed()
        return status
