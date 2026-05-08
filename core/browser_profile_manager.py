from __future__ import annotations

from pathlib import Path

from config.paths import STORAGE_DIR
from models.profile import BrowserProfile
from core import profile_manager


MANAGED_BROWSER_PROFILES_DIR = STORAGE_DIR / "browser_profiles"
SUPPORTED_BROWSERS = {"chrome", "edge"}
SUPPORTED_START_TARGETS = {"chatgpt", "claude", "custom"}


class BrowserProfileManager:
    """Business layer for browser profile management."""

    def __init__(self):
        MANAGED_BROWSER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    def validate_profile(self, profile: BrowserProfile) -> tuple[bool, str]:
        if not profile.name.strip():
            return False, "Profile 名称不能为空"
        if profile.browser_type not in SUPPORTED_BROWSERS:
            return False, "仅支持 Chrome 和 Edge"
        if profile.profile_mode not in {"managed", "external"}:
            return False, "profile_mode 必须是 managed 或 external"
        if profile.start_target not in SUPPORTED_START_TARGETS:
            return False, "start_target 无效"
        if profile.start_target == "custom" and not (profile.custom_url or "").strip():
            return False, "自定义目标需要填写 URL"
        if not profile.user_data_dir.strip():
            return False, "user_data_dir 不能为空"
        if profile.browser_executable:
            exe = Path(profile.browser_executable).expanduser()
            if not exe.exists():
                return False, "指定的浏览器可执行文件不存在"
            if not exe.is_file():
                return False, "指定的浏览器可执行文件无效"

        path = Path(profile.user_data_dir).expanduser()
        if profile.profile_mode == "external":
            if not path.exists():
                return False, "外部 Profile 目录不存在"
            if not path.is_dir():
                return False, "外部 Profile 路径必须是目录"
        else:
            managed_root = MANAGED_BROWSER_PROFILES_DIR.resolve()
            try:
                resolved = path.resolve()
            except FileNotFoundError:
                resolved = path
            if managed_root not in resolved.parents and resolved != managed_root:
                return False, "托管 Profile 必须位于应用的 browser_profiles 目录下"
        return True, ""

    def build_managed_profile_path(self, name: str, browser_type: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or "profile"
        return MANAGED_BROWSER_PROFILES_DIR / f"{browser_type}_{safe}"

    def ensure_managed_profile_dir(self, profile: BrowserProfile) -> Path:
        path = Path(profile.user_data_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_profile(self, profile: BrowserProfile) -> None:
        valid, error = self.validate_profile(profile)
        if not valid:
            raise ValueError(error)
        if profile.profile_mode == "managed":
            self.ensure_managed_profile_dir(profile)
        profile_manager.save_browser_profile(profile)

    def delete_profile(self, name: str) -> None:
        profile_manager.delete_browser_profile(name)

    def diagnose_profile(self, profile: BrowserProfile) -> dict[str, bool | str | None]:
        from core.browser_launcher import browser_launcher
        from core.browser_data_manager import browser_data_manager

        valid, error = self.validate_profile(profile)
        exe = browser_launcher.find_browser_executable(profile.browser_type, profile.browser_executable)
        path = Path(profile.user_data_dir).expanduser()
        path_exists = path.exists() and path.is_dir()
        browser_running = False
        if path_exists:
            try:
                browser_running = browser_data_manager.is_browser_running(profile)
            except Exception:
                browser_running = True
        can_reset, reset_reason = browser_data_manager.can_full_reset(profile) if path_exists else (False, "Profile 路径不存在")

        return {
            "valid": valid,
            "validation_error": error,
            "executable_found": exe is not None,
            "resolved_executable": str(exe) if exe else None,
            "profile_path_exists": path_exists,
            "browser_running": browser_running,
            "can_full_reset": can_reset,
            "full_reset_reason": reset_reason,
        }

    def build_template_profile(self, browser_type: str, target: str) -> BrowserProfile:
        if browser_type not in SUPPORTED_BROWSERS:
            raise ValueError("仅支持 Chrome 和 Edge")
        if target not in {"chatgpt", "claude"}:
            raise ValueError("模板目标仅支持 chatgpt 或 claude")

        display_target = "ChatGPT" if target == "chatgpt" else "Claude"
        display_browser = "Chrome" if browser_type == "chrome" else "Edge"
        name = f"{display_browser}-{display_target}"
        path = self.build_managed_profile_path(name, browser_type)

        return BrowserProfile(
            name=name,
            browser_type=browser_type,
            profile_mode="managed",
            user_data_dir=str(path),
            start_target=target,
            custom_url=None,
            notes=f"快速模板: {display_browser} / {display_target}",
            allow_full_reset=True,
            created_by_app=True,
            browser_executable=None,
        )

    def create_template_profile(self, browser_type: str, target: str) -> BrowserProfile:
        profile = self.build_template_profile(browser_type, target)
        # Avoid accidental overwrite by appending a counter if the name already exists.
        existing = {p.name for p in profile_manager.list_browser_profiles()}
        if profile.name in existing:
            index = 2
            base_name = profile.name
            while f"{base_name}-{index}" in existing:
                index += 1
            profile.name = f"{base_name}-{index}"
            profile.user_data_dir = str(self.build_managed_profile_path(profile.name, browser_type))
        self.save_profile(profile)
        return profile

    def clone_profile(self, source: BrowserProfile) -> BrowserProfile:
        existing = {p.name for p in profile_manager.list_browser_profiles()}
        base_name = f"{source.name}-副本"
        new_name = base_name
        index = 2
        while new_name in existing:
            new_name = f"{base_name}-{index}"
            index += 1

        cloned = BrowserProfile(
            name=new_name,
            browser_type=source.browser_type,
            profile_mode="managed",
            user_data_dir=str(self.build_managed_profile_path(new_name, source.browser_type)),
            start_target=source.start_target,
            custom_url=source.custom_url,
            notes=(source.notes + " | 复制自现有 Profile") if source.notes else "复制自现有 Profile",
            allow_full_reset=True,
            created_by_app=True,
            browser_executable=source.browser_executable,
        )
        self.save_profile(cloned)
        return cloned


browser_profile_manager = BrowserProfileManager()
