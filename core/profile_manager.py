import hashlib
import json
import logging
import re
import shutil
from pathlib import Path

from config.paths import PROFILES_FILE
from models.profile import ClaudeProfile, CodexProfile, SSHProfile, BrowserProfile
from core import security

logger = logging.getLogger(__name__)

PROFILE_LIST_KEYS = (
    "claude_profiles",
    "codex_profiles",
    "ssh_profiles",
    "browser_profiles",
)
ACTIVE_PROFILE_KEYS = (
    "active_claude_profile",
    "active_codex_profile",
    "active_ssh_profile",
    "active_browser_profile",
)


def _get_default_store() -> dict:
    """Return default empty store structure."""
    return {
        "version": 4,  # 更新版本号以支持新的 provider 字段
        "claude_profiles": [],
        "codex_profiles": [],
        "ssh_profiles": [],
        "browser_profiles": [],
        "active_claude_profile": None,
        "active_codex_profile": None,
        "active_ssh_profile": None,
        "active_browser_profile": None,
    }


def _normalize_store(store: dict) -> bool:
    """Normalize a loaded profile store in place. Returns True if changed."""
    changed = False
    defaults = _get_default_store()

    for key, value in defaults.items():
        if key not in store:
            logger.warning(f"Missing key in store: {key}, adding default")
            store[key] = value
            changed = True

    if not isinstance(store.get("version"), int):
        logger.warning("Invalid profile store version, resetting to current version")
        store["version"] = defaults["version"]
        changed = True

    for key in PROFILE_LIST_KEYS:
        if not isinstance(store.get(key), list):
            logger.warning(f"Invalid profile list field: {key}, resetting to empty list")
            store[key] = []
            changed = True

    for key in ACTIVE_PROFILE_KEYS:
        if store.get(key) is not None and not isinstance(store.get(key), str):
            logger.warning(f"Invalid active profile field: {key}, resetting to None")
            store[key] = None
            changed = True

    active_links = {
        "active_claude_profile": "claude_profiles",
        "active_codex_profile": "codex_profiles",
        "active_ssh_profile": "ssh_profiles",
        "active_browser_profile": "browser_profiles",
    }
    for active_key, list_key in active_links.items():
        active = store.get(active_key)
        if active:
            names = {
                item.get("name")
                for item in store.get(list_key, [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            if active not in names:
                logger.warning(f"Active profile {active!r} was not found in {list_key}, resetting")
                store[active_key] = None
                changed = True

    for profile in store.get("claude_profiles", []):
        if not isinstance(profile, dict):
            continue
        if "provider" not in profile:
            profile["provider"] = "anthropic"
            changed = True
        if "custom_provider_name" not in profile:
            profile["custom_provider_name"] = None
            changed = True

    return changed


def _load_store() -> dict:
    """Load profiles store with backup and recovery mechanism."""
    if not PROFILES_FILE.exists():
        logger.info("Profiles file does not exist, returning default store")
        return _get_default_store()

    backup_file = PROFILES_FILE.with_suffix(".backup")

    try:
        content = PROFILES_FILE.read_text(encoding="utf-8")
        store = json.loads(content)

        # Validate basic structure
        if not isinstance(store, dict):
            raise ValueError("Store is not a dictionary")

        changed = _normalize_store(store)

        # Migrate old versions
        version = store.get("version", 1)
        if version == 1:
            logger.info("Migrating store from version 1 to 4")
            store["version"] = 4
            changed = True
            store["ssh_profiles"] = []
            store["active_ssh_profile"] = None
            store["browser_profiles"] = []
            store["active_browser_profile"] = None
            # 为所有 Claude profiles 添加默认 provider
            for profile in store.get("claude_profiles", []):
                if not isinstance(profile, dict):
                    continue
                if "provider" not in profile:
                    profile["provider"] = "anthropic"
                    changed = True
                if "custom_provider_name" not in profile:
                    profile["custom_provider_name"] = None
                    changed = True
        elif version == 2:
            logger.info("Migrating store from version 2 to 4")
            store["version"] = 4
            changed = True
            if "browser_profiles" not in store:
                store["browser_profiles"] = []
            if "active_browser_profile" not in store:
                store["active_browser_profile"] = None
            # 为所有 Claude profiles 添加默认 provider
            for profile in store.get("claude_profiles", []):
                if not isinstance(profile, dict):
                    continue
                if "provider" not in profile:
                    profile["provider"] = "anthropic"
                    changed = True
                if "custom_provider_name" not in profile:
                    profile["custom_provider_name"] = None
                    changed = True
        elif version == 3:
            logger.info("Migrating store from version 3 to 4")
            store["version"] = 4
            changed = True
            # 为所有 Claude profiles 添加默认 provider
            for profile in store.get("claude_profiles", []):
                if not isinstance(profile, dict):
                    continue
                if "provider" not in profile:
                    profile["provider"] = "anthropic"
                    changed = True
                if "custom_provider_name" not in profile:
                    profile["custom_provider_name"] = None
                    changed = True
        else:
            # Ensure all required fields exist
            if "browser_profiles" not in store:
                store["browser_profiles"] = []
                changed = True
            if "active_browser_profile" not in store:
                store["active_browser_profile"] = None
                changed = True
            # 确保所有 Claude profiles 都有 provider 字段
            for profile in store.get("claude_profiles", []):
                if not isinstance(profile, dict):
                    continue
                if "provider" not in profile:
                    profile["provider"] = "anthropic"
                    changed = True
                if "custom_provider_name" not in profile:
                    profile["custom_provider_name"] = None
                    changed = True

        changed = _normalize_store(store) or changed

        if changed:
            _save_store(store)

        return store

    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in profiles file: {e}")
        # Try to load from backup
        if backup_file.exists():
            try:
                logger.info("Attempting to restore from backup")
                content = backup_file.read_text(encoding="utf-8")
                store = json.loads(content)
                logger.info("Successfully restored from backup")
                # Save restored backup as main file
                _save_store(store)
                return store
            except Exception as backup_error:
                logger.error(f"Failed to restore from backup: {backup_error}")

        logger.warning("Returning default store due to corrupted profiles file")
        return _get_default_store()

    except Exception as e:
        logger.error(f"Failed to load profiles: {e}")
        # Try backup
        if backup_file.exists():
            try:
                logger.info("Attempting to restore from backup")
                content = backup_file.read_text(encoding="utf-8")
                store = json.loads(content)
                logger.info("Successfully restored from backup")
                _save_store(store)
                return store
            except Exception:
                pass

        return _get_default_store()


def _save_store(store: dict) -> None:
    """Save profiles store with atomic write and backup."""
    try:
        # Ensure parent directory exists
        PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Validate store structure before saving
        if not isinstance(store, dict):
            raise ValueError("Store must be a dictionary")

        # Serialize to JSON
        content = json.dumps(store, indent=2, ensure_ascii=False)

        # Create backup of existing file
        backup_file = PROFILES_FILE.with_suffix(".backup")
        if PROFILES_FILE.exists():
            try:
                shutil.copy2(PROFILES_FILE, backup_file)
                logger.debug(f"Created backup: {backup_file}")
            except Exception as e:
                logger.warning(f"Failed to create backup: {e}")

        # Atomic write: write to temp file, then replace
        tmp = PROFILES_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(PROFILES_FILE)
            logger.debug(f"Successfully saved profiles to {PROFILES_FILE}")
        except Exception as e:
            # Clean up temp file on error
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to save profiles: {e}") from e

    except Exception as e:
        logger.error(f"Error saving profiles store: {e}")
        raise


# --- Claude Profile CRUD ---

def _load_profile_list(items: list[dict], cls, label: str) -> list:
    if not isinstance(items, list):
        logger.warning(f"Skipping invalid {label} profile list: expected list")
        return []

    profiles = []
    for idx, item in enumerate(items):
        try:
            profiles.append(cls.from_dict(item))
        except Exception as e:
            logger.warning(f"Skipping invalid {label} profile at index {idx}: {e}")
    return profiles


def detect_claude_provider(settings: dict) -> str:
    """Infer the Claude provider from settings env fields."""
    if not isinstance(settings, dict):
        return "anthropic"

    env = settings.get("env", {})
    if not isinstance(env, dict):
        env = {}

    base_url = str(env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")

    from core.providers import ProviderRegistry
    for provider in ProviderRegistry.get_all_providers():
        provider_base = str(provider.base_url_for_claude() or "").rstrip("/")
        if provider_base and provider_base == base_url:
            return provider.name

    for provider in ProviderRegistry.get_all_providers():
        if provider.claude_env and all(env.get(key) == value for key, value in provider.claude_env.items()):
            return provider.name

    return "anthropic"


def list_claude_profiles() -> list[ClaudeProfile]:
    store = _load_store()
    return _load_profile_list(store.get("claude_profiles", []), ClaudeProfile, "Claude")


def is_third_party_claude_profile(profile: ClaudeProfile) -> bool:
    return getattr(profile, "provider", "anthropic") != "anthropic"


def list_switchable_claude_profiles() -> list[ClaudeProfile]:
    return [p for p in list_claude_profiles() if is_third_party_claude_profile(p)]


def get_active_claude_name() -> str | None:
    return _load_store().get("active_claude_profile")


def set_active_claude(name: str) -> None:
    store = _load_store()
    store["active_claude_profile"] = name
    _save_store(store)


def _claude_env(settings: dict) -> dict:
    env = settings.get("env", {})
    return env if isinstance(env, dict) else {}


def _claude_permissions(settings: dict) -> dict:
    permissions = settings.get("permissions", {})
    return permissions if isinstance(permissions, dict) else {}


def _claude_auth_token_from_current(settings: dict, config: dict) -> str:
    env = _claude_env(settings)
    return (
        env.get("ANTHROPIC_AUTH_TOKEN")
        or env.get("ANTHROPIC_API_KEY")
        or config.get("primaryApiKey")
        or ""
    )


def _claude_primary_api_key(config: dict) -> str:
    return config.get("primaryApiKey") or ""


def _claude_auth_identity_from_current(settings: dict, config: dict) -> str:
    token = _claude_auth_token_from_current(settings, config)
    return f"auth-{_short_fingerprint(token)}" if token else "no-auth"


def describe_claude_profile_identity(profile: ClaudeProfile) -> str:
    """Return a non-secret auth identity label for display."""
    token = security.get_secret(profile.auth_token_ref)
    if not token:
        token = security.get_secret(getattr(profile, "primary_api_key_ref", None))
    return f"auth-{_short_fingerprint(token)}" if token else "no-auth"


def _claude_additional_directories(settings: dict, permissions: dict | None = None) -> list:
    if isinstance(settings.get("additionalDirectories"), list):
        return settings.get("additionalDirectories", [])
    permissions = permissions if permissions is not None else _claude_permissions(settings)
    value = permissions.get("additionalDirectories")
    return value if isinstance(value, list) else []


def _claude_profile_kwargs_from_current(name: str, settings: dict, config: dict) -> dict:
    env = _claude_env(settings)
    permissions = _claude_permissions(settings)
    token_value = _claude_auth_token_from_current(settings, config)
    primary_key = _claude_primary_api_key(config)
    token_ref = f"claude:{name}:auth_token"
    primary_ref = f"claude:{name}:primary_api_key"

    if token_value:
        security.set_secret(token_ref, token_value)
    if primary_key:
        security.set_secret(primary_ref, primary_key)

    return {
        "name": name,
        "auth_token_ref": token_ref,
        "primary_api_key_ref": primary_ref if primary_key else None,
        "base_url": env.get("ANTHROPIC_BASE_URL", ""),
        "model": settings.get("model", ""),
        "effort_level": settings.get("effortLevel", "high"),
        "permissions_mode": permissions.get("defaultMode", "default"),
        "skip_dangerous_prompt": settings.get("skipDangerousModePermissionPrompt", False),
        "permissions_allow": permissions.get("allow", []),
        "additional_directories": _claude_additional_directories(settings, permissions),
        "provider": detect_claude_provider(settings),
    }


def _build_claude_import_name(settings: dict, config: dict) -> str:
    provider = _safe_name_part(detect_claude_provider(settings), "anthropic")
    model = _safe_name_part(settings.get("model"), "model")
    identity = _safe_name_part(_claude_auth_identity_from_current(settings, config), "auth")
    return f"Claude-{provider}-{model}-{identity}"


def _claude_profile_config_matches(profile: ClaudeProfile, settings: dict) -> bool:
    env = _claude_env(settings)
    permissions = _claude_permissions(settings)

    detected_provider = detect_claude_provider(settings)
    if detected_provider == "anthropic" or not is_third_party_claude_profile(profile):
        return False
    if detected_provider != profile.provider:
        return False
    if (env.get("ANTHROPIC_BASE_URL") or "") != (profile.base_url or ""):
        return False
    if settings.get("model", "") != profile.model:
        return False
    if settings.get("effortLevel", "high") != profile.effort_level:
        return False
    if permissions.get("defaultMode", "default") != profile.permissions_mode:
        return False
    if bool(settings.get("skipDangerousModePermissionPrompt", False)) != bool(profile.skip_dangerous_prompt):
        return False
    if (permissions.get("allow", []) or []) != (profile.permissions_allow or []):
        return False
    if (_claude_additional_directories(settings, permissions) or []) != (profile.additional_directories or []):
        return False
    return True


def _claude_profile_auth_matches(profile: ClaudeProfile, settings: dict, config: dict) -> bool:
    env = _claude_env(settings)
    current_token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""
    current_primary = _claude_primary_api_key(config)
    stored_token = security.get_secret(profile.auth_token_ref) or ""
    stored_primary = security.get_secret(getattr(profile, "primary_api_key_ref", None)) or ""
    stored_values = {value for value in [stored_token, stored_primary] if value}

    if current_token:
        if current_token not in stored_values:
            return False
    if current_primary:
        if current_primary not in stored_values:
            return False
    if current_token or current_primary:
        return True
    return not stored_values


def _claude_profile_matches(profile: ClaudeProfile, settings: dict, config: dict) -> bool:
    return _claude_profile_config_matches(profile, settings) and _claude_profile_auth_matches(profile, settings, config)


def get_current_claude_name() -> str | None:
    """Return the profile that matches the actual Claude files on disk."""
    from core.parser import read_claude_settings, read_claude_config

    settings = read_claude_settings()
    config = read_claude_config()
    if not settings and not config:
        return None

    for profile in list_switchable_claude_profiles():
        if _claude_profile_matches(profile, settings, config):
            return profile.name
    return None


def get_claude_runtime_summary() -> dict:
    """Return display-safe details for the actual Claude settings/config on disk."""
    from core.parser import read_claude_settings, read_claude_config

    settings = read_claude_settings()
    config = read_claude_config()
    current_name = None
    if settings or config:
        for profile in list_switchable_claude_profiles():
            if _claude_profile_matches(profile, settings, config):
                current_name = profile.name
                break

    return {
        "profile_name": current_name,
        "stored_active": get_active_claude_name(),
        "provider": detect_claude_provider(settings) if settings else "anthropic",
        "model": settings.get("model", "") if settings else "",
        "auth_identity": _claude_auth_identity_from_current(settings, config),
        "has_settings": bool(settings),
        "has_config": bool(config),
    }


def _pick_claude_import_name(settings: dict, config: dict) -> str:
    base_name = _build_claude_import_name(settings, config)
    profiles = list_claude_profiles()
    generic_names = {"current", "claude-current"}
    for profile in profiles:
        if profile.name.lower() not in generic_names and _claude_profile_matches(profile, settings, config):
            return profile.name

    existing = {profile.name for profile in profiles}
    if base_name not in existing:
        return base_name

    index = 2
    while f"{base_name}-{index}" in existing:
        index += 1
    return f"{base_name}-{index}"


def save_claude_profile(profile: ClaudeProfile) -> None:
    store = _load_store()
    profiles = store.get("claude_profiles", [])
    # Replace if same name exists, otherwise append
    profiles = [p for p in profiles if isinstance(p, dict) and p.get("name") != profile.name]
    profiles.append(profile.to_dict())
    store["claude_profiles"] = profiles
    _save_store(store)


def delete_claude_profile(name: str) -> None:
    store = _load_store()
    profile_refs = set()
    for profile in store.get("claude_profiles", []):
        if isinstance(profile, dict) and profile.get("name") == name:
            profile_refs.update(
                value
                for key, value in profile.items()
                if key.endswith("_ref") and isinstance(value, str) and value
            )
            break

    for ref in profile_refs:
        security.delete_secret(ref)

    # Clean up legacy/conventional key names too.
    for suffix in ["auth_token", "primary_api_key"]:
        security.delete_secret(f"claude:{name}:{suffix}")

    store["claude_profiles"] = [
        p for p in store["claude_profiles"]
        if isinstance(p, dict) and p.get("name") != name
    ]
    if store.get("active_claude_profile") == name:
        store["active_claude_profile"] = None
    _save_store(store)


# --- Codex Profile CRUD ---

def list_codex_profiles() -> list[CodexProfile]:
    store = _load_store()
    return _load_profile_list(store.get("codex_profiles", []), CodexProfile, "Codex")


def is_third_party_codex_profile(profile: CodexProfile) -> bool:
    return getattr(profile, "model_provider", "openai") != "openai"


def list_switchable_codex_profiles() -> list[CodexProfile]:
    return [p for p in list_codex_profiles() if is_third_party_codex_profile(p)]


def get_active_codex_name() -> str | None:
    return _load_store().get("active_codex_profile")


def set_active_codex(name: str) -> None:
    store = _load_store()
    store["active_codex_profile"] = name
    _save_store(store)


def _codex_auth_mode(auth: dict) -> str:
    mode = str(auth.get("auth_mode") or "").strip()
    if mode in {"chatgpt", "api_key"}:
        return mode
    if auth.get("OPENAI_API_KEY"):
        return "api_key"
    return "chatgpt"


def _short_fingerprint(value: object) -> str:
    text = str(value or "")
    if not text:
        return "none"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _safe_name_part(value: object, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
    text = text.strip("-_.")
    return (text or fallback)[:40]


def _codex_auth_identity_from_auth(auth: dict) -> str:
    mode = _codex_auth_mode(auth)
    if mode == "api_key":
        api_key = auth.get("OPENAI_API_KEY")
        return f"key-{_short_fingerprint(api_key)}" if api_key else "api-key"

    tokens = auth.get("tokens", {})
    return "official-login" if isinstance(tokens, dict) and tokens else "no-api-key"


def describe_codex_profile_identity(profile: CodexProfile) -> str:
    """Return a non-secret auth identity label for display."""
    api_key = security.get_secret(profile.api_key_ref)
    return f"key-{_short_fingerprint(api_key)}" if api_key else "api-key"


def _build_codex_import_name(config: dict, auth: dict) -> str:
    provider = _safe_name_part(config.get("model_provider"), "openai")
    model = _safe_name_part(config.get("model"), "model")
    identity = _safe_name_part(_codex_auth_identity_from_auth(auth), "auth")
    return f"Codex-{provider}-{model}-{identity}"


def _codex_model_providers(config: dict) -> dict:
    model_providers = config.get("model_providers", {})
    return model_providers if isinstance(model_providers, dict) else {}


def _codex_provider_table(config: dict, provider_id: str) -> dict:
    table = _codex_model_providers(config).get(provider_id, {})
    return table if isinstance(table, dict) else {}


def _codex_profile_kwargs_from_current(name: str, config: dict, auth: dict) -> dict:
    profile_kwargs = {
        "name": name,
        "model": config.get("model", "gpt-5.5"),
        "model_provider": config.get("model_provider", "openai"),
        "model_reasoning_effort": config.get("model_reasoning_effort", "high"),
        "approval_policy": config.get("approval_policy", "never"),
        "sandbox_mode": config.get("sandbox_mode", "danger-full-access"),
        "disable_response_storage": config.get("disable_response_storage", True),
    }

    provider_id = profile_kwargs["model_provider"]
    custom = _codex_provider_table(config, provider_id)
    if custom:
        profile_kwargs["custom_base_url"] = custom.get("base_url")
        profile_kwargs["custom_name"] = custom.get("name")
        profile_kwargs["custom_wire_api"] = custom.get("wire_api")
        profile_kwargs["custom_requires_openai_auth"] = custom.get("requires_openai_auth", False)

    _store_codex_auth_secrets(name, profile_kwargs, auth)
    return profile_kwargs


def _store_codex_auth_secrets(name: str, profile_kwargs: dict, auth: dict) -> None:
    api_key = auth.get("OPENAI_API_KEY") if isinstance(auth, dict) else None
    if api_key:
        ref = f"codex:{name}:api_key"
        security.set_secret(ref, api_key)
        profile_kwargs["api_key_ref"] = ref


def _codex_expected_base_url(profile: CodexProfile) -> str:
    if profile.custom_base_url:
        return profile.custom_base_url
    try:
        from core.providers import ProviderRegistry

        provider = ProviderRegistry.get_provider(profile.model_provider)
        return provider.base_url_for_codex() if provider else ""
    except Exception:
        return ""


def _same_optional(left: object, right: object) -> bool:
    return (left or "") == (right or "")


def _codex_config_matches(profile: CodexProfile, config: dict) -> bool:
    if not is_third_party_codex_profile(profile):
        return False
    if config.get("model_provider", "openai") == "openai":
        return False
    if profile.model != config.get("model", "gpt-5.5"):
        return False
    if profile.model_provider != config.get("model_provider", "openai"):
        return False
    if profile.model_reasoning_effort != config.get("model_reasoning_effort", "high"):
        return False
    if profile.approval_policy != config.get("approval_policy", "never"):
        return False
    if profile.sandbox_mode != config.get("sandbox_mode", "danger-full-access"):
        return False
    if profile.disable_response_storage != config.get("disable_response_storage", True):
        return False

    custom = _codex_provider_table(config, profile.model_provider)
    current_base_url = custom.get("base_url")
    expected_base_url = _codex_expected_base_url(profile)
    if expected_base_url or current_base_url:
        if not _same_optional(expected_base_url, current_base_url):
            return False
    if profile.custom_wire_api or custom.get("wire_api"):
        if not _same_optional(profile.custom_wire_api, custom.get("wire_api")):
            return False
    if bool(profile.custom_requires_openai_auth) != bool(custom.get("requires_openai_auth", False)):
        return False
    return True


def _codex_auth_matches(profile: CodexProfile, auth: dict) -> bool:
    auth_mode = _codex_auth_mode(auth)
    if not is_third_party_codex_profile(profile):
        return False
    if auth_mode != "api_key":
        return False

    current_key = auth.get("OPENAI_API_KEY") or ""
    stored_key = security.get_secret(profile.api_key_ref) or ""
    return bool(current_key and stored_key) and current_key == stored_key


def codex_profile_matches_current(profile: CodexProfile) -> bool:
    from core.toml_parser import read_codex_config
    from core.auth_parser import read_codex_auth

    return _codex_profile_matches(profile, read_codex_config(), read_codex_auth())


def _codex_profile_matches(profile: CodexProfile, config: dict, auth: dict) -> bool:
    return _codex_config_matches(profile, config) and _codex_auth_matches(profile, auth)


def get_current_codex_name() -> str | None:
    """Return the profile that matches the actual Codex files on disk."""
    from core.toml_parser import read_codex_config
    from core.auth_parser import read_codex_auth

    config = read_codex_config()
    auth = read_codex_auth()
    if not config and not auth:
        return None

    for profile in list_switchable_codex_profiles():
        if _codex_profile_matches(profile, config, auth):
            return profile.name
    return None


def get_codex_runtime_summary() -> dict:
    """Return display-safe details for the actual Codex config/auth on disk."""
    from core.toml_parser import read_codex_config
    from core.auth_parser import read_codex_auth

    config = read_codex_config()
    auth = read_codex_auth()
    current_name = None
    if config or auth:
        for profile in list_switchable_codex_profiles():
            if _codex_profile_matches(profile, config, auth):
                current_name = profile.name
                break

    return {
        "profile_name": current_name,
        "stored_active": get_active_codex_name(),
        "provider": config.get("model_provider", "openai") if config else "openai",
        "model": config.get("model", "gpt-5.5") if config else "gpt-5.5",
        "auth_mode": _codex_auth_mode(auth),
        "auth_identity": _codex_auth_identity_from_auth(auth),
        "has_config": bool(config),
        "has_auth": bool(auth),
    }


def _pick_codex_import_name(config: dict, auth: dict) -> str:
    base_name = _build_codex_import_name(config, auth)
    profiles = list_codex_profiles()
    generic_names = {"current", "codex-current"}
    for profile in profiles:
        if profile.name.lower() not in generic_names and _codex_profile_matches(profile, config, auth):
            return profile.name

    existing = {profile.name for profile in profiles}
    if base_name not in existing:
        return base_name

    index = 2
    while f"{base_name}-{index}" in existing:
        index += 1
    return f"{base_name}-{index}"


def save_codex_profile(profile: CodexProfile) -> None:
    store = _load_store()
    profiles = store.get("codex_profiles", [])
    profiles = [p for p in profiles if isinstance(p, dict) and p.get("name") != profile.name]
    profiles.append(profile.to_dict())
    store["codex_profiles"] = profiles
    _save_store(store)


def delete_codex_profile(name: str) -> None:
    store = _load_store()
    profile_refs = set()
    for profile in store.get("codex_profiles", []):
        if isinstance(profile, dict) and profile.get("name") == name:
            profile_refs.update(
                value
                for key, value in profile.items()
                if key.endswith("_ref") and isinstance(value, str) and value
            )
            break

    for ref in profile_refs:
        security.delete_secret(ref)

    # Clean up legacy/conventional key names too.
    for suffix in ["api_key", "openai_auth_key", "oauth_tokens", "oauth_meta", "auth_data"]:
        security.delete_secret(f"codex:{name}:{suffix}")

    store["codex_profiles"] = [
        p for p in store["codex_profiles"]
        if isinstance(p, dict) and p.get("name") != name
    ]
    if store.get("active_codex_profile") == name:
        store["active_codex_profile"] = None
    _save_store(store)


# --- Import from current config ---

def import_current_claude() -> ClaudeProfile | None:
    """Create a ClaudeProfile from the current settings.json + config.json."""
    from core.parser import read_claude_settings, read_claude_config

    settings = read_claude_settings()
    config = read_claude_config()
    if not settings and not config:
        return None
    if detect_claude_provider(settings) == "anthropic":
        return None

    name = _pick_claude_import_name(settings, config)
    return ClaudeProfile(**_claude_profile_kwargs_from_current(name, settings, config))


def import_current_codex() -> CodexProfile | None:
    """Create a CodexProfile from the current config.toml + auth.json."""
    from core.toml_parser import read_codex_config
    from core.auth_parser import read_codex_auth

    config = read_codex_config()
    auth = read_codex_auth()
    if not config and not auth:
        return None
    if config.get("model_provider", "openai") == "openai":
        return None
    if _codex_auth_mode(auth) != "api_key" or not auth.get("OPENAI_API_KEY"):
        return None

    name = _pick_codex_import_name(config, auth)
    return CodexProfile(**_codex_profile_kwargs_from_current(name, config, auth))


# --- Browser Profile CRUD ---

def list_browser_profiles() -> list[BrowserProfile]:
    store = _load_store()
    return _load_profile_list(store.get("browser_profiles", []), BrowserProfile, "Browser")


def get_active_browser_name() -> str | None:
    return _load_store().get("active_browser_profile")


def set_active_browser(name: str) -> None:
    store = _load_store()
    store["active_browser_profile"] = name
    _save_store(store)


def save_browser_profile(profile: BrowserProfile) -> None:
    store = _load_store()
    profiles = store.get("browser_profiles", [])
    profiles = [p for p in profiles if isinstance(p, dict) and p.get("name") != profile.name]
    profiles.append(profile.to_dict())
    store["browser_profiles"] = profiles
    _save_store(store)


def delete_browser_profile(name: str) -> None:
    store = _load_store()
    store["browser_profiles"] = [
        p for p in store.get("browser_profiles", [])
        if isinstance(p, dict) and p.get("name") != name
    ]
    if store.get("active_browser_profile") == name:
        store["active_browser_profile"] = None
    _save_store(store)


def list_ssh_profiles() -> list[SSHProfile]:
    store = _load_store()
    return _load_profile_list(store.get("ssh_profiles", []), SSHProfile, "SSH")


def get_active_ssh_name() -> str | None:
    return _load_store().get("active_ssh_profile")


def set_active_ssh(name: str) -> None:
    store = _load_store()
    store["active_ssh_profile"] = name
    _save_store(store)


def save_ssh_profile(profile: SSHProfile) -> None:
    store = _load_store()
    profiles = store.get("ssh_profiles", [])
    profiles = [p for p in profiles if isinstance(p, dict) and p.get("name") != profile.name]
    profiles.append(profile.to_dict())
    store["ssh_profiles"] = profiles
    _save_store(store)


def delete_ssh_profile(name: str) -> None:
    store = _load_store()
    profile_refs = set()
    for profile in store.get("ssh_profiles", []):
        if isinstance(profile, dict) and profile.get("name") == name:
            profile_refs.update(
                value
                for key, value in profile.items()
                if key.endswith("_ref") and isinstance(value, str) and value
            )
            break

    for ref in profile_refs:
        security.delete_secret(ref)

    # Clean up legacy/conventional key names too.
    for suffix in ["password", "key_passphrase"]:
        security.delete_secret(f"ssh:{name}:{suffix}")

    store["ssh_profiles"] = [
        p for p in store.get("ssh_profiles", [])
        if isinstance(p, dict) and p.get("name") != name
    ]
    if store.get("active_ssh_profile") == name:
        store["active_ssh_profile"] = None
    _save_store(store)
