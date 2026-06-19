from ui.tabs.common_tab import _build_overview_text, _build_storage_info_text


def test_common_tab_overview_masks_sensitive_values(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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
    assert "OPENAI_API_KEY=sk-test-...cdef" in text
    assert "=== Claude Code ===" in text
    assert "=== Codex CLI ===" in text
    assert "=== VS Code (Claude 相关) ===" in text


def test_common_tab_overview_uses_codex_provider_env_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-secret-abcdef")
    monkeypatch.setenv("OPENAI_API_KEY", "stale-openai-secret")

    text = _build_overview_text(
        {},
        {
            "model": "deepseek-v4-flash",
            "model_provider": "deepseek",
            "model_providers": {
                "deepseek": {
                    "name": "DeepSeek",
                    "base_url": "https://api.deepseek.com",
                    "env_key": "DEEPSEEK_API_KEY",
                }
            },
        },
        {"auth_mode": "chatgpt", "OPENAI_API_KEY": "stale-auth-key"},
        {},
    )

    assert "DEEPSEEK_API_KEY=sk-deeps...cdef" in text
    assert "stale-auth-key" not in text
    assert "stale-openai-secret" not in text


def test_common_tab_overview_marks_openai_auth_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-official-abcdef")

    text = _build_overview_text(
        {},
        {
            "model": "gpt-5.5",
            "model_provider": "custom",
            "model_providers": {
                "custom": {
                    "name": "Proxy",
                    "base_url": "https://proxy.example.com/v1",
                    "requires_openai_auth": True,
                }
            },
        },
        {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-openai-official-abcdef"},
        {},
    )

    assert "OpenAI auth (requires_openai_auth=true)" in text
    assert "OPENAI_API_KEY=sk-opena...cdef" in text


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
