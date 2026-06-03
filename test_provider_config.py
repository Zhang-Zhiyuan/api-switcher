"""Regression checks for provider presets and config generation."""
import json
import tempfile
from pathlib import Path

from config import paths
from core import auth_parser, profile_manager, security, toml_parser
from models.profile import ClaudeProfile, CodexProfile
from core.auth_parser import apply_codex_apikey
from core.parser import apply_claude_profile
from core.profile_manager import detect_claude_provider
from core.providers import ProviderRegistry
from core.toml_parser import apply_codex_profile


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def check_codex_provider(provider_id, model, base_url, wire_api, writes_effort):
    profile = CodexProfile(
        name=provider_id,
        model=model,
        model_provider=provider_id,
        model_reasoning_effort="high",
        custom_base_url=base_url,
        custom_name=ProviderRegistry.get_provider(provider_id).display_name,
        custom_wire_api=wire_api,
    )
    config = apply_codex_profile({}, profile)

    assert_equal(config["model"], model, f"{provider_id} model")
    assert_equal(config["model_provider"], provider_id, f"{provider_id} provider id")
    assert_equal(config["model_providers"][provider_id]["base_url"], base_url, f"{provider_id} base_url")
    assert_equal(config["model_providers"][provider_id]["wire_api"], wire_api, f"{provider_id} wire_api")
    assert_equal(
        config["model_providers"][provider_id]["env_key"],
        ProviderRegistry.get_provider(provider_id).codex_env_key,
        f"{provider_id} env_key",
    )

    has_effort = "model_reasoning_effort" in config
    assert_equal(has_effort, writes_effort, f"{provider_id} reasoning effort presence")


def test_codex_runtime_env_keys_include_openai_fallback():
    profile = CodexProfile(name="deepseek", model_provider="deepseek")

    assert ProviderRegistry.get_codex_runtime_env_keys_for_profile(profile) == [
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
    ]

    openai_profile = CodexProfile(name="openai", model_provider="openai")
    assert ProviderRegistry.get_codex_runtime_env_keys_for_profile(openai_profile) == ["OPENAI_API_KEY"]


def test_openai_codex_preset_uses_responses_wire_api():
    provider = ProviderRegistry.get_provider("openai")
    assert provider is not None
    assert provider.codex_supported is True
    assert provider.claude_supported is False
    assert provider.base_url_for_codex() == "https://api.openai.com/v1"
    assert provider.wire_api == "responses"
    assert provider.codex_env_key == "OPENAI_API_KEY"

    profile = CodexProfile(
        name="openai",
        model=provider.default_model,
        model_provider="openai",
    )
    config = apply_codex_profile({}, profile)

    assert config["model"] == "gpt-5.5"
    assert config["model_provider"] == "openai"
    # OpenAI official does not write model_providers table
    assert "model_providers" not in config or not config.get("model_providers")


def test_codex_wire_api_defaults_and_invalid_values_use_provider_preset():
    provider = ProviderRegistry.get_provider("deepseek")
    assert provider is not None

    assert ProviderRegistry.get_codex_wire_api("deepseek") == "responses"
    assert ProviderRegistry.get_codex_wire_api("deepseek", "auto") == "responses"
    assert ProviderRegistry.get_codex_wire_api("deepseek", "invalid") == "responses"
    assert ProviderRegistry.get_codex_wire_api("custom", "") == "responses"

    config = apply_codex_profile(
        {},
        CodexProfile(
            name="deepseek",
            model="deepseek-v4-flash",
            model_provider="deepseek",
            custom_wire_api="invalid",
        ),
    )

    assert config["model_providers"]["deepseek"]["wire_api"] == "responses"


def test_reasoning_efforts_follow_model_family():
    assert ProviderRegistry.model_supports_max_reasoning("claude-notopus-model") is False
    assert ProviderRegistry.get_reasoning_efforts_for_model("openai", "gpt-5.5") == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert ProviderRegistry.get_reasoning_efforts_for_model("openai", "claude-opus-4-7") == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert ProviderRegistry.get_reasoning_efforts_for_model("anthropic", "claude-opus-4-7[1m]") == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert ProviderRegistry.get_reasoning_efforts_for_model("anthropic", "claude-sonnet-4-6") == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert ProviderRegistry.get_default_reasoning_effort_for_model("openai", "gpt-5.5") == "xhigh"
    assert ProviderRegistry.get_default_reasoning_effort_for_model("openai", "claude-opus-4-7") == "max"
    assert ProviderRegistry.get_default_reasoning_effort_for_model(
        "relay",
        "claude-opus-4-7",
        custom_name="Custom",
    ) == "max"
    assert ProviderRegistry.get_reasoning_efforts_for_model("relay", "gpt-5.5") == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert ProviderRegistry.get_default_reasoning_effort_for_model("relay", "claude-opus-4-7") == "max"
    # Kimi provider has no reasoning_efforts, so any model returns []
    assert ProviderRegistry.get_reasoning_efforts_for_model("kimi", "gpt-5.5") == []


def check_claude_provider(provider_id, model, base_url, writes_effort):
    profile = ClaudeProfile(
        name=provider_id,
        auth_token_ref=None,
        base_url=base_url,
        model=model,
        provider=provider_id,
        effort_level="high",
    )
    settings = apply_claude_profile({"env": {}}, profile)

    assert_equal(settings["model"], model, f"{provider_id} claude model")
    assert_equal(settings["env"].get("ANTHROPIC_BASE_URL"), base_url or None, f"{provider_id} claude base_url")
    assert_equal("effortLevel" in settings, writes_effort, f"{provider_id} claude effort presence")


def test_claude_stale_fields_are_removed():
    profile = ClaudeProfile(
        name="clean",
        auth_token_ref=None,
        base_url="https://api.anthropic.com",
        model="claude-sonnet-4",
        provider="anthropic",
        permissions_allow=[],
        additional_directories=[],
    )
    settings = apply_claude_profile(
        {
            "env": {"ANTHROPIC_AUTH_TOKEN": "old", "ANTHROPIC_API_KEY": "old"},
            "permissions": {"defaultMode": "default", "allow": ["old"]},
            "additionalDirectories": ["C:/old"],
        },
        profile,
    )

    assert_equal("allow" in settings["permissions"], False, "claude stale permissions allow")
    assert_equal("additionalDirectories" in settings, False, "claude stale additional directories")
    assert_equal("ANTHROPIC_AUTH_TOKEN" in settings["env"], False, "claude stale auth token")
    assert_equal("ANTHROPIC_API_KEY" in settings["env"], False, "claude stale api key")


def test_malformed_config_shapes_are_repaired():
    claude_profile = ClaudeProfile(
        name="shape",
        auth_token_ref=None,
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-flash",
        provider="deepseek",
    )
    settings = apply_claude_profile({"env": "bad", "permissions": "bad"}, claude_profile)
    assert_equal(isinstance(settings["env"], dict), True, "claude env shape")
    assert_equal(isinstance(settings["permissions"], dict), True, "claude permissions shape")

    codex_profile = CodexProfile(
        name="shape",
        model="kimi-k2.6",
        model_provider="kimi",
    )
    config = apply_codex_profile({"model_providers": []}, codex_profile)
    assert_equal(isinstance(config["model_providers"], dict), True, "codex model_providers shape")
    assert_equal(config["model_providers"]["kimi"]["wire_api"], "responses", "codex repaired wire_api")

    openai_config = apply_codex_profile({"model_providers": []}, CodexProfile(name="openai"))
    assert_equal("model_providers" in openai_config, False, "openai malformed model_providers removed")


def test_stale_codex_auth_is_cleared():
    api_auth = apply_codex_apikey({"OPENAI_API_KEY": "old", "tokens": {"old": True}}, CodexProfile(name="api"))
    assert_equal(api_auth.get("OPENAI_API_KEY"), "", "stale codex api key")
    assert_equal("tokens" in api_auth, False, "codex api mode stale tokens")
    assert_equal(api_auth.get("auth_mode"), "apikey", "codex api mode")
    assert_equal("last_refresh" in api_auth, False, "codex api mode stale last_refresh")


def test_claude_provider_detection():
    assert_equal(
        detect_claude_provider({"env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic/"}}),
        "deepseek",
        "deepseek provider detection",
    )
    assert_equal(
        detect_claude_provider(
            {
                "env": {
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "GLM-5.1",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "GLM-5.1",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "GLM-4.5-air",
                }
            }
        ),
        "glm",
        "glm provider detection",
    )


def _set_codex_identity_test_paths(root: Path) -> None:
    data_dir = root / "data"
    paths.STORAGE_DIR = data_dir
    paths.PROFILES_FILE = data_dir / "profiles.json"
    paths.BACKUPS_DIR = data_dir / "backups"
    paths.SECRETS_DIR = data_dir / "secrets"
    paths.CODEX_CONFIG = root / "codex" / "config.toml"
    paths.CODEX_AUTH = root / "codex" / "auth.json"

    profile_manager.PROFILES_FILE = paths.PROFILES_FILE
    security.SECRETS_DIR = paths.SECRETS_DIR
    toml_parser.CODEX_CONFIG = paths.CODEX_CONFIG
    auth_parser.CODEX_AUTH = paths.CODEX_AUTH

    paths.ensure_storage_dirs(migrate_legacy=False)
    profile_manager._save_store(profile_manager._get_default_store())


def _write_codex_identity_files(api_key: str) -> None:
    paths.CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths.CODEX_CONFIG.write_text(
        "\n".join([
            'model = "deepseek-v4-flash"',
            'model_provider = "deepseek"',
            'model_reasoning_effort = "high"',
            'approval_policy = "never"',
            'sandbox_mode = "danger-full-access"',
            'disable_response_storage = true',
            '[model_providers.deepseek]',
            'base_url = "https://api.deepseek.com"',
            'name = "DeepSeek"',
            'wire_api = "responses"',
            'requires_openai_auth = false',
            "",
        ]),
        encoding="utf-8",
    )
    paths.CODEX_AUTH.parent.mkdir(parents=True, exist_ok=True)
    paths.CODEX_AUTH.write_text(
        json.dumps(
            {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": api_key,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_codex_import_names_and_runtime_detection():
    with tempfile.TemporaryDirectory() as tmp:
        _set_codex_identity_test_paths(Path(tmp))
        try:
            _write_codex_identity_files("key-a")
            first = profile_manager.import_current_codex()
            assert first is not None
            assert first.name != "Current"
            assert_equal(first.name, "Codex-DeepSeek-deepseek-v4-flash", "friendly codex import name")
            profile_manager.save_codex_profile(first)

            _write_codex_identity_files("key-a")
            assert_equal(profile_manager.get_current_codex_name(), first.name, "codex current api key match")

            _write_codex_identity_files("key-b")
            assert_equal(profile_manager.get_current_codex_name(), None, "codex different api key mismatch")
            second = profile_manager.import_current_codex()
            assert second is not None
            assert_equal(second.name, "Codex-DeepSeek-deepseek-v4-flash-2", "duplicate import name suffix")
            profile_manager.save_codex_profile(second)

            names = {profile.name for profile in profile_manager.list_codex_profiles()}
            if first.name not in names or second.name not in names:
                raise AssertionError(f"Expected both imported Codex profiles, got {names}")
        finally:
            for profile in profile_manager.list_codex_profiles():
                for ref in [profile.api_key_ref]:
                    security.delete_secret(ref)


def test_health_check_codex_uses_provider_base_url_and_wire_api(monkeypatch):
    from core.api_tester import APITester, TestResult
    from core.validator import ConfigValidator

    profile = CodexProfile(
        name="deepseek",
        api_key_ref="codex:deepseek:api_key",
        model="deepseek-v4-flash",
        model_provider="deepseek",
    )
    captured = {}

    monkeypatch.setattr(profile_manager, "get_current_claude_name", lambda: None)
    monkeypatch.setattr(profile_manager, "get_current_codex_name", lambda: "deepseek")
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [profile])
    monkeypatch.setattr(security, "get_secret", lambda ref: "sk-test" if ref == "codex:deepseek:api_key" else None)

    def fake_test_openai_api(api_key, base_url, model, timeout=10, wire_api="chat"):
        captured.update(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            wire_api=wire_api,
        )
        return TestResult(True, "ok", response_time=12)

    monkeypatch.setattr(APITester, "test_openai_api", staticmethod(fake_test_openai_api))

    ConfigValidator()._validate_api_connections()

    assert captured == {
        "api_key": "sk-test",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "timeout": 10,
        "wire_api": "responses",
    }


def main():
    check_codex_provider("deepseek", "deepseek-v4-flash", "https://api.deepseek.com", "responses", True)
    check_codex_provider("kimi", "kimi-k2.6", "https://api.moonshot.ai/v1", "responses", False)
    check_codex_provider("glm", "GLM-5.1", "https://open.bigmodel.cn/api/coding/paas/v4", "responses", False)
    check_codex_provider("openai", "gpt-5.5", "https://openai.cc/v1", "responses", True)

    check_claude_provider("deepseek", "deepseek-v4-pro", "https://api.deepseek.com/anthropic", True)
    check_claude_provider("glm", "GLM-5.1", "", False)
    test_claude_stale_fields_are_removed()
    test_malformed_config_shapes_are_repaired()
    test_stale_codex_auth_is_cleared()
    test_claude_provider_detection()
    test_codex_import_names_and_runtime_detection()

    print("OK provider config regression checks passed")


if __name__ == "__main__":
    main()
