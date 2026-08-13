#!/usr/bin/env python3
# ruff: noqa: E402
"""
测试快速切换和使用统计功能
"""

import sys
import tempfile
import re
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core import profile_manager
from core.api_tester import APITester
import core.usage_stats as usage_stats_module
from core.usage_stats import format_token_count as format_tokens
from models.auto_continue import AutoContinueSettings, DEFAULT_BLOCKER_PATTERNS, DEFAULT_INCOMPLETE_PATTERNS
from ui.app import App


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
        print("  Token 使用:")
        print(f"    - 总计: {format_tokens(stats.total_tokens)}")
        print(f"    - 输入: {format_tokens(stats.input_tokens)}")
        print(f"    - 输出: {format_tokens(stats.output_tokens)}")
        print()


def test_usage_stats_load_ignores_unknown_fields(tmp_path, monkeypatch):
    stats_file = tmp_path / "usage_stats.json"
    stats_file.write_text(
        """
{
  "claude:legacy": {
    "profile_name": "legacy",
    "profile_type": "claude",
    "switch_count": 2,
    "future_field": "ignored",
    "daily_history": {
      "2026-05-11": {
        "date": "2026-05-11",
        "switch_count": 2,
        "future_daily_field": "ignored"
      }
    }
  }
}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_stats_module, "STATS_FILE", stats_file)

    manager = usage_stats_module.UsageStatsManager()
    stats = manager.get_stats("legacy", "claude")

    assert stats.switch_count == 2
    assert stats.daily_history["2026-05-11"].switch_count == 2


def test_usage_stats_save_creates_parent_and_cleans_temp_file(tmp_path, monkeypatch):
    stats_file = tmp_path / "nested" / "usage_stats.json"
    monkeypatch.setattr(usage_stats_module, "STATS_FILE", stats_file)

    manager = usage_stats_module.UsageStatsManager()
    manager.record_switch("Atomic Profile", "claude")

    assert stats_file.exists()
    assert not stats_file.with_suffix(stats_file.suffix + ".tmp").exists()


def test_usage_dashboard_data_reuses_one_filtered_stats_pass(tmp_path, monkeypatch):
    manager = _isolated_usage_manager(tmp_path, monkeypatch)
    manager.record_switch("Claude A", "claude")
    manager.record_switch("Claude A", "claude")
    manager.record_switch("Claude B", "claude")
    manager.record_switch("Codex A", "codex")
    manager.record_tokens("Claude A", "claude", input_tokens=1000, output_tokens=500)

    original_get_all_stats = manager.get_all_stats
    calls = {"count": 0}

    def counting_get_all_stats(profile_type=None):
        calls["count"] += 1
        return original_get_all_stats(profile_type)

    manager.get_all_stats = counting_get_all_stats

    dashboard = manager.get_dashboard_data("claude", top_limit=1, recent_limit=2, trend_days=7)

    assert calls["count"] == 1
    assert dashboard["summary"]["total_profiles"] == 2
    assert dashboard["summary"]["total_switches"] == 3
    assert dashboard["summary"]["total_tokens"] == 1500
    assert [item.profile_name for item in dashboard["top_profiles"]] == ["Claude A"]
    assert {item.profile_name for item in dashboard["recent_profiles"]} == {"Claude A", "Claude B"}
    assert len(dashboard["trend"]) == 7


def _matches_incomplete(text: str, settings: AutoContinueSettings | None = None) -> bool:
    settings = settings or AutoContinueSettings()
    return any(re.search(pattern, text) for pattern in settings.incomplete_patterns)


def _matches_blocker(text: str, settings: AutoContinueSettings | None = None) -> bool:
    settings = settings or AutoContinueSettings()
    return any(re.search(pattern, text) for pattern in settings.blocker_patterns)


def test_auto_continue_patterns_match_chinese_unfinished_work():
    examples = [
        "还有一处未完成：需要补充远程 hook 的验证。",
        "接下来需要修复中文识别规则。",
        "下一步：继续验证打包后的 exe。",
        "后续步骤：添加更多回归测试。",
        "这个功能仍然需要优化错误提示。",
        "如果你同意，我下一步就直接开始这个抓取前缀专项重训。",
        "接下来直接开始 VL-geometry 抓取前缀专项实验。",
        "<task-notification><status>killed</status><summary>Background command \"Train grasp-prefix finetune from v5 checkpoint\" was stopped</summary></task-notification>",
        "Background command \"Train grasp-prefix finetune from v5 checkpoint\" was stopped",
    ]

    for message in examples:
        assert _matches_incomplete(message), message


def test_auto_continue_patterns_do_not_match_completed_chinese_summary():
    assert not _matches_incomplete("已经完成，测试也通过了。")
    assert not _matches_incomplete("不需要继续处理，当前结果可以停止。")
    assert not _matches_incomplete("无需继续优化，这一轮已经收尾。")


def test_auto_continue_blocker_patterns_match_chinese_user_input_requests():
    examples = [
        "需要你确认要使用哪个配置。",
        "请选择下一步操作。",
        "等待用户输入 API Key 后才能继续。",
        "缺少必要文件，无法继续。",
        "找不到配置文件。",
        "当前没有权限写入目标目录。",
    ]

    for message in examples:
        assert _matches_blocker(message), message


def test_auto_continue_blocker_patterns_do_not_match_finished_chinese_summary():
    assert not _matches_blocker("已经确认完成，测试通过。")
    assert not _matches_blocker("已提供完整结果，无需用户继续操作。")
    assert not _matches_blocker("不存在问题，所有检查都已经通过。")
    assert not _matches_blocker("如果你同意，我下一步就直接开始这个抓取前缀专项重训。")


def test_auto_continue_patterns_match_english_unfinished_work():
    examples = [
        "This is still WIP; remaining work includes tests and verification.",
        "Would you like me to continue with the remaining tests?",
        "Reply with continue to continue the implementation.",
        "The feature is not production-ready because verification is missing.",
        "Next steps: implement the remote hook regression tests.",
    ]

    for message in examples:
        assert _matches_incomplete(message), message


def test_auto_continue_patterns_do_not_match_completed_english_summary():
    assert not _matches_incomplete("Completed implementation and verified tests pass.")
    assert not _matches_incomplete("No remaining work; all checks passed.")
    assert not _matches_incomplete("There are no remaining TODOs and no follow-up steps.")


def test_auto_continue_blocker_patterns_match_english_user_input_requests():
    examples = [
        "I need your confirmation before deploying to production.",
        "Waiting for user input with the API key.",
        "Please choose which configuration profile to use.",
        "The credential file is missing, so I cannot sign in.",
        "Which approach would you like me to use?",
    ]

    for message in examples:
        assert _matches_blocker(message), message


def test_auto_continue_blocker_patterns_do_not_match_completed_english_summary():
    assert not _matches_blocker("The confirmation step is complete and tests passed.")
    assert not _matches_blocker("No user action is required; the implementation is finished.")


def test_auto_continue_from_dict_preserves_custom_patterns_and_adds_defaults():
    custom_pattern = r"自定义未完模式"
    settings = AutoContinueSettings.from_dict({
        "incomplete_patterns": [custom_pattern],
        "blocker_patterns": [],
    })

    assert settings.incomplete_patterns[0] == custom_pattern
    for pattern in DEFAULT_INCOMPLETE_PATTERNS:
        assert pattern in settings.incomplete_patterns
    for pattern in DEFAULT_BLOCKER_PATTERNS:
        assert pattern in settings.blocker_patterns
    assert _matches_incomplete("接下来需要测试中文规则。", settings)


def test_api_model_recommendation_prefers_strong_latest_models():
    assert APITester.recommend_best_model([
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "opus[1m]",
        "claude-opus-4-7",
    ]) == "opus[1m]"

    assert APITester.recommend_best_model([
        "text-embedding-3-large",
        "gpt-5.5-mini",
        "gpt-4.1",
        "gpt-5.5",
    ]) == "gpt-5.5"

    assert APITester.recommend_best_model([
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-chat",
    ]) == "deepseek-v4-pro"

    assert APITester.recommend_best_model([
        "default",
        "gpt-5.5",
    ]) == "gpt-5.5"


def test_api_model_preference_sort_filters_utility_models_to_the_end():
    models = APITester.sort_models_by_preference([
        "text-embedding-3-large",
        "gpt-5.5",
        "gpt-4.1",
    ])

    assert models[0] == "gpt-5.5"
    assert models[-1] == "text-embedding-3-large"


def test_api_model_metadata_breaks_ties_by_created_time():
    models = ["gpt-5.5-stable", "gpt-5.5-candidate"]
    metadata = {
        "gpt-5.5-stable": {"created": "2026-01-01T00:00:00Z"},
        "gpt-5.5-candidate": {"created": "2026-05-01T00:00:00Z"},
    }

    assert APITester.recommend_best_model(models, metadata) == "gpt-5.5-candidate"
    assert APITester.sort_models_by_preference(models, metadata)[0] == "gpt-5.5-candidate"


def test_api_model_extraction_preserves_display_metadata():
    data = {
        "data": [
            {
                "id": "opaque-utility",
                "display_name": "Text Embedding 3 Large",
                "created": 1_760_000_000,
            },
            {
                "id": "opaque-chat",
                "display_name": "GPT-5.5",
                "created_at": "2026-05-01T00:00:00Z",
            },
        ]
    }
    infos = APITester._extract_model_infos(data)
    models = [model.id for model in infos]
    metadata = APITester._model_metadata_from_infos(infos)

    assert APITester._extract_model_ids(data) == ["opaque-chat", "opaque-utility"]
    assert APITester.recommend_best_model(models, metadata) == "opaque-chat"


def test_app_quick_switch_loader_accepts_delay_override():
    app = object.__new__(App)
    app._exit_requested = False
    calls = []
    app._load_quick_switch_profiles_delayed = lambda delay_ms=80: calls.append(delay_ms)

    App._load_quick_switch_profiles(app, delay_ms=0)

    assert calls == [0]


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
