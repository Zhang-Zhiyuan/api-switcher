import logging
from pathlib import Path

from config.paths import CODEX_CONFIG
from core.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def read_codex_config() -> dict:
    if not CODEX_CONFIG.exists():
        return {}
    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        with open(CODEX_CONFIG, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        logger.error(f"Failed to read {CODEX_CONFIG}: {e}")
        return {}


def write_codex_config(data: dict) -> None:
    try:
        import tomli_w
        content = tomli_w.dumps(data)
        _atomic_write(CODEX_CONFIG, content)
    except Exception as e:
        logger.error(f"Failed to write {CODEX_CONFIG}: {e}")
        raise


def apply_codex_profile(config: dict, profile) -> dict:
    """Apply a CodexProfile to config dict. Only modifies model/provider fields."""
    config = dict(config)

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

    return config


def apply_codex_official_account(config: dict) -> dict:
    """Make Codex use file-backed ChatGPT credentials instead of third-party API auth."""
    config = dict(config)
    previous_provider = config.get("model_provider", "openai")
    config["model_provider"] = "openai"
    config["cli_auth_credentials_store"] = "file"
    if previous_provider != "openai" or not config.get("model"):
        config["model"] = "gpt-5.5"
    return config
