from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
import logging
from core.atomic_io import atomic_write_text
from models.auto_continue import AutoContinueSettings, ProviderStatus

logger = logging.getLogger(__name__)


class AutoContinueProvider(ABC):
    """Base class for auto-continue providers."""

    def __init__(self, name: str):
        self.name = name
        self._settings: Optional[AutoContinueSettings] = None

    @abstractmethod
    def get_config_dir(self) -> Path:
        """Get the configuration directory for this provider."""
        pass

    @abstractmethod
    def get_hook_script_path(self) -> Path:
        """Get the path to the hook script."""
        pass

    @abstractmethod
    def get_settings_path(self) -> Path:
        """Get the path to the settings file."""
        pass

    @abstractmethod
    def is_hook_registered(self) -> bool:
        """Check if the hook is registered in the provider's config."""
        pass

    @abstractmethod
    def register_hook(self) -> None:
        """Register the hook in the provider's config."""
        pass

    def register_hook_for_settings(self, settings: AutoContinueSettings) -> None:
        """Register the hook, preserving provider-specific settings where needed."""
        import inspect

        params = inspect.signature(self.register_hook).parameters
        kwargs = {}
        if "apply_to_subagents" in params:
            kwargs["apply_to_subagents"] = getattr(settings, "apply_to_subagents", False)
        if "settings" in params:
            kwargs["settings"] = settings
        self.register_hook(**kwargs)

    def _settings_require_hook(self, settings: AutoContinueSettings | None) -> bool:
        if not settings:
            return False
        return (
            bool(settings.enabled)
            or bool(settings.training_auto_continue_enabled)
            or bool(settings.git_auto_snapshot and settings.git_snapshot_on_start)
            or bool(self.name == "claude" and settings.auto_approve_permission_requests)
        )

    @abstractmethod
    def unregister_hook(self) -> None:
        """Unregister the hook from the provider's config."""
        pass

    @abstractmethod
    def install_hook_script(self) -> None:
        """Install the hook script."""
        pass

    @abstractmethod
    def uninstall_hook_script(self) -> None:
        """Remove the hook script."""
        pass

    def get_status(self) -> ProviderStatus:
        """Get the current status of this provider."""
        try:
            settings = self.load_settings()
            return ProviderStatus(
                provider_name=self.name,
                enabled=settings.enabled if settings else False,
                hook_script_exists=self.get_hook_script_path().exists(),
                hook_registered=self.is_hook_registered(),
                guidance_installed=False,  # Override in subclass if applicable
            )
        except Exception as e:
            return ProviderStatus(
                provider_name=self.name,
                enabled=False,
                hook_script_exists=False,
                hook_registered=False,
                guidance_installed=False,
                last_error=str(e)
            )

    def load_settings(self) -> Optional[AutoContinueSettings]:
        """Load settings from disk with error handling."""
        import json
        settings_path = self.get_settings_path()
        if not settings_path.exists():
            return None
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            settings = AutoContinueSettings.from_dict(data)
            # Validate settings
            is_valid, error = settings.validate()
            if not is_valid:
                logger.warning(f"Invalid settings in {settings_path}: {error}")
                return None
            if (
                data.get("incomplete_patterns") != settings.incomplete_patterns
                or data.get("blocker_patterns") != settings.blocker_patterns
            ):
                try:
                    self.save_settings(settings)
                except Exception as e:
                    logger.warning(f"Failed to migrate settings in {settings_path}: {e}")
            return settings
        except Exception as e:
            logger.error(f"Error loading settings from {settings_path}: {e}")
            return None

    def save_settings(self, settings: AutoContinueSettings) -> None:
        """Save settings to disk with atomic write."""
        import json

        # Validate settings before saving
        is_valid, error = settings.validate()
        if not is_valid:
            raise ValueError(f"Invalid settings: {error}")

        settings_path = self.get_settings_path()
        try:
            atomic_write_text(
                settings_path,
                json.dumps(settings.to_dict(), indent=2, ensure_ascii=False),
            )
            self._settings = settings
        except Exception as e:
            raise RuntimeError(f"Failed to save settings: {e}") from e

    def enable(self, settings: Optional[AutoContinueSettings] = None) -> None:
        """Enable auto-continue for this provider with rollback on failure."""
        if settings is None:
            settings = self.load_settings() or AutoContinueSettings()
        settings.enabled = True

        # Validate settings
        is_valid, error = settings.validate()
        if not is_valid:
            raise ValueError(f"Invalid settings: {error}")

        # Track what we've done for rollback
        script_installed = False
        hook_registered = False

        try:
            # Install hook script
            self.install_hook_script()
            script_installed = True

            # Register hook
            self.register_hook_for_settings(settings)
            hook_registered = True

            # Save settings (atomic)
            self.save_settings(settings)

        except Exception as e:
            # Rollback on failure
            logger.error(f"Error enabling auto-continue, rolling back: {e}")
            if hook_registered:
                try:
                    self.unregister_hook()
                except Exception:
                    pass
            if script_installed:
                try:
                    self.uninstall_hook_script()
                except Exception:
                    pass
            raise RuntimeError(f"Failed to enable auto-continue: {e}") from e

    def pause(self) -> None:
        """Pause auto-continue (unregister hook but keep script and settings)."""
        try:
            settings = self.load_settings()
            if settings:
                settings.enabled = False
                self.save_settings(settings)

            if self._settings_require_hook(settings):
                self.install_hook_script()
                self.register_hook_for_settings(settings)
            else:
                self.unregister_hook()
        except Exception as e:
            raise RuntimeError(f"Failed to pause auto-continue: {e}") from e

    def uninstall(self) -> None:
        """Completely uninstall auto-continue with error handling."""
        errors = []

        # Unregister hook
        try:
            self.unregister_hook()
        except Exception as e:
            errors.append(f"Failed to unregister hook: {e}")

        uninstall_error_recovery = getattr(self, "uninstall_error_recovery", None)
        if callable(uninstall_error_recovery):
            try:
                uninstall_error_recovery()
            except Exception as e:
                errors.append(f"Failed to uninstall error recovery: {e}")

        # Remove script
        try:
            self.uninstall_hook_script()
        except Exception as e:
            errors.append(f"Failed to remove script: {e}")

        # Remove settings
        try:
            settings_path = self.get_settings_path()
            if settings_path.exists():
                settings_path.unlink()
        except Exception as e:
            errors.append(f"Failed to remove settings: {e}")

        # Remove state/log files from both legacy roots and the current tmp dir.
        try:
            config_dir = self.get_config_dir()
            cleanup_dirs = [config_dir, config_dir / "tmp"]
            cleanup_names = [
                "auto_continue_stop_state.json",
                "auto_continue_stop_state.json.lock",
                "auto_continue_stop_state.json.tmp",
                "auto_continue_permission_state.json",
                "auto_continue_permission_state.json.lock",
                "auto_continue_permission_state.json.tmp",
                "auto_continue_stop_log.jsonl",
                "error_recovery_state.json",
                "error_recovery_state.json.lock",
                "error_recovery_state.json.tmp",
                "error_recovery_log.jsonl",
            ]
            reset_marker_prefix = "auto_continue_stop_state.json.reset."
            hex_characters = frozenset("0123456789abcdefABCDEF")
            for cleanup_dir in cleanup_dirs:
                for name in cleanup_names:
                    path = cleanup_dir / name
                    if path.exists():
                        path.unlink()
                # Reset markers contain only a SHA-256 scope digest. Restrict
                # cleanup to an exact filename prefix plus 64 hexadecimal
                # characters so similarly named user files and directories are
                # never removed.
                for marker_path in cleanup_dir.glob(f"{reset_marker_prefix}*"):
                    suffix = marker_path.name[len(reset_marker_prefix):]
                    is_scope_marker = (
                        len(suffix) == 64
                        and all(character in hex_characters for character in suffix)
                    )
                    if is_scope_marker and (marker_path.is_file() or marker_path.is_symlink()):
                        marker_path.unlink()
        except Exception as e:
            errors.append(f"Failed to remove state files: {e}")

        if errors:
            logger.warning(f"Warnings during uninstall: {'; '.join(errors)}")

    def update_settings(self, settings: AutoContinueSettings) -> None:
        """Update settings and synchronize hook registration atomically."""
        # Validate first
        is_valid, error = settings.validate()
        if not is_valid:
            raise ValueError(f"Invalid settings: {error}")

        previous_settings = self.load_settings()

        try:
            # Re-install/register the stop hook for either auto-continue or standalone Git snapshots.
            self._apply_hook_state_for_settings(settings)
            self.save_settings(settings)
        except Exception as exc:
            rollback_error = self._rollback_settings_update(previous_settings)
            if rollback_error:
                raise RuntimeError(
                    f"Failed to update settings: {exc}; rollback failed: {rollback_error}"
                ) from exc
            raise RuntimeError(f"Failed to update settings: {exc}") from exc

    def _apply_hook_state_for_settings(self, settings: AutoContinueSettings | None) -> None:
        if self._settings_require_hook(settings):
            self.install_hook_script()
            self.register_hook_for_settings(settings)
        else:
            self.unregister_hook()

    def _rollback_settings_update(self, previous_settings: AutoContinueSettings | None) -> str:
        try:
            if previous_settings is None:
                try:
                    self.unregister_hook()
                finally:
                    try:
                        self.uninstall_hook_script()
                    finally:
                        settings_path = self.get_settings_path()
                        if settings_path.exists():
                            settings_path.unlink()
                return ""
            self._apply_hook_state_for_settings(previous_settings)
            self.save_settings(previous_settings)
            return ""
        except Exception as exc:
            logger.warning(f"Failed to rollback auto-continue settings update: {exc}")
            return str(exc)
