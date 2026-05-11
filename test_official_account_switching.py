import base64
import json
from types import SimpleNamespace

import pytest

from core import auth_parser, backup_manager, parser, profile_manager, security, switcher, toml_parser
from core.providers import ProviderRegistry


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"header.{body.rstrip('=')}.sig"


@pytest.fixture()
def isolated_accounts(tmp_path, monkeypatch):
    secret_store: dict[str, str] = {}

    monkeypatch.setattr(security, "set_secret", lambda key, value: secret_store.__setitem__(key, value or ""))
    monkeypatch.setattr(security, "get_secret", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "delete_secret", lambda key: secret_store.pop(key, None) if key else None)
    monkeypatch.setattr(security, "set_secret_json", lambda key, data: secret_store.__setitem__(key, json.dumps(data)))
    monkeypatch.setattr(security, "get_secret_json", lambda key: json.loads(secret_store[key]) if key in secret_store else None)
    monkeypatch.setattr(backup_manager, "create_backup", lambda description="": SimpleNamespace(description=description))

    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(profile_manager, "CLAUDE_CREDENTIALS", tmp_path / "claude" / ".credentials.json")
    monkeypatch.setattr(parser, "CLAUDE_CREDENTIALS", profile_manager.CLAUDE_CREDENTIALS)
    monkeypatch.setattr(parser, "CLAUDE_SETTINGS", tmp_path / "claude" / "settings.json")
    monkeypatch.setattr(parser, "CLAUDE_CONFIG", tmp_path / "claude" / "config.json")
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", tmp_path / "codex" / "auth.json")
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", tmp_path / "codex" / "config.toml")

    return tmp_path


def test_switch_claude_account_clears_api_overrides_and_resets_third_party_model(isolated_accounts):
    parser.CLAUDE_CREDENTIALS.parent.mkdir(parents=True)
    parser.CLAUDE_CREDENTIALS.write_text(
        json.dumps({"token": _jwt({"email": "claude@example.test"})}),
        encoding="utf-8",
    )
    parser.write_claude_settings({
        "env": {
            "ANTHROPIC_AUTH_TOKEN": "third-party",
            "ANTHROPIC_BASE_URL": "https://example.test/anthropic",
        },
        "model": "GLM-5.1",
        "effortLevel": "invalid",
    })
    parser.write_claude_config({"primaryApiKey": "third-party"})

    account = profile_manager.import_current_claude_account()
    assert account is not None
    profile_manager.save_claude_account_profile(account)

    switcher.switch_claude_account(account.name)

    settings = parser.read_claude_settings()
    assert settings.get("env") is None
    assert settings["model"] == "claude-sonnet-4"
    assert settings["effortLevel"] == "high"
    assert "primaryApiKey" not in parser.read_claude_config()
    assert profile_manager.get_current_claude_account_name() == account.name


def test_switch_claude_account_preserves_official_model_aliases(isolated_accounts):
    for model in ["opus[1m]", "sonnet[1m]", "opusplan", "claude-opus-4-7[1m]"]:
        settings = parser.clear_claude_api_overrides({"model": model, "env": {}})
        assert settings["model"] == model

    settings = parser.clear_claude_api_overrides({"model": "gpt-5.5", "env": {}})
    assert settings["model"] == "claude-sonnet-4"


def test_anthropic_presets_include_opus_1m_alias():
    provider = ProviderRegistry.get_provider("anthropic")
    assert provider is not None
    assert "opus[1m]" in provider.supported_models
    assert provider.supported_models.index("opus[1m]") < provider.supported_models.index("opus")


def test_switch_codex_account_normalizes_mixed_auth_and_provider(isolated_accounts):
    auth_parser.write_codex_auth({
        "auth_mode": "api_key",
        "OPENAI_API_KEY": "third-party",
        "tokens": {"id_token": _jwt({"email": "codex@example.test"})},
    })
    toml_parser.write_codex_config({"model_provider": "deepseek", "model": "deepseek-v4-flash"})

    account = profile_manager.import_current_codex_account()
    assert account is not None
    profile_manager.save_codex_account_profile(account)
    assert profile_manager.get_current_codex_account_name() is None

    switcher.switch_codex_account(account.name)

    auth = auth_parser.read_codex_auth()
    assert auth["auth_mode"] == "chatgpt"
    assert "OPENAI_API_KEY" not in auth
    config = toml_parser.read_codex_config()
    assert config["model_provider"] == "openai"
    assert config["model"] == "gpt-5.5"
    assert config["cli_auth_credentials_store"] == "file"
    assert profile_manager.get_current_codex_account_name() == account.name


def test_invalid_account_snapshot_is_reported(isolated_accounts):
    account = profile_manager.import_current_codex_account()
    assert account is None

    from models.profile import CodexAccountProfile

    broken = CodexAccountProfile(name="broken", auth_json_ref="missing-ref")
    profile_manager.save_codex_account_profile(broken)

    ok, reason = profile_manager.validate_codex_account_snapshot(broken)
    assert not ok
    assert "不可读取" in reason
    with pytest.raises(ValueError, match="不可读取"):
        switcher.switch_codex_account("broken")


def test_import_current_api_configs_use_station_names(isolated_accounts):
    parser.write_claude_settings({
        "env": {
            "ANTHROPIC_AUTH_TOKEN": "relay-token",
            "ANTHROPIC_BASE_URL": "https://relay.example.com/anthropic",
        },
        "model": "claude-sonnet-4",
    })
    claude_profile = profile_manager.import_current_claude()
    assert claude_profile is not None
    assert claude_profile.name == "Claude-relay.example.com-claude-sonnet-4"
    assert claude_profile.provider == "custom"

    toml_parser.write_codex_config({
        "model": "gpt-5.5",
        "model_provider": "my-relay",
        "model_providers": {
            "my-relay": {
                "name": "KiloCode 中转",
                "base_url": "https://relay.example.com/v1",
            }
        },
    })
    auth_parser.write_codex_auth({"auth_mode": "api_key", "OPENAI_API_KEY": "relay-key"})
    codex_profile = profile_manager.import_current_codex()
    assert codex_profile is not None
    assert codex_profile.name == "Codex-KiloCode-中转-gpt-5.5"


def test_import_current_accounts_use_human_identity_names(isolated_accounts):
    parser.CLAUDE_CREDENTIALS.parent.mkdir(parents=True)
    parser.CLAUDE_CREDENTIALS.write_text(
        json.dumps({"token": _jwt({"name": "张三 Claude", "email": "zhang@example.test"})}),
        encoding="utf-8",
    )
    claude_account = profile_manager.import_current_claude_account()
    assert claude_account is not None
    assert claude_account.identity == "zhang@example.test"
    assert claude_account.name == "Claude-账号-张三-Claude"

    auth_parser.write_codex_auth({
        "auth_mode": "chatgpt",
        "tokens": {"id_token": _jwt({"preferred_username": "zzy", "email": "zzy@example.test"})},
    })
    codex_account = profile_manager.import_current_codex_account()
    assert codex_account is not None
    assert codex_account.identity == "zzy@example.test"
    assert codex_account.name == "Codex-账号-zzy"


def test_account_matching_accepts_legacy_display_identity(isolated_accounts):
    from models.profile import ClaudeAccountProfile

    parser.CLAUDE_CREDENTIALS.parent.mkdir(parents=True)
    credentials = {"token": _jwt({"name": "Same Display", "email": "same@example.test"})}
    parser.CLAUDE_CREDENTIALS.write_text(json.dumps(credentials), encoding="utf-8")
    security.set_secret_json("legacy:claude", credentials)
    profile_manager.save_claude_account_profile(
        ClaudeAccountProfile(name="Legacy Display", credentials_ref="legacy:claude", identity="Same Display")
    )

    assert profile_manager.import_current_claude_account().name == "Legacy Display"
    assert profile_manager.get_current_claude_account_name() == "Legacy Display"


def test_account_matching_rejects_same_display_with_different_stable_identity(isolated_accounts):
    from models.profile import ClaudeAccountProfile

    parser.CLAUDE_CREDENTIALS.parent.mkdir(parents=True)
    current_credentials = {"token": _jwt({"name": "Same Display", "email": "current@example.test"})}
    legacy_credentials = {"token": _jwt({"name": "Same Display", "email": "legacy@example.test"})}
    parser.CLAUDE_CREDENTIALS.write_text(json.dumps(current_credentials), encoding="utf-8")
    security.set_secret_json("legacy:claude", legacy_credentials)
    profile_manager.save_claude_account_profile(
        ClaudeAccountProfile(name="Legacy Display", credentials_ref="legacy:claude", identity="Same Display")
    )

    imported = profile_manager.import_current_claude_account()
    assert imported is not None
    assert imported.name == "Claude-账号-Same-Display"
    profile_manager.save_claude_account_profile(imported)
    assert profile_manager.get_current_claude_account_name() == imported.name


def test_import_names_handle_generic_labels_and_unsafe_values(isolated_accounts):
    parser.write_claude_settings({
        "env": {
            "ANTHROPIC_AUTH_TOKEN": "relay-token",
            "ANTHROPIC_BASE_URL": "api.relay.example.com/anthropic",
        },
        "model": "bad/model name with spaces",
    })
    claude_profile = profile_manager.import_current_claude()
    assert claude_profile is not None
    assert claude_profile.name == "Claude-api.relay.example.com-bad-model-name-with-spaces"

    toml_parser.write_codex_config({
        "model": "model/with spaces",
        "model_provider": "custom",
        "model_providers": {
            "custom": {
                "name": "OpenAI Compatible",
                "base_url": "https://api.codex-relay.example.com/v1",
            }
        },
    })
    auth_parser.write_codex_auth({"auth_mode": "api_key", "OPENAI_API_KEY": "relay-key"})
    codex_profile = profile_manager.import_current_codex()
    assert codex_profile is not None
    assert codex_profile.name == "Codex-api.codex-relay.example.com-model-with-spaces"
