"""
Auto-continue manager - unified interface for both providers.
"""
from typing import Optional
from models.auto_continue import AutoContinueSettings, ProviderStatus
from core.auto_continue.codex_provider import CodexProvider
from core.auto_continue.claude_provider import ClaudeProvider


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

    def enable(self, provider_name: str, settings: Optional[AutoContinueSettings] = None,
               apply_to_subagents: bool = False) -> None:
        """Enable auto-continue for a provider."""
        provider = self.get_provider(provider_name)

        if settings is None:
            settings = provider.load_settings() or AutoContinueSettings()

        settings.enabled = True
        if provider_name.lower() == "claude":
            settings.apply_to_subagents = apply_to_subagents

        provider.enable(settings)
        self._sync_error_recovery(provider, settings)

        # Install guidance
        if hasattr(provider, 'install_guidance'):
            provider.install_guidance()

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
        provider.update_settings(settings)
        self._sync_error_recovery(provider, settings)

        # provider.update_settings re-installs/registers the hook when either
        # auto-continue or standalone Git snapshots need it. Error recovery is
        # synchronized above because it uses a separate Error/ResponseError hook.
        if settings.enabled and hasattr(provider, 'install_guidance'):
            provider.install_guidance()

    def repair(self, provider_name: str) -> None:
        """Repair installation (re-enable with current settings)."""
        provider = self.get_provider(provider_name)
        settings = provider.load_settings()
        if not settings:
            return

        if provider._settings_require_hook(settings):
            provider.install_hook_script()
            provider.register_hook_for_settings(settings)

        self._sync_error_recovery(provider, settings)

        if settings.enabled and hasattr(provider, 'install_guidance'):
            provider.install_guidance()

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
