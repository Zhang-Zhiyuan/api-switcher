import json
import logging
import re
from pathlib import Path
from core.atomic_io import atomic_write_text
from core.auto_continue.base import AutoContinueProvider
from core.auto_continue.script_generator import generate_hook_script
from core.auto_continue.error_recovery_script import generate_codex_error_recovery_script

logger = logging.getLogger(__name__)


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
            event_hooks = _codex_event_hooks(data.get(event_name))
            managed_hooks = [
                hook for hook in event_hooks
                if _is_managed_codex_hook(str(hook.get("command", "")))
            ]
            if not managed_hooks:
                continue
            existing = _codex_event_hooks(hooks.get(event_name))
            hooks[event_name] = _format_codex_event_hooks(existing + managed_hooks)

            remaining = [
                hook for hook in event_hooks
                if not _is_managed_codex_hook(str(hook.get("command", "")))
            ]
            formatted = _format_legacy_codex_event_hooks(remaining)
            if formatted is None:
                data.pop(event_name, None)
            else:
                data[event_name] = formatted

    return hooks


def _codex_event_has_command(data: dict, event_name: str, marker: str) -> bool:
    hooks = _codex_hooks_container(data)
    candidates = _codex_event_hooks(hooks.get(event_name))
    candidates.extend(_codex_event_hooks(data.get(event_name)))
    return any(marker in str(hook.get("command", "")) for hook in candidates)


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
    existing = [
        hook for hook in _codex_event_hooks(hooks.get(event_name))
        if marker not in str(hook.get("command", ""))
    ]
    existing.append(hook_def)
    hooks[event_name] = _format_codex_event_hooks(existing)


def _remove_codex_event_hook(hooks: dict, event_name: str, marker: str) -> bool:
    if event_name not in hooks:
        return False
    existing = _codex_event_hooks(hooks.get(event_name))
    remaining = [hook for hook in existing if marker not in str(hook.get("command", ""))]
    if len(remaining) == len(existing):
        return False
    formatted = _format_codex_event_hooks(remaining)
    if formatted is None:
        hooks.pop(event_name, None)
    else:
        hooks[event_name] = formatted
    return True


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
                data = json.load(f)
            return _codex_event_has_command(data, "Stop", "auto_continue_stop.ps1")
        except Exception as e:
            logger.error(f"Failed to read hooks.json: {e}")
            return False

    def register_hook(self) -> None:
        """Register hook in hooks.json."""
        hooks_path = self.get_hooks_json_path()
        hooks_path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing hooks
        data = {}
        if hooks_path.exists():
            try:
                with open(hooks_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                pass
        if not isinstance(data, dict):
            data = {}

        hooks = _codex_hooks_container(data, migrate_legacy=True)

        # Register Stop hook
        script_path = str(self.get_hook_script_path()).replace("\\", "\\\\")
        _upsert_codex_event_hook(hooks, "Stop", {
            "type": "command",
            "command": f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
            "timeout": 10,
            "statusMessage": "Checking whether Codex should continue"
        }, "auto_continue_stop.ps1")

        # Write hooks.json
        atomic_write_text(hooks_path, json.dumps(data, indent=2))

        # Enable codex_hooks in config.toml
        self._enable_codex_hooks()

    def unregister_hook(self) -> None:
        """Unregister hook from hooks.json."""
        hooks_path = self.get_hooks_json_path()
        if not hooks_path.exists():
            return

        try:
            with open(hooks_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            hooks = _codex_hooks_container(data, migrate_legacy=True)

            # Remove Stop hook if it's ours
            changed = _remove_codex_event_hook(hooks, "Stop", "auto_continue_stop.ps1")

            # Write back
            atomic_write_text(hooks_path, json.dumps(data, indent=2))

            if changed and not _codex_data_has_entries(data):
                self._set_codex_hooks_enabled(False)
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

        atomic_write_text(script_path, script_content, encoding='utf-8-sig')

        logger.info(f"Installed hook script: {script_path}")

    def uninstall_hook_script(self) -> None:
        """Remove the hook script."""
        script_path = self.get_hook_script_path()
        if script_path.exists():
            script_path.unlink()

    def _enable_codex_hooks(self) -> None:
        """Enable codex_hooks in config.toml."""
        self._set_codex_hooks_enabled(True)

    def _set_codex_hooks_enabled(self, enabled: bool) -> None:
        """Set the root codex_hooks flag without rewriting unrelated TOML."""
        config_path = self.get_config_toml_path()
        if not enabled and not config_path.exists():
            return

        try:
            lines = []
            if config_path.exists():
                lines = config_path.read_text(encoding="utf-8").splitlines()

            value = "true" if enabled else "false"
            root_end = next((i for i, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
            found = False

            for i in range(root_end):
                if re.match(r"^\s*codex_hooks\s*=", lines[i]):
                    lines[i] = f"codex_hooks = {value}"
                    found = True
                    break

            if not found:
                lines.insert(root_end, f"codex_hooks = {value}")

            atomic_write_text(config_path, "\n".join(lines).rstrip() + "\n")
        except Exception as e:
            logger.error(f"Failed to update codex_hooks: {e}")

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

        # 加载设置以检查是否启用git
        settings = self.load_settings()
        enable_git = (
            bool(settings.git_auto_snapshot and settings.git_snapshot_on_recovery)
            if settings else True
        )

        settings_path = str(self.get_settings_path()).replace("\\", "\\\\")
        script_content = generate_codex_error_recovery_script(settings_path, enable_git)

        atomic_write_text(script_path, script_content, encoding='utf-8-sig')

        logger.info(f"Installed Codex error recovery script: {script_path}")

        # 注册到 hooks.json
        self._register_error_recovery_hook()

    def _register_error_recovery_hook(self) -> None:
        """注册错误恢复 Hook 到 hooks.json"""
        hooks_path = self.get_hooks_json_path()
        hooks_path.parent.mkdir(parents=True, exist_ok=True)

        # 读取现有 hooks
        data = {}
        if hooks_path.exists():
            try:
                with open(hooks_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                pass
        if not isinstance(data, dict):
            data = {}

        hooks = _codex_hooks_container(data, migrate_legacy=True)

        # 注册 Error hook
        script_path = str(self.get_error_recovery_script_path()).replace("\\", "\\\\")
        _upsert_codex_event_hook(hooks, "Error", {
            "type": "command",
            "command": f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
            "timeout": 10,
            "statusMessage": "Checking for Codex API errors and auto-recovery"
        }, "error_recovery.ps1")

        # 写入 hooks.json
        atomic_write_text(hooks_path, json.dumps(data, indent=2))

        self._enable_codex_hooks()

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
                data = json.load(f)
            if not isinstance(data, dict):
                return
            hooks = _codex_hooks_container(data, migrate_legacy=True)

            changed = _remove_codex_event_hook(hooks, "Error", "error_recovery.ps1")

            atomic_write_text(hooks_path, json.dumps(data, indent=2))

            if changed and not _codex_data_has_entries(data):
                self._set_codex_hooks_enabled(False)

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
                data = json.load(f)

            return _codex_event_has_command(data, "Error", "error_recovery.ps1")
        except Exception:
            return False

    def get_status(self):
        """Get status with error recovery check."""
        status = super().get_status()
        status.error_recovery_installed = self.is_error_recovery_installed()
        return status
