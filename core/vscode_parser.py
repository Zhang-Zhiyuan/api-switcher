import json
import logging
from pathlib import Path

from config.paths import VSCODE_SETTINGS
from core.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def read_vscode_settings() -> dict:
    if not VSCODE_SETTINGS.exists():
        return {}
    try:
        return json.loads(VSCODE_SETTINGS.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to read {VSCODE_SETTINGS}: {e}")
        return {}


def write_vscode_settings(data: dict) -> None:
    content = json.dumps(data, indent=4, ensure_ascii=False)
    _atomic_write(VSCODE_SETTINGS, content)


VSCODE_CLAUDE_INITIAL_PERMISSION_MODES = {"default", "acceptEdits", "plan", "bypassPermissions"}


def apply_permission_mode(settings: dict, permission_mode: str, skip_dangerous: bool) -> dict:
    """Apply Claude Code permission mode to VS Code settings.json."""
    settings = dict(settings)
    permission_mode = str(permission_mode or "default").strip() or "default"
    settings["claudeCode.allowDangerouslySkipPermissions"] = bool(skip_dangerous)
    if permission_mode in VSCODE_CLAUDE_INITIAL_PERMISSION_MODES:
        settings["claudeCode.initialPermissionMode"] = permission_mode
    else:
        settings.pop("claudeCode.initialPermissionMode", None)
    return settings


def apply_permissions(settings: dict, bypass: bool, skip_dangerous: bool) -> dict:
    """Apply legacy bypass/default permission settings to VS Code settings.json."""
    mode = "bypassPermissions" if bypass else "default"
    return apply_permission_mode(settings, mode, skip_dangerous)


def apply_model(settings: dict, model: str) -> dict:
    """Apply model selection to VS Code settings.json."""
    settings = dict(settings)
    settings["claudeCode.selectedModel"] = model
    return settings
