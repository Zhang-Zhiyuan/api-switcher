import json
import logging
from pathlib import Path

from config.paths import CLAUDE_SETTINGS, CLAUDE_CONFIG, CLAUDE_CREDENTIALS
from core.atomic_io import atomic_write_text
from core.file_cache import CACHE_MISS, FileValueCache
from core.providers import CLAUDE_CODE_MODEL_ALIASES

logger = logging.getLogger(__name__)
_JSON_FILE_CACHE = FileValueCache()


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def clear_claude_file_cache(path: Path | None = None) -> None:
    _JSON_FILE_CACHE.clear(path)


def _read_json_object(path: Path, encoding: str = "utf-8") -> dict:
    cached = _JSON_FILE_CACHE.get(path)
    if cached is not CACHE_MISS:
        return cached if isinstance(cached, dict) else {}

    if not path.exists():
        _JSON_FILE_CACHE.set(path, {})
        return {}
    try:
        data = json.loads(path.read_text(encoding=encoding))
        if not isinstance(data, dict):
            logger.error(f"Failed to read {path}: top-level JSON is not an object")
            _JSON_FILE_CACHE.set(path, {})
            return {}
        _JSON_FILE_CACHE.set(path, data)
        return data
    except Exception as e:
        logger.error(f"Failed to read {path}: {e}")
        _JSON_FILE_CACHE.clear(path)
        return {}


def read_claude_settings() -> dict:
    return _read_json_object(CLAUDE_SETTINGS)


def write_claude_settings(data: dict) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False)
    _atomic_write(CLAUDE_SETTINGS, content)
    _JSON_FILE_CACHE.set(CLAUDE_SETTINGS, data)


def read_claude_config() -> dict:
    return _read_json_object(CLAUDE_CONFIG)


def write_claude_config(data: dict) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False)
    _atomic_write(CLAUDE_CONFIG, content)
    _JSON_FILE_CACHE.set(CLAUDE_CONFIG, data)


def read_claude_credentials() -> dict:
    return _read_json_object(CLAUDE_CREDENTIALS)


def write_claude_credentials(data: dict) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False)
    _atomic_write(CLAUDE_CREDENTIALS, content)
    _JSON_FILE_CACHE.set(CLAUDE_CREDENTIALS, data)


def clear_claude_api_overrides(settings: dict) -> dict:
    """Remove settings that make Claude Code prefer API keys over login credentials."""
    settings = dict(settings)
    env = settings.get("env")
    if isinstance(env, dict):
        for key in [
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        ]:
            env.pop(key, None)
        if env:
            settings["env"] = env
        else:
            settings.pop("env", None)

    # Third-party Claude profiles can leave non-Claude model names behind.
    # When returning to an official login, keep valid Claude model choices but
    # fall back to the current app default if the model clearly belongs elsewhere.
    model = str(settings.get("model") or "").strip()
    if model and not _is_claude_code_model(model):
        settings["model"] = "claude-sonnet-4"

    effort = str(settings.get("effortLevel") or "").strip()
    if effort and effort not in {"low", "medium", "high", "xhigh", "max"}:
        settings["effortLevel"] = "high"
    return settings


def _is_claude_code_model(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    if normalized in CLAUDE_CODE_MODEL_ALIASES:
        return True
    return normalized.startswith("claude-")


def clear_claude_config_auth(config: dict) -> dict:
    """Remove API-key auth fields from Claude config while preserving other settings."""
    config = dict(config)
    config.pop("primaryApiKey", None)
    return config


def _get_claude_profile_token(profile) -> str | None:
    from core import security

    token = security.get_secret(profile.auth_token_ref)
    if token:
        return token
    return security.get_secret(getattr(profile, "primary_api_key_ref", None))


def apply_claude_profile(settings: dict, profile) -> dict:
    """Apply a ClaudeProfile to settings dict. Only modifies API-related fields."""
    settings = dict(settings)

    # Ensure env dict exists
    if not isinstance(settings.get("env"), dict):
        settings["env"] = {}

    from core.providers import ProviderRegistry

    provider = ProviderRegistry.get_provider(profile.provider)

    # Get actual token value from security module
    token = _get_claude_profile_token(profile)
    if token:
        settings["env"]["ANTHROPIC_AUTH_TOKEN"] = token
        settings["env"]["ANTHROPIC_API_KEY"] = token
    else:
        settings["env"].pop("ANTHROPIC_AUTH_TOKEN", None)
        settings["env"].pop("ANTHROPIC_API_KEY", None)

    if profile.base_url:
        settings["env"]["ANTHROPIC_BASE_URL"] = profile.base_url
    else:
        settings["env"].pop("ANTHROPIC_BASE_URL", None)

    for key in [
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ]:
        settings["env"].pop(key, None)
    if provider and provider.claude_env:
        settings["env"].update(provider.claude_env)

    settings["model"] = profile.model

    # 根据提供商决定是否设置 effortLevel
    # 不支持推理力度的提供商会跳过该字段，避免向 API 发送无效参数。
    if ProviderRegistry.supports_reasoning_effort(profile.provider):
        settings["effortLevel"] = profile.effort_level
    elif "effortLevel" in settings:
        # 如果提供商不支持推理力度，移除该字段
        del settings["effortLevel"]

    settings["skipDangerousModePermissionPrompt"] = profile.skip_dangerous_prompt

    # Permissions
    if not isinstance(settings.get("permissions"), dict):
        settings["permissions"] = {}
    settings["permissions"]["defaultMode"] = profile.permissions_mode

    if profile.permissions_allow:
        settings["permissions"]["allow"] = profile.permissions_allow
    else:
        settings["permissions"].pop("allow", None)

    if profile.additional_directories:
        settings["additionalDirectories"] = profile.additional_directories
    else:
        settings.pop("additionalDirectories", None)

    return settings


def apply_claude_config(config: dict, profile) -> dict:
    """Apply Claude auth state to config.json while preserving unrelated fields."""
    from core import security

    config = dict(config)
    primary_key = security.get_secret(getattr(profile, "primary_api_key_ref", None))
    if not primary_key:
        primary_key = security.get_secret(profile.auth_token_ref)

    if primary_key:
        config["primaryApiKey"] = primary_key
    else:
        config.pop("primaryApiKey", None)

    return config
