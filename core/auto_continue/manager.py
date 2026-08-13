"""
Auto-continue manager - unified interface for both providers.
"""
import logging
from typing import Optional
from models.auto_continue import AutoContinueSettings, ProviderStatus
from core.auto_continue.base import SettingsLoadStatus
from core.auto_continue.codex_provider import CodexProvider
from core.auto_continue.claude_provider import ClaudeProvider

logger = logging.getLogger(__name__)


class AutoContinueManager:
    """Manages auto-continue for both Codex and Claude Code."""

    def __init__(self):
        self.codex = CodexProvider()
        self.claude = ClaudeProvider()

    def get_provider(self, provider_name: str):
        """Get provider by name."""
        if provider_name.lower() == "codex":
            return self.codex
        elif provider_name.lower() == "claude":
            return self.claude
        else:
            raise ValueError(f"Unknown provider: {provider_name}")

    def get_status(self, provider_name: str) -> ProviderStatus:
        """Get status for a provider."""
        provider = self.get_provider(provider_name)
        return provider.get_status()

    def get_settings(self, provider_name: str) -> Optional[AutoContinueSettings]:
        """Get settings for a provider."""
        provider = self.get_provider(provider_name)
        return provider.load_settings()

    def _sync_error_recovery(self, provider, settings: AutoContinueSettings) -> None:
        if not (
            hasattr(provider, "install_error_recovery")
            and hasattr(provider, "uninstall_error_recovery")
        ):
            return

        if settings.error_recovery_enabled:
            provider.install_error_recovery()
        else:
            provider.uninstall_error_recovery()

    def _rollback_full_update(
        self,
        provider,
        previous_settings: Optional[AutoContinueSettings],
    ) -> str:
        """Restore settings, stop hooks, recovery hooks, and guidance together."""
        errors = []
        rollback_settings = getattr(provider, "_rollback_settings_update", None)
        if callable(rollback_settings):
            rollback_error = rollback_settings(previous_settings)
            if rollback_error:
                errors.append(f"settings/hook rollback failed: {rollback_error}")

        recovery_settings = previous_settings or AutoContinueSettings()
        try:
            self._sync_error_recovery(provider, recovery_settings)
        except Exception as exc:
            errors.append(f"error recovery rollback failed: {exc}")

        try:
            if previous_settings and previous_settings.enabled:
                install_guidance = getattr(provider, "install_guidance", None)
                if callable(install_guidance):
                    install_guidance()
            else:
                uninstall_guidance = getattr(provider, "uninstall_guidance", None)
                if callable(uninstall_guidance):
                    uninstall_guidance()
        except Exception as exc:
            errors.append(f"guidance rollback failed: {exc}")

        return "; ".join(errors)

    def enable(self, provider_name: str, settings: Optional[AutoContinueSettings] = None,
               apply_to_subagents: bool = False) -> None:
        """Enable auto-continue for a provider."""
        provider = self.get_provider(provider_name)
        load_settings = getattr(provider, "load_settings", None)
        previous_settings = load_settings() if callable(load_settings) else None

        if settings is None:
            settings = (load_settings() if callable(load_settings) else None) or AutoContinueSettings()

        settings.enabled = True
        if provider_name.lower() == "claude":
            settings.apply_to_subagents = apply_to_subagents

        try:
            provider.enable(settings)
            self._sync_error_recovery(provider, settings)

            # Install guidance
            if hasattr(provider, 'install_guidance'):
                provider.install_guidance()
        except Exception as exc:
            rollback_error = self._rollback_full_update(provider, previous_settings)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(f"Failed to enable auto-continue: {exc}{detail}") from exc

    def pause(self, provider_name: str) -> None:
        """Pause auto-continue for a provider."""
        provider = self.get_provider(provider_name)
        provider.pause()

    def uninstall(self, provider_name: str) -> None:
        """Uninstall auto-continue for a provider."""
        provider = self.get_provider(provider_name)
        provider.uninstall()

        # Remove guidance
        if hasattr(provider, 'uninstall_guidance'):
            provider.uninstall_guidance()

    def update_settings(self, provider_name: str, settings: AutoContinueSettings) -> None:
        """Update settings for a provider."""
        provider = self.get_provider(provider_name)
        load_settings = getattr(provider, "load_settings", None)
        previous_settings = load_settings() if callable(load_settings) else None
        try:
            provider.update_settings(settings)
            self._sync_error_recovery(provider, settings)

            # provider.update_settings re-installs/registers the hook when either
            # auto-continue or standalone Git snapshots need it. Error recovery is
            # synchronized above because it uses a separate Error/ResponseError hook.
            if settings.enabled and hasattr(provider, 'install_guidance'):
                provider.install_guidance()
            elif hasattr(provider, 'uninstall_guidance'):
                provider.uninstall_guidance()
        except Exception as exc:
            rollback_error = self._rollback_full_update(provider, previous_settings)
            detail = f"; rollback failed: {rollback_error}" if rollback_error else ""
            raise RuntimeError(f"Failed to update auto-continue settings: {exc}{detail}") from exc

    def repair(self, provider_name: str) -> None:
        """Repair installation (re-enable with current settings)."""
        provider = self.get_provider(provider_name)
        settings = provider.load_settings()
        if not settings:
            return

        ensure_current = getattr(provider, "ensure_current_installation", None)
        if callable(ensure_current):
            ensure_current(settings)
        else:
            # Applying the complete state is important during repair: when no
            # feature needs the stop hook, stale registrations must be removed.
            apply_hook_state = getattr(provider, "_apply_hook_state_for_settings", None)
            if callable(apply_hook_state):
                apply_hook_state(settings)
            elif provider._settings_require_hook(settings):
                provider.install_hook_script()
                provider.register_hook_for_settings(settings)
            else:
                provider.unregister_hook()
            self._sync_error_recovery(provider, settings)

        if settings.enabled and hasattr(provider, 'install_guidance'):
            provider.install_guidance()
        elif hasattr(provider, 'uninstall_guidance'):
            provider.uninstall_guidance()

    def reconcile_installation(self, provider_name: str) -> bool:
        """Best-effort startup reconciliation for an existing installation.

        Startup must remain usable even when a user-owned config is malformed or
        temporarily locked, so failures are logged rather than propagated. A
        provider without an explicit transactional reconciler is left untouched.
        """
        try:
            provider = self.get_provider(provider_name)
            ensure_current = getattr(provider, "ensure_current_installation", None)
            if not callable(ensure_current):
                return True

            load_result_method = getattr(provider, "load_settings_result", None)
            if callable(load_result_method):
                load_result = load_result_method()
                if load_result.status is SettingsLoadStatus.MISSING:
                    return True
                if load_result.status in {
                    SettingsLoadStatus.IO_ERROR,
                    SettingsLoadStatus.ERROR,
                }:
                    logger.warning(
                        "Could not read %s auto-continue settings at startup; "
                        "preserving existing hooks: %s",
                        provider_name,
                        load_result.error or load_result.status.value,
                    )
                    return False
                if load_result.status is SettingsLoadStatus.INVALID:
                    disable_invalid = getattr(
                        provider,
                        "disable_managed_hooks_for_invalid_settings",
                        None,
                    )
                    if not callable(disable_invalid):
                        raise RuntimeError(
                            "settings content is invalid and the provider cannot "
                            "safely disable its managed hooks"
                        )
                    disable_invalid()
                    logger.warning(
                        "Disabled %s managed hooks because settings content is invalid: %s",
                        provider_name,
                        provider.get_settings_path(),
                    )
                    return True
                if load_result.status is not SettingsLoadStatus.LOADED:
                    raise RuntimeError(
                        f"unsupported settings load status: {load_result.status!r}"
                    )
                settings = load_result.settings
                if settings is None:
                    raise RuntimeError("loaded settings result did not contain settings")
            else:
                # Compatibility path for external/custom providers that implement
                # only the historical Optional-returning API.
                settings = provider.load_settings()
            if settings is None:
                settings_path = provider.get_settings_path()
                if not settings_path.exists():
                    return True
                disable_invalid = getattr(
                    provider,
                    "disable_managed_hooks_for_invalid_settings",
                    None,
                )
                if not callable(disable_invalid):
                    raise RuntimeError(
                        f"settings file exists but could not be loaded: {settings_path}"
                    )
                disable_invalid()
                logger.warning(
                    "Disabled %s managed hooks because legacy settings could not be loaded: %s",
                    provider_name,
                    settings_path,
                )
                return True
            ensure_current(settings)
            return True
        except Exception as exc:
            logger.warning(
                "Failed to reconcile %s auto-continue installation at startup: %s",
                provider_name,
                exc,
            )
            return False

    def reconcile_all_installations(self) -> dict[str, bool]:
        """Reconcile every provider that offers a safe startup reconciler."""
        return {
            provider_name: self.reconcile_installation(provider_name)
            for provider_name in ("codex", "claude")
        }

    def enable_error_recovery(self, provider_name: str) -> None:
        """启用错误自动恢复功能"""
        provider = self.get_provider(provider_name)

        # 加载或创建设置
        settings = provider.load_settings() or AutoContinueSettings()
        original_state = settings.error_recovery_enabled
        settings.error_recovery_enabled = True

        try:
            # 安装错误恢复脚本
            provider.install_error_recovery()

            # 保存设置
            provider.save_settings(settings)
        except Exception as e:
            # 回滚设置
            settings.error_recovery_enabled = original_state
            # 尝试卸载（如果安装了一半）
            try:
                provider.uninstall_error_recovery()
            except Exception:
                pass
            raise RuntimeError(f"Failed to enable error recovery: {e}") from e

    def disable_error_recovery(self, provider_name: str) -> None:
        """禁用错误自动恢复功能"""
        provider = self.get_provider(provider_name)

        # 更新设置
        settings = provider.load_settings()
        if settings:
            settings.error_recovery_enabled = False
            provider.save_settings(settings)

        # 卸载错误恢复脚本
        provider.uninstall_error_recovery()

    def is_error_recovery_enabled(self, provider_name: str) -> bool:
        """检查错误恢复是否启用"""
        provider = self.get_provider(provider_name)
        settings = provider.load_settings()
        return settings and settings.error_recovery_enabled if settings else False


# Global instance
auto_continue_manager = AutoContinueManager()
