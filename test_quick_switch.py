#!/usr/bin/env python3
"""
测试快速切换和使用统计功能
"""

import sys
import tempfile
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core import profile_manager
import core.usage_stats as usage_stats_module
from core.usage_stats import format_token_count as format_tokens


def _isolated_usage_manager(tmp_path, monkeypatch=None):
    stats_file = tmp_path / "usage_stats.json"
    if monkeypatch:
        monkeypatch.setattr(usage_stats_module, "STATS_FILE", stats_file)
    else:
        usage_stats_module.STATS_FILE = stats_file
    return usage_stats_module.UsageStatsManager()


def test_profile_loading():
    """测试配置加载"""
    print("=" * 60)
    print("测试配置加载")
    print("=" * 60)

    # Test Claude profiles
    print("\n【Claude 配置】")
    claude_profiles = profile_manager.list_switchable_claude_profiles()
    if claude_profiles:
        for p in claude_profiles:
            print(f"  - {p.name}")
        active = profile_manager.get_active_claude_name()
        print(f"  当前激活: {active or '无'}")
    else:
        print("  无配置")

    # Test Codex profiles
    print("\n【Codex 配置】")
    codex_profiles = profile_manager.list_switchable_codex_profiles()
    if codex_profiles:
        for p in codex_profiles:
            print(f"  - {p.name}")
        active = profile_manager.get_active_codex_name()
        print(f"  当前激活: {active or '无'}")
    else:
        print("  无配置")


def test_usage_recording(tmp_path, monkeypatch):
    """测试使用统计记录"""
    print("\n" + "=" * 60)
    print("测试使用统计记录")
    print("=" * 60)

    # Record some test switches
    print("\n记录测试切换...")
    manager = _isolated_usage_manager(tmp_path, monkeypatch)
    manager.record_switch("Test Claude Profile", "claude")
    manager.record_switch("Test Codex Profile", "codex")

    # Record some test tokens
    print("记录测试 token...")
    manager.record_tokens("Test Claude Profile", "claude", input_tokens=1500, output_tokens=2500)
    manager.record_tokens("Test Codex Profile", "codex", input_tokens=3000, output_tokens=5000)

    claude_stats = manager.get_stats("Test Claude Profile", "claude")
    codex_stats = manager.get_stats("Test Codex Profile", "codex")
    assert claude_stats.switch_count == 1
    assert claude_stats.total_tokens == 4000
    assert codex_stats.switch_count == 1
    assert codex_stats.total_tokens == 8000

    print("[OK] 记录完成")


def test_usage_stats(tmp_path, monkeypatch):
    """测试使用统计查询"""
    print("\n" + "=" * 60)
    print("测试使用统计查询")
    print("=" * 60)

    manager = _isolated_usage_manager(tmp_path, monkeypatch)
    manager.record_switch("Test Claude Profile", "claude")
    manager.record_tokens("Test Claude Profile", "claude", input_tokens=1200, output_tokens=800)

    all_stats = manager.get_all_stats()

    assert all_stats

    if not all_stats:
        print("\n暂无统计数据")
        return

    print(f"\n共有 {len(all_stats)} 条统计记录:\n")

    for stats in all_stats:
        print(f"【{stats.profile_name}】({stats.profile_type.upper()})")
        print(f"  切换次数: {stats.switch_count}")
        print(f"  最后使用: {stats.last_used}")
        print(f"  Token 使用:")
        print(f"    - 总计: {format_tokens(stats.total_tokens)}")
        print(f"    - 输入: {format_tokens(stats.input_tokens)}")
        print(f"    - 输出: {format_tokens(stats.output_tokens)}")
        print()


def test_token_formatting():
    """测试 token 格式化"""
    print("=" * 60)
    print("测试 Token 格式化")
    print("=" * 60)

    test_cases = [
        (0, "0"),
        (500, "500"),
        (999, "999"),
        (1000, "1.0K"),
        (1500, "1.5K"),
        (15000, "15.0K"),
        (150000, "150.0K"),
        (999999, "1000.0K"),
        (1000000, "1.0M"),
        (1500000, "1.5M"),
        (15000000, "15.0M"),
        (1000000000, "1.0B"),
    ]

    print("\nToken 数量 -> 格式化结果:")
    for value, expected in test_cases:
        result = format_tokens(value)
        status = "[OK]" if result == expected else "[FAIL]"
        print(f"  {status} {value:>12,} -> {result:>8} (期望: {expected})")


def main():
    """主测试函数"""
    print("\n" + "=" * 60)
    print("API 切换器 - 快速切换和统计功能测试")
    print("=" * 60)

    try:
        # Test 1: Profile loading
        test_profile_loading()

        # Test 2: Token formatting
        test_token_formatting()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Test 3: Usage recording
            test_usage_recording(tmp_path, None)

            # Test 4: Usage stats
            test_usage_stats(tmp_path, None)

        print("\n" + "=" * 60)
        print("测试完成")
        print("=" * 60)

    except Exception as e:
        print(f"\n[FAIL] 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
