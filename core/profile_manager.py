import json
import logging
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


def get_active_claude_name() -> str | None:
    return _load_store().get("active_claude_profile")


def set_active_claude(name: str) -> None:
    store = _load_store()
    store["active_claude_profile"] = name
    _save_store(store)


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
    # Clean up keyring secrets
    for suffix in ["auth_token"]:
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


def get_active_codex_name() -> str | None:
    return _load_store().get("active_codex_profile")


def set_active_codex(name: str) -> None:
    store = _load_store()
    store["active_codex_profile"] = name
    _save_store(store)


def save_codex_profile(profile: CodexProfile) -> None:
    store = _load_store()
    profiles = store.get("codex_profiles", [])
    profiles = [p for p in profiles if isinstance(p, dict) and p.get("name") != profile.name]
    profiles.append(profile.to_dict())
    store["codex_profiles"] = profiles
    _save_store(store)


def delete_codex_profile(name: str) -> None:
    store = _load_store()
    # Clean up keyring secrets
    for suffix in ["api_key", "openai_auth_key", "oauth_tokens", "oauth_meta"]:
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
    """Create a ClaudeProfile from the current settings.json."""
    from core.parser import read_claude_settings
    settings = read_claude_settings()
    if not settings:
        return None

    env = settings.get("env", {})
    if not isinstance(env, dict):
        env = {}
    token_value = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY", "")
    base_url = env.get("ANTHROPIC_BASE_URL", "")
    provider_name = detect_claude_provider(settings)

    name = "Current"
    token_ref = f"claude:{name}:auth_token"

    if token_value:
        security.set_secret(token_ref, token_value)

    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        permissions = {}

    profile = ClaudeProfile(
        name=name,
        auth_token_ref=token_ref,
        base_url=base_url,
        model=settings.get("model", ""),
        effort_level=settings.get("effortLevel", "high"),
        permissions_mode=permissions.get("defaultMode", "default"),
        skip_dangerous_prompt=settings.get("skipDangerousModePermissionPrompt", False),
        permissions_allow=permissions.get("allow", []),
        additional_directories=settings.get("additionalDirectories", []),
        provider=provider_name,
    )
    return profile


def import_current_codex() -> CodexProfile | None:
    """Create a CodexProfile from the current config.toml + auth.json."""
    from core.toml_parser import read_codex_config
    from core.auth_parser import read_codex_auth, extract_oauth_meta

    config = read_codex_config()
    auth = read_codex_auth()
    if not config and not auth:
        return None

    name = "Current"
    auth_mode = auth.get("auth_mode", "chatgpt")

    profile_kwargs = {
        "name": name,
        "auth_mode": auth_mode,
        "model": config.get("model", "gpt-5.5"),
        "model_provider": config.get("model_provider", "openai"),
        "model_reasoning_effort": config.get("model_reasoning_effort", "high"),
        "approval_policy": config.get("approval_policy", "never"),
        "sandbox_mode": config.get("sandbox_mode", "danger-full-access"),
        "disable_response_storage": config.get("disable_response_storage", True),
    }

    # Custom provider
    provider_id = profile_kwargs["model_provider"]
    model_providers = config.get("model_providers", {})
    if not isinstance(model_providers, dict):
        model_providers = {}
    custom = model_providers.get(provider_id, {})
    if not isinstance(custom, dict):
        custom = {}
    if custom:
        profile_kwargs["custom_base_url"] = custom.get("base_url")
        profile_kwargs["custom_name"] = custom.get("name")
        profile_kwargs["custom_wire_api"] = custom.get("wire_api")
        profile_kwargs["custom_requires_openai_auth"] = custom.get("requires_openai_auth", False)

    if auth_mode == "api_key":
        api_key = auth.get("OPENAI_API_KEY")
        if api_key:
            ref = f"codex:{name}:api_key"
            security.set_secret(ref, api_key)
            profile_kwargs["api_key_ref"] = ref
    else:
        tokens = auth.get("tokens", {})
        if isinstance(tokens, dict) and tokens:
            ref = f"codex:{name}:oauth_tokens"
            security.set_secret_json(ref, tokens)
            profile_kwargs["oauth_tokens_ref"] = ref
            profile_kwargs["last_refresh"] = auth.get("last_refresh")

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
    # Clean up keyring secrets
    for suffix in ["password", "key_passphrase"]:
        security.delete_secret(f"ssh:{name}:{suffix}")

    store["ssh_profiles"] = [
        p for p in store.get("ssh_profiles", [])
        if isinstance(p, dict) and p.get("name") != name
    ]
    if store.get("active_ssh_profile") == name:
        store["active_ssh_profile"] = None
    _save_store(store)
