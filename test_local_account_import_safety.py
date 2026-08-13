import json

import pytest

from core import auth_parser, profile_manager, security, toml_parser
from models.profile import ClaudeAccountProfile, CodexAccountProfile


@pytest.fixture()
def isolated_account_import(tmp_path, monkeypatch):
    secret_store: dict[str, str] = {}

    profiles_path = tmp_path / "profiles.json"
    codex_dir = tmp_path / "codex"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_path)
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", codex_dir / "config.toml")
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", codex_dir / "auth.json")
    profile_manager.clear_profile_store_cache()
    toml_parser.clear_codex_config_cache()
    auth_parser.clear_codex_auth_cache()

    monkeypatch.setattr(
        security,
        "get_secret_strict",
        lambda key: secret_store.get(key) if key else None,
    )
    monkeypatch.setattr(
        security,
        "get_secret_json",
        lambda key: json.loads(secret_store[key]) if key in secret_store else None,
    )
    monkeypatch.setattr(
        security,
        "set_secret_json",
        lambda key, data: secret_store.__setitem__(key, json.dumps(data)),
    )
    monkeypatch.setattr(
        security,
        "set_secret",
        lambda key, value: secret_store.__setitem__(key, value),
    )
    monkeypatch.setattr(
        security,
        "delete_secret",
        lambda key: secret_store.pop(key, None) if key else None,
    )

    yield secret_store

    profile_manager.clear_profile_store_cache()
    toml_parser.clear_codex_config_cache()
    auth_parser.clear_codex_auth_cache()


def _official_auth(email: str = "account@example.test") -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "opaque-test-token"},
        "email": email,
    }


def test_keyring_store_rejects_stale_auth_file(isolated_account_import):
    toml_parser.write_codex_config({"cli_auth_credentials_store": "keyring"})
    auth_parser.write_codex_auth(_official_auth())

    with pytest.raises(ValueError, match="keyring.*无法导入"):
        profile_manager.import_current_codex_account()

    assert isolated_account_import == {}
    assert profile_manager.list_codex_account_profiles() == []


def test_file_store_imports_secret_and_profile_atomically(isolated_account_import):
    auth = _official_auth()
    toml_parser.write_codex_config({"cli_auth_credentials_store": "file"})
    auth_parser.write_codex_auth(auth)

    profile = profile_manager.import_current_codex_account()

    assert profile is not None
    assert [saved.name for saved in profile_manager.list_codex_account_profiles()] == [profile.name]
    assert security.get_secret_json(profile.auth_json_ref) == auth


def test_auto_store_does_not_trust_stale_auth_file(isolated_account_import):
    toml_parser.write_codex_config({})
    auth_parser.write_codex_auth(_official_auth())

    with pytest.raises(ValueError, match="auto.*auth.json"):
        profile_manager.import_current_codex_account()

    summary = profile_manager.get_codex_account_runtime_summary()
    assert summary["credentials_store"] == "auto"
    assert summary["has_auth"] is False
    assert isolated_account_import == {}


def test_codex_account_save_failure_restores_previous_secret(
    isolated_account_import,
    monkeypatch,
):
    ref = "codex-account:existing:auth_json"
    isolated_account_import[ref] = "previous-secret"
    profile = CodexAccountProfile(name="existing", auth_json_ref=ref)
    monkeypatch.setattr(
        profile_manager,
        "save_codex_account_profile",
        lambda _profile: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        profile_manager.save_codex_account_profile_with_auth(profile, _official_auth())

    assert isolated_account_import[ref] == "previous-secret"
    assert profile_manager.list_codex_account_profiles() == []


def test_claude_account_save_failure_restores_previous_secret(
    isolated_account_import,
    monkeypatch,
):
    ref = "claude-account:existing:credentials"
    isolated_account_import[ref] = "previous-secret"
    profile = ClaudeAccountProfile(name="existing", credentials_ref=ref)
    monkeypatch.setattr(
        profile_manager,
        "save_claude_account_profile",
        lambda _profile: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        profile_manager.save_claude_account_profile_with_credentials(
            profile,
            {"token": "opaque-test-token"},
        )

    assert isolated_account_import[ref] == "previous-secret"
    assert profile_manager.list_claude_account_profiles() == []
