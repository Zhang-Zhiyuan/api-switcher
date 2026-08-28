"""Regression checks for provider presets and config generation."""
import json
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


def test_codex_runtime_env_keys_follow_provider_env_key():
    profile = CodexProfile(name="deepseek", model_provider="deepseek")

    assert ProviderRegistry.get_codex_runtime_env_keys_for_profile(profile) == ["DEEPSEEK_API_KEY"]

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


def test_anthropic_provider_defaults_to_current_claude_models():
    provider = ProviderRegistry.get_provider("anthropic")

    assert provider is not None
    assert provider.default_model == "claude-opus-5"
    assert provider.claude_auth_scheme == "api_key"
    assert provider.supported_models[:6] == [
        "opus",
        "opus[1m]",
        "opusplan",
        "sonnet",
        "sonnet[1m]",
        "haiku",
    ]
    assert "claude-opus-5" in provider.supported_models
    assert "claude-fable-5" in provider.supported_models
    assert "claude-sonnet-5" in provider.supported_models


def test_provider_picker_labels_are_compact_and_legacy_names_still_resolve():
    expected = {
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "deepseek": "DeepSeek",
        "kimi": "Kimi",
        "minimax": "MiniMax",
        "qwen": "Qwen",
        "gemini": "Gemini",
        "glm": "GLM",
        "zai": "Z.AI",
        "custom": "自定义",
    }

    assert {
        provider.name: provider.display_name
        for provider in ProviderRegistry.get_all_providers()
    } == expected
    assert ProviderRegistry.get_provider_by_display_name("GLM (Zhipu/Z.ai)").name == "glm"
    assert ProviderRegistry.get_provider_by_display_name("Qwen (通义千问)").name == "qwen"
    assert ProviderRegistry.get_provider_by_display_name("Custom").name == "custom"


def test_glm_claude_presets_use_region_specific_anthropic_endpoints():
    glm = ProviderRegistry.get_provider("glm")
    zai = ProviderRegistry.get_provider("zai")

    assert glm is not None and glm.claude_supported is True
    assert zai is not None and zai.claude_supported is True
    assert glm.base_url_for_claude() == "https://open.bigmodel.cn/api/anthropic"
    assert zai.base_url_for_claude() == "https://api.z.ai/api/anthropic"
    assert glm.default_model_for_claude() == "glm-5.2"
    assert zai.default_model_for_claude() == "glm-5.1"
    assert {provider.name for provider in ProviderRegistry.get_claude_providers()} >= {"glm", "zai"}

    profile = ClaudeProfile(
        name="glm-claude",
        auth_token_ref=None,
        base_url=glm.base_url_for_claude(),
        model="glm-5.2",
        provider="glm",
    )
    settings = apply_claude_profile({"env": {}}, profile)
    env = settings["env"]

    assert settings["model"] == "opus"
    assert env["ANTHROPIC_BASE_URL"] == "https://open.bigmodel.cn/api/anthropic"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "glm-5.2"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "glm-5.2"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "glm-4.7"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "glm-5.2"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env

    profile.model = "glm-5.2[1m]"
    one_million = apply_claude_profile({"env": {}}, profile)
    assert one_million["model"] == "opus"
    assert one_million["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "glm-5.2[1m]"
    assert one_million["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"


def test_legacy_combined_glm_profiles_migrate_to_the_endpoint_region():
    domestic = ClaudeProfile.from_dict({
        "name": "domestic",
        "auth_token_ref": "ref",
        "provider": "glm",
        "base_url": "https://open.bigmodel.cn/api/anthropic",
    })
    global_profile = ClaudeProfile.from_dict({
        "name": "global",
        "auth_token_ref": "ref",
        "provider": "glm",
        "base_url": "https://api.z.ai/api/anthropic",
    })
    global_codex = CodexProfile.from_dict({
        "name": "global-codex",
        "model_provider": "glm",
        "custom_base_url": "https://api.z.ai/api/coding/paas/v4",
        "custom_name": "GLM (Zhipu/Z.ai)",
    })

    assert domestic.provider == "glm"
    assert global_profile.provider == "zai"
    assert global_codex.model_provider == "zai"
    assert global_codex.custom_name == "Z.AI"


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
    api_auth = apply_codex_apikey(
        {"auth_mode": "apikey", "OPENAI_API_KEY": "old", "tokens": {"old": True}},
        CodexProfile(name="api", model_provider="deepseek"),
    )
    assert_equal("OPENAI_API_KEY" in api_auth, False, "stale codex api key")
    assert_equal("tokens" in api_auth, True, "codex chatgpt tokens preserved")
    assert_equal(api_auth.get("auth_mode"), "chatgpt", "codex auth mode restored")
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
    assert_equal(
        detect_claude_provider(
            {"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}}
        ),
        "zai",
        "zai provider detection",
    )


def _set_codex_identity_test_paths(root: Path, monkeypatch) -> None:
    data_dir = root / "data"
    profiles_file = data_dir / "profiles.json"
    secrets_dir = data_dir / "secrets"
    codex_config = root / "codex" / "config.toml"
    codex_auth = root / "codex" / "auth.json"
    monkeypatch.setattr(paths, "STORAGE_DIR", data_dir)
    monkeypatch.setattr(paths, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(paths, "BACKUPS_DIR", data_dir / "backups")
    monkeypatch.setattr(paths, "SECRETS_DIR", secrets_dir)
    monkeypatch.setattr(paths, "CODEX_CONFIG", codex_config)
    monkeypatch.setattr(paths, "CODEX_AUTH", codex_auth)
    monkeypatch.setattr(paths, "CODEX_ENV", root / "codex" / ".env")

    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(security, "SECRETS_DIR", secrets_dir)
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", codex_config)
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", codex_auth)

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


def test_codex_import_names_and_runtime_detection(monkeypatch, tmp_path):
    secret_store: dict[str, str] = {}
    monkeypatch.setattr(
        security,
        "set_secret",
        lambda ref, value: secret_store.__setitem__(ref, value or ""),
    )
    monkeypatch.setattr(security, "get_secret", lambda ref: secret_store.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secret_store.get(ref))
    monkeypatch.setattr(security, "delete_secret", lambda ref: secret_store.pop(ref, None))
    _set_codex_identity_test_paths(tmp_path, monkeypatch)
    profile_manager.clear_profile_store_cache()
    toml_parser.clear_codex_config_cache()
    auth_parser.clear_codex_auth_cache()

    _write_codex_identity_files("key-a")
    first = profile_manager.import_current_codex()
    assert first is not None
    assert first.name != "Current"
    assert_equal(first.name, "Codex-DeepSeek-deepseek-v4-flash", "friendly codex import name")

    _write_codex_identity_files("key-a")
    assert_equal(profile_manager.get_current_codex_name(), first.name, "codex current api key match")

    _write_codex_identity_files("key-b")
    assert_equal(profile_manager.get_current_codex_name(), None, "codex different api key mismatch")
    second = profile_manager.import_current_codex()
    assert second is not None
    assert_equal(second.name, "Codex-DeepSeek-deepseek-v4-flash-2", "duplicate import name suffix")

    names = {profile.name for profile in profile_manager.list_codex_profiles()}
    if first.name not in names or second.name not in names:
        raise AssertionError(f"Expected both imported Codex profiles, got {names}")


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
    check_codex_provider("qwen", "qwen-max", "https://dashscope.aliyuncs.com/compatible-mode/v1", "responses", False)
    check_codex_provider("gemini", "gemini-2.5-pro", "https://generativelanguage.googleapis.com/v1beta/openai/", "responses", False)
    check_codex_provider("glm", "glm-5.2", "https://open.bigmodel.cn/api/coding/paas/v4", "responses", False)
    check_codex_provider("zai", "glm-5.1", "https://api.z.ai/api/coding/paas/v4", "responses", False)

    check_claude_provider("deepseek", "deepseek-v4-pro", "https://api.deepseek.com/anthropic", True)
    check_claude_provider("gemini", "gemini-2.5-pro", "https://generativelanguage.googleapis.com/v1beta/openai", False)
    test_glm_claude_presets_use_region_specific_anthropic_endpoints()
    test_claude_stale_fields_are_removed()
    test_malformed_config_shapes_are_repaired()
    test_stale_codex_auth_is_cleared()
    test_claude_provider_detection()
    # The import transaction regression uses pytest fixtures and is exercised
    # by pytest; it must not be called directly from this lightweight script.

    print("OK provider config regression checks passed")


if __name__ == "__main__":
    main()
