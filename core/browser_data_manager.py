from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from pathlib import Path

from models.profile import BrowserProfile
from core.browser_profile_manager import MANAGED_BROWSER_PROFILES_DIR

logger = logging.getLogger(__name__)

TARGET_DOMAINS = ["chat.openai.com", "chatgpt.com", "claude.ai"]


class BrowserDataManager:
    """Clear site data for a browser profile with strict safety boundaries."""

    def is_browser_running(self, profile: BrowserProfile) -> bool:
        """Check if browser is running with multiple detection methods."""
        try:
            profile_dir = self._resolve_profile_dir(profile)
        except Exception as e:
            logger.error(f"Failed to resolve profile directory: {e}")
            return True  # Conservative: assume running if we can't check

        # 1) Prefer a file-lock heuristic on files typically held by Chromium.
        lock_candidates = [
            profile_dir / "SingletonLock",
            profile_dir / "SingletonCookie",
            profile_dir / "Default" / "Network" / "Cookies",
            profile_dir / "Default" / "Preferences",
        ]
        for candidate in lock_candidates:
            try:
                if candidate.exists() and self._is_file_locked(candidate):
                    logger.debug(f"File locked: {candidate}")
                    return True
            except Exception as e:
                logger.debug(f"Error checking lock on {candidate}: {e}")
                continue

        # 2) Fallback to process-name heuristic only for the selected browser family.
        process_names = ["chrome"] if profile.browser_type == "chrome" else ["msedge"]
        try:
            import subprocess
            command = (
                "$names=@('" + "','".join(process_names) + "'); "
                "Get-Process -ErrorAction SilentlyContinue | "
                "Where-Object { $names -contains $_.ProcessName } | "
                "Select-Object -ExpandProperty ProcessName"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = (result.stdout or "").lower()
            is_running = any(name in output for name in process_names)
            if is_running:
                logger.debug(f"Browser process detected: {process_names}")
            return is_running
        except subprocess.TimeoutExpired:
            logger.warning("Process check timed out")
            return True  # Conservative: assume running
        except Exception as e:
            logger.error(f"Error checking browser process: {e}")
            return True  # Conservative fallback

    def _is_file_locked(self, path: Path) -> bool:
        """Check if a file is locked by attempting a rename operation."""
        try:
            # Windows rename-to-self-parent tmp probe; if locked, rename often fails.
            probe = path.with_name(path.name + ".lockprobe")
            path.replace(probe)
            probe.replace(path)
            return False
        except (OSError, PermissionError):
            return True
        except Exception as e:
            logger.debug(f"Unexpected error in lock check: {e}")
            return True  # Conservative: assume locked

    def _resolve_profile_dir(self, profile: BrowserProfile) -> Path:
        """Resolve and validate profile directory."""
        try:
            path = Path(profile.user_data_dir).expanduser().resolve()
        except Exception as e:
            raise ValueError(f"无效的 Profile 路径: {profile.user_data_dir}") from e

        if not path.exists():
            raise FileNotFoundError(f"Profile 目录不存在: {path}")
        if not path.is_dir():
            raise ValueError(f"Profile 路径不是目录: {path}")
        return path

    def can_full_reset(self, profile: BrowserProfile) -> tuple[bool, str]:
        """Check if full reset is allowed with comprehensive validation."""
        if profile.profile_mode != "managed":
            return False, "external profile 不允许整目录清理"
        if not profile.created_by_app:
            return False, "仅应用创建的 profile 才允许整目录清理"

        try:
            profile_dir = self._resolve_profile_dir(profile)
        except Exception as e:
            return False, f"无法访问 Profile 目录: {e}"

        managed_root = MANAGED_BROWSER_PROFILES_DIR.resolve()
        try:
            # Ensure profile_dir is within managed_root
            if managed_root not in profile_dir.parents and profile_dir != managed_root:
                return False, "目标目录不在应用托管目录下"
        except Exception as e:
            return False, f"路径验证失败: {e}"

        return True, ""

    def clear_site_data(self, profile: BrowserProfile, scope: str) -> None:
        """Clear site data with comprehensive validation and error handling."""
        # Validate scope
        if scope not in {"chatgpt", "claude", "both"}:
            raise ValueError(f"无效的清理范围: {scope}")

        # Check if browser is running
        if self.is_browser_running(profile):
            raise RuntimeError(
                f"检测到 {profile.browser_type} 正在运行。\n"
                "请先关闭浏览器后再清理数据，以避免数据损坏。"
            )

        # Resolve profile directory
        try:
            profile_dir = self._resolve_profile_dir(profile)
        except Exception as e:
            raise RuntimeError(f"无法访问 Profile 目录: {e}") from e

        default_dir = profile_dir / "Default"
        if not default_dir.exists():
            default_dir = profile_dir

        # Determine target domains
        if scope == "chatgpt":
            domains = ["chat.openai.com", "chatgpt.com"]
        elif scope == "claude":
            domains = ["claude.ai"]
        else:  # both
            domains = TARGET_DOMAINS

        # Perform cleanup with error collection
        errors = []

        try:
            self._clear_cookies_db(default_dir, domains)
        except Exception as e:
            logger.error(f"Failed to clear cookies: {e}")
            errors.append(f"清理 Cookies 失败: {e}")

        try:
            self._clear_network_cache(default_dir)
        except Exception as e:
            logger.error(f"Failed to clear network cache: {e}")
            errors.append(f"清理网络缓存失败: {e}")

        try:
            self._clear_storage_for_domains(default_dir, domains)
        except Exception as e:
            logger.error(f"Failed to clear storage: {e}")
            errors.append(f"清理存储数据失败: {e}")

        if errors:
            raise RuntimeError(f"部分清理操作失败:\n" + "\n".join(errors))

    def _clear_cookies_db(self, default_dir: Path, domains: list[str]) -> None:
        """Clear cookies database with backup and validation."""
        cookies_path = default_dir / "Network" / "Cookies"
        if not cookies_path.exists():
            logger.info("Cookies database not found, skipping")
            return

        # Verify it's a file
        if not cookies_path.is_file():
            raise ValueError(f"Cookies path is not a file: {cookies_path}")

        temp_copy = cookies_path.with_suffix(".tmpcopy")
        backup_path = cookies_path.with_suffix(".backup")

        try:
            # Create backup
            shutil.copy2(cookies_path, backup_path)
            logger.debug(f"Created backup: {backup_path}")

            # Create working copy
            shutil.copy2(cookies_path, temp_copy)

            # Modify working copy
            with sqlite3.connect(temp_copy, timeout=10.0) as conn:
                cur = conn.cursor()
                deleted_count = 0
                for domain in domains:
                    cur.execute("DELETE FROM cookies WHERE host_key = ? OR host_key LIKE ?", (domain, f"%.{domain}"))
                    deleted_count += cur.rowcount
                conn.commit()
                logger.info(f"Deleted {deleted_count} cookies for domains: {domains}")

            # Replace original with modified copy
            shutil.copy2(temp_copy, cookies_path)

            # Remove backup on success
            backup_path.unlink(missing_ok=True)

        except sqlite3.Error as e:
            logger.error(f"SQLite error: {e}")
            # Restore from backup if available
            if backup_path.exists():
                try:
                    shutil.copy2(backup_path, cookies_path)
                    logger.info("Restored cookies from backup")
                except Exception as restore_error:
                    logger.error(f"Failed to restore backup: {restore_error}")
            raise RuntimeError(f"清理 Cookies 数据库失败: {e}") from e
        finally:
            # Clean up temporary files
            temp_copy.unlink(missing_ok=True)
            if backup_path.exists():
                backup_path.unlink(missing_ok=True)

    def _clear_network_cache(self, default_dir: Path) -> None:
        """Clear network cache directories with validation."""
        cache_dirs = [
            default_dir / "Cache",
            default_dir / "Code Cache",
            default_dir / "GPUCache",
        ]

        for cache_dir in cache_dirs:
            if not cache_dir.exists():
                continue

            if not cache_dir.is_dir():
                logger.warning(f"Cache path is not a directory: {cache_dir}")
                continue

            try:
                shutil.rmtree(cache_dir, ignore_errors=False)
                logger.info(f"Cleared cache: {cache_dir}")
            except Exception as e:
                logger.error(f"Failed to clear {cache_dir}: {e}")
                raise

    def _clear_storage_for_domains(self, default_dir: Path, domains: list[str]) -> None:
        """Clear site-specific storage directories with validation."""
        storage_paths = [
            Path("Local Storage/leveldb"),
            Path("Session Storage"),
            Path("IndexedDB"),
            Path("Service Worker/CacheStorage"),
            Path("Service Worker/Database"),
        ]

        for relative in storage_paths:
            target = default_dir / relative
            if not target.exists():
                continue

            if not target.is_dir():
                logger.warning(f"Storage path is not a directory: {target}")
                continue

            try:
                for child in list(target.iterdir()):
                    name = child.name.lower()
                    if any(domain in name for domain in domains):
                        try:
                            if child.is_dir():
                                shutil.rmtree(child, ignore_errors=False)
                            else:
                                child.unlink(missing_ok=False)
                            logger.debug(f"Cleared storage: {child}")
                        except Exception as e:
                            logger.error(f"Failed to clear {child}: {e}")
                            raise
            except Exception as e:
                logger.error(f"Error processing {target}: {e}")
                raise

    def full_reset(self, profile: BrowserProfile) -> None:
        """Completely reset profile directory with comprehensive safety checks."""
        # Validate permissions
        allowed, reason = self.can_full_reset(profile)
        if not allowed:
            raise RuntimeError(f"不允许整目录清理: {reason}")

        # Check if browser is running
        if self.is_browser_running(profile):
            raise RuntimeError(
                f"检测到 {profile.browser_type} 正在运行。\n"
                "请先关闭浏览器后再执行整目录清理，以避免数据损坏。"
            )

        # Resolve and validate directory
        try:
            profile_dir = self._resolve_profile_dir(profile)
        except Exception as e:
            raise RuntimeError(f"无法访问 Profile 目录: {e}") from e

        # Final safety check: ensure it's within managed directory
        managed_root = MANAGED_BROWSER_PROFILES_DIR.resolve()
        if managed_root not in profile_dir.parents and profile_dir != managed_root:
            raise RuntimeError(f"安全检查失败: 目标目录不在托管目录下")

        # Perform reset with backup
        backup_dir = profile_dir.with_name(profile_dir.name + ".backup")
        try:
            # Create backup
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            shutil.copytree(profile_dir, backup_dir)
            logger.info(f"Created backup: {backup_dir}")

            # Remove original
            shutil.rmtree(profile_dir)
            logger.info(f"Removed profile directory: {profile_dir}")

            # Recreate empty directory
            profile_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Recreated profile directory: {profile_dir}")

            # Remove backup on success
            shutil.rmtree(backup_dir)
            logger.info("Removed backup after successful reset")

        except Exception as e:
            logger.error(f"Full reset failed: {e}")
            # Attempt to restore from backup
            if backup_dir.exists() and not profile_dir.exists():
                try:
                    shutil.copytree(backup_dir, profile_dir)
                    logger.info("Restored profile from backup")
                except Exception as restore_error:
                    logger.error(f"Failed to restore backup: {restore_error}")
            raise RuntimeError(f"整目录清理失败: {e}") from e
        finally:
            # Clean up backup if it still exists
            if backup_dir.exists():
                try:
                    shutil.rmtree(backup_dir)
                except Exception:
                    pass


browser_data_manager = BrowserDataManager()
