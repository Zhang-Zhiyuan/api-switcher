import json
import logging
import ntpath
import os
import re
import shlex
from datetime import datetime
from pathlib import Path
from core.atomic_io import atomic_write_bytes, atomic_write_text
from core.auto_continue.base import AutoContinueProvider, SettingsLoadStatus
from core.auto_continue.permission_rules import (
    apply_managed_permission_rules,
    ask_rules_from_payload,
    conflicting_permission_rules,
    missing_allow_rules,
    permission_rules_from_auto_settings,
    rules_from_payload,
    rules_payload,
)
from core.auto_continue.script_generator import generate_hook_script
from core.auto_continue.error_recovery_script import generate_error_recovery_script
from models.auto_continue import AutoContinueSettings

logger = logging.getLogger(__name__)
AUTO_CONTINUE_HOOK_TIMEOUT_SECONDS = 30
ERROR_RECOVERY_HOOK_TIMEOUT_SECONDS = 30
AUTO_CONTINUE_EVENTS = (
    "Stop",
    "SubagentStop",
    "UserPromptSubmit",
    "SessionStart",
    "PreToolUse",
    "PermissionRequest",
)
ERROR_RECOVERY_EVENTS = ("ResponseError",)


def _powershell_file_target(command: str) -> str | None:
    """Return the script passed to a PowerShell ``-File`` argument."""
    if not isinstance(command, str):
        return None
    try:
        arguments = shlex.split(command, posix=False)
    except ValueError:
        return None
    if not arguments:
        return None

    def unquote(value: str) -> str:
        value = str(value).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value

    executable = ntpath.basename(unquote(arguments[0])).casefold()
    if executable not in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return None
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
    collapsed = re.sub(r"[\\/]+", r"\\", str(value).strip())
    return ntpath.normcase(ntpath.normpath(collapsed))


def _command_targets_script(command: str, script_path: Path) -> bool:
    target = _powershell_file_target(command)
    if not target:
        return False
    normalized_target = _normalize_windows_hook_path(target)
    normalized_expected = _normalize_windows_hook_path(str(script_path))
    # API Switcher has always emitted an absolute path. A relative script or a
    # same-named script under another directory can be user-owned.
    return normalized_target == normalized_expected


def _snapshot_local_files(paths: list[Path]) -> dict[Path, bytes | None]:
    snapshots = {}
    for path in dict.fromkeys(paths):
        try:
            snapshots[path] = path.read_bytes()
        except FileNotFoundError:
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
                # Do not atomically replace an untouched user file when the
                # operation failed before making its first write.
                if path.exists() and path.read_bytes() == content:
                    continue
                atomic_write_bytes(path, content)
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    return "; ".join(errors)


def _iter_claude_hook_commands(
    settings: dict,
    event_names: tuple[str, ...] | None = None,
):
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return
    if event_names is None:
        event_names = tuple(hooks)
    for event_name in event_names:
        groups = hooks.get(event_name, [])
        if isinstance(groups, dict):
            groups = [groups]
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            hook_list = group.get("hooks", [])
            if isinstance(hook_list, dict):
                hook_list = [hook_list]
            if not isinstance(hook_list, list):
                continue
            for hook in hook_list:
                if isinstance(hook, dict):
                    yield str(hook.get("command", ""))


def _claude_owned_event_names(settings: dict, is_owned) -> set[str]:
    """Return every Claude event containing an exactly owned command."""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return set()
    return {
        str(event_name)
        for event_name in hooks
        if any(
            is_owned(command)
            for command in _iter_claude_hook_commands(settings, (event_name,))
        )
    }


def _claude_event_has_command(settings: dict, event_name: str, marker: str) -> bool:
    return any(marker in command for command in _iter_claude_hook_commands(settings, (event_name,)))


def _permission_auto_approve_issues(
    auto_settings: AutoContinueSettings | None,
    claude_settings: dict | None,
) -> list[str]:
    if not auto_settings or not auto_settings.auto_approve_permission_requests:
        return []

    if not isinstance(claude_settings, dict):
        return ["Claude settings.json 无法读取，权限自动确认状态未知"]

    permissions = claude_settings.get("permissions")
    permissions = permissions if isinstance(permissions, dict) else {}
    issues: list[str] = []

    if permissions.get("defaultMode") != "dontAsk":
        issues.append("Claude 权限模式不是 dontAsk，权限自动确认需修复")
    if claude_settings.get("skipDangerousModePermissionPrompt") is True:
        issues.append("skipDangerousModePermissionPrompt 仍为 true，权限自动确认需修复")

    desired_rules = permission_rules_from_auto_settings(auto_settings)
    missing_rules = missing_allow_rules(desired_rules, permissions.get("allow", []))
    ask_conflicts = conflicting_permission_rules(desired_rules, permissions.get("ask", []))
    deny_conflicts = conflicting_permission_rules(desired_rules, permissions.get("deny", []))

    if missing_rules:
        issues.append("权限 allow 未预授权: " + ", ".join(missing_rules[:5]))
    if ask_conflicts:
        issues.append("permissions.ask 仍会强制询问: " + ", ".join(ask_conflicts[:5]))
    if deny_conflicts:
        issues.append("permissions.deny 会阻止自动执行: " + ", ".join(deny_conflicts[:5]))

    auto_approves_everything = any(
        str(tool or "").strip() == "*"
        for tool in auto_settings.auto_approve_tools
    )
    if auto_approves_everything:
        deny_conflict_keys = {rule.casefold() for rule in deny_conflicts}
        broad_deny_rules = [
            rule
            for rule in rules_from_payload(permissions.get("deny", []))
            if rule.casefold() not in deny_conflict_keys
        ]
        if broad_deny_rules:
            issues.append("permissions.deny 会阻止通配自动执行: " + ", ".join(broad_deny_rules[:5]))

    return issues


def _backup_claude_settings_file(path: Path, reason: str) -> Path | None:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(f"Failed to read Claude settings.json for backup {path}: {exc}")
        return None

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for suffix in [""] + [f".{i}" for i in range(1, 100)]:
        backup_path = path.with_name(f"{path.name}.bak-{timestamp}{suffix}")
        if backup_path.exists():
            continue
        try:
            atomic_write_bytes(backup_path, content)
            logger.warning(f"Backed up Claude settings.json to {backup_path}: {reason}")
            return backup_path
        except Exception as e:
            logger.warning(f"Failed to back up Claude settings.json {path}: {e}")
            return None
    return None


def _read_claude_settings_json(path: Path, *, recover: bool = False) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as e:
        logger.error(f"Failed to read Claude settings.json: {e}")
        return None
    except UnicodeError as e:
        if recover:
            if _backup_claude_settings_file(path, f"invalid encoding: {e}") is not None:
                return {}
            return None
        logger.error(f"Failed to read Claude settings.json: {e}")
        return None

    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        if recover:
            if _backup_claude_settings_file(path, f"invalid JSON: {e}") is not None:
                return {}
            return None
        logger.error(f"Failed to read Claude settings.json: {e}")
        return None

    if not isinstance(data, dict):
        reason = f"expected object, got {type(data).__name__}"
        if recover:
            if _backup_claude_settings_file(path, reason) is not None:
                return {}
            return None
        logger.error(f"Invalid Claude settings.json: {reason}")
        return None

    return data


class ClaudeProvider(AutoContinueProvider):
    """Auto-continue provider for Claude Code."""

    def __init__(self):
        super().__init__("claude")

    def get_config_dir(self) -> Path:
        """Get Claude Code config directory."""
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(configured) if configured else Path.home() / ".claude"

    def get_hook_script_path(self) -> Path:
        return self.get_config_dir() / "hooks" / "auto_continue_stop.ps1"

    def get_error_recovery_script_path(self) -> Path:
        """获取错误恢复脚本路径"""
        return self.get_config_dir() / "hooks" / "error_recovery.ps1"

    def get_settings_path(self) -> Path:
        return self.get_config_dir() / "auto_continue_settings.json"

    def get_claude_settings_path(self) -> Path:
        return self.get_config_dir() / "settings.json"

    def get_claude_md_path(self) -> Path:
        return self.get_config_dir() / "CLAUDE.md"

    def get_permission_rules_state_path(self) -> Path:
        return self.get_config_dir() / "auto_continue_permission_rules.json"

    @staticmethod
    def _powershell_hook_command(script_path: Path) -> str:
        escaped = str(script_path).replace("\\", "\\\\")
        return f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{escaped}"'

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
        *,
        apply_to_subagents: bool | None = None,
    ) -> tuple[str, ...]:
        if settings is None:
            needs_stop = True
            needs_prompt = True
            needs_permission = False
            include_subagents = bool(apply_to_subagents)
        else:
            needs_stop = bool(settings.enabled or settings.training_auto_continue_enabled)
            needs_prompt = bool(
                needs_stop
                or (settings.git_auto_snapshot and settings.git_snapshot_on_start)
            )
            needs_permission = bool(settings.auto_approve_permission_requests)
            include_subagents = bool(
                settings.apply_to_subagents
                if apply_to_subagents is None
                else apply_to_subagents
            )

        required = []
        if needs_stop:
            required.append("Stop")
            if include_subagents:
                required.append("SubagentStop")
        if needs_prompt:
            required.extend(["UserPromptSubmit", "SessionStart"])
        if needs_permission:
            required.extend(["PreToolUse", "PermissionRequest"])
        return tuple(required)

    @staticmethod
    def _event_hook_definitions(settings: dict, event_name: str) -> list[dict]:
        hooks = settings.get("hooks", {})
        if not isinstance(hooks, dict):
            return []
        groups = hooks.get(event_name, [])
        if isinstance(groups, dict):
            groups = [groups]
        if not isinstance(groups, list):
            return []
        definitions = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            hook_list = group.get("hooks", [])
            if isinstance(hook_list, dict):
                hook_list = [hook_list]
            if isinstance(hook_list, list):
                definitions.extend(hook for hook in hook_list if isinstance(hook, dict))
        return definitions

    def _event_registration_is_current(
        self,
        settings: dict,
        event_name: str,
        expected: dict | None,
        is_owned,
    ) -> bool:
        owned = [
            hook
            for hook in self._event_hook_definitions(settings, event_name)
            if is_owned(str(hook.get("command", "")))
        ]
        if expected is None:
            return not owned
        return len(owned) == 1 and all(
            owned[0].get(key) == expected[key]
            for key in ("type", "command", "timeout")
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
        """Check registration shape and generated script content."""
        settings_path = self.get_claude_settings_path()
        if not settings_path.exists():
            return False
        claude_settings = _read_claude_settings_json(settings_path)
        if not isinstance(claude_settings, dict):
            return False

        load_result = self.load_settings_result()
        if load_result.status is not SettingsLoadStatus.LOADED:
            return False
        auto_settings = load_result.settings
        required_events = set(self._required_auto_continue_events(auto_settings))
        expected = self._auto_continue_hook_definition()
        registrations_current = all(
            self._event_registration_is_current(
                claude_settings,
                event_name,
                expected if event_name in required_events else None,
                self._is_owned_auto_continue_command,
            )
            for event_name in AUTO_CONTINUE_EVENTS
        )
        unexpected_events = (
            _claude_owned_event_names(
                claude_settings,
                self._is_owned_auto_continue_command,
            )
            - required_events
        )
        if not registrations_current or unexpected_events:
            return False
        if not required_events:
            return True
        return self._script_content_is_current(
            self.get_hook_script_path(),
            self._render_hook_script(auto_settings),
        )

    def register_hook(
        self,
        apply_to_subagents: bool | None = None,
        settings: AutoContinueSettings | None = None,
    ) -> None:
        """Register hook in settings.json."""
        auto_settings = settings
        settings_path = self.get_claude_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        snapshots = _snapshot_local_files([
            settings_path,
            self.get_permission_rules_state_path(),
        ])

        try:
            # Explicit repair may rebuild invalid JSON only after a recoverable
            # backup was created. Startup reconciliation validates first and
            # never enters this recovery path for damaged user configuration.
            claude_settings = _read_claude_settings_json(settings_path, recover=True)
            if not isinstance(claude_settings, dict):
                raise RuntimeError(
                    "Claude settings.json is invalid and could not be backed up; hooks were not changed"
                )
            if "hooks" not in claude_settings:
                claude_settings["hooks"] = {}
            elif not isinstance(claude_settings["hooks"], dict):
                raise RuntimeError("Claude settings.json hooks must be an object")

            hook_def = {
                **self._auto_continue_hook_definition(),
                "statusMessage": "Checking whether Claude should continue",
            }
            git_snapshot_on_start = (
                True
                if auto_settings is None
                else bool(auto_settings.git_auto_snapshot and auto_settings.git_snapshot_on_start)
            )
            required_events = set(
                self._required_auto_continue_events(
                    auto_settings,
                    apply_to_subagents=apply_to_subagents,
                )
            )

            self._remove_owned_hook_entries_from_all_events(
                claude_settings,
                self._is_owned_auto_continue_command,
            )

            stop_hook = hook_def if "Stop" in required_events else None
            self._register_hook_event(
                claude_settings,
                "Stop",
                stop_hook,
                self._is_owned_auto_continue_command,
            )

            prompt_hook = None
            if "UserPromptSubmit" in required_events:
                prompt_hook = dict(hook_def)
                prompt_hook["statusMessage"] = (
                    "Creating Git snapshot before Claude starts work"
                    if git_snapshot_on_start
                    else "Starting a new Claude auto-continue chain"
                )
            self._register_hook_event(
                claude_settings,
                "UserPromptSubmit",
                prompt_hook,
                self._is_owned_auto_continue_command,
            )

            session_hook = None
            if "SessionStart" in required_events:
                session_hook = dict(hook_def)
                session_hook["statusMessage"] = (
                    "Creating Git snapshot when Claude session starts"
                    if git_snapshot_on_start
                    else "Resetting Claude auto-continue state for this session"
                )
            self._register_hook_event(
                claude_settings,
                "SessionStart",
                session_hook,
                self._is_owned_auto_continue_command,
            )

            subagent_hook = None
            if "SubagentStop" in required_events:
                subagent_hook = dict(hook_def)
                subagent_hook["statusMessage"] = "Checking whether Claude subagent should continue"
            self._register_hook_event(
                claude_settings,
                "SubagentStop",
                subagent_hook,
                self._is_owned_auto_continue_command,
            )

            if "PermissionRequest" in required_events:
                permissions = claude_settings.get("permissions")
                permissions = dict(permissions) if isinstance(permissions, dict) else {}
                permissions["defaultMode"] = "dontAsk"
                claude_settings["permissions"] = permissions
                claude_settings["skipDangerousModePermissionPrompt"] = False

            pre_tool_hook = None
            if "PreToolUse" in required_events:
                pre_tool_hook = dict(hook_def)
                pre_tool_hook["statusMessage"] = "Auto-allowing configured Claude tool call if allowed"
            self._register_hook_event(
                claude_settings,
                "PreToolUse",
                pre_tool_hook,
                self._is_owned_auto_continue_command,
            )

            approval_hook = None
            if "PermissionRequest" in required_events:
                approval_hook = dict(hook_def)
                approval_hook["statusMessage"] = (
                    "Auto-approving configured Claude permission request if allowed"
                )
            self._register_hook_event(
                claude_settings,
                "PermissionRequest",
                approval_hook,
                self._is_owned_auto_continue_command,
            )

            desired_rules = permission_rules_from_auto_settings(auto_settings)
            previous_rules, previous_ask_rules = self._load_managed_permission_state()
            claude_settings, managed_rules, removed_ask_rules = apply_managed_permission_rules(
                claude_settings,
                desired_rules,
                previous_rules,
                previous_ask_rules,
            )

            atomic_write_text(
                settings_path,
                json.dumps(claude_settings, indent=2, ensure_ascii=False),
            )
            self._save_managed_permission_state(managed_rules, removed_ask_rules)
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(f"Failed to register Claude hooks: {exc}{detail}") from exc

    def _register_hook_event(
        self,
        settings: dict,
        event_name: str,
        hook_def: dict | None,
        is_owned=None,
    ) -> None:
        """Replace only API Switcher's hook in one Claude event."""
        hooks_container = settings.get("hooks")
        if not isinstance(hooks_container, dict):
            raise RuntimeError("Claude settings.json hooks must be an object")
        original_groups = hooks_container.get(event_name, [])
        singleton_group = isinstance(original_groups, dict)
        groups = [original_groups] if singleton_group else original_groups
        if not isinstance(groups, list):
            raise RuntimeError(f"Claude hook event {event_name} must be a list or object")

        owner = is_owned or self._is_owned_hook_command
        filtered_groups = []
        for group in groups:
            if not isinstance(group, dict):
                filtered_groups.append(group)
                continue
            original_hook_list = group.get("hooks", [])
            singleton_hook = isinstance(original_hook_list, dict)
            hook_list = [original_hook_list] if singleton_hook else original_hook_list
            if not isinstance(hook_list, list):
                filtered_groups.append(group)
                continue
            removed_owned = any(
                isinstance(hook, dict)
                and owner(str(hook.get("command", "")))
                for hook in hook_list
            )
            if not removed_owned:
                # Leave user-owned and legacy singleton shapes structurally
                # unchanged instead of normalizing unrelated configuration.
                filtered_groups.append(group)
                continue
            filtered_hooks = [
                hook
                for hook in hook_list
                if not (
                    isinstance(hook, dict)
                    and owner(str(hook.get("command", "")))
                )
            ]
            if filtered_hooks:
                updated_group = dict(group)
                updated_group["hooks"] = (
                    filtered_hooks[0]
                    if singleton_hook and len(filtered_hooks) == 1
                    else filtered_hooks
                )
                filtered_groups.append(updated_group)
            elif any(key != "hooks" for key in group):
                # A group created by API Switcher contains only ``hooks``. If
                # the user attached matcher or custom metadata, retain those
                # fields as an inert empty group instead of deleting user data.
                updated_group = dict(group)
                updated_group["hooks"] = []
                filtered_groups.append(updated_group)

        if hook_def is not None:
            filtered_groups.append({"hooks": [hook_def]})
        if filtered_groups:
            hooks_container[event_name] = (
                filtered_groups[0]
                if singleton_group and hook_def is None and len(filtered_groups) == 1
                else filtered_groups
            )
        else:
            hooks_container.pop(event_name, None)

    def _remove_owned_hook_entries_from_all_events(self, settings: dict, is_owned) -> bool:
        """Remove exact managed commands from every supported Claude event shape."""
        event_names = _claude_owned_event_names(settings, is_owned)
        for event_name in event_names:
            self._register_hook_event(settings, event_name, None, is_owned)
        return bool(event_names)

    def unregister_hook(self) -> None:
        """Unregister hook from settings.json."""
        settings_path = self.get_claude_settings_path()
        state_path = self.get_permission_rules_state_path()
        snapshots = _snapshot_local_files([settings_path, state_path])
        if snapshots[settings_path] is None:
            self._save_managed_permission_state([], [])
            return

        try:
            settings = _read_claude_settings_json(settings_path)
            if not isinstance(settings, dict):
                raise RuntimeError("Claude settings.json is invalid; managed hooks were not removed")
            hooks = settings.get("hooks")
            if hooks is None:
                settings["hooks"] = {}
            elif not isinstance(hooks, dict):
                raise RuntimeError("Claude settings.json hooks must be an object")

            self._remove_owned_hook_entries_from_all_events(
                settings,
                self._is_owned_auto_continue_command,
            )

            previous_rules, previous_ask_rules = self._load_managed_permission_state()
            settings, _managed_rules, _removed_ask_rules = apply_managed_permission_rules(
                settings,
                [],
                previous_rules,
                previous_ask_rules,
            )

            # Write back
            atomic_write_text(settings_path, json.dumps(settings, indent=2, ensure_ascii=False))
            self._save_managed_permission_state([], [])
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(f"Failed to unregister Claude hooks: {exc}{detail}") from exc

    def _load_managed_permission_state(self) -> tuple[list[str], list[str]]:
        state_path = self.get_permission_rules_state_path()
        if not state_path.exists():
            return [], []
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            return rules_from_payload(payload), ask_rules_from_payload(payload)
        except Exception as e:
            logger.warning(f"Failed to read managed Claude permission rules: {e}")
            return [], []

    def _save_managed_permission_state(self, rules: list[str], ask_rules: list[str]) -> None:
        state_path = self.get_permission_rules_state_path()
        if not rules and not ask_rules:
            try:
                state_path.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Failed to remove managed Claude permission rules state: {e}")
            return

        state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(state_path, json.dumps(rules_payload(rules, ask_rules), indent=2))

    def _render_hook_script(self, settings: AutoContinueSettings | None) -> str:
        enable_git = (
            bool(settings.git_auto_snapshot and settings.git_snapshot_on_start)
            if settings else True
        )
        settings_path = str(self.get_settings_path()).replace("\\", "\\\\")
        return generate_hook_script(settings_path, enable_git, provider_name="claude")

    def _render_error_recovery_script(self, settings: AutoContinueSettings | None) -> str:
        enable_git = (
            bool(settings.git_auto_snapshot and settings.git_snapshot_on_recovery)
            if settings else True
        )
        settings_path = str(self.get_settings_path()).replace("\\", "\\\\")
        return generate_error_recovery_script(settings_path, enable_git)

    @staticmethod
    def _script_content_is_current(path: Path, expected: str) -> bool:
        try:
            return path.exists() and path.read_text(encoding="utf-8-sig") == expected
        except OSError:
            return False

    def _has_owned_hook_entries(self, is_owned) -> bool:
        settings_path = self.get_claude_settings_path()
        settings = _read_claude_settings_json(settings_path)
        if not isinstance(settings, dict):
            raise RuntimeError(
                "Claude settings.json is invalid; managed hooks were not inspected or removed"
            )
        return any(is_owned(command) for command in _iter_claude_hook_commands(settings))

    def install_hook_script(self, settings: AutoContinueSettings | None = None) -> None:
        """Install the hook script."""
        script_path = self.get_hook_script_path()
        script_path.parent.mkdir(parents=True, exist_ok=True)

        # Create tmp directory for logs
        tmp_dir = self.get_config_dir() / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        resolved_settings = settings if settings is not None else self.load_settings()
        script_content = self._render_hook_script(resolved_settings)

        atomic_write_text(script_path, script_content, encoding='utf-8-sig')

        logger.info(f"Installed hook script: {script_path}")

    def ensure_current_installation(self, settings: AutoContinueSettings) -> bool:
        """Transactionally reconcile Claude scripts and managed hook entries."""
        is_valid, validation_error = settings.validate()
        if not is_valid:
            raise ValueError(f"Invalid settings: {validation_error}")

        claude_settings_path = self.get_claude_settings_path()
        if not isinstance(_read_claude_settings_json(claude_settings_path), dict):
            raise RuntimeError(
                "Claude settings.json is invalid; startup reconciliation did not overwrite it"
            )

        paths = [
            self.get_hook_script_path(),
            self.get_error_recovery_script_path(),
            claude_settings_path,
            self.get_permission_rules_state_path(),
        ]
        snapshots = _snapshot_local_files(paths)
        try:
            needs_auto_hook = self._settings_require_hook(settings)
            if needs_auto_hook:
                expected_script = self._render_hook_script(settings)
                if not self._script_content_is_current(
                    self.get_hook_script_path(),
                    expected_script,
                ):
                    self.install_hook_script(settings=settings)
                if not self.is_hook_registered():
                    self.register_hook_for_settings(settings)
            elif (
                self._has_owned_hook_entries(self._is_owned_auto_continue_command)
                or self.get_permission_rules_state_path().exists()
            ):
                self.unregister_hook()

            if settings.error_recovery_enabled:
                expected_recovery = self._render_error_recovery_script(settings)
                if (
                    not self._script_content_is_current(
                        self.get_error_recovery_script_path(),
                        expected_recovery,
                    )
                    or not self.is_error_recovery_installed()
                ):
                    self.install_error_recovery(settings=settings)
            elif (
                self.get_error_recovery_script_path().exists()
                or self._has_owned_hook_entries(self._is_owned_error_recovery_command)
            ):
                self.uninstall_error_recovery()
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(
                f"Failed to reconcile Claude hook installation: {exc}{detail}"
            ) from exc

        return snapshots != _snapshot_local_files(paths)

    def disable_managed_hooks_for_invalid_settings(self) -> bool:
        """Disable managed hooks without replacing invalid persisted settings."""
        safe_settings = AutoContinueSettings(
            enabled=False,
            training_auto_continue_enabled=False,
            git_auto_snapshot=False,
            git_snapshot_on_start=False,
            auto_approve_permission_requests=False,
            error_recovery_enabled=False,
        )
        return self.ensure_current_installation(safe_settings)

    def uninstall_hook_script(self) -> None:
        """Remove the hook script."""
        script_path = self.get_hook_script_path()
        if script_path.exists():
            script_path.unlink()

    def install_guidance(self) -> None:
        """Install guidance in CLAUDE.md."""
        claude_md = self.get_claude_md_path()
        claude_md.parent.mkdir(parents=True, exist_ok=True)

        guidance = """<!-- BEGIN AUTO CONTINUE GUIDANCE -->
# Auto-Continue Guidance

Before providing your final response, check if the task is truly complete:
- Are there any remaining TODOs or unfinished work?
- Have all tests been run and passed?
- Has verification been completed?
- Are there any follow-up steps mentioned?

If work remains incomplete, continue working on it rather than stopping.
Only stop when you encounter a genuine blocker that requires user input or decision.
<!-- END AUTO CONTINUE GUIDANCE -->
"""

        # Read existing content
        existing = ""
        if claude_md.exists():
            existing = claude_md.read_text(encoding='utf-8')

        # Check if guidance block exists
        if "BEGIN AUTO CONTINUE GUIDANCE" in existing:
            # Replace existing block
            import re
            pattern = r'<!-- BEGIN AUTO CONTINUE GUIDANCE -->.*?<!-- END AUTO CONTINUE GUIDANCE -->'
            new_content = re.sub(pattern, guidance.strip(), existing, flags=re.DOTALL)
            atomic_write_text(claude_md, new_content)
        else:
            # Append new block
            content = existing
            if content and not content.endswith('\n'):
                content += '\n\n'
            content += guidance
            atomic_write_text(claude_md, content)

    def uninstall_guidance(self) -> None:
        """Remove guidance from CLAUDE.md."""
        claude_md = self.get_claude_md_path()
        if not claude_md.exists():
            return

        content = claude_md.read_text(encoding='utf-8')

        # Remove the guidance block
        import re
        pattern = r'<!-- BEGIN AUTO CONTINUE GUIDANCE -->.*?<!-- END AUTO CONTINUE GUIDANCE -->\n*'
        new_content = re.sub(pattern, '', content, flags=re.DOTALL)

        if new_content.strip():
            atomic_write_text(claude_md, new_content)
        else:
            # Delete file if empty
            claude_md.unlink()

    def is_guidance_installed(self) -> bool:
        """Check if guidance is installed."""
        claude_md = self.get_claude_md_path()
        if not claude_md.exists():
            return False
        content = claude_md.read_text(encoding='utf-8')
        return "BEGIN AUTO CONTINUE GUIDANCE" in content

    def get_status(self):
        """Get status with guidance check."""
        status = super().get_status()
        status.guidance_installed = self.is_guidance_installed()
        status.error_recovery_installed = self.is_error_recovery_installed()
        settings = self.load_settings()
        if settings and settings.auto_approve_permission_requests:
            claude_settings = _read_claude_settings_json(self.get_claude_settings_path())
            issues = _permission_auto_approve_issues(settings, claude_settings)
            if issues:
                status.last_error = "；".join(issues[:5])
        return status

    def install_error_recovery(self, settings: AutoContinueSettings | None = None) -> None:
        """安装错误恢复 Hook"""
        script_path = self.get_error_recovery_script_path()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        snapshots = _snapshot_local_files([
            script_path,
            self.get_claude_settings_path(),
        ])
        try:
            resolved_settings = settings if settings is not None else self.load_settings()
            script_content = self._render_error_recovery_script(resolved_settings)
            atomic_write_text(script_path, script_content, encoding='utf-8-sig')
            logger.info(f"Installed error recovery script: {script_path}")
            self._register_error_recovery_hook()
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(f"Failed to install Claude error recovery: {exc}{detail}") from exc

    def _register_error_recovery_hook(self) -> None:
        """注册错误恢复 Hook 到 settings.json"""
        settings_path = self.get_claude_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        snapshots = _snapshot_local_files([settings_path])
        try:
            claude_settings = _read_claude_settings_json(settings_path, recover=True)
            if not isinstance(claude_settings, dict):
                raise RuntimeError(
                    "Claude settings.json is invalid and could not be backed up; recovery hook was not changed"
                )
            if "hooks" not in claude_settings:
                claude_settings["hooks"] = {}
            elif not isinstance(claude_settings["hooks"], dict):
                raise RuntimeError("Claude settings.json hooks must be an object")

            hook_def = {
                **self._error_recovery_hook_definition(),
                "statusMessage": "Checking for API errors and auto-recovery",
            }
            self._remove_owned_hook_entries_from_all_events(
                claude_settings,
                self._is_owned_error_recovery_command,
            )
            self._register_hook_event(
                claude_settings,
                "ResponseError",
                hook_def,
                self._is_owned_error_recovery_command,
            )
            atomic_write_text(
                settings_path,
                json.dumps(claude_settings, indent=2, ensure_ascii=False),
            )
            logger.info("Registered error recovery hook to ResponseError event")
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(
                f"Failed to register Claude error recovery hook: {exc}{detail}"
            ) from exc

    def uninstall_error_recovery(self) -> None:
        """卸载错误恢复功能"""
        script_path = self.get_error_recovery_script_path()
        settings_path = self.get_claude_settings_path()
        snapshots = _snapshot_local_files([script_path, settings_path])
        try:
            if script_path.exists():
                script_path.unlink()
            if snapshots[settings_path] is None:
                return
            claude_settings = _read_claude_settings_json(settings_path)
            if not isinstance(claude_settings, dict):
                raise RuntimeError(
                    "Claude settings.json is invalid; recovery hook was not removed"
                )
            hooks = claude_settings.get("hooks")
            if hooks is None:
                claude_settings["hooks"] = {}
            elif not isinstance(hooks, dict):
                raise RuntimeError("Claude settings.json hooks must be an object")
            self._remove_owned_hook_entries_from_all_events(
                claude_settings,
                self._is_owned_error_recovery_command,
            )
            atomic_write_text(
                settings_path,
                json.dumps(claude_settings, indent=2, ensure_ascii=False),
            )
            logger.info("Uninstalled error recovery hook")
        except Exception as exc:
            rollback_error = _restore_local_files(snapshots)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(
                f"Failed to uninstall Claude error recovery hook: {exc}{detail}"
            ) from exc

    def is_error_recovery_installed(self) -> bool:
        """Check recovery registration shape and generated script content."""
        settings_path = self.get_claude_settings_path()
        if not settings_path.exists():
            return False
        try:
            claude_settings = _read_claude_settings_json(settings_path)
            if not isinstance(claude_settings, dict):
                return False
            load_result = self.load_settings_result()
            if load_result.status is not SettingsLoadStatus.LOADED:
                return False
            auto_settings = load_result.settings
            expected = self._error_recovery_hook_definition()
            registrations_current = all(
                self._event_registration_is_current(
                    claude_settings,
                    event_name,
                    expected,
                    self._is_owned_error_recovery_command,
                )
                for event_name in ERROR_RECOVERY_EVENTS
            )
            unexpected_events = (
                _claude_owned_event_names(
                    claude_settings,
                    self._is_owned_error_recovery_command,
                )
                - set(ERROR_RECOVERY_EVENTS)
            )
            return (
                registrations_current
                and not unexpected_events
                and self._script_content_is_current(
                    self.get_error_recovery_script_path(),
                    self._render_error_recovery_script(auto_settings),
                )
            )
        except Exception:
            return False
