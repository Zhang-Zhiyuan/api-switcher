"""Regression test for profiles.json migration.

The test temporarily replaces storage/profiles.json, runs the normal loader,
asserts migration was persisted, then restores the original files.
"""
import json

from config.paths import PROFILES_FILE
from core import profile_manager


def _read_bytes(path):
    return path.read_bytes() if path.exists() else None


def _restore(path, content):
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


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
        if persisted.get("version") != 4:
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
