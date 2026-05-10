"""Regression checks for password-protected portable profile migration."""
import json
import tempfile
from pathlib import Path

from config import paths
from core import portable_migration, profile_manager, security
from models.profile import BrowserProfile, ClaudeProfile, CodexProfile, SSHProfile


def _reset_store() -> None:
    profile_manager._save_store(profile_manager._get_default_store())


def _set_data_dir(data_dir: Path) -> None:
    paths.STORAGE_DIR = data_dir
    paths.PROFILES_FILE = paths.STORAGE_DIR / "profiles.json"
    paths.BACKUPS_DIR = paths.STORAGE_DIR / "backups"
    paths.SECRETS_DIR = paths.STORAGE_DIR / "secrets"
    profile_manager.PROFILES_FILE = paths.PROFILES_FILE
    security.SECRETS_DIR = paths.SECRETS_DIR
    paths.ensure_storage_dirs(migrate_legacy=False)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        unique = root.name.replace("-", "_")
        machine_a = root / "machine_a"
        machine_b = root / "machine_b"
        _set_data_dir(machine_a)

        _reset_store()

        claude_ref = f"claude:MoveMe_{unique}:auth_token"
        codex_ref = f"codex:MoveMe_{unique}:api_key"
        ssh_ref = f"ssh:Server_{unique}:password"
        security.set_secret(claude_ref, "claude-secret")
        security.set_secret(codex_ref, "codex-secret")
        security.set_secret(ssh_ref, "ssh-secret")

        profile_manager.save_claude_profile(ClaudeProfile(
            name="MoveMe",
            auth_token_ref=claude_ref,
            base_url="https://api.deepseek.com/anthropic",
            model="deepseek-v4-flash",
            provider="deepseek",
        ))
        profile_manager.save_codex_profile(CodexProfile(
            name="MoveMe",
            api_key_ref=codex_ref,
            model="deepseek-v4-flash",
            model_provider="deepseek",
            custom_base_url="https://api.deepseek.com",
        ))
        profile_manager.save_ssh_profile(SSHProfile(
            name="Server",
            host="example.com",
            password_ref=ssh_ref,
        ))

        browser_dir = paths.STORAGE_DIR / "browser_profiles" / "chrome_BrowserMoveMe"
        (browser_dir / "Default" / "Network").mkdir(parents=True)
        (browser_dir / "Default" / "Local Storage" / "leveldb").mkdir(parents=True)
        (browser_dir / "Default" / "Cache").mkdir(parents=True)
        (browser_dir / "Local State").write_text('{"os_crypt":{"encrypted_key":"fake"}}', encoding="utf-8")
        (browser_dir / "Default" / "Network" / "Cookies").write_bytes(b"cookie-db")
        (browser_dir / "Default" / "Local Storage" / "leveldb" / "chatgpt.ldb").write_bytes(b"local-storage")
        (browser_dir / "Default" / "Cache" / "cache.bin").write_bytes(b"skip-cache")
        profile_manager.save_browser_profile(BrowserProfile(
            name="BrowserMoveMe",
            browser_type="chrome",
            profile_mode="managed",
            user_data_dir=str(browser_dir),
            start_target="chatgpt",
            allow_full_reset=True,
            created_by_app=True,
        ))
        profile_manager.set_active_claude("MoveMe")
        profile_manager.set_active_codex("MoveMe")
        profile_manager.set_active_browser("BrowserMoveMe")

        bundle = root / "profiles.asxprofile"
        result = portable_migration.export_portable_profiles(bundle, "strong-password")
        assert result.profile_count == 4, result
        assert result.secret_count == 3, result
        assert result.missing_secret_refs == [], result
        assert result.browser_file_count >= 3, result
        assert result.browser_bytes > 0, result

        raw = json.loads(bundle.read_text(encoding="utf-8"))
        assert raw["format"] == portable_migration.BUNDLE_FORMAT
        assert "claude-secret" not in bundle.read_text(encoding="utf-8")
        assert "cookie-db" not in bundle.read_text(encoding="utf-8")

        _set_data_dir(machine_b)
        _reset_store()
        for ref in [claude_ref, codex_ref, ssh_ref]:
            security.delete_secret(ref)
        preexisting_browser_dir = machine_b / "browser_profiles" / "chrome_BrowserMoveMe"
        preexisting_browser_dir.mkdir(parents=True)
        (preexisting_browser_dir / "old.txt").write_text("old-browser-data", encoding="utf-8")

        imported = portable_migration.import_portable_profiles(bundle, "strong-password")
        assert imported.profile_count == 4, imported
        assert imported.secret_count == 3, imported
        assert imported.browser_file_count >= 3, imported
        assert profile_manager.get_active_claude_name() == "MoveMe"
        assert profile_manager.get_active_codex_name() == "MoveMe"
        assert profile_manager.get_active_browser_name() == "BrowserMoveMe"
        assert security.get_secret(claude_ref) == "claude-secret"
        assert security.get_secret(codex_ref) == "codex-secret"
        assert security.get_secret(ssh_ref) == "ssh-secret"
        [browser_profile] = profile_manager.list_browser_profiles()
        imported_browser_dir = Path(browser_profile.user_data_dir)
        assert imported_browser_dir.exists()
        assert imported_browser_dir != browser_dir
        assert (imported_browser_dir / "Local State").read_text(encoding="utf-8") == '{"os_crypt":{"encrypted_key":"fake"}}'
        assert (imported_browser_dir / "Default" / "Network" / "Cookies").read_bytes() == b"cookie-db"
        assert (imported_browser_dir / "Default" / "Local Storage" / "leveldb" / "chatgpt.ldb").read_bytes() == b"local-storage"
        assert not (imported_browser_dir / "Default" / "Cache" / "cache.bin").exists()
        assert not (imported_browser_dir / "old.txt").exists()
        assert not (machine_b / "browser_profiles" / "chrome_BrowserMoveMe.import_backup").exists()

        bad_json_bundle = root / "bad-json.asxprofile"
        bad_json_bundle.write_text("{", encoding="utf-8")
        try:
            portable_migration.import_portable_profiles(bad_json_bundle, "strong-password")
        except ValueError as e:
            assert "JSON" in str(e)
        else:
            raise AssertionError("Broken JSON bundle should fail")

        try:
            portable_migration.import_portable_profiles(bundle, "wrong-password")
        except ValueError as e:
            assert "迁移密码错误" in str(e)
        else:
            raise AssertionError("Wrong password should fail")

        corrupt_payload = portable_migration._decrypt_bundle(raw, "strong-password")
        for item in corrupt_payload.get("browser_data", {}).values():
            for file_entry in item.get("files", []):
                file_entry["data"] = "@@not-base64@@"
        corrupt_browser_bundle = root / "corrupt-browser.asxprofile"
        corrupt_browser_bundle.write_text(
            json.dumps(portable_migration._encrypt_payload(corrupt_payload, "strong-password"), ensure_ascii=False),
            encoding="utf-8",
        )
        machine_c = root / "machine_c"
        _set_data_dir(machine_c)
        _reset_store()
        imported_corrupt = portable_migration.import_portable_profiles(corrupt_browser_bundle, "strong-password")
        assert imported_corrupt.browser_file_count == 0
        assert imported_corrupt.skipped_browser_files
        assert profile_manager.list_browser_profiles() == []
        assert profile_manager.get_active_browser_name() is None

        for ref in [claude_ref, codex_ref, ssh_ref]:
            security.delete_secret(ref)

    print("OK portable migration checks passed")


if __name__ == "__main__":
    main()
