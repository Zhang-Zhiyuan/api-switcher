import base64
import json
from types import SimpleNamespace

import pytest

from core import auth_parser, backup_manager, parser, profile_manager, security, switcher, toml_parser


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
