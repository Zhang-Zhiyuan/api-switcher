from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from models.profile import BrowserProfile

logger = logging.getLogger(__name__)

CHATGPT_URL = "https://chatgpt.com/"
CLAUDE_URL = "https://claude.ai/"
MIN_WINDOW_WIDTH = 640
MIN_WINDOW_HEIGHT = 480
MAX_WINDOW_WIDTH = 3840
MAX_WINDOW_HEIGHT = 2160


class BrowserLauncher:
    """Launch Chrome / Edge with a specific user-data-dir."""

    def find_browser_executable(self, browser_type: str, explicit_path: str | None = None) -> Path | None:
        """Find browser executable with validation."""
        if explicit_path:
            path = Path(explicit_path).expanduser().resolve()
            if path.exists() and path.is_file():
                # Verify it's actually executable
                if path.suffix.lower() == '.exe':
                    return path
                logger.warning(f"Explicit path is not an .exe file: {path}")
            else:
                logger.warning(f"Explicit path does not exist or is not a file: {explicit_path}")

        candidates: list[Path] = []
        try:
            local = Path.home() / "AppData" / "Local"
        except Exception as e:
            logger.error(f"Failed to resolve home directory: {e}")
            local = Path("C:/Users/Default/AppData/Local")

        program_files = Path("C:/Program Files")
        program_files_x86 = Path("C:/Program Files (x86)")

        if browser_type == "chrome":
            candidates = [
                local / "Google/Chrome/Application/chrome.exe",
                local / "Chrome/Application/chrome.exe",
                program_files / "Google/Chrome/Application/chrome.exe",
                program_files_x86 / "Google/Chrome/Application/chrome.exe",
            ]
        elif browser_type == "edge":
            candidates = [
                local / "Microsoft/Edge/Application/msedge.exe",
                program_files / "Microsoft/Edge/Application/msedge.exe",
                program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
            ]
        else:
            logger.error(f"Unsupported browser type: {browser_type}")
            return None

        for candidate in candidates:
            try:
                if candidate.exists() and candidate.is_file():
                    return candidate
            except Exception as e:
                logger.debug(f"Error checking candidate {candidate}: {e}")
                continue

        # Fallback to PATH search
        found_names = ["chrome.exe", "chrome", "google-chrome"] if browser_type == "chrome" else ["msedge.exe", "msedge", "edge"]
        for name in found_names:
            try:
                found = shutil.which(name)
                if found:
                    path = Path(found)
                    if path.exists() and path.is_file():
                        return path
            except Exception as e:
                logger.debug(f"Error searching for {name} in PATH: {e}")
                continue

        return None

    def resolve_target_url(self, profile: BrowserProfile, target: str | None = None) -> str:
        """Resolve target URL with validation."""
        use_target = target or profile.start_target
        if use_target == "chatgpt":
            return CHATGPT_URL
        if use_target == "claude":
            return CLAUDE_URL
        if use_target == "custom":
            custom_url = (profile.custom_url or "").strip()
            if not custom_url:
                raise ValueError("自定义目标 URL 为空")
            # Basic URL validation
            if not (custom_url.startswith("http://") or custom_url.startswith("https://")):
                raise ValueError(f"无效的 URL 格式: {custom_url}")
            return custom_url
        raise ValueError(f"未知目标站点: {use_target}")

    def _launch_window_size(self, profile: BrowserProfile) -> tuple[int, int]:
        def clamp(value: object, default: int, minimum: int, maximum: int) -> int:
            try:
                number = int(value)
            except (TypeError, ValueError):
                number = default
            return max(minimum, min(maximum, number))

        width = clamp(getattr(profile, "launch_width", 1280), 1280, MIN_WINDOW_WIDTH, MAX_WINDOW_WIDTH)
        height = clamp(getattr(profile, "launch_height", 900), 900, MIN_WINDOW_HEIGHT, MAX_WINDOW_HEIGHT)
        return width, height

    def _launch_language(self, profile: BrowserProfile) -> str | None:
        language = (getattr(profile, "launch_language", "") or "").strip()
        if not language:
            return None
        # Chrome accepts BCP-47-ish language tags here. Keep it conservative.
        if len(language) > 20 or any(ch for ch in language if not (ch.isalnum() or ch == "-")):
            logger.warning("Ignoring invalid browser launch language: %s", language)
            return None
        return language

    def launch(self, profile: BrowserProfile, target: str | None = None) -> subprocess.Popen:
        """Launch browser with comprehensive validation and error handling."""
        # Validate browser executable
        exe = self.find_browser_executable(profile.browser_type, profile.browser_executable)
        if not exe:
            raise FileNotFoundError(
                f"未找到 {profile.browser_type} 可执行文件。\n"
                f"请确保浏览器已安装，或在 Profile 中指定 browser_executable 路径。"
            )

        # Verify executable is still valid
        if not exe.exists():
            raise FileNotFoundError(f"浏览器可执行文件不存在: {exe}")
        if not exe.is_file():
            raise ValueError(f"浏览器可执行文件路径无效: {exe}")

        # Validate and prepare user data directory
        try:
            user_data_dir = Path(profile.user_data_dir).expanduser().resolve()
        except Exception as e:
            raise ValueError(f"无效的 Profile 路径: {profile.user_data_dir}") from e

        if not user_data_dir.exists():
            # Try to create it for managed profiles
            if profile.profile_mode == "managed":
                try:
                    user_data_dir.mkdir(parents=True, exist_ok=True)
                    logger.info(f"Created managed profile directory: {user_data_dir}")
                except Exception as e:
                    raise RuntimeError(f"无法创建 Profile 目录: {user_data_dir}") from e
            else:
                raise FileNotFoundError(f"Profile 目录不存在: {user_data_dir}")

        if not user_data_dir.is_dir():
            raise ValueError(f"Profile 路径不是目录: {user_data_dir}")

        # Resolve target URL
        try:
            url = self.resolve_target_url(profile, target)
        except Exception as e:
            raise ValueError(f"无法解析目标 URL: {e}") from e

        # Build command
        width, height = self._launch_window_size(profile)
        language = self._launch_language(profile)
        cmd = [
            str(exe),
            f"--user-data-dir={str(user_data_dir)}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--new-window",
            f"--window-size={width},{height}",
        ]
        if language:
            cmd.append(f"--lang={language}")
        cmd.append(
            url,
        )

        # Launch process with error handling
        try:
            logger.info(f"Launching {profile.browser_type} with profile: {profile.name}")
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )

            # Quick check if process started successfully
            returncode = process.poll()
            if returncode is not None:
                raise RuntimeError(f"浏览器进程立即退出，返回码: {returncode}")

            return process
        except FileNotFoundError as e:
            raise FileNotFoundError(f"无法启动浏览器: {exe} 不存在") from e
        except PermissionError as e:
            raise PermissionError(f"无权限执行浏览器: {exe}") from e
        except Exception as e:
            raise RuntimeError(f"启动浏览器失败: {e}") from e


browser_launcher = BrowserLauncher()
