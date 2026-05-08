import json
import logging
from pathlib import Path
from core.auto_continue.base import AutoContinueProvider
from core.auto_continue.script_generator import generate_hook_script
from core.auto_continue.error_recovery_script import generate_codex_error_recovery_script

logger = logging.getLogger(__name__)


class CodexProvider(AutoContinueProvider):
    """Auto-continue provider for Codex CLI."""

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

    def get_agents_md_path(self) -> Path:
        return self.get_config_dir() / "AGENTS.md"

    def is_hook_registered(self) -> bool:
        """Check if hook is registered in hooks.json."""
        hooks_path = self.get_hooks_json_path()
        if not hooks_path.exists():
            return False
        try:
            with open(hooks_path, 'r', encoding='utf-8') as f:
                hooks = json.load(f)
            stop_hook = hooks.get("Stop", {})
            command = stop_hook.get("command", "")
            return "auto_continue_stop.ps1" in command
        except Exception as e:
            logger.error(f"Failed to read hooks.json: {e}")
            return False

    def register_hook(self) -> None:
        """Register hook in hooks.json."""
        hooks_path = self.get_hooks_json_path()
        hooks_path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing hooks
        hooks = {}
        if hooks_path.exists():
            try:
                with open(hooks_path, 'r', encoding='utf-8') as f:
                    hooks = json.load(f)
            except Exception:
                pass

        # Register Stop hook
        script_path = str(self.get_hook_script_path()).replace("\\", "\\\\")
        hooks["Stop"] = {
            "command": f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
            "timeout": 10
        }

        # Write hooks.json
        with open(hooks_path, 'w', encoding='utf-8') as f:
            json.dump(hooks, f, indent=2)

        # Enable codex_hooks in config.toml
        self._enable_codex_hooks()

    def unregister_hook(self) -> None:
        """Unregister hook from hooks.json."""
        hooks_path = self.get_hooks_json_path()
        if not hooks_path.exists():
            return

        try:
            with open(hooks_path, 'r', encoding='utf-8') as f:
                hooks = json.load(f)

            # Remove Stop hook if it's ours
            if "Stop" in hooks:
                command = hooks["Stop"].get("command", "")
                if "auto_continue_stop.ps1" in command:
                    del hooks["Stop"]

            # Write back
            with open(hooks_path, 'w', encoding='utf-8') as f:
                json.dump(hooks, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to unregister hook: {e}")

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
        script_content = generate_hook_script(settings_path, enable_git)

        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(script_content)

        logger.info(f"Installed hook script: {script_path}")

    def uninstall_hook_script(self) -> None:
        """Remove the hook script."""
        script_path = self.get_hook_script_path()
        if script_path.exists():
            script_path.unlink()

    def _enable_codex_hooks(self) -> None:
        """Enable codex_hooks in config.toml."""
        config_path = self.get_config_toml_path()
        if not config_path.exists():
            return

        try:
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib
            with open(config_path, 'rb') as f:
                config = tomllib.load(f)

            config["codex_hooks"] = True

            import tomli_w
            tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
            with open(tmp_path, 'wb') as f:
                tomli_w.dump(config, f)
            tmp_path.replace(config_path)
        except Exception as e:
            logger.error(f"Failed to enable codex_hooks: {e}")

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
            with open(agents_md, 'a', encoding='utf-8') as f:
                f.write(guidance)

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

        agents_md.write_text('\n'.join(filtered), encoding='utf-8')

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
        script_content = generate_codex_error_recovery_script(settings_path, enable_git)

        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(script_content)

        logger.info(f"Installed Codex error recovery script: {script_path}")

        # 注册到 hooks.json
        self._register_error_recovery_hook()

    def _register_error_recovery_hook(self) -> None:
        """注册错误恢复 Hook 到 hooks.json"""
        hooks_path = self.get_hooks_json_path()
        hooks_path.parent.mkdir(parents=True, exist_ok=True)

        # 读取现有 hooks
        hooks = {}
        if hooks_path.exists():
            try:
                with open(hooks_path, 'r', encoding='utf-8') as f:
                    hooks = json.load(f)
            except Exception:
                pass

        # 注册 Error hook
        script_path = str(self.get_error_recovery_script_path()).replace("\\", "\\\\")
        hooks["Error"] = {
            "command": f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
            "timeout": 10
        }

        # 写入 hooks.json
        with open(hooks_path, 'w', encoding='utf-8') as f:
            json.dump(hooks, f, indent=2)

        logger.info("Registered Codex error recovery hook")

    def uninstall_error_recovery(self) -> None:
        """卸载错误恢复功能"""
        # 删除脚本
        script_path = self.get_error_recovery_script_path()
        if script_path.exists():
            script_path.unlink()

        # 从 hooks.json 移除
        hooks_path = self.get_hooks_json_path()
        if not hooks_path.exists():
            return

        try:
            with open(hooks_path, 'r', encoding='utf-8') as f:
                hooks = json.load(f)

            if "Error" in hooks:
                command = hooks["Error"].get("command", "")
                if "error_recovery.ps1" in command:
                    del hooks["Error"]

            with open(hooks_path, 'w', encoding='utf-8') as f:
                json.dump(hooks, f, indent=2)

            logger.info("Uninstalled Codex error recovery hook")
        except Exception as e:
            logger.error(f"Failed to uninstall error recovery: {e}")

    def is_error_recovery_installed(self) -> bool:
        """检查错误恢复是否已安装"""
        script_path = self.get_error_recovery_script_path()
        if not script_path.exists():
            return False

        hooks_path = self.get_hooks_json_path()
        if not hooks_path.exists():
            return False

        try:
            with open(hooks_path, 'r', encoding='utf-8') as f:
                hooks = json.load(f)

            if "Error" in hooks:
                command = hooks["Error"].get("command", "")
                return "error_recovery.ps1" in command
            return False
        except Exception:
            return False

    def get_status(self):
        """Get status with error recovery check."""
        status = super().get_status()
        status.error_recovery_installed = self.is_error_recovery_installed()
        return status
