import logging
from core import ssh_manager, remote_config, parser, toml_parser, auth_parser, profile_manager, security
from models.profile import SSHProfile

logger = logging.getLogger(__name__)


def sync_claude_to_server(ssh_name: str, claude_name: str) -> str:
    """Sync Claude profile to remote server. Returns status message."""
    profiles = profile_manager.list_claude_profiles()
    claude_profile = next((p for p in profiles if p.name == claude_name), None)
    if not claude_profile:
        raise ValueError(f"Claude profile '{claude_name}' not found")

    ssh_profiles = profile_manager.list_ssh_profiles()
    ssh_profile = next((p for p in ssh_profiles if p.name == ssh_name), None)
    if not ssh_profile:
        raise ValueError(f"SSH server '{ssh_name}' not found")

    client = ssh_manager.connect(ssh_profile)

    # Get current local settings and apply profile
    settings = parser.read_claude_settings()
    settings = parser.apply_claude_profile(settings, claude_profile)

    # Write to remote
    remote_config.write_remote_claude_settings(client, settings)

    logger.info(f"Synced Claude profile '{claude_name}' to {ssh_profile.host}")
    return f"已同步 Claude 配置到 {ssh_profile.host}"


def sync_codex_to_server(ssh_name: str, codex_name: str) -> str:
    """Sync Codex profile to remote server. Returns status message."""
    profiles = profile_manager.list_codex_profiles()
    codex_profile = next((p for p in profiles if p.name == codex_name), None)
    if not codex_profile:
        raise ValueError(f"Codex profile '{codex_name}' not found")

    ssh_profiles = profile_manager.list_ssh_profiles()
    ssh_profile = next((p for p in ssh_profiles if p.name == ssh_name), None)
    if not ssh_profile:
        raise ValueError(f"SSH server '{ssh_name}' not found")

    client = ssh_manager.connect(ssh_profile)

    # Update config.toml
    config = toml_parser.read_codex_config()
    config = toml_parser.apply_codex_profile(config, codex_profile)
    remote_config.write_remote_codex_config(client, config)

    # Update auth.json
    auth = auth_parser.read_codex_auth()
    if codex_profile.auth_mode == "api_key":
        auth = auth_parser.apply_codex_apikey(auth, codex_profile)
    else:
        auth = auth_parser.apply_codex_oauth(auth, codex_profile)
    remote_config.write_remote_codex_auth(client, auth)

    logger.info(f"Synced Codex profile '{codex_name}' to {ssh_profile.host}")
    return f"已同步 Codex 配置到 {ssh_profile.host}"


def sync_all_to_server(ssh_name: str) -> str:
    """Sync current local Claude + Codex config to remote server."""
    results = []

    # Sync Claude
    active_claude = profile_manager.get_active_claude_name()
    if active_claude:
        results.append(sync_claude_to_server(ssh_name, active_claude))

    # Sync Codex
    active_codex = profile_manager.get_active_codex_name()
    if active_codex:
        results.append(sync_codex_to_server(ssh_name, active_codex))

    return " | ".join(results) if results else "没有活动的 Profile 可同步"


def pull_claude_from_server(ssh_name: str) -> str:
    """Pull Claude config from server and save as a profile."""
    ssh_profiles = profile_manager.list_ssh_profiles()
    ssh_profile = next((p for p in ssh_profiles if p.name == ssh_name), None)
    if not ssh_profile:
        raise ValueError(f"SSH server '{ssh_name}' not found")

    client = ssh_manager.connect(ssh_profile)
    settings = remote_config.read_remote_claude_settings(client)
    if not settings:
        return "服务器上未找到 Claude 配置"

    # Create profile from remote settings
    name = f"Remote-{ssh_name}"
    env = settings.get("env", {})
    if not isinstance(env, dict):
        env = {}
    token_value = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY", "")

    from models.profile import ClaudeProfile
    token_ref = f"claude:{name}:auth_token"
    if token_value:
        security.set_secret(token_ref, token_value)

    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        permissions = {}
    profile = ClaudeProfile(
        name=name,
        auth_token_ref=token_ref,
        base_url=env.get("ANTHROPIC_BASE_URL", ""),
        model=settings.get("model", ""),
        effort_level=settings.get("effortLevel", "high"),
        permissions_mode=permissions.get("defaultMode", "default"),
        skip_dangerous_prompt=settings.get("skipDangerousModePermissionPrompt", False),
        permissions_allow=permissions.get("allow", []),
        provider=profile_manager.detect_claude_provider(settings),
    )
    profile_manager.save_claude_profile(profile)

    return f"已从 {ssh_profile.host} 拉取 Claude 配置，保存为 '{name}'"


def pull_codex_from_server(ssh_name: str) -> str:
    """Pull Codex config from server and save as a profile."""
    ssh_profiles = profile_manager.list_ssh_profiles()
    ssh_profile = next((p for p in ssh_profiles if p.name == ssh_name), None)
    if not ssh_profile:
        raise ValueError(f"SSH server '{ssh_name}' not found")

    client = ssh_manager.connect(ssh_profile)

    config = remote_config.read_remote_codex_config(client)
    auth = remote_config.read_remote_codex_auth(client)

    if not config and not auth:
        return "服务器上未找到 Codex 配置"

    name = f"Remote-{ssh_name}"
    auth_mode = auth.get("auth_mode", "chatgpt") if auth else "chatgpt"

    from models.profile import CodexProfile
    profile_kwargs = {
        "name": name,
        "auth_mode": auth_mode,
        "model": config.get("model", "gpt-5.5") if config else "gpt-5.5",
        "model_provider": config.get("model_provider", "openai") if config else "openai",
        "model_reasoning_effort": config.get("model_reasoning_effort", "high") if config else "high",
        "approval_policy": config.get("approval_policy", "never") if config else "never",
        "sandbox_mode": config.get("sandbox_mode", "danger-full-access") if config else "danger-full-access",
        "disable_response_storage": config.get("disable_response_storage", True) if config else True,
    }

    if config:
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

    if auth:
        if auth_mode == "api_key" and auth.get("OPENAI_API_KEY"):
            ref = f"codex:{name}:api_key"
            security.set_secret(ref, auth["OPENAI_API_KEY"])
            profile_kwargs["api_key_ref"] = ref
        elif isinstance(auth.get("tokens"), dict) and auth.get("tokens"):
            ref = f"codex:{name}:oauth_tokens"
            security.set_secret_json(ref, auth["tokens"])
            profile_kwargs["oauth_tokens_ref"] = ref
            profile_kwargs["last_refresh"] = auth.get("last_refresh")

    profile = CodexProfile(**profile_kwargs)
    profile_manager.save_codex_profile(profile)

    return f"已从 {ssh_profile.host} 拉取 Codex 配置，保存为 '{name}'"
