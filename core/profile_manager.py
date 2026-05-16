import hashlib
import json
import logging
import re
import shutil
import base64
from datetime import datetime
from urllib.parse import urlparse

from config.paths import PROFILES_FILE, CLAUDE_CREDENTIALS
from core.atomic_io import atomic_write_text
from models.profile import (
    ClaudeProfile,
    CodexProfile,
    ClaudeAccountProfile,
    CodexAccountProfile,
    SSHProfile,
    BrowserProfile,
)
from core import security

logger = logging.getLogger(__name__)

PROFILE_LIST_KEYS = (
    "claude_profiles",
    "codex_profiles",
    "claude_account_profiles",
    "codex_account_profiles",
    "ssh_profiles",
    "browser_profiles",
)
ACTIVE_PROFILE_KEYS = (
    "active_claude_profile",
    "active_codex_profile",
    "active_claude_account",
    "active_codex_account",
    "active_ssh_profile",
    "active_browser_profile",
)


def _get_default_store() -> dict:
    """Return default empty store structure."""
    return {
        "version": 5,  # provider fields plus local-only official account snapshots
        "claude_profiles": [],
        "codex_profiles": [],
        "claude_account_profiles": [],
        "codex_account_profiles": [],
        "ssh_profiles": [],
        "browser_profiles": [],
        "active_claude_profile": None,
        "active_codex_profile": None,
        "active_claude_account": None,
        "active_codex_account": None,
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
    elif store["version"] < defaults["version"]:
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
        "active_claude_account": "claude_account_profiles",
        "active_codex_account": "codex_account_profiles",
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

        try:
            atomic_write_text(PROFILES_FILE, content)
            logger.debug(f"Successfully saved profiles to {PROFILES_FILE}")
        except Exception as e:
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

    if base_url and base_url not in {"https://api.anthropic.com", "https://api.anthropic.com/v1"}:
        return "custom"

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


def set_active_claude(name: str | None) -> None:
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
    provider = _safe_name_part(_claude_station_label(settings), "Claude-API")
    model = _safe_name_part(settings.get("model"), "model")
    if model and model != "model":
        return f"Claude-{provider}-{model}"
    identity = _safe_name_part(_claude_auth_identity_from_current(settings, config), "auth")
    return f"Claude-{provider}-{identity}"


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

    return _unique_profile_name({profile.name for profile in profiles}, base_name)


def save_claude_profile(profile: ClaudeProfile, previous_name: str | None = None) -> None:
    store = _load_store()
    profiles = store.get("claude_profiles", [])
    replaced_names = {profile.name}
    if previous_name:
        replaced_names.add(previous_name)

    replaced_refs: set[str] = set()
    for existing in profiles:
        if isinstance(existing, dict) and existing.get("name") in replaced_names:
            replaced_refs.update(_profile_secret_refs(existing))

    new_refs = _profile_secret_refs(profile)
    profiles = [
        p for p in profiles
        if isinstance(p, dict) and p.get("name") not in replaced_names
    ]
    profiles.append(profile.to_dict())
    store["claude_profiles"] = profiles
    if previous_name and store.get("active_claude_profile") == previous_name:
        store["active_claude_profile"] = profile.name
    _save_store(store)

    for ref in replaced_refs - new_refs:
        security.delete_secret(ref)


def clone_claude_profile(name: str) -> ClaudeProfile:
    profiles = list_claude_profiles()
    source = next((p for p in profiles if p.name == name), None)
    if not source:
        raise ValueError(f"Claude profile '{name}' not found")

    new_name = _unique_profile_name({p.name for p in profiles}, f"{source.name}-copy")
    token_ref = f"claude:{new_name}:auth_token"
    primary_ref = f"claude:{new_name}:primary_api_key"

    token_value = (
        security.get_secret(source.auth_token_ref)
        or security.get_secret(getattr(source, "primary_api_key_ref", None))
        or ""
    )
    primary_value = security.get_secret(getattr(source, "primary_api_key_ref", None)) or ""
    if token_value:
        security.set_secret(token_ref, token_value)
    if primary_value:
        security.set_secret(primary_ref, primary_value)

    cloned = ClaudeProfile(
        name=new_name,
        auth_token_ref=token_ref,
        primary_api_key_ref=primary_ref if primary_value else None,
        base_url=source.base_url,
        model=source.model,
        effort_level=source.effort_level,
        permissions_mode=source.permissions_mode,
        skip_dangerous_prompt=source.skip_dangerous_prompt,
        permissions_allow=list(source.permissions_allow or []),
        additional_directories=list(source.additional_directories or []),
        provider=source.provider,
        custom_provider_name=source.custom_provider_name,
    )
    save_claude_profile(cloned)
    return cloned


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


# --- Claude Official Account CRUD ---

def list_claude_account_profiles() -> list[ClaudeAccountProfile]:
    store = _load_store()
    return _load_profile_list(store.get("claude_account_profiles", []), ClaudeAccountProfile, "Claude account")


def get_active_claude_account_name() -> str | None:
    return _load_store().get("active_claude_account")


def set_active_claude_account(name: str | None) -> None:
    store = _load_store()
    store["active_claude_account"] = name
    _save_store(store)


def save_claude_account_profile(profile: ClaudeAccountProfile) -> None:
    store = _load_store()
    profiles = store.get("claude_account_profiles", [])
    profiles = [p for p in profiles if isinstance(p, dict) and p.get("name") != profile.name]
    profiles.append(profile.to_dict())
    store["claude_account_profiles"] = profiles
    _save_store(store)


def get_claude_account_credentials(profile: ClaudeAccountProfile) -> dict | None:
    return security.get_secret_json(profile.credentials_ref)


def _validate_account_snapshot(data: object, label: str) -> tuple[bool, str]:
    if data is None:
        return False, f"{label}快照不可读取，可能已被删除或密钥存储损坏"
    if not isinstance(data, dict):
        return False, f"{label}快照格式异常"
    if not data:
        return False, f"{label}快照为空"
    return True, "可用"


def validate_claude_account_snapshot(profile: ClaudeAccountProfile) -> tuple[bool, str]:
    credentials = get_claude_account_credentials(profile)
    return _validate_claude_account_credentials(credentials)


def _validate_claude_account_credentials(credentials: object) -> tuple[bool, str]:
    ok, reason = _validate_account_snapshot(credentials, "Claude 账号")
    if not ok:
        return ok, reason
    if not any(value.strip() for value in _iter_nested_strings(credentials)):
        return False, "Claude 账号快照里没有可用凭据内容"
    return True, reason


def load_claude_account_credentials(profile: ClaudeAccountProfile) -> dict:
    credentials = get_claude_account_credentials(profile)
    ok, reason = _validate_claude_account_credentials(credentials)
    if not ok:
        raise ValueError(reason)
    return credentials


def _claude_account_identity_from_credentials(credentials: dict) -> str:
    return _identity_from_json(credentials, "claude-login")


def _claude_account_preferred_name(credentials: dict) -> str:
    return _account_preferred_name_from_json(credentials, "claude-login")


def _claude_account_identity_candidates(credentials: dict) -> set[str]:
    return _account_identity_candidates_from_json(credentials, "claude-login")


def _claude_account_matches_credentials(profile: ClaudeAccountProfile, credentials: dict) -> bool:
    identity = _claude_account_identity_from_credentials(credentials)
    if profile.identity == identity:
        return True

    saved = get_claude_account_credentials(profile)
    if isinstance(saved, dict) and saved:
        return _account_snapshots_match(saved, credentials, "claude-login")

    stable_candidates = _account_stable_identity_candidates_from_json(credentials, "claude-login")
    if stable_candidates:
        return profile.identity in stable_candidates
    return profile.identity in _claude_account_identity_candidates(credentials)


def _claude_api_override_active(settings: dict, config: dict) -> bool:
    env = _claude_env(settings)
    override_keys = {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    }
    if any(env.get(key) for key in override_keys):
        return True
    if config.get("primaryApiKey"):
        return True
    return detect_claude_provider(settings) != "anthropic"


def _pick_claude_account_import_name(identity: str, preferred_name: str | None = None, credentials: dict | None = None) -> str:
    profiles = list_claude_account_profiles()
    for profile in profiles:
        if profile.identity == identity:
            return profile.name
        if credentials and _claude_account_matches_credentials(profile, credentials):
            return profile.name
    return _account_import_name("Claude-账号", preferred_name or identity, {profile.name for profile in profiles})


def import_current_claude_account() -> ClaudeAccountProfile | None:
    """Create a local-only account snapshot from Claude Code credentials."""
    from core.parser import read_claude_credentials

    credentials = read_claude_credentials()
    ok, _reason = _validate_claude_account_credentials(credentials)
    if not ok:
        return None

    identity = _claude_account_identity_from_credentials(credentials)
    preferred_name = _claude_account_preferred_name(credentials)
    name = _pick_claude_account_import_name(identity, preferred_name, credentials)
    ref = f"claude-account:{name}:credentials"
    security.set_secret_json(ref, credentials)
    return ClaudeAccountProfile(
        name=name,
        credentials_ref=ref,
        identity=identity,
        created_at=_now_iso(),
    )


def delete_claude_account_profile(name: str) -> None:
    store = _load_store()
    profile_refs = set()
    for profile in store.get("claude_account_profiles", []):
        if isinstance(profile, dict) and profile.get("name") == name:
            ref = profile.get("credentials_ref")
            if isinstance(ref, str) and ref:
                profile_refs.add(ref)
            break
    for ref in profile_refs:
        security.delete_secret(ref)
    security.delete_secret(f"claude-account:{name}:credentials")

    store["claude_account_profiles"] = [
        p for p in store.get("claude_account_profiles", [])
        if isinstance(p, dict) and p.get("name") != name
    ]
    if store.get("active_claude_account") == name:
        store["active_claude_account"] = None
    _save_store(store)


def get_current_claude_account_name() -> str | None:
    from core.parser import read_claude_settings, read_claude_config, read_claude_credentials

    settings = read_claude_settings()
    config = read_claude_config()
    credentials = read_claude_credentials()
    ok, _reason = _validate_claude_account_credentials(credentials)
    if not ok or _claude_api_override_active(settings, config):
        return None

    for profile in list_claude_account_profiles():
        if _claude_account_matches_credentials(profile, credentials):
            return profile.name
    return None


def get_claude_account_runtime_summary() -> dict:
    from core.parser import read_claude_settings, read_claude_config, read_claude_credentials

    settings = read_claude_settings()
    config = read_claude_config()
    credentials = read_claude_credentials()
    override_active = _claude_api_override_active(settings, config)
    credentials_ok, credentials_status = _validate_claude_account_credentials(credentials)
    identity = _claude_account_identity_from_credentials(credentials) if credentials_ok else "no-login"
    profile_name = None
    if credentials_ok and not override_active:
        for profile in list_claude_account_profiles():
            if _claude_account_matches_credentials(profile, credentials):
                profile_name = profile.name
                break

    return {
        "profile_name": profile_name,
        "stored_active": get_active_claude_account_name(),
        "identity": identity,
        "has_credentials": credentials_ok,
        "credentials_status": credentials_status,
        "credentials_path": str(CLAUDE_CREDENTIALS),
        "api_override_active": override_active,
    }


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


def set_active_codex(name: str | None) -> None:
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
    text = re.sub(r"[^\w.-]+", "-", text, flags=re.UNICODE)
    text = text.strip("-_.")
    return (text or fallback)[:40]


def _looks_like_url(value: str) -> bool:
    text = str(value or "").strip().lower()
    return "://" in text or text.startswith(("www.", "api."))


def _looks_like_endpoint(value: str) -> bool:
    text = str(value or "").strip().lower()
    if _looks_like_url(text):
        return True
    if not text or any(ch.isspace() for ch in text):
        return False
    host = text.split("/", 1)[0].split(":", 1)[0].strip("[]")
    return host == "localhost" or ("." in host and re.fullmatch(r"[a-z0-9.-]+", host) is not None)


def _usable_label(value: object, max_length: int = 160) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > max_length:
        return ""
    if len(text.split(".")) >= 3 and not any(ch.isspace() for ch in text) and _decode_jwt_payload(text):
        return ""
    return text


def _host_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    try:
        host = urlparse(text).hostname or ""
    except Exception:
        host = ""
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _station_label_candidate(value: object) -> str:
    text = _usable_label(value, max_length=80)
    if not text:
        return ""
    if _looks_like_endpoint(text):
        return _host_label(text)
    generic = re.sub(r"[\s_-]+", "-", text.strip().lower())
    if generic in {
        "custom",
        "api",
        "openai",
        "openai-compatible",
        "custom-provider",
        "default",
        "provider",
    }:
        return ""
    return text


def _provider_display_name(provider_id: object, fallback: str = "custom") -> str:
    provider_name = str(provider_id or "").strip() or fallback
    try:
        from core.providers import ProviderRegistry

        provider = ProviderRegistry.get_provider(provider_name)
        if provider and provider.display_name:
            return provider.display_name
    except Exception:
        pass
    return provider_name


def _claude_station_label(settings: dict) -> str:
    env = _claude_env(settings)
    provider_id = detect_claude_provider(settings)
    if provider_id == "custom":
        return _station_label_candidate(env.get("ANTHROPIC_BASE_URL")) or "Custom"
    return _provider_display_name(provider_id, provider_id)


def _codex_station_label(config: dict) -> str:
    provider_id = str(config.get("model_provider") or "openai")
    custom = _codex_provider_table(config, provider_id)
    for value in [custom.get("name"), custom.get("display_name")]:
        label = _station_label_candidate(value)
        if label:
            return label
    if provider_id == "custom" or custom:
        return (
            _station_label_candidate(custom.get("base_url"))
            or _station_label_candidate(provider_id)
            or _provider_display_name(provider_id, provider_id)
        )
    return _provider_display_name(provider_id, provider_id)


def _unique_profile_name(existing: set[str], preferred: str) -> str:
    base = str(preferred or "").strip() or "Profile"
    if base not in existing:
        return base

    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_fingerprint(data: object) -> str:
    try:
        text = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        text = str(data)
    return _short_fingerprint(text)


def _decode_jwt_payload(value: str) -> dict:
    parts = str(value or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _iter_nested_strings(value: object, limit: int = 200):
    seen = 0
    stack = [value]
    while stack and seen < limit:
        item = stack.pop()
        if isinstance(item, str):
            seen += 1
            yield item
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _account_identity_parts(data: dict, fallback_prefix: str = "official-login") -> dict:
    human_keys = [
        "name",
        "display_name",
        "displayName",
        "full_name",
        "nickname",
        "preferred_username",
        "username",
    ]
    id_keys = [
        "email",
        "user_email",
        "account_email",
        "userId",
        "user_id",
        "account_id",
        "sub",
    ]
    human_values: list[str] = []
    email_values: list[str] = []
    stable_values: list[str] = []

    def add_human(value: object) -> None:
        label = _usable_label(value)
        if label and not _looks_like_url(label) and "@" not in label:
            human_values.append(label)

    def add_id(value: object) -> None:
        label = _usable_label(value)
        if not label:
            return
        if "@" in label and len(label) <= 160:
            email_values.append(label.lower())
        else:
            stable_values.append(label)

    for key in human_keys:
        add_human(data.get(key))
    for key in id_keys:
        add_id(data.get(key))

    for value in _iter_nested_strings(data):
        if "@" in value and len(value) <= 160:
            add_id(value)
        jwt_payload = _decode_jwt_payload(value)
        for key in human_keys:
            add_human(jwt_payload.get(key))
        for key in id_keys:
            add_id(jwt_payload.get(key))

    display = next((value for value in human_values if value), "")
    email = next((value for value in email_values if value), "")
    stable = email or next((value for value in stable_values if value), "")
    if stable and stable != email:
        stable = f"id-{_short_fingerprint(stable)}"
    if not stable:
        stable = display or f"{fallback_prefix}-{_json_fingerprint(data)}"

    stable_candidates = set(email_values)
    stable_candidates.update(f"id-{_short_fingerprint(value)}" for value in stable_values if value)

    candidates = {stable}
    candidates.update(value for value in human_values if value)
    candidates.update(value for value in email_values if value)
    candidates.update(stable_candidates)

    return {
        "display": display,
        "email": email,
        "identity": stable,
        "preferred_name": display or email or stable,
        "stable_candidates": {value for value in stable_candidates if value},
        "candidates": {value for value in candidates if value},
    }


def _identity_from_json(data: dict, fallback_prefix: str = "official-login") -> str:
    if not isinstance(data, dict) or not data:
        return f"{fallback_prefix}-empty"
    return str(_account_identity_parts(data, fallback_prefix)["identity"])


def _account_preferred_name_from_json(data: dict, fallback_prefix: str = "official-login") -> str:
    if not isinstance(data, dict) or not data:
        return f"{fallback_prefix}-empty"
    return str(_account_identity_parts(data, fallback_prefix)["preferred_name"])


def _account_identity_candidates_from_json(data: dict, fallback_prefix: str = "official-login") -> set[str]:
    if not isinstance(data, dict) or not data:
        return {f"{fallback_prefix}-empty"}
    return set(_account_identity_parts(data, fallback_prefix)["candidates"])


def _account_stable_identity_candidates_from_json(data: dict, fallback_prefix: str = "official-login") -> set[str]:
    if not isinstance(data, dict) or not data:
        return set()
    return set(_account_identity_parts(data, fallback_prefix)["stable_candidates"])


def _account_snapshots_match(saved: dict, current: dict, fallback_prefix: str = "official-login") -> bool:
    if not isinstance(saved, dict) or not saved or not isinstance(current, dict) or not current:
        return False

    saved_parts = _account_identity_parts(saved, fallback_prefix)
    current_parts = _account_identity_parts(current, fallback_prefix)
    saved_stable = set(saved_parts["stable_candidates"])
    current_stable = set(current_parts["stable_candidates"])
    if saved_stable and current_stable:
        return bool(saved_stable & current_stable)
    return bool(set(saved_parts["candidates"]) & set(current_parts["candidates"]))


def _account_import_name(prefix: str, identity: str, existing: set[str]) -> str:
    safe_identity = _safe_name_part(identity, "official-login")
    return _unique_profile_name(existing, f"{prefix}-{safe_identity}")


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
    provider = _safe_name_part(_codex_station_label(config), "Codex-API")
    model = _safe_name_part(config.get("model"), "model")
    if model and model != "model":
        return f"Codex-{provider}-{model}"
    identity = _safe_name_part(_codex_auth_identity_from_auth(auth), "auth")
    return f"Codex-{provider}-{identity}"


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
        profile_kwargs["custom_env_key"] = custom.get("env_key")
        profile_kwargs["custom_requires_openai_auth"] = custom.get("requires_openai_auth", False)

    _store_codex_auth_secrets(name, profile_kwargs, auth)
    return profile_kwargs


def _store_codex_auth_secrets(name: str, profile_kwargs: dict, auth: dict) -> None:
    api_key = auth.get("OPENAI_API_KEY") if isinstance(auth, dict) else None
    if api_key:
        ref = f"codex:{name}:api_key"
        security.set_secret(ref, api_key)
        profile_kwargs["api_key_ref"] = ref


def _codex_config_env_key(config: dict) -> str:
    provider_id = str(config.get("model_provider") or "openai")
    custom = _codex_provider_table(config, provider_id)
    if custom.get("env_key"):
        return str(custom.get("env_key"))
    try:
        from core.providers import ProviderRegistry

        return ProviderRegistry.get_codex_env_key(provider_id, custom_name=custom.get("name"))
    except Exception:
        return "OPENAI_API_KEY"


def _codex_api_key_from_config_or_env(config: dict, auth: dict) -> tuple[str, str]:
    auth_key = str(auth.get("OPENAI_API_KEY") or "") if isinstance(auth, dict) else ""
    if auth_key:
        return auth_key, "OPENAI_API_KEY"

    env_key = _codex_config_env_key(config)
    try:
        from core import persistent_env

        return persistent_env._environment_value(env_key), env_key
    except Exception:
        return "", env_key


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


def _codex_expected_wire_api(profile: CodexProfile) -> str:
    try:
        from core.providers import ProviderRegistry

        return ProviderRegistry.get_codex_wire_api_for_profile(profile)
    except Exception:
        return str(profile.custom_wire_api or "responses")


def _codex_current_wire_api(config: dict, profile: CodexProfile, custom: dict) -> str:
    try:
        from core.providers import ProviderRegistry

        raw_wire_api = custom.get("wire_api")
        if raw_wire_api:
            normalized = ProviderRegistry.normalize_codex_wire_api(str(raw_wire_api))
            return normalized or f"invalid:{raw_wire_api}"
        return ProviderRegistry.get_codex_wire_api(
            profile.model_provider,
            None,
            custom.get("name"),
        )
    except Exception:
        return str(custom.get("wire_api") or profile.custom_wire_api or "responses")


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
    if _codex_expected_wire_api(profile) != _codex_current_wire_api(config, profile, custom):
        return False
    try:
        from core.providers import ProviderRegistry

        expected_env_key = ProviderRegistry.get_codex_env_key_for_profile(profile)
    except Exception:
        expected_env_key = profile.custom_env_key or "OPENAI_API_KEY"
    current_env_key = custom.get("env_key") or expected_env_key
    if current_env_key != expected_env_key:
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

    return _unique_profile_name({profile.name for profile in profiles}, base_name)


def save_codex_profile(profile: CodexProfile, previous_name: str | None = None) -> None:
    store = _load_store()
    profiles = store.get("codex_profiles", [])
    replaced_names = {profile.name}
    if previous_name:
        replaced_names.add(previous_name)

    replaced_refs: set[str] = set()
    for existing in profiles:
        if isinstance(existing, dict) and existing.get("name") in replaced_names:
            replaced_refs.update(_profile_secret_refs(existing))

    new_refs = _profile_secret_refs(profile)
    profiles = [
        p for p in profiles
        if isinstance(p, dict) and p.get("name") not in replaced_names
    ]
    profiles.append(profile.to_dict())
    store["codex_profiles"] = profiles
    if previous_name and store.get("active_codex_profile") == previous_name:
        store["active_codex_profile"] = profile.name
    _save_store(store)

    for ref in replaced_refs - new_refs:
        security.delete_secret(ref)


def clone_codex_profile(name: str) -> CodexProfile:
    profiles = list_codex_profiles()
    source = next((p for p in profiles if p.name == name), None)
    if not source:
        raise ValueError(f"Codex profile '{name}' not found")

    new_name = _unique_profile_name({p.name for p in profiles}, f"{source.name}-copy")
    api_key_ref = None
    api_key = security.get_secret(source.api_key_ref) or ""
    if api_key:
        api_key_ref = f"codex:{new_name}:api_key"
        security.set_secret(api_key_ref, api_key)

    cloned = CodexProfile(
        name=new_name,
        api_key_ref=api_key_ref,
        model=source.model,
        model_provider=source.model_provider,
        model_reasoning_effort=source.model_reasoning_effort,
        approval_policy=source.approval_policy,
        sandbox_mode=source.sandbox_mode,
        custom_base_url=source.custom_base_url,
        custom_name=source.custom_name,
        custom_wire_api=source.custom_wire_api,
        custom_env_key=source.custom_env_key,
        custom_requires_openai_auth=source.custom_requires_openai_auth,
        disable_response_storage=source.disable_response_storage,
    )
    save_codex_profile(cloned)
    return cloned


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


# --- Codex Official Account CRUD ---

def list_codex_account_profiles() -> list[CodexAccountProfile]:
    store = _load_store()
    return _load_profile_list(store.get("codex_account_profiles", []), CodexAccountProfile, "Codex account")


def get_active_codex_account_name() -> str | None:
    return _load_store().get("active_codex_account")


def set_active_codex_account(name: str | None) -> None:
    store = _load_store()
    store["active_codex_account"] = name
    _save_store(store)


def save_codex_account_profile(profile: CodexAccountProfile) -> None:
    store = _load_store()
    profiles = store.get("codex_account_profiles", [])
    profiles = [p for p in profiles if isinstance(p, dict) and p.get("name") != profile.name]
    profiles.append(profile.to_dict())
    store["codex_account_profiles"] = profiles
    _save_store(store)


def get_codex_account_auth(profile: CodexAccountProfile) -> dict | None:
    return security.get_secret_json(profile.auth_json_ref)


def _codex_official_auth_available(auth: dict) -> bool:
    if not isinstance(auth, dict):
        return False
    tokens = auth.get("tokens")
    return isinstance(tokens, dict) and any(bool(value) for value in tokens.values())


def _normalize_codex_official_auth(auth: dict) -> dict:
    if not isinstance(auth, dict):
        raise ValueError("Codex 账号快照格式异常")
    if not _codex_official_auth_available(auth):
        raise ValueError("Codex 账号快照里没有可用 ChatGPT 登录 token")

    normalized = dict(auth)
    normalized["auth_mode"] = "chatgpt"
    normalized.pop("OPENAI_API_KEY", None)
    return normalized


def _validate_codex_account_auth(auth: object) -> tuple[bool, str]:
    ok, reason = _validate_account_snapshot(auth, "Codex 账号")
    if not ok:
        return ok, reason
    if not _codex_official_auth_available(auth):
        return False, "Codex 账号快照里没有可用 ChatGPT 登录 token"
    return True, "可用"


def validate_codex_account_snapshot(profile: CodexAccountProfile) -> tuple[bool, str]:
    return _validate_codex_account_auth(get_codex_account_auth(profile))


def load_codex_account_auth(profile: CodexAccountProfile) -> dict:
    auth = get_codex_account_auth(profile)
    ok, reason = _validate_codex_account_auth(auth)
    if not ok:
        raise ValueError(reason)
    return _normalize_codex_official_auth(auth)


def _codex_account_identity_from_auth(auth: dict) -> str:
    return _identity_from_json(auth, "codex-login")


def _codex_account_preferred_name(auth: dict) -> str:
    return _account_preferred_name_from_json(auth, "codex-login")


def _codex_account_identity_candidates(auth: dict) -> set[str]:
    return _account_identity_candidates_from_json(auth, "codex-login")


def _codex_account_matches_auth(profile: CodexAccountProfile, auth: dict) -> bool:
    identity = _codex_account_identity_from_auth(auth)
    if profile.identity == identity:
        return True

    saved = get_codex_account_auth(profile)
    if isinstance(saved, dict) and saved:
        return _account_snapshots_match(saved, auth, "codex-login")

    stable_candidates = _account_stable_identity_candidates_from_json(auth, "codex-login")
    if stable_candidates:
        return profile.identity in stable_candidates
    return profile.identity in _codex_account_identity_candidates(auth)


def _codex_account_override_active(config: dict, auth: dict) -> bool:
    if not _codex_official_auth_available(auth):
        return True
    if str(auth.get("auth_mode") or "").strip() == "api_key":
        return True
    return config.get("model_provider", "openai") != "openai"


def _pick_codex_account_import_name(identity: str, preferred_name: str | None = None, auth: dict | None = None) -> str:
    profiles = list_codex_account_profiles()
    for profile in profiles:
        if profile.identity == identity:
            return profile.name
        if auth and _codex_account_matches_auth(profile, auth):
            return profile.name
    return _account_import_name("Codex-账号", preferred_name or identity, {profile.name for profile in profiles})


def import_current_codex_account() -> CodexAccountProfile | None:
    """Create a local-only account snapshot from Codex auth.json."""
    from core.auth_parser import read_codex_auth

    auth = read_codex_auth()
    if not _codex_official_auth_available(auth):
        return None
    auth = _normalize_codex_official_auth(auth)

    identity = _codex_account_identity_from_auth(auth)
    preferred_name = _codex_account_preferred_name(auth)
    name = _pick_codex_account_import_name(identity, preferred_name, auth)
    ref = f"codex-account:{name}:auth_json"
    security.set_secret_json(ref, auth)
    return CodexAccountProfile(
        name=name,
        auth_json_ref=ref,
        identity=identity,
        created_at=_now_iso(),
    )


def delete_codex_account_profile(name: str) -> None:
    store = _load_store()
    profile_refs = set()
    for profile in store.get("codex_account_profiles", []):
        if isinstance(profile, dict) and profile.get("name") == name:
            ref = profile.get("auth_json_ref")
            if isinstance(ref, str) and ref:
                profile_refs.add(ref)
            break
    for ref in profile_refs:
        security.delete_secret(ref)
    security.delete_secret(f"codex-account:{name}:auth_json")

    store["codex_account_profiles"] = [
        p for p in store.get("codex_account_profiles", [])
        if isinstance(p, dict) and p.get("name") != name
    ]
    if store.get("active_codex_account") == name:
        store["active_codex_account"] = None
    _save_store(store)


def get_current_codex_account_name() -> str | None:
    from core.toml_parser import read_codex_config
    from core.auth_parser import read_codex_auth

    config = read_codex_config()
    auth = read_codex_auth()
    if not _codex_official_auth_available(auth) or _codex_account_override_active(config, auth):
        return None

    for profile in list_codex_account_profiles():
        if _codex_account_matches_auth(profile, auth):
            return profile.name
    return None


def get_codex_account_runtime_summary() -> dict:
    from core.toml_parser import read_codex_config
    from core.auth_parser import read_codex_auth

    config = read_codex_config()
    auth = read_codex_auth()
    has_official_auth = _codex_official_auth_available(auth)
    override_active = _codex_account_override_active(config, auth)
    identity = _codex_account_identity_from_auth(auth) if has_official_auth else "no-login"
    profile_name = None
    if has_official_auth and not override_active:
        for profile in list_codex_account_profiles():
            if _codex_account_matches_auth(profile, auth):
                profile_name = profile.name
                break

    return {
        "profile_name": profile_name,
        "stored_active": get_active_codex_account_name(),
        "identity": identity,
        "has_auth": bool(auth),
        "has_official_auth": has_official_auth,
        "api_override_active": override_active,
        "credentials_store": config.get("cli_auth_credentials_store", "file") if config else "file",
    }


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
    api_key, env_key = _codex_api_key_from_config_or_env(config, auth)
    if not api_key:
        return None

    auth_for_import = dict(auth)
    auth_for_import["auth_mode"] = "api_key"
    auth_for_import["OPENAI_API_KEY"] = api_key
    name = _pick_codex_import_name(config, auth_for_import)
    profile_kwargs = _codex_profile_kwargs_from_current(name, config, auth_for_import)
    if env_key != "OPENAI_API_KEY":
        profile_kwargs["custom_env_key"] = env_key
    return CodexProfile(**profile_kwargs)


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


def save_browser_profile(profile: BrowserProfile, previous_name: str | None = None) -> None:
    store = _load_store()
    profiles = store.get("browser_profiles", [])
    replaced_names = {profile.name}
    if previous_name:
        replaced_names.add(previous_name)
    profiles = [
        p for p in profiles
        if isinstance(p, dict) and p.get("name") not in replaced_names
    ]
    profiles.append(profile.to_dict())
    store["browser_profiles"] = profiles
    if previous_name and store.get("active_browser_profile") == previous_name:
        store["active_browser_profile"] = profile.name
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


def _disconnect_ssh_profiles(names: set[str]) -> None:
    if not names:
        return
    try:
        from core.ssh_manager import ssh_manager

        for name in names:
            if name:
                ssh_manager.disconnect(name)
    except Exception as e:
        logger.debug(f"Failed to disconnect SSH profiles after profile update: {e}")


def _profile_secret_refs(profile: object) -> set[str]:
    if hasattr(profile, "to_dict"):
        data = profile.to_dict()
    elif isinstance(profile, dict):
        data = profile
    else:
        data = {}
    return {
        value
        for key, value in data.items()
        if key.endswith("_ref") and isinstance(value, str) and value
    }


def save_ssh_profile(profile: SSHProfile, previous_name: str | None = None) -> None:
    store = _load_store()
    profiles = store.get("ssh_profiles", [])
    replaced_names = {profile.name}
    if previous_name:
        replaced_names.add(previous_name)

    replaced_refs: set[str] = set()
    for existing in profiles:
        if isinstance(existing, dict) and existing.get("name") in replaced_names:
            replaced_refs.update(_profile_secret_refs(existing))

    new_refs = _profile_secret_refs(profile)
    profiles = [
        p for p in profiles
        if isinstance(p, dict) and p.get("name") not in replaced_names
    ]
    profiles.append(profile.to_dict())
    store["ssh_profiles"] = profiles
    if previous_name and store.get("active_ssh_profile") == previous_name:
        store["active_ssh_profile"] = profile.name
    _save_store(store)

    _disconnect_ssh_profiles(replaced_names | {profile.name})

    for ref in replaced_refs - new_refs:
        security.delete_secret(ref)

    if previous_name and previous_name != profile.name:
        for suffix in ["password", "key_passphrase"]:
            ref = f"ssh:{previous_name}:{suffix}"
            if ref not in new_refs:
                security.delete_secret(ref)


def delete_ssh_profile(name: str) -> None:
    store = _load_store()
    _disconnect_ssh_profiles({name})

    profile_refs = set()
    for profile in store.get("ssh_profiles", []):
        if isinstance(profile, dict) and profile.get("name") == name:
            profile_refs.update(_profile_secret_refs(profile))
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
