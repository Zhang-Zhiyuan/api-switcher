import logging

from core import backup_manager, parser, toml_parser, auth_parser, vscode_parser, profile_manager, security
from core.usage_recorder import usage_recorder

logger = logging.getLogger(__name__)


def switch_claude_profile(name: str) -> None:
    """Switch to a named Claude profile. Auto-backup before switching."""
    profiles = profile_manager.list_switchable_claude_profiles()
    target = next((p for p in profiles if p.name == name), None)
    if not target:
        raise ValueError(f"Claude profile '{name}' not found")

    if not profile_manager.is_third_party_claude_profile(target):
        raise ValueError("只能切换第三方 Claude API Profile")
    if not (security.get_secret(target.auth_token_ref) or security.get_secret(getattr(target, "primary_api_key_ref", None))):
        raise ValueError("Claude API Profile 需要 Auth Token")

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
    vscode = vscode_parser.apply_permissions(
        vscode, target.permissions_mode == "bypassPermissions", target.skip_dangerous_prompt
    )
    vscode = vscode_parser.apply_model(vscode, target.model)
    vscode_parser.write_vscode_settings(vscode)

    profile_manager.set_active_claude(name)

    # Record usage statistics
    usage_recorder.start_session(name, "claude")

    logger.info(f"Switched Claude profile to: {name}")


def switch_codex_profile(name: str) -> None:
    """Switch to a named Codex profile. Auto-backup before switching."""
    profiles = profile_manager.list_switchable_codex_profiles()
    target = next((p for p in profiles if p.name == name), None)
    if not target:
        raise ValueError(f"Codex profile '{name}' not found")

    if not profile_manager.is_third_party_codex_profile(target):
        raise ValueError("只能切换第三方 Codex API Profile")
    if not security.get_secret(target.api_key_ref):
        raise ValueError("Codex API Profile 需要 API Key")

    backup_manager.create_backup(f"切换 Codex 到: {name}")

    # Update config.toml
    config = toml_parser.read_codex_config()
    config = toml_parser.apply_codex_profile(config, target)
    toml_parser.write_codex_config(config)

    # Update auth.json
    auth = auth_parser.read_codex_auth()
    auth = auth_parser.apply_codex_apikey(auth, target)
    auth_parser.write_codex_auth(auth)

    profile_manager.set_active_codex(name)

    # Record usage statistics
    usage_recorder.start_session(name, "codex")

    logger.info(f"Switched Codex profile to: {name}")


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
