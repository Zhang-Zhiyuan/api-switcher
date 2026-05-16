import logging
from core import remote_config, parser, toml_parser, auth_parser, profile_manager, security
from core.providers import ProviderRegistry
from core.ssh_manager import ssh_manager

logger = logging.getLogger(__name__)


ROOT_BYPASS_ADJUSTED_MESSAGE = (
    "已兼容 root 登录：Claude Code 禁止 root/sudo 使用 "
    "bypassPermissions（--dangerously-skip-permissions），已自动将远端权限模式改为 default。"
)


def _find_profile(profiles: list, name: str, label: str):
    profile = next((p for p in profiles if p.name == name), None)
    if not profile:
        raise ValueError(f"未找到 {label}: {name}")
    return profile


def _connect_ssh(ssh_name: str):
    ssh_profile = _find_profile(profile_manager.list_ssh_profiles(), ssh_name, "SSH 服务器")
    return ssh_profile, ssh_manager.connect(ssh_profile)


def _is_root_ssh_user(ssh_profile) -> bool:
    username = str(getattr(ssh_profile, "username", "") or "").strip().lower()
    return username == "root"


def _make_claude_settings_root_safe(settings: dict, ssh_profile) -> tuple[dict, bool]:
    if not _is_root_ssh_user(ssh_profile):
        return settings, False

    settings = dict(settings)
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        return settings, False

    if permissions.get("defaultMode") != "bypassPermissions":
        return settings, False

    permissions = dict(permissions)
    permissions["defaultMode"] = "default"
    settings["permissions"] = permissions
    settings["skipDangerousModePermissionPrompt"] = False
    return settings, True


def _codex_profile_api_key(profile) -> str:
    return security.get_secret(getattr(profile, "api_key_ref", None)) or ""


def _codex_profile_env_key(profile) -> str:
    return ProviderRegistry.get_codex_env_key_for_profile(profile)


def _persist_remote_codex_env(client, profile, api_key: str) -> str:
    from core import persistent_env

    env_key = _codex_profile_env_key(profile)
    persistent_env.set_remote_user_env(client, {env_key: api_key})
    return env_key


def sync_claude_to_server(ssh_name: str, claude_name: str) -> str:
    """Sync Claude profile to remote server. Returns status message."""
    claude_profile = _find_profile(profile_manager.list_switchable_claude_profiles(), claude_name, "Claude API Profile")
    if not profile_manager.is_third_party_claude_profile(claude_profile):
        raise ValueError("只能同步第三方 Claude API Profile")
    if not (security.get_secret(claude_profile.auth_token_ref) or security.get_secret(getattr(claude_profile, "primary_api_key_ref", None))):
        raise ValueError("Claude API Profile 需要 Auth Token")

    ssh_profile, client = _connect_ssh(ssh_name)

    settings = remote_config.read_remote_claude_settings(client, ssh_profile) or {}
    settings = parser.apply_claude_profile(settings, claude_profile)
    settings, root_adjusted = _make_claude_settings_root_safe(settings, ssh_profile)
    config = remote_config.read_remote_claude_config(client, ssh_profile) or {}
    config = parser.apply_claude_config(config, claude_profile)

    remote_config.write_remote_claude_settings(client, settings, ssh_profile)
    remote_config.write_remote_claude_config(client, config, ssh_profile)

    logger.info(f"Synced Claude API profile '{claude_name}' to {ssh_profile.host}")
    message = f"已同步 Claude API '{claude_name}' 到 {ssh_profile.host}"
    return f"{message} | {ROOT_BYPASS_ADJUSTED_MESSAGE}" if root_adjusted else message


def sync_claude_account_to_server(ssh_name: str, account_name: str) -> str:
    """Sync a saved Claude official account snapshot to the remote server."""
    account = _find_profile(profile_manager.list_claude_account_profiles(), account_name, "Claude 官方账号")
    credentials = profile_manager.load_claude_account_credentials(account)

    ssh_profile, client = _connect_ssh(ssh_name)

    remote_config.write_remote_claude_credentials(client, credentials, ssh_profile)

    settings = remote_config.read_remote_claude_settings(client, ssh_profile) or {}
    settings = parser.clear_claude_api_overrides(settings)
    settings, root_adjusted = _make_claude_settings_root_safe(settings, ssh_profile)
    remote_config.write_remote_claude_settings(client, settings, ssh_profile)

    config = remote_config.read_remote_claude_config(client, ssh_profile) or {}
    remote_config.write_remote_claude_config(client, parser.clear_claude_config_auth(config), ssh_profile)

    logger.info(f"Synced Claude account '{account_name}' to {ssh_profile.host}")
    message = f"已同步 Claude 账号 '{account_name}' 到 {ssh_profile.host}"
    return f"{message} | {ROOT_BYPASS_ADJUSTED_MESSAGE}" if root_adjusted else message


def sync_codex_to_server(ssh_name: str, codex_name: str) -> str:
    """Sync Codex profile to remote server. Returns status message."""
    codex_profile = _find_profile(profile_manager.list_switchable_codex_profiles(), codex_name, "Codex API Profile")
    if not profile_manager.is_third_party_codex_profile(codex_profile):
        raise ValueError("只能同步第三方 Codex API Profile")
    api_key = _codex_profile_api_key(codex_profile)
    if not api_key:
        raise ValueError("Codex API Profile 需要 API Key")

    ssh_profile, client = _connect_ssh(ssh_name)

    config = remote_config.read_remote_codex_config(client, ssh_profile) or {}
    config = toml_parser.apply_codex_profile(config, codex_profile)
    remote_config.write_remote_codex_config(client, config, ssh_profile)

    auth = remote_config.read_remote_codex_auth(client, ssh_profile) or {}
    auth = auth_parser.apply_codex_apikey(auth, codex_profile)
    remote_config.write_remote_codex_auth(client, auth, ssh_profile)
    env_key = _persist_remote_codex_env(client, codex_profile, api_key)

    logger.info(f"Synced Codex API profile '{codex_name}' to {ssh_profile.host}")
    return f"已同步 Codex API '{codex_name}' 到 {ssh_profile.host} | 已写入远端环境变量 {env_key}"


def sync_codex_account_to_server(ssh_name: str, account_name: str) -> str:
    """Sync a saved Codex official account snapshot to the remote server."""
    account = _find_profile(profile_manager.list_codex_account_profiles(), account_name, "Codex 官方账号")
    auth = profile_manager.load_codex_account_auth(account)

    ssh_profile, client = _connect_ssh(ssh_name)

    remote_config.write_remote_codex_auth(client, auth, ssh_profile)

    config = remote_config.read_remote_codex_config(client, ssh_profile) or {}
    remote_config.write_remote_codex_config(client, toml_parser.apply_codex_official_account(config), ssh_profile)

    logger.info(f"Synced Codex account '{account_name}' to {ssh_profile.host}")
    return f"已同步 Codex 账号 '{account_name}' 到 {ssh_profile.host}"


def sync_selected_to_server(ssh_name: str, target_kind: str, name: str) -> str:
    """Sync one explicit local API profile or official account snapshot to the remote server."""
    handlers = {
        "claude_api": sync_claude_to_server,
        "claude_account": sync_claude_account_to_server,
        "codex_api": sync_codex_to_server,
        "codex_account": sync_codex_account_to_server,
    }
    handler = handlers.get(target_kind)
    if not handler:
        raise ValueError(f"不支持的同步类型: {target_kind}")
    return handler(ssh_name, name)


def sync_all_to_server(ssh_name: str) -> str:
    """Sync currently active local Claude + Codex target to remote server."""
    results = []
    failures = []

    claude_api = {p.name for p in profile_manager.list_switchable_claude_profiles()}
    claude_accounts = {p.name for p in profile_manager.list_claude_account_profiles()}
    active_claude_api = profile_manager.get_current_claude_name() or profile_manager.get_active_claude_name()
    active_claude_account = profile_manager.get_current_claude_account_name() or profile_manager.get_active_claude_account_name()
    if active_claude_api in claude_api:
        try:
            results.append(sync_claude_to_server(ssh_name, active_claude_api))
        except Exception as e:
            failures.append(f"Claude: {e}")
    elif active_claude_account in claude_accounts:
        try:
            results.append(sync_claude_account_to_server(ssh_name, active_claude_account))
        except Exception as e:
            failures.append(f"Claude 账号: {e}")

    codex_api = {p.name for p in profile_manager.list_switchable_codex_profiles()}
    codex_accounts = {p.name for p in profile_manager.list_codex_account_profiles()}
    active_codex_api = profile_manager.get_current_codex_name() or profile_manager.get_active_codex_name()
    active_codex_account = profile_manager.get_current_codex_account_name() or profile_manager.get_active_codex_account_name()
    if active_codex_api in codex_api:
        try:
            results.append(sync_codex_to_server(ssh_name, active_codex_api))
        except Exception as e:
            failures.append(f"Codex: {e}")
    elif active_codex_account in codex_accounts:
        try:
            results.append(sync_codex_account_to_server(ssh_name, active_codex_account))
        except Exception as e:
            failures.append(f"Codex 账号: {e}")

    if results and failures:
        return " | ".join(results) + " | 部分失败: " + "；".join(failures)
    if results:
        return " | ".join(results)
    if failures:
        raise RuntimeError("；".join(failures))
    return "没有当前生效的 API 或账号可同步"


def pull_claude_from_server(ssh_name: str) -> str:
    """Pull Claude config from server and save as a profile."""
    ssh_profiles = profile_manager.list_ssh_profiles()
    ssh_profile = next((p for p in ssh_profiles if p.name == ssh_name), None)
    if not ssh_profile:
        raise ValueError(f"SSH server '{ssh_name}' not found")

    client = ssh_manager.connect(ssh_profile)
    settings = remote_config.read_remote_claude_settings(client, ssh_profile)
    config = remote_config.read_remote_claude_config(client, ssh_profile) or {}
    if not settings and not config:
        return "服务器上未找到 Claude 配置"
    if not settings:
        settings = {}

    # Create profile from remote settings
    name = f"Remote-{ssh_name}"
    env = settings.get("env", {})
    if not isinstance(env, dict):
        env = {}
    token_value = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or config.get("primaryApiKey", "")
    primary_key = config.get("primaryApiKey", "")
    provider = profile_manager.detect_claude_provider(settings)
    if provider == "anthropic":
        return "远程 Claude 配置是官方 Anthropic，已跳过；当前只导入第三方 API Profile"

    from models.profile import ClaudeProfile
    token_ref = f"claude:{name}:auth_token"
    primary_ref = f"claude:{name}:primary_api_key"
    if token_value:
        security.set_secret(token_ref, token_value)
    if primary_key:
        security.set_secret(primary_ref, primary_key)

    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        permissions = {}
    additional_directories = settings.get("additionalDirectories", [])
    if not isinstance(additional_directories, list):
        additional_directories = permissions.get("additionalDirectories", [])
    if not isinstance(additional_directories, list):
        additional_directories = []
    profile = ClaudeProfile(
        name=name,
        auth_token_ref=token_ref,
        primary_api_key_ref=primary_ref if primary_key else None,
        base_url=env.get("ANTHROPIC_BASE_URL", ""),
        model=settings.get("model", ""),
        effort_level=settings.get("effortLevel", "high"),
        permissions_mode=permissions.get("defaultMode", "default"),
        skip_dangerous_prompt=settings.get("skipDangerousModePermissionPrompt", False),
        permissions_allow=permissions.get("allow", []),
        additional_directories=additional_directories,
        provider=provider,
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

    config = remote_config.read_remote_codex_config(client, ssh_profile)
    auth = remote_config.read_remote_codex_auth(client, ssh_profile)

    if not config and not auth:
        return "服务器上未找到 Codex 配置"

    name = f"Remote-{ssh_name}"
    provider_id = config.get("model_provider", "openai") if config else "openai"
    if provider_id == "openai":
        return "远程 Codex 配置是官方 OpenAI，已跳过；当前只导入第三方 API Profile"
    if not auth or auth.get("OPENAI_API_KEY") is None:
        return "远程 Codex 配置没有 API Key，已跳过；当前只导入第三方 API Profile"

    from models.profile import CodexProfile
    profile_kwargs = {
        "name": name,
        "model": config.get("model", "gpt-5.5") if config else "gpt-5.5",
        "model_provider": provider_id,
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
            profile_kwargs["custom_env_key"] = custom.get("env_key")
            profile_kwargs["custom_requires_openai_auth"] = custom.get("requires_openai_auth", False)

    ref = f"codex:{name}:api_key"
    security.set_secret(ref, auth["OPENAI_API_KEY"])
    profile_kwargs["api_key_ref"] = ref

    profile = CodexProfile(**profile_kwargs)
    profile_manager.save_codex_profile(profile)

    return f"已从 {ssh_profile.host} 拉取 Codex 配置，保存为 '{name}'"
