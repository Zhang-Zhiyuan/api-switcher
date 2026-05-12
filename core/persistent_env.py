from __future__ import annotations

import ctypes
import logging
import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

logger = logging.getLogger(__name__)

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXPORT_LINE_RE = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=")
WINDOWS_EXPAND_REF_RE = re.compile(r"%[^%\r\n]+%")

PROVIDER_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "glm": "ZHIPUAI_API_KEY",
}
NON_SECRET_ENV_NAMES = {
    "HF_HOME",
    "HF_ENDPOINT",
    "OPENAI_BASE_URL",
    "ANTHROPIC_BASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "RCLONE_CONFIG",
}
SENSITIVE_NAME_PARTS = ("TOKEN", "KEY", "SECRET", "PASSWORD")

REMOTE_ENV_FILENAME = ".api_switcher_env"
REMOTE_SOURCE_BEGIN = "# >>> API_SWITCHER_ENV_SOURCE >>>"
REMOTE_SOURCE_END = "# <<< API_SWITCHER_ENV_SOURCE <<<"
REMOTE_SOURCE_BLOCK = (
    f"{REMOTE_SOURCE_BEGIN}\n"
    f'[ -f "$HOME/{REMOTE_ENV_FILENAME}" ] && . "$HOME/{REMOTE_ENV_FILENAME}"\n'
    f"{REMOTE_SOURCE_END}\n"
)
REMOTE_SHELL_FILES = (
    (".profile", True),
    (".bashrc", True),
    (".bash_profile", False),
    (".bash_login", False),
    (".zprofile", False),
    (".zshrc", False),
)


@dataclass(frozen=True)
class EnvVariableSpec:
    name: str
    label: str
    category: str


@dataclass(frozen=True)
class EnvImportSource:
    label: str
    env_name: str
    value: str
    source_type: str
    details: str = ""

    def masked_value(self) -> str:
        return mask_secret(self.value)

    def preview_value(self) -> str:
        return preview_env_value(self.env_name, self.value)

    def display_label(self) -> str:
        return f"{self.label}  |  {self.env_name}={self.preview_value()}"


@dataclass
class EnvWriteResult:
    """Non-secret summary of a persistent environment variable write."""

    target: str
    variable_names: list[str]
    action: str = "已写入"
    details: str = ""
    env_file: str | None = None
    shell_files: list[str] = field(default_factory=list)

    def summary(self) -> str:
        names = ", ".join(self.variable_names)
        return f"{self.action} {self.target}: {names}"


ENV_VARIABLE_SPECS = [
    EnvVariableSpec("HF_TOKEN", "Hugging Face Token", "AI / Model Hub"),
    EnvVariableSpec("HF_HOME", "Hugging Face Cache Dir", "AI / Model Hub"),
    EnvVariableSpec("HF_ENDPOINT", "Hugging Face Endpoint", "AI / Model Hub"),
    EnvVariableSpec("OPENAI_API_KEY", "OpenAI-compatible API Key", "AI API"),
    EnvVariableSpec("OPENAI_BASE_URL", "OpenAI-compatible Base URL", "AI API"),
    EnvVariableSpec("ANTHROPIC_API_KEY", "Anthropic API Key", "AI API"),
    EnvVariableSpec("ANTHROPIC_AUTH_TOKEN", "Claude Code Auth Token", "AI API"),
    EnvVariableSpec("ANTHROPIC_BASE_URL", "Claude Code Base URL", "AI API"),
    EnvVariableSpec("DEEPSEEK_API_KEY", "DeepSeek API Key", "AI API"),
    EnvVariableSpec("KIMI_API_KEY", "Kimi API Key", "AI API"),
    EnvVariableSpec("MOONSHOT_API_KEY", "Moonshot/Kimi API Key", "AI API"),
    EnvVariableSpec("ZHIPUAI_API_KEY", "Zhipu/GLM API Key", "AI API"),
    EnvVariableSpec("GEMINI_API_KEY", "Gemini API Key", "Google"),
    EnvVariableSpec("GOOGLE_API_KEY", "Google API Key", "Google"),
    EnvVariableSpec("GOOGLE_APPLICATION_CREDENTIALS", "Google Service Account JSON Path", "Google"),
    EnvVariableSpec("GOOGLE_CLOUD_PROJECT", "Google Cloud Project", "Google"),
    EnvVariableSpec("GOOGLE_DRIVE_API_KEY", "Google Drive API Key", "Google Drive"),
    EnvVariableSpec("GOOGLE_DRIVE_CLIENT_ID", "Google Drive OAuth Client ID", "Google Drive"),
    EnvVariableSpec("GOOGLE_DRIVE_CLIENT_SECRET", "Google Drive OAuth Client Secret", "Google Drive"),
    EnvVariableSpec("GOOGLE_DRIVE_REFRESH_TOKEN", "Google Drive OAuth Refresh Token", "Google Drive"),
    EnvVariableSpec("GDRIVE_TOKEN", "Google Drive Tool Token", "Google Drive"),
    EnvVariableSpec("RCLONE_CONFIG", "Rclone Config Path", "Storage"),
    EnvVariableSpec("HTTP_PROXY", "HTTP Proxy", "Network"),
    EnvVariableSpec("HTTPS_PROXY", "HTTPS Proxy", "Network"),
    EnvVariableSpec("NO_PROXY", "No Proxy Hosts", "Network"),
]

COMMON_ENV_NAMES = [spec.name for spec in ENV_VARIABLE_SPECS]


def normalize_env_updates(updates: Mapping[str, str]) -> dict[str, str]:
    """Validate and normalize environment variable updates."""
    if not updates:
        raise ValueError("请填写要写入的环境变量")

    normalized: dict[str, str] = {}
    for raw_name, raw_value in updates.items():
        name = str(raw_name or "").strip()
        value = str(raw_value or "").strip()

        if not name:
            raise ValueError("环境变量名不能为空")
        if not ENV_NAME_RE.match(name):
            raise ValueError(f"环境变量名无效: {name}")
        if not value:
            raise ValueError(f"{name} 的值不能为空")
        if any(ch in value for ch in ("\0", "\r", "\n")):
            raise ValueError(f"{name} 的值不能包含换行或 NUL 字符")

        normalized[name] = value

    return normalized


def normalize_env_names(names: Iterable[str] | str) -> list[str]:
    """Validate and normalize environment variable names."""
    if isinstance(names, str):
        candidates = [names]
    else:
        candidates = list(names or [])
    if not candidates:
        raise ValueError("请选择要删除的环境变量")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_name in candidates:
        name = str(raw_name or "").strip()
        if not name:
            raise ValueError("环境变量名不能为空")
        if not ENV_NAME_RE.match(name):
            raise ValueError(f"环境变量名无效: {name}")
        if name not in seen:
            normalized.append(name)
            seen.add(name)

    return normalized


def mask_secret(value: str, prefix: int = 8, suffix: int = 4) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= prefix + suffix + 3:
        return "*" * min(len(text), 8)
    return f"{text[:prefix]}...{text[-suffix:]}"


def _truncate_value(value: str, limit: int = 64) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def is_sensitive_env_name(name: str) -> bool:
    normalized = str(name or "").upper()
    if normalized in NON_SECRET_ENV_NAMES:
        return False
    return any(part in normalized for part in SENSITIVE_NAME_PARTS)


def preview_env_value(name: str, value: str) -> str:
    text = str(value or "")
    if is_sensitive_env_name(name):
        return mask_secret(text)
    return _truncate_value(text)


def _uses_windows_expansion(value: str, name: str = "") -> bool:
    if name and is_sensitive_env_name(name):
        return False
    return bool(WINDOWS_EXPAND_REF_RE.search(str(value or "")))


def _windows_registry_value_type(name: str, value: str):
    import winreg

    return winreg.REG_EXPAND_SZ if _uses_windows_expansion(value, name) else winreg.REG_SZ


def _provider_env_key(provider_name: str | None) -> str:
    return PROVIDER_ENV_KEYS.get(str(provider_name or "").strip(), "OPENAI_API_KEY")


def _append_source(sources: list[EnvImportSource], seen: set[tuple[str, str, str]], source: EnvImportSource) -> None:
    if not source.value:
        return
    key = (source.source_type, source.label, source.env_name)
    if key in seen:
        return
    sources.append(source)
    seen.add(key)


def _local_user_env_value(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            value, _value_type = winreg.QueryValueEx(key, name)
            return str(value)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug("Failed to read Windows user environment variable %s: %s", name, e)
        return None


def _environment_value(name: str) -> str:
    return os.environ.get(name) or _local_user_env_value(name) or ""


def _current_config_sources(sources: list[EnvImportSource], seen: set[tuple[str, str, str]]) -> None:
    try:
        from core import auth_parser, parser

        claude = parser.read_claude_settings()
        env = claude.get("env", {}) if isinstance(claude, dict) else {}
        env = env if isinstance(env, dict) else {}
        for name in ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"]:
            value = str(env.get(name) or "")
            _append_source(
                sources,
                seen,
                EnvImportSource(f"当前 Claude 配置: {name}", name, value, "current_config"),
            )

        codex_auth = auth_parser.read_codex_auth()
        if isinstance(codex_auth, dict):
            _append_source(
                sources,
                seen,
                EnvImportSource(
                    "当前 Codex Auth: OPENAI_API_KEY",
                    "OPENAI_API_KEY",
                    str(codex_auth.get("OPENAI_API_KEY") or ""),
                    "current_config",
                ),
            )
    except Exception as e:
        logger.debug("Failed to collect current config env sources: %s", e)


def _profile_sources(sources: list[EnvImportSource], seen: set[tuple[str, str, str]]) -> None:
    try:
        from core import profile_manager, security

        for profile in profile_manager.list_switchable_claude_profiles():
            token = (
                security.get_secret(getattr(profile, "auth_token_ref", None))
                or security.get_secret(getattr(profile, "primary_api_key_ref", None))
                or ""
            )
            if not token:
                continue
            provider = getattr(profile, "provider", "")
            _append_source(
                sources,
                seen,
                EnvImportSource(
                    f"Claude API: {profile.name} -> ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_AUTH_TOKEN",
                    token,
                    "profile",
                    details=f"provider={provider}",
                ),
            )
            provider_key = _provider_env_key(provider)
            if provider_key != "ANTHROPIC_API_KEY":
                _append_source(
                    sources,
                    seen,
                    EnvImportSource(
                        f"Claude API: {profile.name} -> {provider_key}",
                        provider_key,
                        token,
                        "profile",
                        details=f"provider={provider}",
                    ),
                )

        for profile in profile_manager.list_switchable_codex_profiles():
            token = security.get_secret(getattr(profile, "api_key_ref", None)) or ""
            if not token:
                continue
            provider = getattr(profile, "model_provider", "")
            _append_source(
                sources,
                seen,
                EnvImportSource(
                    f"Codex API: {profile.name} -> OPENAI_API_KEY",
                    "OPENAI_API_KEY",
                    token,
                    "profile",
                    details=f"provider={provider}",
                ),
            )
            provider_key = _provider_env_key(provider)
            if provider_key != "OPENAI_API_KEY":
                _append_source(
                    sources,
                    seen,
                    EnvImportSource(
                        f"Codex API: {profile.name} -> {provider_key}",
                        provider_key,
                        token,
                        "profile",
                        details=f"provider={provider}",
                    ),
                )
    except Exception as e:
        logger.debug("Failed to collect profile env sources: %s", e)


def list_env_import_sources(include_environment: bool = True, include_profiles: bool = True) -> list[EnvImportSource]:
    """Return local non-displayed secret sources that can prefill env writes."""
    sources: list[EnvImportSource] = []
    seen: set[tuple[str, str, str]] = set()

    if include_profiles:
        _profile_sources(sources, seen)
        _current_config_sources(sources, seen)

    if include_environment:
        for spec in ENV_VARIABLE_SPECS:
            value = _environment_value(spec.name)
            _append_source(
                sources,
                seen,
                EnvImportSource(
                    f"本机环境: {spec.name}",
                    spec.name,
                    value,
                    "environment",
                    details=spec.label,
                ),
            )

    return sources


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _export_line(name: str, value: str) -> str:
    return f"export {name}={_shell_quote(value)}"


def _upsert_env_exports(content: str | None, variables: Mapping[str, str]) -> str:
    """Upsert export lines while preserving other variables managed by this file."""
    lines = (content or "").splitlines()
    if not lines:
        lines = [
            "# Managed by API 配置切换器.",
            "# This file is sourced from the current user's shell startup files.",
        ]

    output: list[str] = []
    replaced: set[str] = set()

    for line in lines:
        match = EXPORT_LINE_RE.match(line)
        if match and match.group(1) in variables:
            name = match.group(1)
            if name not in replaced:
                output.append(_export_line(name, variables[name]))
                replaced.add(name)
            continue
        output.append(line)

    if output and output[-1].strip():
        output.append("")

    for name, value in variables.items():
        if name not in replaced:
            output.append(_export_line(name, value))

    return "\n".join(output).rstrip() + "\n"


def _remove_env_exports(content: str | None, names: Iterable[str]) -> str:
    remove_names = set(names)
    output: list[str] = []
    for line in (content or "").splitlines():
        match = EXPORT_LINE_RE.match(line)
        if match and match.group(1) in remove_names:
            continue
        output.append(line)
    if not output:
        return ""
    return "\n".join(output).rstrip() + "\n"


def _ensure_source_block(content: str | None) -> str:
    text = content or ""
    pattern = re.compile(
        re.escape(REMOTE_SOURCE_BEGIN) + r".*?" + re.escape(REMOTE_SOURCE_END) + r"\n?",
        re.DOTALL,
    )
    if pattern.search(text):
        return pattern.sub(REMOTE_SOURCE_BLOCK, text)

    if text and not text.endswith("\n"):
        text += "\n"
    if text.strip():
        text += "\n"
    return text + REMOTE_SOURCE_BLOCK


def _broadcast_windows_environment_change() -> None:
    try:
        hwnd_broadcast = 0xFFFF
        wm_setting_change = 0x001A
        smto_abort_if_hung = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            hwnd_broadcast,
            wm_setting_change,
            0,
            "Environment",
            smto_abort_if_hung,
            5000,
            ctypes.byref(result),
        )
    except Exception as e:
        logger.debug("Failed to broadcast Windows environment change: %s", e)


def set_local_user_env(updates: Mapping[str, str]) -> EnvWriteResult:
    """Persist environment variables for the current Windows user."""
    variables = normalize_env_updates(updates)
    if os.name != "nt":
        raise RuntimeError("本机永久环境变量写入目前支持 Windows 当前用户")

    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        for name, value in variables.items():
            value_type = _windows_registry_value_type(name, value)
            winreg.SetValueEx(key, name, 0, value_type, value)
            os.environ[name] = value

    _broadcast_windows_environment_change()
    return EnvWriteResult(
        target="当前 Windows 用户",
        variable_names=list(variables.keys()),
        details="新打开的终端会自动读取；已经打开的终端需要重开。",
    )


def delete_local_user_env(names: Iterable[str] | str) -> EnvWriteResult:
    """Delete persistent environment variables for the current Windows user."""
    variable_names = normalize_env_names(names)
    if os.name != "nt":
        raise RuntimeError("本机永久环境变量删除目前支持 Windows 当前用户")

    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        for name in variable_names:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
            os.environ.pop(name, None)

    _broadcast_windows_environment_change()
    return EnvWriteResult(
        target="当前 Windows 用户",
        variable_names=variable_names,
        action="已删除",
        details="新打开的终端会看到删除后的环境；已经打开的终端需要重开。",
    )


def set_remote_user_env(client, updates: Mapping[str, str]) -> EnvWriteResult:
    """Persist environment variables for the SSH login user's shell startup."""
    variables = normalize_env_updates(updates)

    from core import remote_config
    from core.ssh_manager import ssh_manager

    home = remote_config._remote_home(client)
    env_path = posixpath.join(home, REMOTE_ENV_FILENAME)

    current_env = ssh_manager.read_remote_file(client, env_path) or ""
    ssh_manager.write_remote_file(
        client,
        env_path,
        _upsert_env_exports(current_env, variables),
        file_mode=0o600,
    )

    touched_shell_files: list[str] = []
    for filename, create_if_missing in REMOTE_SHELL_FILES:
        path = posixpath.join(home, filename)
        current = ssh_manager.read_remote_file(client, path)
        if current is None and not create_if_missing:
            continue

        updated = _ensure_source_block(current or "")
        if updated != (current or ""):
            ssh_manager.write_remote_file(client, path, updated)
        touched_shell_files.append(path)

    return EnvWriteResult(
        target=f"SSH 登录用户 {home}",
        variable_names=list(variables.keys()),
        details="写入当前 SSH 用户 HOME，不会修改系统级 /etc/environment。",
        env_file=env_path,
        shell_files=touched_shell_files,
    )


def delete_remote_user_env(client, names: Iterable[str] | str) -> EnvWriteResult:
    """Delete persistent environment variables for the SSH login user's shell startup."""
    variable_names = normalize_env_names(names)

    from core import remote_config
    from core.ssh_manager import ssh_manager

    home = remote_config._remote_home(client)
    env_path = posixpath.join(home, REMOTE_ENV_FILENAME)

    current_env = ssh_manager.read_remote_file(client, env_path)
    if current_env is not None:
        ssh_manager.write_remote_file(
            client,
            env_path,
            _remove_env_exports(current_env, variable_names),
            file_mode=0o600,
        )

    return EnvWriteResult(
        target=f"SSH 登录用户 {home}",
        variable_names=variable_names,
        action="已删除",
        details="只移除当前 SSH 用户环境文件中的对应变量，其他变量会保留。",
        env_file=env_path,
    )
