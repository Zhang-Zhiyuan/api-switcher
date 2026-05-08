import json
import logging
from pathlib import Path

from config.paths import VSCODE_SETTINGS

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


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


def apply_permissions(settings: dict, bypass: bool, skip_dangerous: bool) -> dict:
    """Apply permission-related settings to VS Code settings.json."""
    settings = dict(settings)
    settings["claudeCode.allowDangerouslySkipPermissions"] = bypass
    settings["claudeCode.initialPermissionMode"] = "bypassPermissions" if bypass else "default"
    return settings


def apply_model(settings: dict, model: str) -> dict:
    """Apply model selection to VS Code settings.json."""
    settings = dict(settings)
    settings["claudeCode.selectedModel"] = model
    return settings
