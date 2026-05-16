import json

import pytest

from core import atomic_io, profile_manager, security
from models.profile import BrowserProfile, ClaudeProfile, CodexProfile


@pytest.fixture()
def isolated_profile_store(tmp_path, monkeypatch):
    secret_store = {}
    deleted_refs = []

    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(security, "get_secret", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "set_secret", lambda key, value: secret_store.__setitem__(key, value or ""))

    def delete_secret(key):
        if key:
            deleted_refs.append(key)
            secret_store.pop(key, None)

    monkeypatch.setattr(security, "delete_secret", delete_secret)
    profile_manager._save_store(profile_manager._get_default_store())
    return secret_store, deleted_refs


def _store_names(key: str) -> list[str]:
    data = json.loads(profile_manager.PROFILES_FILE.read_text(encoding="utf-8"))
    return [item["name"] for item in data[key]]


def test_profile_store_replace_retries_transient_permission_error(tmp_path, monkeypatch):
    source = tmp_path / "profiles.tmp"
    target = tmp_path / "profiles.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    original_replace = type(source).replace
    attempts = {"count": 0}

    def flaky_replace(self, destination):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise PermissionError("temporarily locked")
        return original_replace(self, destination)

    monkeypatch.setattr(type(source), "replace", flaky_replace)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _seconds: None)

    atomic_io.replace_with_retry(source, target, attempts=3)

    assert attempts["count"] == 2
    assert target.read_text(encoding="utf-8") == "new"


def test_renaming_claude_profile_replaces_old_entry_and_keeps_shared_secret(isolated_profile_store):
    _secret_store, deleted_refs = isolated_profile_store
    old = ClaudeProfile(name="Old Claude", auth_token_ref="claude:old:auth_token", base_url="https://old.example")
    profile_manager.save_claude_profile(old)
    profile_manager.set_active_claude(old.name)

    renamed = ClaudeProfile(name="New Claude", auth_token_ref=old.auth_token_ref, base_url="https://new.example")
    profile_manager.save_claude_profile(renamed, previous_name=old.name)

    assert _store_names("claude_profiles") == ["New Claude"]
    assert profile_manager.get_active_claude_name() == "New Claude"
    assert old.auth_token_ref not in deleted_refs


def test_renaming_codex_profile_removes_stale_secret_ref(isolated_profile_store):
    _secret_store, deleted_refs = isolated_profile_store
    old = CodexProfile(name="Old Codex", api_key_ref="codex:old:api_key", model_provider="deepseek")
    profile_manager.save_codex_profile(old)
    profile_manager.set_active_codex(old.name)

    renamed = CodexProfile(name="New Codex", api_key_ref="codex:new:api_key", model_provider="deepseek")
    profile_manager.save_codex_profile(renamed, previous_name=old.name)

    assert _store_names("codex_profiles") == ["New Codex"]
    assert profile_manager.get_active_codex_name() == "New Codex"
    assert "codex:old:api_key" in deleted_refs


def test_renaming_browser_profile_replaces_old_entry_and_active_name(isolated_profile_store):
    old = BrowserProfile(
        name="Old Browser",
        browser_type="edge",
        profile_mode="managed",
        user_data_dir="C:/profiles/old",
    )
    profile_manager.save_browser_profile(old)
    profile_manager.set_active_browser(old.name)

    renamed = BrowserProfile(
        name="New Browser",
        browser_type="edge",
        profile_mode="managed",
        user_data_dir="C:/profiles/new",
    )
    profile_manager.save_browser_profile(renamed, previous_name=old.name)

    assert _store_names("browser_profiles") == ["New Browser"]
    assert profile_manager.get_active_browser_name() == "New Browser"
