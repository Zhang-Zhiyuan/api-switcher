"""Claude Code API profile contract regression tests."""

from core import parser, security
from core.api_tester import APITester
from core.providers import ProviderRegistry
from models.profile import ClaudeProfile


def _profile(*, scheme: str, provider: str = "deepseek", effort: str = "max") -> ClaudeProfile:
    return ClaudeProfile(
        name="contract",
        auth_token_ref="claude:contract:auth_token",
        auth_scheme=scheme,
        base_url=ProviderRegistry.get_claude_base_url(provider),
        model="test-model",
        effort_level=effort,
        provider=provider,
    )


def test_apply_claude_profile_writes_only_bearer_contract(monkeypatch):
    monkeypatch.setattr(security, "get_secret", lambda ref: "secret" if ref else None)
    original = {
        "env": {
            "ANTHROPIC_API_KEY": "stale",
            "ANTHROPIC_MODEL": "stale-model",
            "CLAUDE_CODE_SUBAGENT_MODEL": "stale-model",
        },
        "permissions": {"defaultMode": "plan"},
    }

    applied = parser.apply_claude_profile(original, _profile(scheme="auth_token"))

    assert applied["env"]["ANTHROPIC_AUTH_TOKEN"] == "secret"
    assert "ANTHROPIC_API_KEY" not in applied["env"]
    assert applied["env"]["ANTHROPIC_MODEL"] == "test-model"
    assert applied["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "test-model"
    assert applied["env"]["CLAUDE_CODE_EFFORT_LEVEL"] == "max"
    assert "effortLevel" not in applied
    # Nested input objects are snapshots and must not be mutated in place.
    assert original["env"]["ANTHROPIC_API_KEY"] == "stale"
    assert original["permissions"]["defaultMode"] == "plan"


def test_apply_claude_profile_writes_only_x_api_key_contract(monkeypatch):
    monkeypatch.setattr(security, "get_secret", lambda ref: "secret" if ref else None)

    applied = parser.apply_claude_profile(
        {"env": {"ANTHROPIC_AUTH_TOKEN": "stale"}},
        _profile(scheme="api_key"),
    )

    assert applied["env"]["ANTHROPIC_API_KEY"] == "secret"
    assert "ANTHROPIC_AUTH_TOKEN" not in applied["env"]


def test_claude_legacy_primary_api_key_is_read_compatible_but_not_written():
    imported = ClaudeProfile.from_dict(
        {
            "name": "old",
            "auth_token_ref": "claude:old:auth_token",
            "primary_api_key_ref": "claude:old:primary_api_key",
            "base_url": "https://api.anthropic.com",
            "provider": "anthropic",
        }
    )
    assert imported.primary_api_key_ref == "claude:old:primary_api_key"
    assert imported.auth_scheme == "api_key"
    assert parser.apply_claude_config({"primaryApiKey": "legacy", "other": True}, imported) == {
        "other": True
    }


def test_clear_claude_overrides_removes_model_and_auth_precedence():
    managed = {
        "ANTHROPIC_AUTH_TOKEN": "token",
        "ANTHROPIC_API_KEY": "key",
        "ANTHROPIC_BASE_URL": "https://gateway.invalid",
        "ANTHROPIC_MODEL": "model",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "model",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "model",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "model",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "model",
        "CLAUDE_CODE_SUBAGENT_MODEL": "model",
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
    }
    settings = parser.clear_claude_api_overrides({"env": {**managed, "KEEP": "yes"}})

    assert settings["env"] == {"KEEP": "yes"}


def test_clear_claude_overrides_rejects_max_as_persistent_effort():
    settings = parser.clear_claude_api_overrides({"effortLevel": "max"})

    assert settings["effortLevel"] == "high"


def test_api_tester_auth_headers_follow_profile_scheme():
    bearer = APITester._claude_headers("secret", "auth_token")
    api_key = APITester._claude_headers("secret", "api_key")

    assert bearer["Authorization"] == "Bearer secret"
    assert "x-api-key" not in bearer
    assert api_key["x-api-key"] == "secret"
    assert "Authorization" not in api_key


def test_claude_provider_presets_use_anthropic_endpoints():
    assert ProviderRegistry.get_claude_base_url("kimi") == "https://api.kimi.com/coding/"
    assert ProviderRegistry.get_claude_base_url("minimax") == "https://api.minimax.io/anthropic"
    assert ProviderRegistry.get_claude_base_url("qwen") == "https://dashscope.aliyuncs.com/apps/anthropic"
    assert ProviderRegistry.get_claude_auth_scheme("kimi") == "api_key"
    assert ProviderRegistry.get_claude_auth_scheme("minimax") == "auth_token"
    assert ProviderRegistry.get_claude_auth_scheme("qwen") == "auth_token"
    assert ProviderRegistry.get_claude_default_model("kimi") == "k3-256k"
    assert ProviderRegistry.get_provider("kimi").default_model == "kimi-k2.6"
    assert ProviderRegistry.get_claude_default_model("minimax") == "MiniMax-M2.7"
    assert ProviderRegistry.get_provider("minimax").default_model == "MiniMax-Text-01"
    assert ProviderRegistry.get_provider("gemini").claude_supported is False
    assert "gemini" not in {provider.name for provider in ProviderRegistry.get_claude_providers()}


def test_claude_profile_defaults_are_safe_and_old_scheme_migrates_by_provider():
    fresh = ClaudeProfile(name="fresh", auth_token_ref="ref", base_url="")
    assert fresh.permissions_mode == "default"
    assert fresh.skip_dangerous_prompt is False

    anthropic = ClaudeProfile.from_dict(
        {"name": "a", "auth_token_ref": "ref", "base_url": "", "provider": "anthropic"}
    )
    relay = ClaudeProfile.from_dict(
        {"name": "r", "auth_token_ref": "ref", "base_url": "", "provider": "deepseek"}
    )
    assert anthropic.auth_scheme == "api_key"
    assert relay.auth_scheme == "auth_token"
