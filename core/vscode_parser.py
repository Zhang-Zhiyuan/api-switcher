import json
import logging
from pathlib import Path

from config.paths import VSCODE_SETTINGS
from core.atomic_io import atomic_write_text
from core.file_cache import CACHE_MISS, FileValueCache

logger = logging.getLogger(__name__)
_JSON_FILE_CACHE = FileValueCache()


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def read_vscode_settings() -> dict:
    try:
        VSCODE_SETTINGS.stat()
    except FileNotFoundError:
        _JSON_FILE_CACHE.set(VSCODE_SETTINGS, {})
        return {}
    except OSError as e:
        logger.error("Failed to access %s: %s", VSCODE_SETTINGS, e)
        _JSON_FILE_CACHE.clear(VSCODE_SETTINGS)
        raise

    cached = _JSON_FILE_CACHE.get(VSCODE_SETTINGS)
    if cached is not CACHE_MISS:
        return cached if isinstance(cached, dict) else {}

    try:
        data = json.loads(VSCODE_SETTINGS.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{VSCODE_SETTINGS} 的顶层 JSON 必须是对象")
        _JSON_FILE_CACHE.set(VSCODE_SETTINGS, data)
        return data
    except FileNotFoundError:
        _JSON_FILE_CACHE.set(VSCODE_SETTINGS, {})
        return {}
    except Exception as e:
        logger.error("Failed to read %s: %s", VSCODE_SETTINGS, e)
        _JSON_FILE_CACHE.clear(VSCODE_SETTINGS)
        raise


def write_vscode_settings(data: dict) -> None:
    content = json.dumps(data, indent=4, ensure_ascii=False)
    _atomic_write(VSCODE_SETTINGS, content)
    _JSON_FILE_CACHE.set(VSCODE_SETTINGS, data)


def clear_vscode_settings_cache(path: Path | None = None) -> None:
    _JSON_FILE_CACHE.clear(path)


VSCODE_CLAUDE_INITIAL_PERMISSION_MODES = {"default", "acceptEdits", "dontAsk", "plan", "bypassPermissions"}


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


def clear_claude_profile_overrides(settings: dict, official_model: str = "") -> dict:
    """Remove VS Code state left by an app-managed third-party profile.

    Account switching must not leave a relay-only model or a dangerous
    permission shortcut active in the extension after Claude Code itself has
    returned to official credentials.
    """
    settings = dict(settings)
    model = str(official_model or "").strip()
    if model:
        settings["claudeCode.selectedModel"] = model
    else:
        settings.pop("claudeCode.selectedModel", None)
    settings["claudeCode.initialPermissionMode"] = "default"
    settings["claudeCode.allowDangerouslySkipPermissions"] = False
    return settings
