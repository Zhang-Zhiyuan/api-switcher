import copy
import json

import pytest

from core import auth_parser, parser, profile_manager, security, toml_parser
from models.profile import ClaudeAccountProfile, ClaudeProfile, CodexAccountProfile, CodexProfile


def _write_initial_store(tmp_path, monkeypatch, kind: str) -> tuple[dict, bytes, object]:
    profile_path = tmp_path / "profiles.json"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profile_path)

    store = profile_manager._get_default_store()
    if kind == "claude":
        old_profile = ClaudeProfile(
            name="old-claude-api",
            auth_token_ref="claude:old-claude-api:auth_token",
            base_url="https://old.invalid/anthropic",
            provider="custom",
        )
        old_account = ClaudeAccountProfile(
            name="old-claude-account",
            credentials_ref="claude-account:old-claude-account:credentials",
        )
        store["claude_profiles"] = [old_profile.to_dict()]
        store["claude_account_profiles"] = [old_account.to_dict()]
        store["active_claude_profile"] = old_profile.name
        store["active_claude_account"] = old_account.name
    else:
        old_profile = CodexProfile(
            name="old-codex-api",
            api_key_ref="codex:old-codex-api:api_key",
            model_provider="custom",
        )
        old_account = CodexAccountProfile(
            name="old-codex-account",
            auth_json_ref="codex-account:old-codex-account:auth_json",
        )
        store["codex_profiles"] = [old_profile.to_dict()]
        store["codex_account_profiles"] = [old_account.to_dict()]
        store["active_codex_profile"] = old_profile.name
        store["active_codex_account"] = old_account.name

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_bytes = json.dumps(store, indent=1, ensure_ascii=False).replace("\n", "\r\n").encode("utf-8")
    profile_path.write_bytes(profile_bytes)
    profile_manager.clear_profile_store_cache()
    return store, profile_bytes, profile_path


def _install_secrets(monkeypatch) -> dict[str, str]:
    values: dict[str, str] = {}
    monkeypatch.setattr(security, "get_secret", lambda ref: values.get(ref) if ref else None)
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: values.get(ref) if ref else None)
    monkeypatch.setattr(security, "set_secret", lambda ref, value: values.__setitem__(ref, value or ""))
    monkeypatch.setattr(security, "delete_secret", lambda ref: values.pop(ref, None) if ref else None)
    return values


def _install_current_api(monkeypatch, kind: str) -> tuple[dict, dict, str]:
    if kind == "claude":
        settings = {
            "env": {
                "ANTHROPIC_AUTH_TOKEN": "new-claude-secret",
                "ANTHROPIC_BASE_URL": "https://relay.example.test/anthropic",
                "CLAUDE_CODE_EFFORT_LEVEL": "max",
            },
            "model": "claude-test",
            "effortLevel": "high",
        }
        config = {}
        monkeypatch.setattr(parser, "read_claude_settings", lambda: copy.deepcopy(settings))
        monkeypatch.setattr(parser, "read_claude_config", lambda: copy.deepcopy(config))
        name = profile_manager._build_claude_import_name(settings, config)
        return settings, config, f"claude:{name}:auth_token"

    config = {
        "model": "codex-test",
        "model_provider": "relay",
        "model_providers": {
            "relay": {
                "name": "Relay",
                "base_url": "https://relay.example.test/v1",
                "wire_api": "responses",
            }
        },
    }
    auth = {"auth_mode": "apikey", "OPENAI_API_KEY": "new-codex-secret"}
    monkeypatch.setattr(toml_parser, "read_codex_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(auth_parser, "read_codex_auth", lambda: copy.deepcopy(auth))
    name = profile_manager._build_codex_import_name(config, auth)
    return config, auth, f"codex:{name}:api_key"


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_current_api_import_commits_profile_secret_and_active_markers_once(tmp_path, monkeypatch, kind):
    original_store, profile_bytes, profile_path = _write_initial_store(tmp_path, monkeypatch, kind)
    backup_path = profile_path.with_suffix(".backup")
    secrets = _install_secrets(monkeypatch)
    _current, _auth, secret_ref = _install_current_api(monkeypatch, kind)

    real_save_store = profile_manager._save_store
    committed = []

    def counted_save(store, create_backup=True):
        committed.append(copy.deepcopy(store))
        return real_save_store(store, create_backup=create_backup)

    monkeypatch.setattr(profile_manager, "_save_store", counted_save)
    imported = (
        profile_manager.import_current_claude()
        if kind == "claude"
        else profile_manager.import_current_codex()
    )

    assert imported is not None
    assert len(committed) == 1
    assert backup_path.read_bytes() == profile_bytes
    assert secrets[secret_ref] == f"new-{kind}-secret"
    saved = json.loads(profile_path.read_text(encoding="utf-8"))
    if kind == "claude":
        assert saved["active_claude_profile"] == imported.name
        assert saved["active_claude_account"] is None
        assert imported.effort_level == "max"
        assert {item["name"] for item in saved["claude_profiles"]} == {
            "old-claude-api",
            imported.name,
        }
    else:
        assert saved["active_codex_profile"] == imported.name
        assert saved["active_codex_account"] is None
        assert {item["name"] for item in saved["codex_profiles"]} == {
            "old-codex-api",
            imported.name,
        }
    assert original_store != saved


@pytest.mark.parametrize("kind", ["claude", "codex"])
@pytest.mark.parametrize("backup_existed", [True, False], ids=["backup-present", "backup-absent"])
def test_current_api_import_restores_old_secret_store_backup_and_active_after_commit_failure(
    tmp_path,
    monkeypatch,
    kind,
    backup_existed,
):
    original_store, profile_bytes, profile_path = _write_initial_store(tmp_path, monkeypatch, kind)
    backup_path = profile_path.with_suffix(".backup")
    old_backup = b"\x00old-profile-backup\r\n"
    if backup_existed:
        backup_path.write_bytes(old_backup)

    secrets = _install_secrets(monkeypatch)
    _current, _auth, secret_ref = _install_current_api(monkeypatch, kind)
    secrets[secret_ref] = "old-secret"
    real_save_store = profile_manager._save_store
    committed = []

    def fail_after_active_commit(store, create_backup=True):
        committed.append(copy.deepcopy(store))
        real_save_store(store, create_backup=create_backup)
        raise OSError("injected active commit failure")

    monkeypatch.setattr(profile_manager, "_save_store", fail_after_active_commit)

    importer = (
        profile_manager.import_current_claude
        if kind == "claude"
        else profile_manager.import_current_codex
    )
    with pytest.raises(OSError, match="active commit failure"):
        importer()

    assert len(committed) == 1
    assert secrets[secret_ref] == "old-secret"
    assert profile_path.read_bytes() == profile_bytes
    assert backup_path.exists() is backup_existed
    if backup_existed:
        assert backup_path.read_bytes() == old_backup
    assert json.loads(profile_path.read_text(encoding="utf-8")) == original_store


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_current_api_import_snapshots_secret_before_any_mutation(tmp_path, monkeypatch, kind):
    _original_store, profile_bytes, profile_path = _write_initial_store(tmp_path, monkeypatch, kind)
    _current, _auth, _secret_ref = _install_current_api(monkeypatch, kind)
    writes = []
    monkeypatch.setattr(security, "get_secret_strict", lambda _ref: (_ for _ in ()).throw(OSError("unreadable")))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: writes.append((ref, value)))

    importer = (
        profile_manager.import_current_claude
        if kind == "claude"
        else profile_manager.import_current_codex
    )
    with pytest.raises(OSError, match="unreadable"):
        importer()

    assert writes == []
    assert profile_path.read_bytes() == profile_bytes


@pytest.mark.parametrize("effort", ["high", "max"])
def test_claude_profile_effort_matching_follows_runtime_storage_contract(monkeypatch, effort):
    profile = ClaudeProfile(
        name="effort-test",
        auth_token_ref="claude:effort-test:auth_token",
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-test",
        effort_level=effort,
        provider="deepseek",
    )
    monkeypatch.setattr(security, "get_secret", lambda _ref: "token")
    settings = parser.apply_claude_profile({}, profile)

    assert settings["env"]["CLAUDE_CODE_EFFORT_LEVEL"] == effort
    if effort == "max":
        assert "effortLevel" not in settings
    else:
        assert settings["effortLevel"] == effort
    assert profile_manager._claude_profile_config_matches(profile, settings)

    invalid = copy.deepcopy(settings)
    if effort == "max":
        invalid["effortLevel"] = "max"
    else:
        invalid.pop("effortLevel")
    assert not profile_manager._claude_profile_config_matches(profile, invalid)
