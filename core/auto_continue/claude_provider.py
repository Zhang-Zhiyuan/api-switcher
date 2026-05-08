import json
import logging
from pathlib import Path
from core.auto_continue.base import AutoContinueProvider
from core.auto_continue.script_generator import generate_hook_script
from core.auto_continue.error_recovery_script import generate_error_recovery_script

logger = logging.getLogger(__name__)


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

    def is_hook_registered(self) -> bool:
        """Check if hook is registered in settings.json."""
        settings_path = self.get_claude_settings_path()
        if not settings_path.exists():
            return False
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)

            hooks = settings.get("hooks", {})
            stop_hooks = hooks.get("Stop", [])

            for hook_group in stop_hooks:
                for hook in hook_group.get("hooks", []):
                    command = hook.get("command", "")
                    if "auto_continue_stop.ps1" in command:
                        return True
            return False
        except Exception as e:
            logger.error(f"Failed to read Claude settings.json: {e}")
            return False

    def register_hook(self, apply_to_subagents: bool = False) -> None:
        """Register hook in settings.json."""
        settings_path = self.get_claude_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing settings
        settings = {}
        if settings_path.exists():
            try:
                with open(settings_path, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
            except Exception:
                pass

        # Ensure hooks structure exists
        if "hooks" not in settings:
            settings["hooks"] = {}

        script_path = str(self.get_hook_script_path()).replace("\\", "\\\\")
        hook_def = {
            "type": "command",
            "command": f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
            "timeout": 10,
            "statusMessage": "Checking whether Claude should continue"
        }

        # Register Stop hook
        self._register_hook_event(settings, "Stop", hook_def)

        # Optionally register SubagentStop
        if apply_to_subagents:
            subagent_hook = dict(hook_def)
            subagent_hook["statusMessage"] = "Checking whether Claude subagent should continue"
            self._register_hook_event(settings, "SubagentStop", subagent_hook)

        # Write settings.json
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)

    def _register_hook_event(self, settings: dict, event_name: str, hook_def: dict) -> None:
        """Register a hook for a specific event."""
        if event_name not in settings["hooks"]:
            settings["hooks"][event_name] = []

        # Remove existing auto_continue hooks
        filtered = []
        for hook_group in settings["hooks"][event_name]:
            hooks = hook_group.get("hooks", [])
            filtered_hooks = [h for h in hooks if "auto_continue_stop.ps1" not in h.get("command", "")]
            if filtered_hooks:
                hook_group["hooks"] = filtered_hooks
                filtered.append(hook_group)

        # Add our hook
        filtered.append({"hooks": [hook_def]})
        settings["hooks"][event_name] = filtered

    def unregister_hook(self) -> None:
        """Unregister hook from settings.json."""
        settings_path = self.get_claude_settings_path()
        if not settings_path.exists():
            return

        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)

            hooks = settings.get("hooks", {})

            # Remove from Stop and SubagentStop
            for event_name in ["Stop", "SubagentStop"]:
                if event_name in hooks:
                    filtered = []
                    for hook_group in hooks[event_name]:
                        hook_list = hook_group.get("hooks", [])
                        filtered_hooks = [h for h in hook_list if "auto_continue_stop.ps1" not in h.get("command", "")]
                        if filtered_hooks:
                            hook_group["hooks"] = filtered_hooks
                            filtered.append(hook_group)
                    hooks[event_name] = filtered

            # Write back
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to unregister hook: {e}")

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

        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(script_content)

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
            claude_md.write_text(new_content, encoding='utf-8')
        else:
            # Append new block
            with open(claude_md, 'a', encoding='utf-8') as f:
                if existing and not existing.endswith('\n'):
                    f.write('\n\n')
                f.write(guidance)

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
            claude_md.write_text(new_content, encoding='utf-8')
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

        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(script_content)

        logger.info(f"Installed error recovery script: {script_path}")

        # 注册到 ResponseError Hook
        self._register_error_recovery_hook()

    def _register_error_recovery_hook(self) -> None:
        """注册错误恢复 Hook 到 settings.json"""
        settings_path = self.get_claude_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        # 读取现有配置
        settings = {}
        if settings_path.exists():
            try:
                with open(settings_path, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
            except Exception:
                pass

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
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)

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
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)

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

            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2)

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
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)

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
