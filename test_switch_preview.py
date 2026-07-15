import json
from types import SimpleNamespace

import pytest

from config import paths
from core import auth_parser, backup_manager, parser, profile_manager, security, switch_preview, switcher, toml_parser, vscode_parser
from models.profile import ClaudeAccountProfile, CodexAccountProfile, CodexProfile


@pytest.fixture()
def isolated_preview(tmp_path, monkeypatch):
    secret_store: dict[str, str] = {}

    monkeypatch.setattr(security, "set_secret", lambda key, value: secret_store.__setitem__(key, value or ""))
    monkeypatch.setattr(security, "get_secret", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "delete_secret", lambda key: secret_store.pop(key, None) if key else None)
    monkeypatch.setattr(security, "set_secret_json", lambda key, data: secret_store.__setitem__(key, json.dumps(data)))
    monkeypatch.setattr(security, "get_secret_json", lambda key: json.loads(secret_store[key]) if key in secret_store else None)
    monkeypatch.setattr(backup_manager, "create_backup", lambda description="": SimpleNamespace(description=description))

    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(parser, "CLAUDE_SETTINGS", tmp_path / "claude" / "settings.json")
    monkeypatch.setattr(parser, "CLAUDE_CONFIG", tmp_path / "claude" / "config.json")
    monkeypatch.setattr(parser, "CLAUDE_CREDENTIALS", tmp_path / "claude" / ".credentials.json")
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", tmp_path / "codex" / "auth.json")
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", tmp_path / "codex" / "config.toml")
    monkeypatch.setattr(paths, "CODEX_ENV", tmp_path / "codex" / ".env")
    monkeypatch.setattr(vscode_parser, "VSCODE_SETTINGS", tmp_path / "vscode" / "settings.json")

    return secret_store


def test_codex_api_preview_preserves_official_auth(isolated_preview):
    security.set_secret("codex:relay:api_key", "sk-relay")
    profile_manager.save_codex_profile(
        CodexProfile(
            name="Relay",
            api_key_ref="codex:relay:api_key",
            model="relay-model",
            model_provider="custom",
            custom_base_url="https://relay.example.com/v1",
        )
    )
    auth_parser.write_codex_auth({"auth_mode": "chatgpt", "tokens": {"id_token": "token"}})

    preview = switch_preview.build_switch_preview("codex_api", "Relay")

    assert preview.can_proceed
    assert any(check.status == "ok" and "官方" in check.item for check in preview.checks)
    assert any(change.label == "认证模式" and "env_key=" in change.after for change in preview.changes)


def test_preview_blocks_missing_codex_api_key(isolated_preview):
    profile_manager.save_codex_profile(
        CodexProfile(
            name="Missing Key",
            api_key_ref="codex:missing:api_key",
            model="relay-model",
            model_provider="custom",
            custom_base_url="https://relay.example.com/v1",
        )
    )

    preview = switch_preview.build_switch_preview("codex_api", "Missing Key")

    assert not preview.can_proceed
    assert any(check.status == "error" and check.item == "API Key" for check in preview.checks)


def test_account_preview_blocks_missing_snapshot(isolated_preview):
    profile_manager.save_claude_account_profile(
        ClaudeAccountProfile(
            name="Broken Claude",
            credentials_ref="claude-account:Broken Claude:credentials",
            identity="broken",
        )
    )
    profile_manager.save_codex_account_profile(
        CodexAccountProfile(
            name="Broken Codex",
            auth_json_ref="codex-account:Broken Codex:auth_json",
            identity="broken",
        )
    )

    claude_preview = switch_preview.build_switch_preview("claude_account", "Broken Claude")
    codex_preview = switch_preview.build_switch_preview("codex_account", "Broken Codex")

    assert not claude_preview.can_proceed
    assert not codex_preview.can_proceed
    assert any(check.status == "error" for check in claude_preview.checks)
    assert any(check.status == "error" for check in codex_preview.checks)


def test_static_health_collects_saved_profile_issues(isolated_preview):
    profile_manager.save_codex_profile(
        CodexProfile(
            name="Bad Relay",
            api_key_ref="codex:bad:api_key",
            model="",
            model_provider="custom",
            custom_base_url="not-a-url",
        )
    )

    checks = switch_preview.collect_static_health_checks("codex")

    assert any(check.status == "error" and "Bad Relay" in check.item for check in checks)


def test_direct_switch_rejects_invalid_target_before_backup_or_write(isolated_preview, monkeypatch):
    security.set_secret("codex:bad-direct:api_key", "sk-test")
    profile_manager.save_codex_profile(
        CodexProfile(
            name="Bad Direct",
            api_key_ref="codex:bad-direct:api_key",
            model="",
            model_provider="custom",
            custom_base_url="not-a-url",
        )
    )
    backup_calls: list[str] = []
    monkeypatch.setattr(backup_manager, "create_backup", lambda description="": backup_calls.append(description))

    with pytest.raises(ValueError, match="配置健康检查未通过"):
        switcher.switch_codex_profile("Bad Direct")

    assert backup_calls == []
    assert not toml_parser.CODEX_CONFIG.exists()


def test_openai_auth_provider_is_blocked_without_official_tokens(isolated_preview, monkeypatch):
    profile_manager.save_codex_profile(
        CodexProfile(
            name="Needs Login",
            model="gpt-test",
            model_provider="custom",
            custom_base_url="https://api.example.test/v1",
            custom_requires_openai_auth=True,
        )
    )
    backup_calls: list[str] = []
    monkeypatch.setattr(backup_manager, "create_backup", lambda description="": backup_calls.append(description))

    preview = switch_preview.build_codex_api_preview("Needs Login")

    assert not preview.can_proceed
    assert any(check.status == "error" and check.item == "OpenAI 认证" for check in preview.checks)
    with pytest.raises(ValueError, match="OpenAI 认证"):
        switcher.switch_codex_profile("Needs Login")
    assert backup_calls == []
