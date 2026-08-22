import json
import logging
import re
from pathlib import Path

from config.paths import CLAUDE_SETTINGS, CLAUDE_CONFIG, CLAUDE_CREDENTIALS
from core.atomic_io import atomic_write_text
from core.file_cache import CACHE_MISS, FileValueCache
from core.providers import CLAUDE_CODE_MODEL_ALIASES, CLAUDE_OFFICIAL_DEFAULT_MODEL
from core.url_validation import normalize_claude_base_url

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
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
)

CLAUDE_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
CLAUDE_MODEL_NAME_ENV_KEYS = (
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
)
_FABLE_MODEL_RE = re.compile(
    r"^(?:claude[-_ ]*)?fable[-_ ]*5(?:\[(?:1m)\])?$",
    re.IGNORECASE,
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


def _fable_model_parts(model: object) -> tuple[str, str] | None:
    """Return the runtime and display names for Claude Code Fable 5.

    Fable is selected through Claude Code's stable ``opus`` alias.  The
    gateway model itself is kept in the default-model environment variables;
    this is the shape emitted by CC Switch and avoids Claude Code rejecting a
    provider-specific model name as the top-level model selection.
    """

    text = str(model or "").strip()
    if not text or not _FABLE_MODEL_RE.fullmatch(text):
        return None
    return "claude-fable-5[1M]", "claude-fable-5"


def claude_model_settings(model: object, provider_env: dict | None = None) -> tuple[str, dict[str, str]]:
    """Build the Claude Code model fields for a profile.

    Fable 5 needs an alias plus explicit default-model mappings.  Other
    providers retain the legacy direct model fields for compatibility, while
    stale Fable ``*_MODEL_NAME`` fields are always removed by the caller.
    """

    text = str(model or "").strip()
    fable = _fable_model_parts(text)
    if fable:
        runtime_model, display_model = fable
        env = {
            "ANTHROPIC_DEFAULT_FABLE_MODEL": runtime_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": runtime_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": runtime_model,
            # CC Switch intentionally keeps the Haiku fallback on the
            # non-1M model; some gateways do not expose a Haiku 1M variant.
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": display_model,
            "CLAUDE_CODE_SUBAGENT_MODEL": runtime_model,
            "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": display_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": display_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": display_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": display_model,
        }
        if provider_env:
            # Explicit provider mappings (for example GLM's Haiku fallback)
            # remain authoritative when a provider supplies them.
            env.update({str(key): str(value) for key, value in provider_env.items()})
        return "opus", env

    env = {key: text for key in CLAUDE_MODEL_ENV_KEYS} if text else {}
    if provider_env:
        env.update({str(key): str(value) for key, value in provider_env.items()})
    return text, env


def claude_model_from_settings(settings: dict) -> str:
    """Resolve a profile model from Claude settings, including alias mappings."""

    if not isinstance(settings, dict):
        return ""
    env = settings.get("env")
    env = env if isinstance(env, dict) else {}
    model = str(settings.get("model") or "").strip()
    alias_keys = {
        "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "opus[1m]": "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "opusplan": "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "sonnet[1m]": "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    }
    mapped = str(env.get(alias_keys.get(model.casefold(), "")) or "").strip()
    return mapped or str(env.get("ANTHROPIC_MODEL") or model).strip()


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
        # Claude Code appends /v1 itself.  Normalize copied OpenCode-style
        # ``.../v1`` values so the runtime never requests ``/v1/v1/...``.
        settings["env"]["ANTHROPIC_BASE_URL"] = normalize_claude_base_url(profile.base_url)
    else:
        settings["env"].pop("ANTHROPIC_BASE_URL", None)

    for key in CLAUDE_MODEL_OVERRIDE_ENV_KEYS:
        settings["env"].pop(key, None)
    settings["model"], model_env = claude_model_settings(
        profile.model,
        provider.claude_env if provider else None,
    )
    if model_env:
        settings["env"].update(model_env)

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
