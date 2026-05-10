"""
配置验证器 - 统一的健康检查系统
"""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import sys

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """验证结果"""
    category: str  # 类别：Claude/Codex/Browser/SSH/System
    item: str  # 检查项
    status: str  # 状态：ok/warning/error
    message: str  # 消息
    suggestion: Optional[str] = None  # 修复建议


class ConfigValidator:
    """配置验证器"""

    def __init__(self):
        self.results: list[ValidationResult] = []

    def validate_all(self) -> list[ValidationResult]:
        """执行所有验证检查"""
        self.results = []

        logger.info("Starting health check...")

        # 系统环境检查
        self._validate_system()

        # Claude Code 配置检查
        self._validate_claude()

        # Codex CLI 配置检查
        self._validate_codex()

        # 已保存 Profile 的静态检查，不发起网络请求
        self._validate_static_profile_health()

        # API 连接测试
        self._validate_api_connections()

        # 浏览器配置检查
        self._validate_browser()

        # SSH 配置检查
        self._validate_ssh()

        logger.info(f"Health check completed: {len(self.results)} items checked")
        return self.results

    def _add_result(self, category: str, item: str, status: str, message: str, suggestion: Optional[str] = None):
        """添加验证结果"""
        self.results.append(ValidationResult(
            category=category,
            item=item,
            status=status,
            message=message,
            suggestion=suggestion
        ))

    def _validate_system(self):
        """验证系统环境"""
        category = "系统环境"

        # Python 版本检查
        try:
            py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            if sys.version_info >= (3, 10):
                self._add_result(category, "Python 版本", "ok", f"Python {py_version}")
            else:
                self._add_result(
                    category, "Python 版本", "warning",
                    f"Python {py_version}（建议 3.10+）",
                    "升级到 Python 3.10 或更高版本"
                )
        except Exception as e:
            self._add_result(category, "Python 版本", "error", f"检查失败: {e}")

        # 存储目录检查
        try:
            from config.paths import BACKUPS_DIR, STORAGE_DIR, get_storage_info
            storage_info = get_storage_info()
            source = storage_info.get("source", "unknown")
            if STORAGE_DIR.exists() and storage_info.get("writable"):
                self._add_result(category, "存储目录", "ok", f"存在且可写: {STORAGE_DIR} (来源: {source})")
            elif STORAGE_DIR.exists():
                self._add_result(
                    category, "存储目录", "error",
                    f"存在但不可写: {STORAGE_DIR}",
                    storage_info.get("write_error") or "检查目录权限"
                )
            else:
                self._add_result(
                    category, "存储目录", "warning",
                    f"不存在: {STORAGE_DIR}",
                    "将自动创建"
                )

            if storage_info.get("data_dir_pointer_exists"):
                self._add_result(category, "自定义数据目录", "ok", f"程序目录指针: {storage_info.get('data_dir_pointer')}")
            if storage_info.get("user_data_dir_pointer_exists"):
                self._add_result(category, "自定义数据目录", "ok", f"用户目录指针: {storage_info.get('user_data_dir_pointer')}")
            if storage_info.get("portable_marker_exists"):
                self._add_result(category, "便携模式", "ok", f"已启用: {storage_info.get('portable_marker')}")
            for warning in storage_info.get("warnings", []):
                self._add_result(category, "数据目录 fallback", "warning", warning)

            if BACKUPS_DIR.exists():
                self._add_result(category, "备份目录", "ok", f"存在: {BACKUPS_DIR}")
            else:
                self._add_result(
                    category, "备份目录", "warning",
                    f"不存在: {BACKUPS_DIR}",
                    "将自动创建"
                )
        except Exception as e:
            self._add_result(category, "目录检查", "error", f"检查失败: {e}")

        # 依赖库检查
        required_packages = [
            ('customtkinter', 'CustomTkinter'),
            ('keyring', 'Keyring'),
            ('paramiko', 'Paramiko'),
            ('cryptography', 'Cryptography'),
        ]

        for module_name, display_name in required_packages:
            try:
                __import__(module_name)
                self._add_result(category, f"{display_name} 库", "ok", "已安装")
            except ImportError:
                self._add_result(
                    category, f"{display_name} 库", "error",
                    "未安装",
                    f"运行: pip install {module_name}"
                )

    def _validate_claude(self):
        """验证 Claude Code 配置"""
        category = "Claude Code"

        try:
            from config.paths import CLAUDE_SETTINGS, CLAUDE_CONFIG
            from core import profile_manager

            # 检查配置文件
            if CLAUDE_SETTINGS.exists():
                self._add_result(category, "settings.json", "ok", f"存在: {CLAUDE_SETTINGS}")
            else:
                self._add_result(
                    category, "settings.json", "warning",
                    f"不存在: {CLAUDE_SETTINGS}",
                    "首次使用时将自动创建"
                )

            # 检查 Profile
            try:
                profiles = profile_manager.list_switchable_claude_profiles()
                if profiles:
                    self._add_result(category, "Profile 数量", "ok", f"{len(profiles)} 个")
                    current = profile_manager.get_current_claude_name()
                    stored_active = profile_manager.get_active_claude_name()
                    if current:
                        self._add_result(category, "当前 Profile", "ok", current)
                    elif stored_active:
                        self._add_result(
                            category, "当前 Profile", "warning",
                            f"磁盘配置未匹配，最近切换: {stored_active}",
                            "重新切换或导入当前配置"
                        )
                    else:
                        self._add_result(
                            category, "当前 Profile", "warning",
                            "未设置",
                            "切换到一个 Profile"
                        )
                else:
                    self._add_result(
                        category, "Profile 数量", "warning",
                        "0 个",
                        "创建至少一个 Claude Profile"
                    )
            except Exception as e:
                self._add_result(category, "Profile 检查", "error", f"检查失败: {e}")

        except Exception as e:
            self._add_result(category, "配置检查", "error", f"检查失败: {e}")

    def _validate_codex(self):
        """验证 Codex CLI 配置"""
        category = "Codex CLI"

        try:
            from config.paths import CODEX_CONFIG, CODEX_AUTH
            from core import profile_manager

            # 检查配置文件
            if CODEX_CONFIG.exists():
                self._add_result(category, "config.toml", "ok", f"存在: {CODEX_CONFIG}")
            else:
                self._add_result(
                    category, "config.toml", "warning",
                    f"不存在: {CODEX_CONFIG}",
                    "首次使用时将自动创建"
                )

            if CODEX_AUTH.exists():
                self._add_result(category, "auth.json", "ok", f"存在: {CODEX_AUTH}")
            else:
                self._add_result(
                    category, "auth.json", "warning",
                    f"不存在: {CODEX_AUTH}",
                    "首次使用时将自动创建"
                )

            # 检查 Profile
            try:
                profiles = profile_manager.list_switchable_codex_profiles()
                if profiles:
                    self._add_result(category, "Profile 数量", "ok", f"{len(profiles)} 个")
                    current = profile_manager.get_current_codex_name()
                    stored_active = profile_manager.get_active_codex_name()
                    if current:
                        self._add_result(category, "当前 Profile", "ok", current)
                    elif stored_active:
                        self._add_result(
                            category, "当前 Profile", "warning",
                            f"磁盘配置未匹配，最近切换: {stored_active}",
                            "重新切换或导入当前配置"
                        )
                    else:
                        self._add_result(
                            category, "当前 Profile", "warning",
                            "未设置",
                            "切换到一个 Profile"
                        )
                else:
                    self._add_result(
                        category, "Profile 数量", "warning",
                        "0 个",
                        "创建至少一个 Codex Profile"
                    )
            except Exception as e:
                self._add_result(category, "Profile 检查", "error", f"检查失败: {e}")

        except Exception as e:
            self._add_result(category, "配置检查", "error", f"检查失败: {e}")

    def _validate_static_profile_health(self):
        """Validate saved API/account switch targets without making network calls."""
        try:
            from core.switch_preview import collect_static_health_checks

            for check in collect_static_health_checks():
                self._add_result(
                    check.category,
                    check.item,
                    check.status,
                    check.message,
                    check.suggestion or None,
                )
        except Exception as e:
            logger.error(f"Static profile health validation error: {e}", exc_info=True)
            self._add_result("Profile 静态检查", "检查失败", "error", str(e))

    def _validate_browser(self):
        """验证浏览器配置"""
        category = "浏览器 Profile"

        try:
            from core import profile_manager
            from core.browser_profile_manager import browser_profile_manager
            from core.browser_launcher import browser_launcher

            # 检查 Profile
            profiles = profile_manager.list_browser_profiles()
            if profiles:
                self._add_result(category, "Profile 数量", "ok", f"{len(profiles)} 个")

                # 检查每个 Profile
                valid_count = 0
                error_count = 0

                for profile in profiles:
                    try:
                        diagnosis = browser_profile_manager.diagnose_profile(profile)
                        if diagnosis['valid'] and diagnosis['executable_found']:
                            valid_count += 1
                        else:
                            error_count += 1
                    except Exception:
                        error_count += 1

                if error_count == 0:
                    self._add_result(category, "Profile 状态", "ok", f"全部正常 ({valid_count} 个)")
                elif valid_count > 0:
                    self._add_result(
                        category, "Profile 状态", "warning",
                        f"{valid_count} 个正常，{error_count} 个异常",
                        "在浏览器 Tab 中查看详情"
                    )
                else:
                    self._add_result(
                        category, "Profile 状态", "error",
                        f"全部异常 ({error_count} 个)",
                        "检查浏览器安装和 Profile 配置"
                    )

                # 检查浏览器可执行文件
                chrome_exe = browser_launcher.find_browser_executable("chrome")
                edge_exe = browser_launcher.find_browser_executable("edge")

                if chrome_exe:
                    self._add_result(category, "Chrome 浏览器", "ok", f"已安装: {chrome_exe}")
                else:
                    self._add_result(
                        category, "Chrome 浏览器", "warning",
                        "未找到",
                        "如需使用 Chrome Profile，请安装 Chrome"
                    )

                if edge_exe:
                    self._add_result(category, "Edge 浏览器", "ok", f"已安装: {edge_exe}")
                else:
                    self._add_result(
                        category, "Edge 浏览器", "warning",
                        "未找到",
                        "如需使用 Edge Profile，请安装 Edge"
                    )

            else:
                self._add_result(
                    category, "Profile 数量", "warning",
                    "0 个",
                    "创建至少一个浏览器 Profile"
                )

        except Exception as e:
            self._add_result(category, "配置检查", "error", f"检查失败: {e}")

    def _validate_ssh(self):
        """验证 SSH 配置"""
        category = "SSH 服务器"

        try:
            from core import profile_manager
            from core.ssh_manager import ssh_manager

            # 检查 Profile
            profiles = profile_manager.list_ssh_profiles()
            if profiles:
                self._add_result(category, "Profile 数量", "ok", f"{len(profiles)} 个")

                # 测试连接（仅测试前3个，避免耗时过长）
                test_profiles = profiles[:3]
                connected_count = 0
                failed_count = 0

                for profile in test_profiles:
                    try:
                        success, message = ssh_manager.test_connection(profile)
                        if success:
                            connected_count += 1
                        else:
                            failed_count += 1
                    except Exception:
                        failed_count += 1

                if failed_count == 0:
                    self._add_result(
                        category, "连接测试", "ok",
                        f"测试通过 ({connected_count}/{len(test_profiles)})"
                    )
                elif connected_count > 0:
                    self._add_result(
                        category, "连接测试", "warning",
                        f"{connected_count} 个成功，{failed_count} 个失败",
                        "在 SSH Tab 中查看详情"
                    )
                else:
                    self._add_result(
                        category, "连接测试", "error",
                        f"全部失败 ({failed_count}/{len(test_profiles)})",
                        "检查网络连接和 SSH 配置"
                    )

                if len(profiles) > 3:
                    self._add_result(
                        category, "连接测试", "ok",
                        f"仅测试了前 3 个 Profile，共 {len(profiles)} 个"
                    )
            else:
                self._add_result(category, "Profile 数量", "ok", "未配置")

        except Exception as e:
            logger.error(f"SSH validation error: {e}", exc_info=True)
            self._add_result(category, "检查失败", "error", str(e))

    def _validate_api_connections(self):
        """验证 API 连接"""
        from core.api_tester import APITester
        from core import profile_manager, security

        # 测试 Claude API
        category = "API 连接测试"

        try:
            active_claude = profile_manager.get_current_claude_name()
            if active_claude:
                profiles = profile_manager.list_switchable_claude_profiles()
                profile = next((p for p in profiles if p.name == active_claude), None)

                if profile:
                    # 获取 API Key
                    api_key = security.get_secret(profile.auth_token_ref) if profile.auth_token_ref else ""
                    if not api_key:
                        api_key = security.get_secret(getattr(profile, "primary_api_key_ref", None)) or ""

                    if api_key:
                        self._add_result(category, f"Claude ({active_claude})", "ok", "正在测试...")

                        # 测试连接
                        result = APITester.test_claude_api(
                            api_key,
                            profile.base_url or "https://api.anthropic.com",
                            profile.model or "claude-sonnet-4",
                            timeout=10
                        )

                        if result.success:
                            time_str = f" ({result.response_time:.0f}ms)" if result.response_time else ""
                            self._add_result(
                                category, f"Claude ({active_claude})", "ok",
                                f"连接成功{time_str}"
                            )
                        else:
                            self._add_result(
                                category, f"Claude ({active_claude})", "error",
                                result.message,
                                result.error_details or "检查 API Key 和网络连接"
                            )
                    else:
                        self._add_result(
                            category, f"Claude ({active_claude})", "warning",
                            "未找到 API Key",
                            "在 Profile 中配置 API Key"
                        )
            else:
                self._add_result(category, "Claude", "ok", "未激活配置")

        except Exception as e:
            logger.error(f"Claude API test error: {e}", exc_info=True)
            self._add_result(category, "Claude", "error", f"测试失败: {e}")

        # 测试 Codex API
        try:
            active_codex = profile_manager.get_current_codex_name()
            if active_codex:
                profiles = profile_manager.list_switchable_codex_profiles()
                profile = next((p for p in profiles if p.name == active_codex), None)

                if profile:
                    # 获取 API Key
                    api_key = security.get_secret(profile.api_key_ref) if profile.api_key_ref else ""

                    if api_key:
                        self._add_result(category, f"Codex ({active_codex})", "ok", "正在测试...")

                        # 测试连接
                        result = APITester.test_openai_api(
                            api_key,
                            profile.custom_base_url or "https://api.openai.com/v1",
                            profile.model or "gpt-5.5",
                            timeout=10
                        )

                        if result.success:
                            time_str = f" ({result.response_time:.0f}ms)" if result.response_time else ""
                            self._add_result(
                                category, f"Codex ({active_codex})", "ok",
                                f"连接成功{time_str}"
                            )
                        else:
                            self._add_result(
                                category, f"Codex ({active_codex})", "error",
                                result.message,
                                result.error_details or "检查 API Key 和网络连接"
                            )
                    else:
                        self._add_result(
                            category, f"Codex ({active_codex})", "warning",
                            "未找到 API Key",
                            "在 Profile 中配置 API Key"
                        )
            else:
                self._add_result(category, "Codex", "ok", "未激活配置")

        except Exception as e:
            logger.error(f"Codex API test error: {e}", exc_info=True)
            self._add_result(category, "Codex", "error", f"测试失败: {e}")

    def get_summary(self) -> dict:
        """获取验证摘要"""
        ok_count = sum(1 for r in self.results if r.status == "ok")
        warning_count = sum(1 for r in self.results if r.status == "warning")
        error_count = sum(1 for r in self.results if r.status == "error")

        return {
            "total": len(self.results),
            "ok": ok_count,
            "warning": warning_count,
            "error": error_count,
            "has_issues": warning_count > 0 or error_count > 0
        }


# 全局验证器实例
config_validator = ConfigValidator()
