from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

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
    def export_profiles_metadata(self, output_path: str, names: Optional[list[str]] = None) -> Path:
        """Export profiles metadata with validation."""
        try:
            profiles = profile_manager.list_browser_profiles()
        except Exception as e:
            raise RuntimeError(f"无法加载 Profile 列表: {e}") from e

        if names:
            selected = [p for p in profiles if p.name in names]
            if len(selected) != len(names):
                found_names = {p.name for p in selected}
                missing = set(names) - found_names
                import logging
                logging.getLogger(__name__).warning(f"Some profiles not found: {missing}")
        else:
            selected = profiles

        if not selected:
            raise ValueError("没有可导出的 Profile")

        data = {
            "version": 1,
            "export_time": __import__("datetime").datetime.now().isoformat(),
            "profiles": [p.to_dict() for p in selected],
            "note": "metadata only; does not include cookies, tokens, sessions, or browser storage",
        }

        try:
            out = Path(output_path).resolve()
            out.parent.mkdir(parents=True, exist_ok=True)

            # Validate output path
            if out.exists() and not out.is_file():
                raise ValueError(f"输出路径不是文件: {out}")

            # Write with atomic operation
            temp_path = out.with_suffix(".tmp")
            try:
                temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                temp_path.replace(out)
            except Exception as e:
                temp_path.unlink(missing_ok=True)
                raise RuntimeError(f"写入导出文件失败: {e}") from e

            return out

        except Exception as e:
            raise RuntimeError(f"导出失败: {e}") from e

    def import_profiles_metadata(self, input_path: str, conflict_policy: str = "rename") -> dict[str, int | list[str]]:
        """Import profiles metadata with comprehensive validation and rollback."""
        # Validate input file
        src = Path(input_path)
        if not src.exists():
            raise FileNotFoundError(f"导入文件不存在: {input_path}")
        if not src.is_file():
            raise ValueError(f"导入路径不是文件: {input_path}")

        # Validate conflict policy
        if conflict_policy not in {"skip", "overwrite", "rename"}:
            raise ValueError(f"无效的冲突策略: {conflict_policy}")

        # Load and validate import data
        try:
            content = src.read_text(encoding="utf-8")
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"导入文件不是有效的 JSON: {e}") from e
        except Exception as e:
            raise RuntimeError(f"读取导入文件失败: {e}") from e

        if not isinstance(data, dict):
            raise ValueError("导入文件格式错误: 根元素必须是对象")
        if "profiles" not in data:
            raise ValueError("导入文件格式错误: 缺少 profiles 字段")
        if not isinstance(data["profiles"], list):
            raise ValueError("导入文件格式错误: profiles 必须是数组")

        result: dict[str, int | list[str]] = {
            "imported": 0,
            "skipped": 0,
            "renamed": 0,
            "overwritten": 0,
            "failures": [],
        }

        # Get existing profiles
        try:
            existing = {p.name: p for p in profile_manager.list_browser_profiles()}
        except Exception as e:
            raise RuntimeError(f"无法加载现有 Profile 列表: {e}") from e

        # Track imported profiles for potential rollback
        imported_names: list[str] = []
        original_store_backup = None

        try:
            # Create backup of current store before any modifications
            from core.profile_manager import _load_store
            original_store_backup = _load_store().copy()

            for idx, item in enumerate(data.get("profiles", [])):
                try:
                    if not isinstance(item, dict):
                        result["failures"].append(f"Profile #{idx + 1}: 不是有效的对象")
                        continue

                    # Parse profile
                    try:
                        profile = BrowserProfile.from_dict(item)
                    except Exception as e:
                        result["failures"].append(f"Profile #{idx + 1}: 解析失败 - {e}")
                        continue

                    # Validate profile
                    valid, error = self.validate_profile(profile)

                    # For external profiles, skip if validation fails
                    if not valid and profile.profile_mode == "external":
                        result["skipped"] += 1
                        result["failures"].append(f"{profile.name}: {error}")
                        continue

                    # Handle name conflicts
                    original_name = profile.name
                    if profile.name in existing:
                        if conflict_policy == "skip":
                            result["skipped"] += 1
                            continue
                        elif conflict_policy == "overwrite":
                            # Save and track
                            profile_manager.save_browser_profile(profile)
                            existing[profile.name] = profile
                            imported_names.append(profile.name)
                            result["imported"] += 1
                            result["overwritten"] += 1
                            continue
                        elif conflict_policy == "rename":
                            base_name = profile.name
                            index = 2
                            while f"{base_name}-{index}" in existing:
                                index += 1
                            profile.name = f"{base_name}-{index}"
                            if profile.profile_mode == "managed":
                                profile.user_data_dir = str(self.build_managed_profile_path(profile.name, profile.browser_type))
                            result["renamed"] += 1

                    # Save profile
                    try:
                        profile_manager.save_browser_profile(profile)
                        existing[profile.name] = profile
                        imported_names.append(profile.name)
                        result["imported"] += 1
                    except Exception as e:
                        result["failures"].append(f"{original_name}: 保存失败 - {e}")
                        continue

                except Exception as e:
                    result["failures"].append(f"Profile #{idx + 1}: 处理失败 - {e}")
                    continue

            return result

        except Exception as e:
            # Critical error during import - attempt rollback
            if original_store_backup and imported_names:
                try:
                    from core.profile_manager import _save_store
                    _save_store(original_store_backup)
                    import logging
                    logging.getLogger(__name__).info("Rolled back import due to critical error")
                except Exception as rollback_error:
                    import logging
                    logging.getLogger(__name__).error(f"Failed to rollback import: {rollback_error}")

            raise RuntimeError(f"导入过程中发生严重错误: {e}") from e


browser_profile_manager = BrowserProfileManager()
