from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
import logging
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

        if "apply_to_subagents" in inspect.signature(self.register_hook).parameters:
            self.register_hook(apply_to_subagents=getattr(settings, "apply_to_subagents", False))
        else:
            self.register_hook()

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
        import tempfile
        import shutil

        # Validate settings before saving
        is_valid, error = settings.validate()
        if not is_valid:
            raise ValueError(f"Invalid settings: {error}")

        settings_path = self.get_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to temp file, then move
        temp_fd, temp_path = tempfile.mkstemp(
            dir=settings_path.parent,
            prefix='.tmp_',
            suffix='.json'
        )
        try:
            with open(temp_fd, 'w', encoding='utf-8') as f:
                json.dump(settings.to_dict(), f, indent=2, ensure_ascii=False)
            # Move temp file to target (atomic on most filesystems)
            shutil.move(temp_path, settings_path)
            self._settings = settings
        except Exception as e:
            # Clean up temp file on error
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
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
            self.register_hook()
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

            if settings and settings.git_auto_snapshot and settings.git_snapshot_on_start:
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

        # Remove state files
        try:
            config_dir = self.get_config_dir()
            state_path = config_dir / "auto_continue_stop_state.json"
            if state_path.exists():
                state_path.unlink()
            lock_path = Path(str(state_path) + ".lock")
            if lock_path.exists():
                lock_path.unlink()
        except Exception as e:
            errors.append(f"Failed to remove state files: {e}")

        if errors:
            logger.warning(f"Warnings during uninstall: {'; '.join(errors)}")

    def update_settings(self, settings: AutoContinueSettings) -> None:
        """Update settings without changing enabled state."""
        # Validate first
        is_valid, error = settings.validate()
        if not is_valid:
            raise ValueError(f"Invalid settings: {error}")

        current = self.load_settings()
        if current:
            settings.enabled = current.enabled
        self.save_settings(settings)

        # Re-install/register the stop hook for either auto-continue or standalone Git snapshots.
        if settings.enabled or (settings.git_auto_snapshot and settings.git_snapshot_on_start):
            try:
                self.install_hook_script()
                self.register_hook_for_settings(settings)
            except Exception as e:
                logger.warning(f"Failed to update hook script: {e}")
        else:
            try:
                self.unregister_hook()
            except Exception as e:
                logger.warning(f"Failed to unregister hook after disabling features: {e}")
