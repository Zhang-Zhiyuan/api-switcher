"""
错误恢复功能验证脚本
快速检查所有组件是否正确安装和配置
"""
import sys
from pathlib import Path
from typing import List, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from core.auto_continue.manager import auto_continue_manager
from core.auto_continue.error_analyzer import get_analyzer


class Colors:
    """终端颜色"""
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_header(text: str):
    """打印标题"""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 80}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 80}{Colors.RESET}\n")


def print_success(text: str):
    """打印成功消息"""
    print(f"{Colors.GREEN}✓{Colors.RESET} {text}")


def print_error(text: str):
    """打印错误消息"""
    print(f"{Colors.RED}✗{Colors.RESET} {text}")


def print_warning(text: str):
    """打印警告消息"""
    print(f"{Colors.YELLOW}⚠{Colors.RESET} {text}")


def print_info(text: str):
    """打印信息"""
    print(f"  {text}")


def check_provider(provider_name: str) -> Tuple[bool, List[str]]:
    """
    检查指定 Provider 的错误恢复功能

    Returns:
        (是否通过, 问题列表)
    """
    issues = []

    print(f"\n{Colors.BOLD}检查 {provider_name.upper()} Provider{Colors.RESET}")
    print("-" * 80)

    try:
        # 1. 检查状态
        print("\n1. 检查安装状态...")
        status = auto_continue_manager.get_status(provider_name)

        if status.error_recovery_installed:
            print_success("错误恢复已安装")
        else:
            print_warning("错误恢复未安装")
            issues.append(f"{provider_name}: 错误恢复未安装")

        # 2. 检查设置
        print("\n2. 检查配置...")
        settings = auto_continue_manager.get_settings(provider_name)

        if settings:
            print_success("配置文件存在")
            print_info(f"错误恢复已启用: {settings.error_recovery_enabled}")
            print_info(f"最大恢复次数: {settings.max_error_recoveries}")

            if not settings.error_recovery_enabled:
                print_warning("错误恢复功能未启用")
                issues.append(f"{provider_name}: 错误恢复功能未启用")
        else:
            print_warning("配置文件不存在")
            issues.append(f"{provider_name}: 配置文件不存在")

        # 3. 检查脚本文件
        print("\n3. 检查脚本文件...")
        provider = auto_continue_manager.get_provider(provider_name)
        script_path = provider.get_error_recovery_script_path()

        if script_path.exists():
            print_success(f"脚本文件存在: {script_path}")

            # 检查脚本大小
            size = script_path.stat().st_size
            if size > 1000:  # 至少应该有 1KB
                print_info(f"脚本大小: {size} 字节")
            else:
                print_warning(f"脚本文件太小: {size} 字节")
                issues.append(f"{provider_name}: 脚本文件可能不完整")
        else:
            print_error(f"脚本文件不存在: {script_path}")
            issues.append(f"{provider_name}: 脚本文件不存在")

        # 4. 检查 Hook 注册
        print("\n4. 检查 Hook 注册...")
        if provider_name.lower() == "claude":
            config_path = Path.home() / ".claude" / "settings.json"
            hook_event = "ResponseError"
        else:
            config_path = Path.home() / ".codex" / "hooks.json"
            hook_event = "Error"

        if config_path.exists():
            print_success(f"配置文件存在: {config_path}")

            import json
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)

                if provider_name.lower() == "claude":
                    hooks = config.get("hooks", {})
                    error_hooks = hooks.get(hook_event, [])

                    found = False
                    for hook_group in error_hooks:
                        for hook in hook_group.get("hooks", []):
                            if "error_recovery.ps1" in hook.get("command", ""):
                                found = True
                                break

                    if found:
                        print_success(f"Hook 已注册到 {hook_event} 事件")
                    else:
                        print_warning(f"Hook 未注册到 {hook_event} 事件")
                        issues.append(f"{provider_name}: Hook 未注册")
                else:
                    error_hook = config.get(hook_event, {})
                    if "error_recovery.ps1" in error_hook.get("command", ""):
                        print_success(f"Hook 已注册到 {hook_event} 事件")
                    else:
                        print_warning(f"Hook 未注册到 {hook_event} 事件")
                        issues.append(f"{provider_name}: Hook 未注册")

            except Exception as e:
                print_error(f"读取配置文件失败: {e}")
                issues.append(f"{provider_name}: 无法读取配置文件")
        else:
            print_warning(f"配置文件不存在: {config_path}")
            issues.append(f"{provider_name}: 配置文件不存在")

        # 5. 检查日志文件
        print("\n5. 检查日志文件...")
        analyzer = get_analyzer(provider_name)
        log_path = analyzer.log_path

        if log_path.exists():
            print_success(f"日志文件存在: {log_path}")

            # 读取日志统计
            stats = analyzer.analyze(days=30)
            print_info(f"总错误数: {stats.total_errors}")
            print_info(f"成功恢复数: {stats.total_recoveries}")

            if stats.total_errors > 0:
                print_info(f"恢复成功率: {stats.recovery_success_rate:.1f}%")
        else:
            print_info(f"日志文件不存在（正常，首次使用时会创建）: {log_path}")

        # 6. 检查状态文件
        print("\n6. 检查状态文件...")
        if provider_name.lower() == "claude":
            state_path = Path.home() / ".claude" / "error_recovery_state.json"
        else:
            state_path = Path.home() / ".codex" / "error_recovery_state.json"

        if state_path.exists():
            print_success(f"状态文件存在: {state_path}")

            try:
                with open(state_path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                print_info(f"记录的会话数: {len(state)}")
            except Exception as e:
                print_warning(f"读取状态文件失败: {e}")
        else:
            print_info(f"状态文件不存在（正常，首次使用时会创建）: {state_path}")

    except Exception as e:
        print_error(f"检查过程中发生错误: {e}")
        issues.append(f"{provider_name}: 检查失败 - {e}")

    return len(issues) == 0, issues


def main():
    """主函数"""
    print_header("API 错误自动恢复功能验证")

    print("此脚本将检查错误恢复功能是否正确安装和配置。\n")

    all_issues = []

    # 检查 Claude Code
    claude_ok, claude_issues = check_provider("claude")
    all_issues.extend(claude_issues)

    # 检查 Codex CLI
    codex_ok, codex_issues = check_provider("codex")
    all_issues.extend(codex_issues)

    # 总结
    print_header("验证结果")

    if not all_issues:
        print_success("所有检查通过！错误恢复功能已正确安装。")
        print("\n您可以开始使用错误恢复功能了。")
        print("当遇到 API 错误时，系统会自动识别并处理。")
    else:
        print_error(f"发现 {len(all_issues)} 个问题：\n")
        for i, issue in enumerate(all_issues, 1):
            print(f"  {i}. {issue}")

        print("\n" + "=" * 80)
        print(f"{Colors.YELLOW}建议操作：{Colors.RESET}")
        print("1. 打开 API切换器 GUI")
        print("2. 进入 '通用设置' Tab")
        print("3. 找到对应的 Provider")
        print("4. 点击 '设置' 按钮")
        print("5. 勾选 '启用错误自动恢复'")
        print("6. 点击 '保存'")
        print("7. 重新运行此验证脚本")

    print("\n" + "=" * 80)
    print(f"{Colors.BLUE}更多信息：{Colors.RESET}")
    print("- 待办记录: PENDING_WORK.md")
    print("=" * 80 + "\n")

    return 0 if not all_issues else 1


if __name__ == "__main__":
    sys.exit(main())
