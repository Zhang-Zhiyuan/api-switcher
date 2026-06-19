import logging
import os

from core import backup_manager, parser, toml_parser, auth_parser, vscode_parser, profile_manager, security
from core.providers import ProviderRegistry
from core.usage_recorder import usage_recorder

logger = logging.getLogger(__name__)


def _codex_api_env_names_to_clear(config: dict | None = None) -> list[str]:
    names = ["OPENAI_API_KEY"]
    for provider in ProviderRegistry.get_codex_providers():
        names.append(provider.codex_env_key)
    for profile in profile_manager.list_switchable_codex_profiles():
        names.extend(ProviderRegistry.get_codex_runtime_env_keys_for_profile(profile))

    model_providers = (config or {}).get("model_providers")
    if isinstance(model_providers, dict):
        for table in model_providers.values():
            if isinstance(table, dict):
                names.append(str(table.get("env_key") or ""))

    normalized = []
    for name in names:
        name = str(name or "").strip()
        if name and name not in normalized:
            normalized.append(name)
    return normalized


def _clear_local_codex_api_env(config: dict | None = None) -> None:
    env_names = _codex_api_env_names_to_clear(config)
    for name in env_names:
        os.environ.pop(name, None)

    if os.name != "nt":
        logger.warning("Local persistent Codex API env cleanup skipped on non-Windows platform")
        return

    from core import persistent_env

    try:
        persistent_env.delete_local_user_env(env_names)
    except Exception as e:
        raise RuntimeError(f"清理 Codex API 环境变量失败: {e}") from e


def switch_claude_profile(name: str) -> None:
    """Switch to a named Claude API configuration. Auto-backup before switching."""
    profiles = profile_manager.list_switchable_claude_profiles()
    target = next((p for p in profiles if p.name == name), None)
    if not target:
        raise ValueError(f"未找到 Claude API 配置: {name}")

    if not profile_manager.is_third_party_claude_profile(target):
        raise ValueError("只能切换第三方 Claude API 配置")
    if not (security.get_secret(target.auth_token_ref) or security.get_secret(getattr(target, "primary_api_key_ref", None))):
        raise ValueError("Claude API 配置需要 Auth Token")

    backup_manager.create_backup(f"切换 Claude 到: {name}")

    settings = parser.read_claude_settings()
    settings = parser.apply_claude_profile(settings, target)
    parser.write_claude_settings(settings)

    current_config = parser.read_claude_config()
    config = parser.apply_claude_config(current_config, target)
    if config or current_config:
        parser.write_claude_config(config)

    # Sync VS Code settings
    vscode = vscode_parser.read_vscode_settings()
    vscode = vscode_parser.apply_permission_mode(vscode, target.permissions_mode, target.skip_dangerous_prompt)
    vscode = vscode_parser.apply_model(vscode, target.model)
    vscode_parser.write_vscode_settings(vscode)

    profile_manager.set_active_claude(name)
    profile_manager.set_active_claude_account(None)

    # Record usage statistics
    usage_recorder.start_session(name, "claude")

    logger.info(f"Switched Claude profile to: {name}")


def switch_codex_profile(name: str) -> None:
    """Switch to a named Codex API configuration. Auto-backup before switching."""
    profiles = profile_manager.list_switchable_codex_profiles()
    target = next((p for p in profiles if p.name == name), None)
    if not target:
        raise ValueError(f"未找到 Codex API 配置: {name}")

    if not profile_manager.is_third_party_codex_profile(target):
        raise ValueError("只能切换第三方 Codex API 配置")
    api_key = security.get_secret(target.api_key_ref)
    if not api_key:
        raise ValueError("Codex API 配置需要 API Key")

    backup_manager.create_backup(f"切换 Codex 到: {name}")

    env_keys = ProviderRegistry.get_codex_runtime_env_keys_for_profile(target)
    env_updates = {key: api_key for key in env_keys}
    for key, value in env_updates.items():
        os.environ[key] = value
    if os.name == "nt":
        from core import persistent_env

        try:
            persistent_env.set_local_user_env(env_updates)
        except Exception as e:
            raise RuntimeError(f"写入 Codex API 环境变量 {', '.join(env_keys)} 失败: {e}") from e
    else:
        logger.warning("Local persistent env write skipped on non-Windows platform for %s", ", ".join(env_keys))

    # Update config.toml
    config = toml_parser.read_codex_config()
    config = toml_parser.apply_codex_profile(config, target)
    toml_parser.write_codex_config(config)

    # Update auth.json
    auth = auth_parser.read_codex_auth()
    auth = auth_parser.apply_codex_apikey(auth, target)
    auth_parser.write_codex_auth(auth)

    profile_manager.set_active_codex(name)
    profile_manager.set_active_codex_account(None)

    # Record usage statistics
    usage_recorder.start_session(name, "codex")

    logger.info(f"Switched Codex profile to: {name}")


def switch_claude_account(name: str) -> None:
    """Switch Claude Code back to a saved official login snapshot."""
    profiles = profile_manager.list_claude_account_profiles()
    target = next((p for p in profiles if p.name == name), None)
    if not target:
        raise ValueError(f"Claude account '{name}' not found")

    credentials = profile_manager.load_claude_account_credentials(target)

    backup_manager.create_backup(f"切换 Claude 官方账号到 {name}")

    parser.write_claude_credentials(credentials)

    settings = parser.read_claude_settings()
    parser.write_claude_settings(parser.clear_claude_api_overrides(settings))

    current_config = parser.read_claude_config()
    config = parser.clear_claude_config_auth(current_config)
    if config or current_config:
        parser.write_claude_config(config)

    profile_manager.set_active_claude_account(name)
    profile_manager.set_active_claude(None)

    logger.info(f"Switched Claude official account to: {name}")


def switch_codex_account(name: str) -> None:
    """Switch Codex CLI back to a saved ChatGPT login snapshot."""
    profiles = profile_manager.list_codex_account_profiles()
    target = next((p for p in profiles if p.name == name), None)
    if not target:
        raise ValueError(f"Codex account '{name}' not found")

    auth = profile_manager.load_codex_account_auth(target)

    backup_manager.create_backup(f"切换 Codex 官方账号到 {name}")

    auth_parser.write_codex_auth(auth)

    config = toml_parser.read_codex_config()
    _clear_local_codex_api_env(config)
    config = toml_parser.apply_codex_official_account(config)
    toml_parser.write_codex_config(config)

    profile_manager.set_active_codex_account(name)
    profile_manager.set_active_codex(None)

    logger.info(f"Switched Codex official account to: {name}")


def toggle_bypass_permissions(enabled: bool) -> None:
    """Toggle bypass permissions for Claude + VS Code."""
    settings = parser.read_claude_settings()
    if not isinstance(settings.get("permissions"), dict):
        settings["permissions"] = {}
    settings["permissions"]["defaultMode"] = "bypassPermissions" if enabled else "default"
    settings["skipDangerousModePermissionPrompt"] = enabled
    parser.write_claude_settings(settings)

    vscode = vscode_parser.read_vscode_settings()
    vscode = vscode_parser.apply_permissions(vscode, enabled, enabled)
    vscode_parser.write_vscode_settings(vscode)

    logger.info(f"Bypass permissions: {'enabled' if enabled else 'disabled'}")
