"""Regression test for profiles.json migration.

The test temporarily replaces storage/profiles.json, runs the normal loader,
asserts migration was persisted, then restores the original files.
"""
import json
import stat

from config.paths import PROFILES_FILE
from core import atomic_io, profile_manager


def _read_bytes(path):
    return path.read_bytes() if path.exists() else None


def _restore(path, content):
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def test_atomic_copy_file_copies_source_mode_before_publish(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    target = tmp_path / "target.backup"
    source.write_bytes(b"profile data")
    source.chmod(0o600)
    expected_mode = stat.S_IMODE(source.stat().st_mode)
    real_replace = atomic_io.replace_with_retry
    published_modes = []

    def inspect_replace(temp_path, destination, *args, **kwargs):
        published_modes.append(stat.S_IMODE(temp_path.stat().st_mode))
        return real_replace(temp_path, destination, *args, **kwargs)

    monkeypatch.setattr(atomic_io, "replace_with_retry", inspect_replace)

    atomic_io.atomic_copy_file(source, target)

    assert target.read_bytes() == b"profile data"
    assert published_modes == [expected_mode]
    assert stat.S_IMODE(target.stat().st_mode) == expected_mode


def test_corrupted_profiles_restore_preserves_valid_backup(tmp_path, monkeypatch):
    profiles_file = tmp_path / "profiles.json"
    backup_file = profiles_file.with_suffix(".backup")
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)

    backup_store = {
        "version": 3,
        "claude_profiles": [
            {
                "name": "Backup Claude",
                "auth_token_ref": "claude:Backup Claude:auth_token",
                "base_url": "https://api.anthropic.com",
                "model": "claude-sonnet-4",
                "effort_level": "high",
                "permissions_mode": "default",
                "skip_dangerous_prompt": False,
            }
        ],
        "codex_profiles": [],
        "ssh_profiles": [],
        "browser_profiles": [],
        "active_claude_profile": "Backup Claude",
        "active_codex_profile": None,
        "active_ssh_profile": None,
        "active_browser_profile": None,
    }
    profiles_file.write_text("{not valid json", encoding="utf-8")
    backup_file.write_text(json.dumps(backup_store, ensure_ascii=False, indent=2), encoding="utf-8")

    restored = profile_manager._load_store()

    assert restored["active_claude_profile"] == "Backup Claude"
    assert restored["version"] == profile_manager._get_default_store()["version"]
    assert restored["claude_profiles"][0]["provider"] == "anthropic"
    assert "claude_account_profiles" in restored

    persisted = json.loads(profiles_file.read_text(encoding="utf-8"))
    backup_after = json.loads(backup_file.read_text(encoding="utf-8"))
    assert persisted["active_claude_profile"] == "Backup Claude"
    assert backup_after["active_claude_profile"] == "Backup Claude"
    assert backup_after["version"] == 3


def test_profile_backup_failure_preserves_previous_recovery_file(tmp_path, monkeypatch):
    profiles_file = tmp_path / "profiles.json"
    backup_file = profiles_file.with_suffix(".backup")
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    profile_manager.clear_profile_store_cache()

    old_store = profile_manager._get_default_store()
    new_store = profile_manager._get_default_store()
    new_store["active_claude_profile"] = None
    new_store["version"] += 1
    profiles_file.write_text(json.dumps(old_store), encoding="utf-8")
    previous_backup = b'{"known_good": true}'
    backup_file.write_bytes(previous_backup)

    real_replace = atomic_io.replace_with_retry

    def fail_backup_write(source, target, *args, **kwargs):
        if target == backup_file:
            raise OSError("forced backup commit failure")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(atomic_io, "replace_with_retry", fail_backup_write)

    profile_manager._save_store(new_store)

    assert json.loads(profiles_file.read_text(encoding="utf-8"))["version"] == new_store["version"]
    assert backup_file.read_bytes() == previous_backup
    assert not list(tmp_path.glob("profiles.backup.*.tmp"))


def test_profile_store_normalization_removes_bad_entries(tmp_path, monkeypatch):
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    store = profile_manager._get_default_store()
    store["claude_profiles"] = [
        {"name": "Valid", "provider": "anthropic", "custom_provider_name": None},
        "bad-entry",
        {"name": ""},
        {"name": "Valid", "provider": "anthropic", "custom_provider_name": None, "model": "duplicate"},
    ]
    store["codex_profiles"] = [{"missing_name": True}]
    store["active_claude_profile"] = "Valid"
    profiles_file.parent.mkdir(parents=True, exist_ok=True)
    profiles_file.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")

    loaded = profile_manager._load_store()

    assert [item["name"] for item in loaded["claude_profiles"]] == ["Valid"]
    assert loaded["codex_profiles"] == []
    assert loaded["active_claude_profile"] == "Valid"

    persisted = json.loads(profiles_file.read_text(encoding="utf-8"))
    assert [item["name"] for item in persisted["claude_profiles"]] == ["Valid"]
    assert persisted["codex_profiles"] == []


def test_profile_store_normalization_strips_names_before_deduping(tmp_path, monkeypatch):
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    store = profile_manager._get_default_store()
    store["claude_profiles"] = [
        {"name": "  Trimmed  ", "auth_token_ref": "claude:one", "base_url": ""},
        {"name": "Trimmed", "auth_token_ref": "claude:two", "base_url": ""},
    ]
    store["active_claude_profile"] = "  Trimmed  "
    profiles_file.parent.mkdir(parents=True, exist_ok=True)
    profiles_file.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")

    loaded = profile_manager._load_store()

    assert [item["name"] for item in loaded["claude_profiles"]] == ["Trimmed"]
    assert loaded["active_claude_profile"] == "Trimmed"
    persisted = json.loads(profiles_file.read_text(encoding="utf-8"))
    assert [item["name"] for item in persisted["claude_profiles"]] == ["Trimmed"]
    assert persisted["active_claude_profile"] == "Trimmed"


def test_profile_models_coerce_dirty_persisted_values(tmp_path, monkeypatch):
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    store = profile_manager._get_default_store()
    store["claude_profiles"] = [
        {
            "name": " Dirty Claude ",
            "auth_token_ref": 123,
            "base_url": None,
            "skip_dangerous_prompt": "false",
            "permissions_allow": ["Read", "", None, 42],
            "additional_directories": "not-a-list",
        }
    ]
    store["codex_profiles"] = [
        {
            "name": "Dirty Codex",
            "api_key_ref": "",
            "custom_requires_openai_auth": "yes",
            "disable_response_storage": "0",
        }
    ]
    store["ssh_profiles"] = [
        {
            "name": "Dirty SSH",
            "host": " example.com ",
            "port": "70000",
            "auth_type": "bad",
        }
    ]
    store["browser_profiles"] = [
        {
            "name": "Dirty Browser",
            "browser_type": "unknown",
            "profile_mode": "external",
            "user_data_dir": 123,
            "allow_full_reset": "on",
            "launch_width": "tiny",
            "launch_height": 99999,
        }
    ]
    profiles_file.parent.mkdir(parents=True, exist_ok=True)
    profiles_file.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")

    claude = profile_manager.list_claude_profiles()[0]
    codex = profile_manager.list_codex_profiles()[0]
    ssh = profile_manager.list_ssh_profiles()[0]
    browser = profile_manager.list_browser_profiles()[0]

    assert claude.name == "Dirty Claude"
    assert claude.auth_token_ref == ""
    assert claude.base_url == ""
    assert claude.skip_dangerous_prompt is False
    assert claude.permissions_allow == ["Read", "42"]
    assert claude.additional_directories == []
    assert codex.api_key_ref is None
    assert codex.custom_requires_openai_auth is True
    assert codex.disable_response_storage is False
    assert ssh.host == "example.com"
    assert ssh.port == 65535
    assert ssh.auth_type == "key"
    assert browser.browser_type == "chrome"
    assert browser.profile_mode == "external"
    assert browser.user_data_dir == "123"
    assert browser.allow_full_reset is True
    assert browser.launch_width == 1280
    assert browser.launch_height == 4320


def test_profile_summary_helpers_coalesce_store_loads(tmp_path, monkeypatch):
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    store = profile_manager._get_default_store()
    store["browser_profiles"] = [
        {
            "name": "Browser A",
            "browser_type": "chrome",
            "profile_mode": "managed",
            "user_data_dir": str(tmp_path / "browser"),
        }
    ]
    store["active_browser_profile"] = "Browser A"
    store["ssh_profiles"] = [
        {
            "name": "SSH A",
            "host": "example.com",
            "port": 22,
            "username": "root",
            "auth_type": "password",
        }
    ]
    store["active_ssh_profile"] = "SSH A"
    profiles_file.parent.mkdir(parents=True, exist_ok=True)
    profiles_file.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    profile_manager.clear_profile_store_cache()

    original_load_store = profile_manager._load_store
    calls = {"count": 0}

    def counting_load_store():
        calls["count"] += 1
        return original_load_store()

    monkeypatch.setattr(profile_manager, "_load_store", counting_load_store)

    browser_summary = profile_manager.get_browser_profiles_summary()

    assert calls["count"] == 1
    assert browser_summary["active"] == "Browser A"
    assert [profile.name for profile in browser_summary["profiles"]] == ["Browser A"]

    calls["count"] = 0
    ssh_summary = profile_manager.get_ssh_profiles_summary()

    assert calls["count"] == 1
    assert ssh_summary["active"] == "SSH A"
    assert [profile.name for profile in ssh_summary["profiles"]] == ["SSH A"]


def test_profile_migration_script_checks_are_collected_by_pytest(tmp_path, monkeypatch) -> None:
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr("test_profile_migration.PROFILES_FILE", profiles_file)

    main()


def main():
    backup_file = PROFILES_FILE.with_suffix(".backup")
    original_profiles = _read_bytes(PROFILES_FILE)
    original_backup = _read_bytes(backup_file)

    try:
        PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
        old_store = {
            "version": 3,
            "claude_profiles": [
                {
                    "name": "Legacy Claude",
                    "auth_token_ref": "claude:Legacy Claude:auth_token",
                    "base_url": "https://api.anthropic.com",
                    "model": "claude-sonnet-4",
                    "effort_level": "high",
                    "permissions_mode": "default",
                    "skip_dangerous_prompt": False,
                }
            ],
            "codex_profiles": [],
            "ssh_profiles": [],
            "browser_profiles": [],
            "active_claude_profile": None,
            "active_codex_profile": None,
            "active_ssh_profile": None,
            "active_browser_profile": None,
        }
        PROFILES_FILE.write_text(json.dumps(old_store, ensure_ascii=False, indent=2), encoding="utf-8")

        profiles = profile_manager.list_claude_profiles()
        if len(profiles) != 1:
            raise AssertionError(f"Expected one migrated profile, got {len(profiles)}")
        if profiles[0].provider != "anthropic":
            raise AssertionError(f"Expected provider=anthropic, got {profiles[0].provider!r}")

        persisted = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
        expected_version = profile_manager._get_default_store()["version"]
        if persisted.get("version") != expected_version:
            raise AssertionError(f"Migration was not persisted: version={persisted.get('version')!r}")
        migrated_profile = persisted["claude_profiles"][0]
        if migrated_profile.get("provider") != "anthropic":
            raise AssertionError("Migrated profile provider was not persisted")
        if "custom_provider_name" not in migrated_profile:
            raise AssertionError("Migrated profile custom_provider_name was not persisted")

        malformed_store = {
            "version": 4,
            "claude_profiles": {"bad": "shape"},
            "codex_profiles": None,
            "ssh_profiles": "bad",
            "browser_profiles": [],
            "active_claude_profile": 123,
            "active_codex_profile": "missing",
            "active_ssh_profile": None,
            "active_browser_profile": None,
        }
        PROFILES_FILE.write_text(json.dumps(malformed_store, ensure_ascii=False, indent=2), encoding="utf-8")

        if profile_manager.list_claude_profiles():
            raise AssertionError("Malformed profile list should load as empty")

        persisted = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
        for key in ("claude_profiles", "codex_profiles", "ssh_profiles", "browser_profiles"):
            if not isinstance(persisted.get(key), list):
                raise AssertionError(f"Malformed list field was not normalized: {key}")
        if persisted.get("active_claude_profile") is not None:
            raise AssertionError("Invalid active profile value was not cleared")
        if persisted.get("active_codex_profile") is not None:
            raise AssertionError("Missing active profile reference was not cleared")

        print("OK profile migration regression checks passed")
    finally:
        _restore(PROFILES_FILE, original_profiles)
        _restore(backup_file, original_backup)


if __name__ == "__main__":
    main()
