from ui.tabs.common_tab import _build_overview_text, _build_storage_info_text


def test_common_tab_overview_masks_sensitive_values():
    text = _build_overview_text(
        {
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.example.com",
                "ANTHROPIC_AUTH_TOKEN": "anthropic-secret-token-123456",
            },
            "model": "claude-sonnet",
            "effortLevel": "high",
            "permissions": {"defaultMode": "bypassPermissions"},
        },
        {
            "model": "gpt-5.5",
            "model_provider": "openai",
            "model_reasoning_effort": "high",
            "approval_policy": "never",
            "sandbox_mode": "danger-full-access",
            "model_providers": {
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                }
            },
        },
        {
            "auth_mode": "apikey",
            "OPENAI_API_KEY": "sk-test-secret-value-abcdef",
            "tokens": {"account_id": "acct_123"},
            "last_refresh": "2026-06-13T00:00:00Z",
        },
        {
            "claudeCode.allowDangerouslySkipPermissions": True,
            "claudeCode.initialPermissionMode": "bypassPermissions",
            "claudeCode.selectedModel": "claude-sonnet",
        },
    )

    assert "anthropic-secret-token-123456" not in text
    assert "sk-test-secret-value-abcdef" not in text
    assert "anthropic-se...3456" in text
    assert "sk-test-...cdef" in text
    assert "=== Claude Code ===" in text
    assert "=== Codex CLI ===" in text
    assert "=== VS Code (Claude 相关) ===" in text


def test_common_tab_storage_info_text_keeps_status_details():
    text = _build_storage_info_text(
        {
            "source": "portable",
            "writable": False,
            "write_error": "locked",
            "data_dir_pointer_exists": True,
            "user_data_dir_pointer_exists": False,
            "portable_marker_exists": True,
            "portable": True,
            "warnings": ["one", "two", "three", "four"],
            "storage_dir": r"C:\data",
            "app_dir": r"C:\app",
        }
    )

    assert "来源: 便携模式" in text
    assert "状态: 不可写: locked" in text
    assert "自定义目录文件: 已设置" in text
    assert "便携模式: 已启用" in text
    assert "警告: one | two | three" in text
    assert "four" not in text
