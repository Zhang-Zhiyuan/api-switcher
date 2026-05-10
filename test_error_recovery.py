"""
测试错误恢复功能
验证所有组件是否正常工作
"""
import json
import sys
from pathlib import Path
from core.auto_continue.error_parser import error_parser, ErrorType, RecoveryStrategy
from core.auto_continue.error_analyzer import get_analyzer
from core.auto_continue.manager import auto_continue_manager

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def test_error_parser():
    """测试错误解析器"""
    print("=" * 80)
    print("测试错误解析器")
    print("=" * 80)

    test_cases = [
        {
            "name": "内容超长错误",
            "data": {
                "error_code": "CONTENT_LENGTH_EXCEEDS_THRESHOLD",
                "error_message": "对话内容超出长度限制",
                "status": 400
            },
            "expected_type": ErrorType.CONTENT_LENGTH_EXCEEDED,
            "expected_strategy": RecoveryStrategy.COMPACT_AND_CONTINUE
        },
        {
            "name": "速率限制错误",
            "data": {
                "error_message": "rate limit exceeded, please retry after 60 seconds",
                "status": 429
            },
            "expected_type": ErrorType.RATE_LIMIT_EXCEEDED,
            "expected_strategy": RecoveryStrategy.WAIT_AND_RETRY
        },
        {
            "name": "认证错误",
            "data": {
                "error_code": "invalid_api_key",
                "error_message": "Authentication failed",
                "status": 401
            },
            "expected_type": ErrorType.AUTHENTICATION_ERROR,
            "expected_strategy": RecoveryStrategy.NOTIFY_USER
        },
        {
            "name": "模型过载",
            "data": {
                "error_message": "model overloaded, please try again later",
                "status": 503
            },
            "expected_type": ErrorType.MODEL_OVERLOADED,
            "expected_strategy": RecoveryStrategy.RETRY_WITH_BACKOFF
        },
        {
            "name": "超时错误",
            "data": {
                "error_message": "request timed out",
                "status": 504
            },
            "expected_type": ErrorType.TIMEOUT_ERROR,
            "expected_strategy": RecoveryStrategy.RETRY_WITH_BACKOFF
        },
        {
            "name": "配额超限",
            "data": {
                "error_message": "quota exceeded, insufficient balance",
                "status": 402
            },
            "expected_type": ErrorType.QUOTA_EXCEEDED,
            "expected_strategy": RecoveryStrategy.NOTIFY_USER
        }
    ]

    passed = 0
    failed = 0

    for test in test_cases:
        print(f"\n测试: {test['name']}")
        parsed = error_parser.parse(test['data'])

        print(f"  错误类型: {parsed.error_type.value}")
        print(f"  恢复策略: {parsed.recovery_strategy.value}")
        print(f"  用户消息: {parsed.user_message}")

        if parsed.error_type == test['expected_type'] and \
           parsed.recovery_strategy == test['expected_strategy']:
            print("  ✓ 通过")
            passed += 1
        else:
            print("  ✗ 失败")
            print(f"    期望类型: {test['expected_type'].value}")
            print(f"    期望策略: {test['expected_strategy'].value}")
            failed += 1

    print(f"\n总结: {passed} 通过, {failed} 失败")
    assert failed == 0


def test_retry_after_extraction():
    """测试重试时间提取"""
    print("\n" + "=" * 80)
    print("测试重试时间提取")
    print("=" * 80)

    test_cases = [
        ("请在 60 秒后重试", 60),
        ("retry after 30 seconds", 30),
        ("wait 120 seconds before retrying", 120),
        ("no time specified", None)
    ]

    passed = 0
    failed = 0

    for message, expected in test_cases:
        data = {"error_message": message}
        parsed = error_parser.parse(data)

        print(f"\n消息: {message}")
        print(f"  提取时间: {parsed.retry_after}")

        if parsed.retry_after == expected:
            print("  ✓ 通过")
            passed += 1
        else:
            print(f"  ✗ 失败 (期望: {expected})")
            failed += 1

    print(f"\n总结: {passed} 通过, {failed} 失败")
    assert failed == 0


def test_provider_status():
    """测试 Provider 状态"""
    print("\n" + "=" * 80)
    print("测试 Provider 状态")
    print("=" * 80)

    for provider_name in ["claude", "codex"]:
        print(f"\n{provider_name.upper()} Provider:")
        try:
            status = auto_continue_manager.get_status(provider_name)
            print(f"  Hook 脚本存在: {status.hook_script_exists}")
            print(f"  Hook 已注册: {status.hook_registered}")
            print(f"  错误恢复已安装: {status.error_recovery_installed}")
            print(f"  已启用: {status.enabled}")

            settings = auto_continue_manager.get_settings(provider_name)
            if settings:
                print(f"  错误恢复已启用: {settings.error_recovery_enabled}")
                print(f"  最大恢复次数: {settings.max_error_recoveries}")
        except Exception as e:
            print(f"  错误: {e}")


def test_error_analyzer():
    """测试错误分析器"""
    print("\n" + "=" * 80)
    print("测试错误分析器")
    print("=" * 80)

    for provider_name in ["claude", "codex"]:
        print(f"\n{provider_name.upper()} 错误日志:")
        try:
            analyzer = get_analyzer(provider_name)
            stats = analyzer.analyze(days=7)

            print(f"  总错误数: {stats.total_errors}")
            print(f"  成功恢复数: {stats.total_recoveries}")
            print(f"  恢复成功率: {stats.recovery_success_rate:.1f}%")
            print(f"  平均恢复次数: {stats.avg_recovery_count:.1f}")

            if stats.errors_by_type:
                print(f"  错误类型分布:")
                for error_type, count in sorted(stats.errors_by_type.items(),
                                               key=lambda x: x[1], reverse=True):
                    print(f"    - {error_type}: {count}")
            else:
                print(f"  暂无错误记录")

        except Exception as e:
            print(f"  错误: {e}")


def test_script_generation():
    """测试脚本生成"""
    print("\n" + "=" * 80)
    print("测试脚本生成")
    print("=" * 80)

    from core.auto_continue.error_recovery_script import (
        generate_error_recovery_script,
        generate_codex_error_recovery_script
    )

    settings_path = "C:\\Users\\Test\\.claude\\auto_continue_settings.json"

    print("\n生成 Claude Code 错误恢复脚本...")
    claude_script = generate_error_recovery_script(settings_path)
    print(f"  脚本长度: {len(claude_script)} 字符")
    print(f"  包含错误类型枚举: {'ErrorTypes' in claude_script}")
    print(f"  包含恢复策略: {'RecoveryStrategies' in claude_script}")
    print(f"  包含压缩命令: {'compact' in claude_script}")

    print("\n生成 Codex CLI 错误恢复脚本...")
    codex_script = generate_codex_error_recovery_script(settings_path)
    print(f"  脚本长度: {len(codex_script)} 字符")
    print(f"  包含错误分类: {'Get-ErrorType' in codex_script}")
    print(f"  包含压缩命令: {'compress' in codex_script}")


def main():
    """运行所有测试"""
    print("API 错误恢复功能测试")
    print("=" * 80)

    checks = [
        ("错误解析器", test_error_parser),
        ("重试时间提取", test_retry_after_extraction),
        ("Provider 状态", test_provider_status),
        ("错误分析器", test_error_analyzer),
        ("脚本生成", test_script_generation),
    ]
    results = []

    # 运行测试
    for name, check in checks:
        try:
            check()
            results.append((name, True))
        except Exception as e:
            print(f"{name} 失败: {e}")
            results.append((name, False))

    # 总结
    print("\n" + "=" * 80)
    print("测试总结")
    print("=" * 80)

    for name, passed in results:
        status = "✓ 通过" if passed else "✗ 失败"
        print(f"{name}: {status}")

    all_passed = all(result[1] for result in results)
    print("\n" + ("=" * 80))
    if all_passed:
        print("所有测试通过！✓")
    else:
        print("部分测试失败 ✗")
    print("=" * 80)


if __name__ == "__main__":
    main()
