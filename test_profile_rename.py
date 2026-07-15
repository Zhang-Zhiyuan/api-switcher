import json
import threading

import pytest

from core import atomic_io, profile_manager, security
from models.profile import BrowserProfile, ClaudeProfile, CodexProfile


@pytest.fixture()
def isolated_profile_store(tmp_path, monkeypatch):
    secret_store = {}
    deleted_refs = []

    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(security, "get_secret", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "get_secret_strict", lambda key: secret_store.get(key) if key else None)
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


def test_concurrent_profile_saves_do_not_lose_updates(
    isolated_profile_store,
    monkeypatch,
):
    first_loaded = threading.Event()
    release_first = threading.Event()
    original_load = profile_manager._load_store
    errors = []

    def delayed_load():
        store = original_load()
        if threading.current_thread().name == "save-first":
            first_loaded.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("test release timed out")
        return store

    monkeypatch.setattr(profile_manager, "_load_store", delayed_load)

    def save(name):
        try:
            profile_manager.save_codex_profile(CodexProfile(
                name=name,
                api_key_ref=f"codex:{name}:api_key",
            ))
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=lambda: save("First"), name="save-first")
    second = threading.Thread(target=lambda: save("Second"), name="save-second")
    first.start()
    assert first_loaded.wait(timeout=5)
    second.start()
    try:
        second.join(timeout=0.1)
        assert second.is_alive(), "second save bypassed the store transaction lock"
    finally:
        release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not errors
    assert not first.is_alive() and not second.is_alive()
    assert sorted(_store_names("codex_profiles")) == ["First", "Second"]


def test_committed_profile_save_does_not_report_old_secret_cleanup_as_save_failure(
    isolated_profile_store,
    monkeypatch,
    caplog,
):
    profile_manager.save_codex_profile(CodexProfile(
        name="Same",
        api_key_ref="codex:OldOwner:api_key",
        model="old",
    ))
    monkeypatch.setattr(
        security,
        "delete_secret",
        lambda _ref: (_ for _ in ()).throw(RuntimeError("backend locked")),
    )

    profile_manager.save_codex_profile(CodexProfile(
        name="Same",
        api_key_ref="codex:NewOwner:api_key",
        model="new",
    ))

    [saved] = profile_manager.list_codex_profiles()
    assert saved.model == "new"
    assert saved.api_key_ref == "codex:NewOwner:api_key"
    assert "旧密钥" in caplog.text and "清理失败" in caplog.text


def test_corrupt_cross_namespace_profile_ref_is_never_deleted(
    isolated_profile_store,
):
    secret_store, deleted_refs = isolated_profile_store
    protected_ref = "network-diagnostics:proxycheck:0"
    secret_store[protected_ref] = "keep-network-key"
    store = profile_manager._get_default_store()
    store["codex_profiles"] = [{
        "name": "Broken",
        "api_key_ref": protected_ref,
        "model": "old",
    }]
    profile_manager.PROFILES_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    profile_manager.clear_profile_store_cache()

    profile_manager.delete_codex_profile("Broken")

    assert secret_store[protected_ref] == "keep-network-key"
    assert protected_ref not in deleted_refs


def test_profile_save_rejects_cross_namespace_secret_ref(isolated_profile_store):
    with pytest.raises(ValueError, match="密钥引用不属于字段 api_key_ref"):
        profile_manager.save_codex_profile(CodexProfile(
            name="Broken",
            api_key_ref="network-diagnostics:proxycheck:0",
        ))

    assert profile_manager.list_codex_profiles() == []


def test_clone_rolls_back_new_secret_when_profile_save_fails(
    isolated_profile_store,
    monkeypatch,
):
    secret_store, _deleted_refs = isolated_profile_store
    source_ref = "codex:Source:api_key"
    clone_ref = "codex:Source-copy:api_key"
    secret_store[source_ref] = "source-key"
    profile_manager.save_codex_profile(CodexProfile(
        name="Source",
        api_key_ref=source_ref,
    ))
    monkeypatch.setattr(
        profile_manager,
        "_save_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        profile_manager.clone_codex_profile("Source")

    assert secret_store[source_ref] == "source-key"
    assert clone_ref not in secret_store


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


def test_profile_store_cache_reuses_reads_and_detects_external_write(tmp_path, monkeypatch):
    target = tmp_path / "profiles.json"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", target)
    store = profile_manager._get_default_store()
    store["claude_profiles"] = [
        ClaudeProfile(
            name="Cached Claude",
            auth_token_ref="claude:cached:auth",
            base_url="https://cached.example",
        ).to_dict()
    ]
    target.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    profile_manager.clear_profile_store_cache()

    original_read_text = type(target).read_text
    read_count = {"value": 0}

    def counting_read_text(self, *args, **kwargs):
        if self == target:
            read_count["value"] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(target), "read_text", counting_read_text)

    assert [p.name for p in profile_manager.list_claude_profiles()] == ["Cached Claude"]
    assert [p.name for p in profile_manager.list_codex_profiles()] == []
    assert read_count["value"] == 1

    store["claude_profiles"].append(
        ClaudeProfile(
            name="External Claude",
            auth_token_ref="claude:external:auth",
            base_url="https://external.example",
        ).to_dict()
    )
    target.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")

    assert [p.name for p in profile_manager.list_claude_profiles()] == [
        "Cached Claude",
        "External Claude",
    ]
    assert read_count["value"] == 2


def test_quick_switch_summary_reads_profile_store_once(isolated_profile_store, monkeypatch):
    import core.auth_parser as auth_parser
    import core.parser as claude_parser
    import core.toml_parser as toml_parser

    store = profile_manager._get_default_store()
    store["claude_profiles"] = [
        ClaudeProfile(
            name="Anthropic Official",
            auth_token_ref="claude:official:auth",
            base_url="https://api.anthropic.com",
            provider="anthropic",
        ).to_dict(),
        ClaudeProfile(
            name="Claude Router",
            auth_token_ref="claude:router:auth",
            base_url="https://router.example",
            provider="custom",
        ).to_dict(),
    ]
    store["codex_profiles"] = [
        CodexProfile(name="OpenAI Official", model_provider="openai").to_dict(),
        CodexProfile(name="Codex Router", model_provider="custom").to_dict(),
    ]
    store["active_claude_profile"] = "Claude Router"
    store["active_codex_profile"] = "Codex Router"
    profile_manager._save_store(store)
    profile_manager.clear_profile_store_cache()

    read_count = {"value": 0}
    original_load_store = profile_manager._load_store

    def counting_load_store():
        read_count["value"] += 1
        return original_load_store()

    monkeypatch.setattr(profile_manager, "_load_store", counting_load_store)
    monkeypatch.setattr(claude_parser, "read_claude_settings", lambda: {})
    monkeypatch.setattr(claude_parser, "read_claude_config", lambda: {})
    monkeypatch.setattr(toml_parser, "read_codex_config", lambda: {})
    monkeypatch.setattr(auth_parser, "read_codex_auth", lambda: {})

    summary = profile_manager.get_quick_switch_summary()

    assert summary == {
        "claude_names": ["Claude Router"],
        "claude_current": "Claude Router",
        "codex_names": ["Codex Router"],
        "codex_current": "Codex Router",
    }
    assert read_count["value"] == 1


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


def test_profile_delete_ignores_unknown_secret_ref_fields(isolated_profile_store):
    secret_store, deleted_refs = isolated_profile_store
    owned_ref = "claude:DeleteKnown:auth_token"
    unrelated_ref = "claude:Other:auth_token"
    secret_store[owned_ref] = "owned-secret"
    secret_store[unrelated_ref] = "unrelated-secret"
    store = profile_manager._load_store()
    store["claude_profiles"] = [{
        "name": "DeleteKnown",
        "auth_token_ref": owned_ref,
        "base_url": "https://delete.example",
        "surprise_ref": unrelated_ref,
    }]
    profile_manager._save_store(store)

    profile_manager.delete_claude_profile("DeleteKnown")

    assert _store_names("claude_profiles") == []
    assert owned_ref not in secret_store
    assert owned_ref in deleted_refs
    assert unrelated_ref in secret_store
    assert unrelated_ref not in deleted_refs


def test_profile_delete_save_failure_keeps_profile_and_secret(
    isolated_profile_store,
    monkeypatch,
):
    secret_store, deleted_refs = isolated_profile_store
    ref = "claude:SaveFailure:auth_token"
    secret_store[ref] = "keep-secret"
    profile_manager.save_claude_profile(ClaudeProfile(
        name="SaveFailure",
        auth_token_ref=ref,
        base_url="https://save-failure.example",
        provider="custom",
    ))
    original_save = profile_manager._save_store

    def fail_delete_save(store, *args, **kwargs):
        if not store.get("claude_profiles"):
            raise OSError("forced delete store failure")
        return original_save(store, *args, **kwargs)

    monkeypatch.setattr(profile_manager, "_save_store", fail_delete_save)

    with pytest.raises(OSError, match="forced delete store failure"):
        profile_manager.delete_claude_profile("SaveFailure")

    assert _store_names("claude_profiles") == ["SaveFailure"]
    assert secret_store[ref] == "keep-secret"
    assert ref not in deleted_refs


def test_profile_delete_secret_failure_rolls_back_profile_and_secret(
    isolated_profile_store,
    monkeypatch,
):
    secret_store, _deleted_refs = isolated_profile_store
    ref = "claude:DeleteRollback:auth_token"
    secret_store[ref] = "restore-secret"
    profile_manager.save_claude_profile(ClaudeProfile(
        name="DeleteRollback",
        auth_token_ref=ref,
        base_url="https://delete-rollback.example",
        provider="custom",
    ))
    failed_once = False

    def fail_one_delete(key):
        nonlocal failed_once
        if key == ref and not failed_once:
            failed_once = True
            secret_store.pop(key, None)
            raise OSError("forced secret delete failure")
        secret_store.pop(key, None)

    monkeypatch.setattr(security, "delete_secret", fail_one_delete)

    with pytest.raises(OSError, match="forced secret delete failure"):
        profile_manager.delete_claude_profile("DeleteRollback")

    assert _store_names("claude_profiles") == ["DeleteRollback"]
    assert secret_store[ref] == "restore-secret"


def test_profile_delete_keeps_secret_still_referenced_by_another_profile(
    isolated_profile_store,
):
    secret_store, deleted_refs = isolated_profile_store
    shared_ref = "claude:Shared:auth_token"
    secret_store[shared_ref] = "shared-secret"
    profile_manager.save_claude_profile(ClaudeProfile(
        name="SharedOne",
        auth_token_ref=shared_ref,
        base_url="https://one.example",
        provider="custom",
    ))
    profile_manager.save_claude_profile(ClaudeProfile(
        name="SharedTwo",
        auth_token_ref=shared_ref,
        base_url="https://two.example",
        provider="custom",
    ))

    profile_manager.delete_claude_profile("SharedOne")

    assert _store_names("claude_profiles") == ["SharedTwo"]
    assert secret_store[shared_ref] == "shared-secret"
    assert shared_ref not in deleted_refs
