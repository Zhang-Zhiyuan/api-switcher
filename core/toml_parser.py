import logging
from pathlib import Path

from config.paths import CODEX_CONFIG
from core.atomic_io import atomic_write_text
from core.file_cache import CACHE_MISS, FileValueCache

logger = logging.getLogger(__name__)
_TOML_FILE_CACHE = FileValueCache()


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def clear_codex_config_cache(path: Path | None = None) -> None:
    _TOML_FILE_CACHE.clear(path)


def read_codex_config() -> dict:
    cached = _TOML_FILE_CACHE.get(CODEX_CONFIG)
    if cached is not CACHE_MISS:
        return cached if isinstance(cached, dict) else {}

    if not CODEX_CONFIG.exists():
        _TOML_FILE_CACHE.set(CODEX_CONFIG, {})
        return {}
    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        with open(CODEX_CONFIG, "rb") as f:
            data = tomllib.load(f)
        if not isinstance(data, dict):
            _TOML_FILE_CACHE.set(CODEX_CONFIG, {})
            return {}
        _TOML_FILE_CACHE.set(CODEX_CONFIG, data)
        return data
    except Exception as e:
        logger.error(f"Failed to read {CODEX_CONFIG}: {e}")
        _TOML_FILE_CACHE.clear(CODEX_CONFIG)
        return {}


def write_codex_config(data: dict) -> None:
    try:
        import tomli_w
        content = tomli_w.dumps(data)
        _atomic_write(CODEX_CONFIG, content)
        _TOML_FILE_CACHE.set(CODEX_CONFIG, data)
    except Exception as e:
        logger.error(f"Failed to write {CODEX_CONFIG}: {e}")
        _TOML_FILE_CACHE.clear(CODEX_CONFIG)
        raise


def sanitize_codex_config(data: dict) -> dict:
    """Normalize Codex config values that can make newer CLI builds fail fast."""
    data = dict(data or {})
    model_providers = data.get("model_providers")
    if isinstance(model_providers, dict):
        for table in model_providers.values():
            if not isinstance(table, dict):
                continue
            wire_api = str(table.get("wire_api") or "").strip().lower()
            if wire_api and wire_api != "responses":
                table["wire_api"] = "responses"
    return data


def apply_codex_profile(config: dict, profile) -> dict:
    """Apply a CodexProfile to config dict. Only modifies model/provider fields."""
    config = sanitize_codex_config(config)

    config["model"] = profile.model
    config["model_provider"] = profile.model_provider

    # 根据提供商决定是否设置 model_reasoning_effort
    from core.providers import ProviderRegistry

    provider = ProviderRegistry.get_provider(profile.model_provider)
    if not provider and profile.custom_name:
        provider = ProviderRegistry.get_provider_by_display_name(profile.custom_name)

    if profile.model_provider != "openai" and provider and not provider.reasoning_efforts:
        config.pop("model_reasoning_effort", None)
    else:
        config["model_reasoning_effort"] = profile.model_reasoning_effort

    config["approval_policy"] = profile.approval_policy
    config["sandbox_mode"] = profile.sandbox_mode
    config["disable_response_storage"] = profile.disable_response_storage

    # Custom/preset provider settings. Codex identifies providers by table id,
    # so keep DeepSeek/Kimi/GLM separate instead of rewriting one "custom" table.
    if profile.model_provider == "openai":
        if "model_providers" in config and not isinstance(config["model_providers"], dict):
            config.pop("model_providers", None)
    else:
        base_url = profile.custom_base_url
        if not base_url and provider:
            base_url = provider.base_url_for_codex()

        if base_url:
            model_providers = config.get("model_providers")
            if not isinstance(model_providers, dict):
                model_providers = {}
                config["model_providers"] = model_providers

            custom = model_providers.get(profile.model_provider)
            if not isinstance(custom, dict):
                custom = {}
                model_providers[profile.model_provider] = custom

            custom["base_url"] = base_url
            custom["name"] = profile.custom_name or (provider.display_name if provider else profile.model_provider)
            custom["env_key"] = ProviderRegistry.get_codex_env_key_for_profile(profile)
            custom["requires_openai_auth"] = profile.custom_requires_openai_auth

            custom["wire_api"] = ProviderRegistry.get_codex_wire_api_for_profile(profile)

    return sanitize_codex_config(config)


def apply_codex_official_account(config: dict) -> dict:
    """Make Codex use file-backed ChatGPT credentials instead of third-party API auth."""
    config = sanitize_codex_config(config)
    previous_provider = config.get("model_provider", "openai")
    config["model_provider"] = "openai"
    config["cli_auth_credentials_store"] = "file"
    if previous_provider != "openai" or not config.get("model"):
        config["model"] = "gpt-5.5"
    return sanitize_codex_config(config)


def clear_codex_api_overrides(config: dict) -> dict:
    """Remove the active third-party Codex API provider from config.toml."""
    config = sanitize_codex_config(config)
    previous_provider = str(config.get("model_provider") or "openai").strip() or "openai"

    if previous_provider != "openai":
        model_providers = config.get("model_providers")
        if isinstance(model_providers, dict):
            model_providers = dict(model_providers)
            model_providers.pop(previous_provider, None)
            if model_providers:
                config["model_providers"] = model_providers
            else:
                config.pop("model_providers", None)

    config["model_provider"] = "openai"
    config["cli_auth_credentials_store"] = "file"
    if previous_provider != "openai" or not config.get("model"):
        config["model"] = "gpt-5.5"
    return sanitize_codex_config(config)
