import json
import logging
from pathlib import Path

from config.paths import CLAUDE_SETTINGS, CLAUDE_CONFIG, CLAUDE_CREDENTIALS
from core.atomic_io import atomic_write_text
from core.file_cache import CACHE_MISS, FileValueCache
from core.providers import CLAUDE_CODE_MODEL_ALIASES, CLAUDE_OFFICIAL_DEFAULT_MODEL

logger = logging.getLogger(__name__)
_JSON_FILE_CACHE = FileValueCache()

CLAUDE_AUTH_ENV_KEYS = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)
CLAUDE_MODEL_OVERRIDE_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
)


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def clear_claude_file_cache(path: Path | None = None) -> None:
    _JSON_FILE_CACHE.clear(path)


def _read_json_object(path: Path, encoding: str = "utf-8") -> dict:
    try:
        path.stat()
    except FileNotFoundError:
        _JSON_FILE_CACHE.set(path, {})
        return {}
    except OSError as e:
        logger.error("Failed to access %s: %s", path, e)
        _JSON_FILE_CACHE.clear(path)
        raise

    cached = _JSON_FILE_CACHE.get(path)
    if cached is not CACHE_MISS:
        return cached if isinstance(cached, dict) else {}

    try:
        data = json.loads(path.read_text(encoding=encoding))
        if not isinstance(data, dict):
            raise ValueError(f"{path} 的顶层 JSON 必须是对象")
        _JSON_FILE_CACHE.set(path, data)
        return data
    except FileNotFoundError:
        _JSON_FILE_CACHE.set(path, {})
        return {}
    except Exception as e:
        logger.error("Failed to read %s: %s", path, e)
        _JSON_FILE_CACHE.clear(path)
        raise


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
        env = dict(env)
        for key in (*CLAUDE_AUTH_ENV_KEYS, "ANTHROPIC_BASE_URL", *CLAUDE_MODEL_OVERRIDE_ENV_KEYS):
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
        settings["model"] = CLAUDE_OFFICIAL_DEFAULT_MODEL

    effort = str(settings.get("effortLevel") or "").strip()
    # ``max`` is valid only as CLAUDE_CODE_EFFORT_LEVEL (or a session
    # override); Claude Code explicitly rejects it in persistent
    # ``settings.json`` effortLevel.
    if effort and effort not in {"low", "medium", "high", "xhigh"}:
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
    settings["env"] = dict(settings.get("env")) if isinstance(settings.get("env"), dict) else {}

    from core.providers import ProviderRegistry

    provider = ProviderRegistry.get_provider(profile.provider)
    auth_scheme = str(
        getattr(profile, "auth_scheme", "")
        or ProviderRegistry.get_claude_auth_scheme(profile.provider)
    ).strip().lower()
    if auth_scheme not in {"auth_token", "api_key"}:
        auth_scheme = ProviderRegistry.get_claude_auth_scheme(profile.provider)

    # Get actual token value from security module
    token = _get_claude_profile_token(profile)
    for key in CLAUDE_AUTH_ENV_KEYS:
        settings["env"].pop(key, None)
    if token:
        env_key = "ANTHROPIC_API_KEY" if auth_scheme == "api_key" else "ANTHROPIC_AUTH_TOKEN"
        settings["env"][env_key] = token

    if profile.base_url:
        settings["env"]["ANTHROPIC_BASE_URL"] = profile.base_url
    else:
        settings["env"].pop("ANTHROPIC_BASE_URL", None)

    for key in CLAUDE_MODEL_OVERRIDE_ENV_KEYS:
        settings["env"].pop(key, None)
    if profile.model:
        for key in (
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_FABLE_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            settings["env"][key] = profile.model
    if provider and provider.claude_env:
        settings["env"].update(provider.claude_env)

    settings["model"] = profile.model

    # 根据提供商决定是否设置 effortLevel
    # 不支持推理力度的提供商会跳过该字段，避免向 API 发送无效参数。
    if ProviderRegistry.supports_reasoning_effort(profile.provider):
        settings["env"]["CLAUDE_CODE_EFFORT_LEVEL"] = profile.effort_level
        if profile.effort_level == "max":
            settings.pop("effortLevel", None)
        else:
            settings["effortLevel"] = profile.effort_level
    elif "effortLevel" in settings:
        # 如果提供商不支持推理力度，移除该字段
        del settings["effortLevel"]

    settings["skipDangerousModePermissionPrompt"] = profile.skip_dangerous_prompt

    # Permissions
    settings["permissions"] = (
        dict(settings.get("permissions")) if isinstance(settings.get("permissions"), dict) else {}
    )
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
    """Remove the legacy plaintext auth field while preserving unrelated fields."""
    config = dict(config)
    config.pop("primaryApiKey", None)
    return config
