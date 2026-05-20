import json
import logging
from datetime import datetime
from pathlib import Path
from core.atomic_io import atomic_write_bytes, atomic_write_text
from core.auto_continue.base import AutoContinueProvider
from core.auto_continue.permission_rules import (
    apply_managed_permission_rules,
    ask_rules_from_payload,
    permission_rules_from_auto_settings,
    rules_from_payload,
    rules_payload,
)
from core.auto_continue.script_generator import generate_hook_script
from core.auto_continue.error_recovery_script import generate_error_recovery_script
from models.auto_continue import AutoContinueSettings

logger = logging.getLogger(__name__)


def _is_managed_hook_command(command: str) -> bool:
    return "auto_continue_stop.ps1" in command or "error_recovery.ps1" in command


def _iter_claude_hook_commands(
    settings: dict,
    event_names: tuple[str, ...] = (
        "Stop",
        "SubagentStop",
        "UserPromptSubmit",
        "SessionStart",
        "PreToolUse",
        "PermissionRequest",
        "ResponseError",
    ),
):
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return
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


def _claude_event_has_command(settings: dict, event_name: str, marker: str) -> bool:
    return any(marker in command for command in _iter_claude_hook_commands(settings, (event_name,)))


def _backup_claude_settings_file(path: Path, reason: str) -> Path | None:
    if not path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for suffix in [""] + [f".{i}" for i in range(1, 100)]:
        backup_path = path.with_name(f"{path.name}.bak-{timestamp}{suffix}")
        if backup_path.exists():
            continue
        try:
            atomic_write_bytes(backup_path, path.read_bytes())
            logger.warning(f"Backed up Claude settings.json to {backup_path}: {reason}")
            return backup_path
        except Exception as e:
            logger.warning(f"Failed to back up Claude settings.json {path}: {e}")
            return None
    return None


def _read_claude_settings_json(path: Path, *, recover: bool = False) -> dict | None:
    if not path.exists():
        return {}

    try:
        raw = path.read_text(encoding="utf-8-sig")
        if not raw.strip():
            return {}
        data = json.loads(raw)
    except Exception as e:
        if recover:
            _backup_claude_settings_file(path, f"invalid JSON: {e}")
            return {}
        logger.error(f"Failed to read Claude settings.json: {e}")
        return None

    if not isinstance(data, dict):
        reason = f"expected object, got {type(data).__name__}"
        if recover:
            _backup_claude_settings_file(path, reason)
            return {}
        logger.error(f"Invalid Claude settings.json: {reason}")
        return None

    return data


class ClaudeProvider(AutoContinueProvider):
    """Auto-continue provider for Claude Code."""

    def __init__(self):
        super().__init__("claude")

    def get_config_dir(self) -> Path:
        """Get Claude Code config directory."""
        return Path.home() / ".claude"

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

    def is_hook_registered(self) -> bool:
        """Check if hook is registered in settings.json."""
        settings_path = self.get_claude_settings_path()
        if not settings_path.exists():
            return False
        claude_settings = _read_claude_settings_json(settings_path)
        if not isinstance(claude_settings, dict):
            return False

        auto_settings = self.load_settings() or AutoContinueSettings()
        git_snapshot_on_start = bool(
            auto_settings.git_auto_snapshot and auto_settings.git_snapshot_on_start
        )
        needs_stop_hook = bool(
            auto_settings.enabled
            or auto_settings.training_auto_continue_enabled
            or git_snapshot_on_start
        )
        needs_permission_hooks = bool(auto_settings.auto_approve_permission_requests)

        required_events = []
        if needs_stop_hook:
            required_events.append("Stop")
            if auto_settings.apply_to_subagents:
                required_events.append("SubagentStop")
        if git_snapshot_on_start:
            required_events.extend(["UserPromptSubmit", "SessionStart"])
        if needs_permission_hooks:
            required_events.extend(["PreToolUse", "PermissionRequest"])

        if required_events:
            return all(
                _claude_event_has_command(claude_settings, event_name, "auto_continue_stop.ps1")
                for event_name in required_events
            )

        return any(
            "auto_continue_stop.ps1" in command
            for command in _iter_claude_hook_commands(claude_settings)
        )

    def register_hook(self, apply_to_subagents: bool = False, settings=None) -> None:
        """Register hook in settings.json."""
        auto_settings = settings
        settings_path = self.get_claude_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing settings. If repair finds corrupt JSON, keep a backup
        # before rebuilding so user configuration can be recovered manually.
        claude_settings = _read_claude_settings_json(settings_path, recover=True) or {}

        # Ensure hooks structure exists
        if "hooks" not in claude_settings:
            claude_settings["hooks"] = {}

        script_path = str(self.get_hook_script_path()).replace("\\", "\\\\")
        hook_def = {
            "type": "command",
            "command": f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
            "timeout": 10,
            "statusMessage": "Checking whether Claude should continue"
        }

        git_snapshot_on_start = (
            True
            if auto_settings is None
            else bool(auto_settings.git_auto_snapshot and auto_settings.git_snapshot_on_start)
        )
        needs_stop_hook = (
            True
            if auto_settings is None
            else bool(
                auto_settings.enabled
                or auto_settings.training_auto_continue_enabled
                or git_snapshot_on_start
            )
        )

        # Register Stop hook
        self._register_hook_event(claude_settings, "Stop", hook_def if needs_stop_hook else None)

        if git_snapshot_on_start:
            prompt_hook = dict(hook_def)
            prompt_hook["statusMessage"] = "Creating Git snapshot before Claude starts work"
            self._register_hook_event(claude_settings, "UserPromptSubmit", prompt_hook)
            session_hook = dict(hook_def)
            session_hook["statusMessage"] = "Creating Git snapshot when Claude session starts"
            self._register_hook_event(claude_settings, "SessionStart", session_hook)
        else:
            self._register_hook_event(claude_settings, "UserPromptSubmit", None)
            self._register_hook_event(claude_settings, "SessionStart", None)

        # Optionally register SubagentStop
        if apply_to_subagents and needs_stop_hook:
            subagent_hook = dict(hook_def)
            subagent_hook["statusMessage"] = "Checking whether Claude subagent should continue"
            self._register_hook_event(claude_settings, "SubagentStop", subagent_hook)
        else:
            self._register_hook_event(claude_settings, "SubagentStop", None)

        if getattr(auto_settings, "auto_approve_permission_requests", False):
            permissions = claude_settings.get("permissions")
            if not isinstance(permissions, dict):
                permissions = {}
            else:
                permissions = dict(permissions)
            permissions["defaultMode"] = "dontAsk"
            claude_settings["permissions"] = permissions
            claude_settings["skipDangerousModePermissionPrompt"] = False

            pre_tool_hook = dict(hook_def)
            pre_tool_hook["statusMessage"] = "Auto-allowing configured Claude tool call if allowed"
            self._register_hook_event(claude_settings, "PreToolUse", pre_tool_hook)
            approval_hook = dict(hook_def)
            approval_hook["statusMessage"] = "Auto-approving configured Claude permission request if allowed"
            self._register_hook_event(claude_settings, "PermissionRequest", approval_hook)
        else:
            self._register_hook_event(claude_settings, "PreToolUse", None)
            self._register_hook_event(claude_settings, "PermissionRequest", None)

        desired_rules = permission_rules_from_auto_settings(auto_settings)
        previous_rules, previous_ask_rules = self._load_managed_permission_state()
        claude_settings, managed_rules, removed_ask_rules = apply_managed_permission_rules(
            claude_settings,
            desired_rules,
            previous_rules,
            previous_ask_rules,
        )

        # Write settings.json
        atomic_write_text(settings_path, json.dumps(claude_settings, indent=2, ensure_ascii=False))
        self._save_managed_permission_state(managed_rules, removed_ask_rules)

    def _register_hook_event(self, settings: dict, event_name: str, hook_def: dict | None) -> None:
        """Register a hook for a specific event."""
        if event_name not in settings["hooks"]:
            settings["hooks"][event_name] = []

        # Remove existing auto_continue hooks
        filtered = []
        for hook_group in settings["hooks"][event_name]:
            hooks = hook_group.get("hooks", [])
            filtered_hooks = [h for h in hooks if not _is_managed_hook_command(h.get("command", ""))]
            if filtered_hooks:
                hook_group["hooks"] = filtered_hooks
                filtered.append(hook_group)

        # Add our hook
        if hook_def:
            filtered.append({"hooks": [hook_def]})
        settings["hooks"][event_name] = filtered

    def unregister_hook(self) -> None:
        """Unregister hook from settings.json."""
        settings_path = self.get_claude_settings_path()
        if not settings_path.exists():
            self._save_managed_permission_state([], [])
            return

        try:
            settings = _read_claude_settings_json(settings_path)
            if not isinstance(settings, dict):
                return

            hooks = settings.get("hooks", {})

            # Remove from all events managed by API Switcher.
            for event_name in [
                "Stop",
                "SubagentStop",
                "UserPromptSubmit",
                "SessionStart",
                "PreToolUse",
                "PermissionRequest",
            ]:
                if event_name in hooks:
                    filtered = []
                    for hook_group in hooks[event_name]:
                        hook_list = hook_group.get("hooks", [])
                        filtered_hooks = [h for h in hook_list if "auto_continue_stop.ps1" not in h.get("command", "")]
                        if filtered_hooks:
                            hook_group["hooks"] = filtered_hooks
                            filtered.append(hook_group)
                    hooks[event_name] = filtered

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
        except Exception as e:
            logger.error(f"Failed to unregister hook: {e}")

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

    def install_hook_script(self) -> None:
        """Install the hook script."""
        script_path = self.get_hook_script_path()
        script_path.parent.mkdir(parents=True, exist_ok=True)

        # Create tmp directory for logs
        tmp_dir = self.get_config_dir() / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # 加载设置以检查是否启用git
        settings = self.load_settings()
        enable_git = (
            bool(settings.git_auto_snapshot and settings.git_snapshot_on_start)
            if settings else True
        )

        settings_path = str(self.get_settings_path()).replace("\\", "\\\\")
        script_content = generate_hook_script(settings_path, enable_git)

        atomic_write_text(script_path, script_content, encoding='utf-8-sig')

        logger.info(f"Installed hook script: {script_path}")

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
        return status

    def install_error_recovery(self) -> None:
        """安装错误恢复 Hook"""
        script_path = self.get_error_recovery_script_path()
        script_path.parent.mkdir(parents=True, exist_ok=True)

        # 加载设置以检查是否启用git
        settings = self.load_settings()
        enable_git = (
            bool(settings.git_auto_snapshot and settings.git_snapshot_on_recovery)
            if settings else True
        )

        settings_path = str(self.get_settings_path()).replace("\\", "\\\\")
        script_content = generate_error_recovery_script(settings_path, enable_git)

        atomic_write_text(script_path, script_content, encoding='utf-8-sig')

        logger.info(f"Installed error recovery script: {script_path}")

        # 注册到 ResponseError Hook
        self._register_error_recovery_hook()

    def _register_error_recovery_hook(self) -> None:
        """注册错误恢复 Hook 到 settings.json"""
        settings_path = self.get_claude_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        # 读取现有配置
        settings = _read_claude_settings_json(settings_path, recover=True) or {}

        if "hooks" not in settings:
            settings["hooks"] = {}

        script_path = str(self.get_error_recovery_script_path()).replace("\\", "\\\\")
        hook_def = {
            "type": "command",
            "command": f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
            "timeout": 10,
            "statusMessage": "Checking for API errors and auto-recovery"
        }

        # 注册到 ResponseError 事件
        self._register_hook_event(settings, "ResponseError", hook_def)

        # 写入配置
        atomic_write_text(settings_path, json.dumps(settings, indent=2, ensure_ascii=False))

        logger.info("Registered error recovery hook to ResponseError event")

    def uninstall_error_recovery(self) -> None:
        """卸载错误恢复功能"""
        # 删除脚本
        script_path = self.get_error_recovery_script_path()
        if script_path.exists():
            script_path.unlink()

        # 从 settings.json 移除 Hook
        settings_path = self.get_claude_settings_path()
        if not settings_path.exists():
            return

        try:
            settings = _read_claude_settings_json(settings_path)
            if not isinstance(settings, dict):
                return

            hooks = settings.get("hooks", {})
            if "ResponseError" in hooks:
                filtered = []
                for hook_group in hooks["ResponseError"]:
                    hook_list = hook_group.get("hooks", [])
                    filtered_hooks = [h for h in hook_list if "error_recovery.ps1" not in h.get("command", "")]
                    if filtered_hooks:
                        hook_group["hooks"] = filtered_hooks
                        filtered.append(hook_group)
                hooks["ResponseError"] = filtered

            atomic_write_text(settings_path, json.dumps(settings, indent=2, ensure_ascii=False))

            logger.info("Uninstalled error recovery hook")
        except Exception as e:
            logger.error(f"Failed to uninstall error recovery hook: {e}")

    def is_error_recovery_installed(self) -> bool:
        """检查错误恢复是否已安装"""
        script_path = self.get_error_recovery_script_path()
        if not script_path.exists():
            return False

        settings_path = self.get_claude_settings_path()
        if not settings_path.exists():
            return False

        try:
            settings = _read_claude_settings_json(settings_path)
            if not isinstance(settings, dict):
                return False

            hooks = settings.get("hooks", {})
            error_hooks = hooks.get("ResponseError", [])

            for hook_group in error_hooks:
                for hook in hook_group.get("hooks", []):
                    command = hook.get("command", "")
                    if "error_recovery.ps1" in command:
                        return True
            return False
        except Exception:
            return False
