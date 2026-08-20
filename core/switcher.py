import logging
import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from core import backup_manager, codex_env, parser, toml_parser, auth_parser, vscode_parser, profile_manager, security
from core.atomic_io import atomic_write_bytes
from core.providers import ProviderRegistry
from core.usage_recorder import usage_recorder

logger = logging.getLogger(__name__)
_SWITCH_LOCK = threading.RLock()


def _is_windows() -> bool:
    return os.name == "nt"


def _serialized_switch(func):
    """Serialize local switches so two UI/tray actions cannot interleave writes."""

    @wraps(func)
    def wrapped(*args, **kwargs):
        with _SWITCH_LOCK:
            return func(*args, **kwargs)

    return wrapped


def _unique_paths(paths) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        path = Path(value)
        key = str(path.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _profile_store_transaction_paths() -> tuple[Path, Path]:
    """Return the profile store and the backup path used by ``_save_store``."""
    profile_path = Path(profile_manager.PROFILES_FILE)
    return profile_path, profile_path.with_suffix(".backup")


def _restore_switch_caches() -> None:
    parser.clear_claude_file_cache()
    toml_parser.clear_codex_config_cache()
    auth_parser.clear_codex_auth_cache()
    vscode_parser.clear_vscode_settings_cache()
    profile_manager.clear_profile_store_cache()


@contextmanager
def _local_switch_transaction(paths, env_names=()):
    """Restore exact local state if any configuration mutation fails.

    Bytes (rather than parsed objects) are retained so comments, formatting,
    BOMs and even temporarily malformed user files survive a failed switch.
    """
    snapshot_paths = _unique_paths(paths)
    file_state = {
        path: (path.exists(), path.read_bytes() if path.exists() else b"")
        for path in snapshot_paths
    }
    names = list(dict.fromkeys(str(name).strip() for name in env_names if str(name).strip()))
    process_state = {name: os.environ.get(name) for name in names}
    persistent_state = None
    if _is_windows() and names:
        from core import persistent_env

        persistent_state = {
            name: persistent_env._local_user_env_value_strict(name)
            for name in names
        }

    try:
        yield
    except Exception as original_error:
        rollback_errors: list[str] = []
        for path, (existed, content) in file_state.items():
            try:
                if existed:
                    atomic_write_bytes(path, content)
                else:
                    path.unlink(missing_ok=True)
            except Exception as error:
                rollback_errors.append(f"{path}: {error}")

        if persistent_state is not None:
            try:
                from core import persistent_env

                deletes = [name for name, value in persistent_state.items() if value is None]
                updates = {name: value for name, value in persistent_state.items() if value is not None}
                if deletes:
                    persistent_env.delete_local_user_env(deletes)
                if updates:
                    persistent_env.set_local_user_env(updates)
            except Exception as error:
                rollback_errors.append(f"Windows 持久环境变量: {error}")

        # Persistent-env helpers also mutate this process; restore it last.
        for name, value in process_state.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        try:
            _restore_switch_caches()
        except Exception as error:
            rollback_errors.append(f"内存缓存: {error}")

        if rollback_errors:
            details = "；".join(rollback_errors)
            raise RuntimeError(f"切换失败，且自动回滚不完整: {details}") from original_error
        raise


def _start_usage_session_best_effort(name: str, kind: str) -> None:
    try:
        usage_recorder.start_session(name, kind)
    except Exception:
        # Statistics must never turn an otherwise committed configuration into
        # an apparent failure that callers may retry.
        logger.exception("Failed to record usage session after switching %s", name)


def _ensure_switch_target_healthy(kind: str, name: str) -> None:
    """Revalidate a target at the mutation boundary.

    Most UI paths show a preview first, but tray/statistics shortcuts can call
    the switcher directly.  Keeping the blocking checks here prevents those
    alternate entry points from writing a known-invalid configuration.
    """
    from core.switch_preview import build_switch_preview

    preview = build_switch_preview(kind, name)
    errors = [check for check in preview.checks if check.status == "error"]
    if not errors:
        return
    details = "；".join(f"{check.item}: {check.message}" for check in errors[:3])
    if len(errors) > 3:
        details += f"；另有 {len(errors) - 3} 项"
    raise ValueError(f"配置健康检查未通过：{details}")


def _codex_api_env_names_to_clear(config: dict | None = None) -> list[str]:
    names = ["OPENAI_API_KEY"]
    for provider in ProviderRegistry.get_codex_providers():
        names.append(provider.codex_env_key)
    for profile in profile_manager.list_switchable_codex_profiles():
        try:
            names.extend(ProviderRegistry.get_codex_runtime_env_keys_for_profile(profile))
        except ValueError as exc:
            logger.warning("Skipping invalid Codex profile env_key for %r: %s", profile.name, exc)

    model_providers = (config or {}).get("model_providers")
    if isinstance(model_providers, dict):
        for table in model_providers.values():
            if isinstance(table, dict):
                names.append(str(table.get("env_key") or ""))

    normalized = []
    for name in names:
        name = str(name or "").strip()
        if not name:
            continue
        try:
            name = ProviderRegistry.validate_codex_env_key(name)
        except ValueError as exc:
            logger.warning("Skipping unsafe Codex env cleanup target %r: %s", name, exc)
            continue
        if name not in normalized:
            normalized.append(name)
    return normalized


def _codex_active_api_env_names_to_clear(config: dict | None = None) -> list[str]:
    names = ["OPENAI_API_KEY"]
    provider_id = str((config or {}).get("model_provider") or "").strip()
    model_providers = (config or {}).get("model_providers")
    custom = {}
    if provider_id and isinstance(model_providers, dict):
        maybe_custom = model_providers.get(provider_id)
        if isinstance(maybe_custom, dict):
            custom = maybe_custom
            names.append(str(custom.get("env_key") or ""))
    if provider_id and provider_id != "openai":
        names.append(ProviderRegistry.get_codex_env_key(provider_id, custom_name=custom.get("name")))

    active_name = profile_manager.get_active_codex_name()
    if active_name:
        for profile in profile_manager.list_switchable_codex_profiles():
            if profile.name == active_name:
                try:
                    names.extend(ProviderRegistry.get_codex_runtime_env_keys_for_profile(profile))
                except ValueError as exc:
                    logger.warning("Skipping invalid active Codex env_key for %r: %s", profile.name, exc)
                break

    normalized = []
    for name in names:
        name = str(name or "").strip()
        if not name:
            continue
        try:
            name = ProviderRegistry.validate_codex_env_key(name)
        except ValueError as exc:
            logger.warning("Skipping unsafe active Codex env cleanup target %r: %s", name, exc)
            continue
        if name not in normalized:
            normalized.append(name)
    return normalized


def _clear_local_codex_api_env(config: dict | None = None) -> None:
    env_names = _codex_api_env_names_to_clear(config)
    for name in env_names:
        os.environ.pop(name, None)

    try:
        codex_env.delete_codex_env(env_names)
    except Exception as e:
        raise RuntimeError(f"清理 Codex .env 环境变量失败: {e}") from e

    if not _is_windows():
        logger.warning("Local persistent Codex API env cleanup skipped on non-Windows platform")
        return

    from core import persistent_env

    try:
        persistent_env.delete_local_user_env(env_names)
    except Exception as e:
        raise RuntimeError(f"清理 Codex API 环境变量失败: {e}") from e


def _claude_api_env_names_to_clear() -> list[str]:
    """Return only the Claude overrides managed by this application."""
    return list(
        dict.fromkeys(
            (
                *parser.CLAUDE_AUTH_ENV_KEYS,
                "ANTHROPIC_BASE_URL",
                *parser.CLAUDE_MODEL_OVERRIDE_ENV_KEYS,
            )
        )
    )


def _clear_local_claude_api_env(env_names) -> None:
    """Clear process and Windows-user Claude API overrides."""
    names = list(dict.fromkeys(str(name).strip() for name in env_names if str(name).strip()))
    for name in names:
        os.environ.pop(name, None)

    if not _is_windows():
        logger.warning("Local persistent Claude API env cleanup skipped on non-Windows platform")
        return

    from core import persistent_env

    try:
        persistent_env.delete_local_user_env(names)
    except Exception as e:
        raise RuntimeError(f"清理 Claude API 环境变量失败: {e}") from e


@_serialized_switch
def switch_claude_profile(name: str) -> None:
    """Switch to a named Claude API configuration. Auto-backup before switching."""
    profiles = profile_manager.list_switchable_claude_profiles()
    target = next((p for p in profiles if p.name == name), None)
    if not target:
        raise ValueError(f"未找到 Claude API 配置: {name}")

    if not profile_manager.is_third_party_claude_profile(target):
        raise ValueError("只能切换第三方 Claude API 配置")
    if not (security.get_secret(target.auth_token_ref) or security.get_secret(getattr(target, "primary_api_key_ref", None))):
        raise ValueError("Claude API 配置需要 API Key 或 Auth Token")
    _ensure_switch_target_healthy("claude_api", name)

    # OAuth credentials can rotate while Claude Code is running. Snapshot the
    # live official login before API mode clears the account marker, so a later
    # switch back restores the newest refresh token rather than an old import.
    profile_manager.preserve_current_claude_account_snapshot()
    backup_manager.create_backup(f"切换 Claude 到: {name}")

    with _local_switch_transaction(
        [
            parser.CLAUDE_SETTINGS,
            parser.CLAUDE_CONFIG,
            vscode_parser.VSCODE_SETTINGS,
            *_profile_store_transaction_paths(),
        ]
    ):
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
    _start_usage_session_best_effort(name, "claude")

    logger.info(f"Switched Claude profile to: {name}")


@_serialized_switch
def switch_codex_profile(name: str) -> None:
    """Switch to a named Codex API configuration. Auto-backup before switching."""
    profiles = profile_manager.list_switchable_codex_profiles()
    target = next((p for p in profiles if p.name == name), None)
    if not target:
        raise ValueError(f"未找到 Codex API 配置: {name}")

    if not profile_manager.is_third_party_codex_profile(target):
        raise ValueError("只能切换第三方 Codex API 配置")
    uses_openai_auth = bool(getattr(target, "custom_requires_openai_auth", False))
    target_env_key = target.validated_env_key()
    api_key = security.get_secret(target.api_key_ref) if not uses_openai_auth else ""
    if not uses_openai_auth and not api_key:
        raise ValueError("Codex API 配置需要 API Key")
    _ensure_switch_target_healthy("codex_api", name)

    # Codex may rotate the file-backed ChatGPT refresh token between switches.
    # Preserve it before replacing the auth/config state with API mode.
    profile_manager.preserve_current_codex_account_snapshot()
    backup_manager.create_backup(f"切换 Codex 到: {name}")

    current_config = toml_parser.read_codex_config()
    env_keys = [] if target_env_key is None else [target_env_key]
    env_updates = {key: api_key for key in env_keys if api_key}
    stale_env_names = [key for key in _codex_active_api_env_names_to_clear(current_config) if key not in env_updates]
    if uses_openai_auth:
        stale_env_names = [key for key in stale_env_names if key != "OPENAI_API_KEY"]

    affected_env_names = [*stale_env_names, *env_updates]
    with _local_switch_transaction(
        [
            toml_parser.CODEX_CONFIG,
            auth_parser.CODEX_AUTH,
            codex_env.paths.CODEX_ENV,
            *_profile_store_transaction_paths(),
        ],
        affected_env_names,
    ):
        for key in stale_env_names:
            os.environ.pop(key, None)
        for key, value in env_updates.items():
            os.environ[key] = value
        if stale_env_names:
            codex_env.delete_codex_env(stale_env_names)
        if env_updates:
            codex_env.set_codex_env(env_updates)

        if _is_windows():
            from core import persistent_env

            try:
                if stale_env_names:
                    persistent_env.delete_local_user_env(stale_env_names)
                if env_updates:
                    persistent_env.set_local_user_env(env_updates)
            except Exception as e:
                raise RuntimeError(f"写入 Codex API 环境变量 {', '.join(env_keys)} 失败: {e}") from e
        else:
            logger.warning("Local persistent env write skipped on non-Windows platform for %s", ", ".join(env_keys))

        # Update config.toml
        config = toml_parser.apply_codex_profile(current_config, target)
        toml_parser.write_codex_config(config)

        # Update auth.json
        auth = auth_parser.read_codex_auth()
        auth = auth_parser.apply_codex_apikey(auth, target)
        auth_parser.write_codex_auth(auth)

        profile_manager.set_active_codex(name)
        profile_manager.set_active_codex_account(None)

    # Record usage statistics
    _start_usage_session_best_effort(name, "codex")

    logger.info(f"Switched Codex profile to: {name}")


@_serialized_switch
def switch_claude_account(name: str) -> None:
    """Switch Claude Code back to a saved official login snapshot."""
    profiles = profile_manager.list_claude_account_profiles()
    target = next((p for p in profiles if p.name == name), None)
    if not target:
        raise ValueError(f"Claude account '{name}' not found")

    # OAuth access/refresh tokens may have rotated since the account was first
    # imported.  Preserve the live credentials (and an unimported current
    # login) before this switch replaces the shared credentials file.
    profile_manager.preserve_current_claude_account_snapshot()
    profiles = profile_manager.list_claude_account_profiles()
    target = next((p for p in profiles if p.name == name), target)
    credentials = profile_manager.load_claude_account_credentials(target)

    backup_manager.create_backup(f"切换 Claude 官方账号到 {name}")

    env_names = _claude_api_env_names_to_clear()
    with _local_switch_transaction(
        [
            parser.CLAUDE_CREDENTIALS,
            parser.CLAUDE_SETTINGS,
            parser.CLAUDE_CONFIG,
            vscode_parser.VSCODE_SETTINGS,
            *_profile_store_transaction_paths(),
        ],
        env_names,
    ):
        parser.write_claude_credentials(credentials)

        _clear_local_claude_api_env(env_names)

        settings = parser.read_claude_settings()
        settings = parser.clear_claude_api_overrides(settings)
        parser.write_claude_settings(settings)

        current_config = parser.read_claude_config()
        config = parser.clear_claude_config_auth(current_config)
        if config or current_config:
            parser.write_claude_config(config)

        vscode = vscode_parser.read_vscode_settings()
        vscode = vscode_parser.clear_claude_profile_overrides(
            vscode,
            str(settings.get("model") or ""),
        )
        vscode_parser.write_vscode_settings(vscode)

        profile_manager.set_active_claude_account(name)
        profile_manager.set_active_claude(None)

    logger.info(f"Switched Claude official account to: {name}")


@_serialized_switch
def switch_codex_account(name: str) -> None:
    """Switch Codex CLI back to a saved ChatGPT login snapshot."""
    profiles = profile_manager.list_codex_account_profiles()
    target = next((p for p in profiles if p.name == name), None)
    if not target:
        raise ValueError(f"Codex account '{name}' not found")

    # Codex rotates ChatGPT tokens in auth.json.  Save the latest file-backed
    # state before restoring another account, otherwise switching back later
    # can resurrect an expired refresh token and appear to log the user out.
    profile_manager.preserve_current_codex_account_snapshot()
    profiles = profile_manager.list_codex_account_profiles()
    target = next((p for p in profiles if p.name == name), target)
    auth = profile_manager.load_codex_account_auth(target)

    backup_manager.create_backup(f"切换 Codex 官方账号到 {name}")

    config = toml_parser.read_codex_config()
    env_names = _codex_api_env_names_to_clear(config)
    with _local_switch_transaction(
        [
            auth_parser.CODEX_AUTH,
            toml_parser.CODEX_CONFIG,
            codex_env.paths.CODEX_ENV,
            *_profile_store_transaction_paths(),
        ],
        env_names,
    ):
        auth_parser.write_codex_auth(auth)

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
