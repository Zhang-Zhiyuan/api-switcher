"""Environment variable name validation shared by configuration flows."""
from __future__ import annotations

import re


ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CODEX_SECRET_ENV_NAME_RE = re.compile(
    r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:KEY|TOKEN|SECRET|PASSWORD)$"
)

# Codex ``model_providers.<id>.env_key`` is persisted into the process, the
# user's environment and Codex's .env file.  Never let that field target
# process-control or filesystem-location variables, even if a future pattern
# change accidentally makes one of them look credential-like.
DANGEROUS_CODEX_ENV_NAMES = frozenset(
    {
        "ALL_PROXY",
        "APPDATA",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "COMSPEC",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "LOCALAPPDATA",
        "LOGNAME",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PSMODULEPATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "SHELL",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)


def validate_codex_env_key(value: object) -> str:
    """Return a safe Codex provider credential variable name.

    The switcher persists this name at several scopes.  Restrict it to an
    uppercase, credential-shaped variable so a typo cannot replace PATH,
    HOME, APPDATA or another process-control variable with an API key.
    """
    name = str(value or "").strip()
    if not name:
        raise ValueError("Codex Provider 环境变量名不能为空")
    if not ENV_NAME_RE.fullmatch(name):
        raise ValueError(f"Codex Provider 环境变量名无效: {name}")

    normalized = name.upper()
    if name != normalized:
        raise ValueError("Codex Provider 环境变量名必须使用大写字母、数字和下划线")
    if normalized in DANGEROUS_CODEX_ENV_NAMES:
        raise ValueError(f"禁止把系统环境变量 {normalized} 用作 Codex API Key")
    if not CODEX_SECRET_ENV_NAME_RE.fullmatch(normalized):
        raise ValueError(
            "Codex Provider 环境变量名必须是专用密钥变量，"
            "例如 MY_PROVIDER_API_KEY 或 MY_PROVIDER_TOKEN"
        )
    return normalized
