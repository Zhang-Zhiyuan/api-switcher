import json
import zipfile
from types import SimpleNamespace

import pytest

from core import backup_manager, local_config_bundle, profile_manager, security
from core.ssh_manager import ssh_manager
from models.profile import (
    BrowserProfile,
    ClaudeAccountProfile,
    ClaudeProfile,
    CodexAccountProfile,
    CodexProfile,
    SSHProfile,
)


@pytest.fixture()
def isolated_local_config(tmp_path, monkeypatch):
    secret_store: dict[str, str] = {}

    monkeypatch.setattr(security, "set_secret", lambda key, value: secret_store.__setitem__(key, value or ""))
    monkeypatch.setattr(security, "get_secret", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "delete_secret", lambda key: secret_store.pop(key, None) if key else None)
    monkeypatch.setattr(security, "set_secret_json", lambda key, data: secret_store.__setitem__(key, json.dumps(data)))
    monkeypatch.setattr(security, "get_secret_json", lambda key: json.loads(secret_store[key]) if key in secret_store else None)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(
        backup_manager,
        "create_backup",
        lambda description="": SimpleNamespace(description=description),
    )
    profile_manager._save_store(profile_manager._get_default_store())
    return secret_store


def test_local_config_zip_round_trip_restores_all_profile_types(isolated_local_config, tmp_path):
    secrets = isolated_local_config
    secret_refs = {
        "claude": "claude:All:auth_token",
        "claude_primary": "claude:All:primary_api_key",
        "codex": "codex:All:api_key",
        "claude_account": "claude-account:OfficialClaude:credentials",
        "codex_account": "codex-account:OfficialCodex:auth_json",
        "ssh_password": "ssh:Prod:password",
        "ssh_passphrase": "ssh:Prod:key_passphrase",
    }
    for name, ref in secret_refs.items():
        security.set_secret(ref, f"{name}-secret")

    profile_manager.save_claude_profile(ClaudeProfile(
        name="All",
        auth_token_ref=secret_refs["claude"],
        primary_api_key_ref=secret_refs["claude_primary"],
        base_url="https://api.example.test/anthropic",
        model="claude-sonnet-4",
        provider="custom",
        custom_provider_name="Example",
    ))
    profile_manager.save_codex_profile(CodexProfile(
        name="All",
        api_key_ref=secret_refs["codex"],
        model="example-model",
        model_provider="custom",
        custom_base_url="https://api.example.test/v1",
        custom_name="Example",
    ))
    profile_manager.save_claude_account_profile(ClaudeAccountProfile(
        name="OfficialClaude",
        credentials_ref=secret_refs["claude_account"],
        identity="claude-login-1",
    ))
    profile_manager.save_codex_account_profile(CodexAccountProfile(
        name="OfficialCodex",
        auth_json_ref=secret_refs["codex_account"],
        identity="codex-login-1",
    ))
    profile_manager.save_ssh_profile(SSHProfile(
        name="Prod",
        host="prod.example.test",
        auth_type="password",
        password_ref=secret_refs["ssh_password"],
        private_key_passphrase_ref=secret_refs["ssh_passphrase"],
    ))
    profile_manager.save_browser_profile(BrowserProfile(
        name="Browser",
        browser_type="chrome",
        profile_mode="external",
        user_data_dir=str(tmp_path / "browser"),
    ))
    profile_manager.set_active_claude("All")
    profile_manager.set_active_codex("All")
    profile_manager.set_active_claude_account("OfficialClaude")
    profile_manager.set_active_codex_account("OfficialCodex")
    profile_manager.set_active_ssh("Prod")
    profile_manager.set_active_browser("Browser")

    package = tmp_path / "local-config.zip"
    exported = local_config_bundle.export_local_config_zip(package, "strong-password")

    assert exported.profile_count == 6
    assert exported.secret_count == len(secret_refs)
    assert exported.missing_secret_refs == []
    summary = local_config_bundle.inspect_local_config_zip(package)
    assert summary.profile_count == 6
    assert summary.secret_count == len(secret_refs)
    assert summary.profile_counts["ssh_profiles"] == 1
    with zipfile.ZipFile(package, "r") as bundle:
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
    assert manifest["format"] == local_config_bundle.PACKAGE_FORMAT
    assert b"claude-secret" not in package.read_bytes()
    assert b"ssh_password-secret" not in package.read_bytes()

    profile_manager._save_store(profile_manager._get_default_store())
    secrets.clear()
    security.set_secret("codex:Keep:api_key", "keep-secret")
    security.set_secret("codex:All:old_api_key", "old-secret")
    profile_manager.save_codex_profile(CodexProfile(
        name="Keep",
        api_key_ref="codex:Keep:api_key",
        model="keep-model",
        model_provider="custom",
    ))
    profile_manager.save_codex_profile(CodexProfile(
        name="All",
        api_key_ref="codex:All:old_api_key",
        model="old-model",
        model_provider="custom",
    ))

    imported = local_config_bundle.import_local_config_zip(package, "strong-password")

    assert imported.profile_count == 6
    assert imported.secret_count == len(secret_refs)
    assert imported.backup_description == "导入完整配置 ZIP 前自动备份"
    assert profile_manager.get_active_claude_name() == "All"
    assert profile_manager.get_active_codex_name() == "All"
    assert profile_manager.get_active_claude_account_name() == "OfficialClaude"
    assert profile_manager.get_active_codex_account_name() == "OfficialCodex"
    assert profile_manager.get_active_ssh_name() == "Prod"
    assert profile_manager.get_active_browser_name() == "Browser"
    assert {profile.name for profile in profile_manager.list_codex_profiles()} == {"All", "Keep"}
    for name, ref in secret_refs.items():
        assert security.get_secret(ref) == f"{name}-secret"
    assert security.get_secret("codex:Keep:api_key") == "keep-secret"
    assert security.get_secret("codex:All:old_api_key") is None


def test_local_config_zip_disconnects_imported_ssh_profiles(isolated_local_config, tmp_path, monkeypatch):
    disconnected: list[str] = []
    monkeypatch.setattr(ssh_manager, "disconnect", lambda name: disconnected.append(name))

    security.set_secret("ssh:Prod:password", "new-password")
    profile_manager.save_ssh_profile(SSHProfile(
        name="Prod",
        host="new.example.test",
        auth_type="password",
        password_ref="ssh:Prod:password",
    ))
    package = tmp_path / "ssh.zip"
    local_config_bundle.export_local_config_zip(package, "strong-password")

    profile_manager._save_store(profile_manager._get_default_store())
    profile_manager.save_ssh_profile(SSHProfile(
        name="Prod",
        host="old.example.test",
        auth_type="password",
        password_ref="ssh:Prod:old_password",
    ))
    disconnected.clear()

    local_config_bundle.import_local_config_zip(package, "strong-password")

    assert disconnected == ["Prod"]


def test_local_config_zip_rejects_wrong_password(isolated_local_config, tmp_path):
    package = tmp_path / "local-config.zip"
    local_config_bundle.export_local_config_zip(package, "strong-password")

    with pytest.raises(ValueError, match="迁移密码错误"):
        local_config_bundle.import_local_config_zip(package, "wrong-password")


def test_local_config_zip_inspect_rejects_missing_or_unexpected_payload(isolated_local_config, tmp_path):
    missing_payload = tmp_path / "missing-payload.zip"
    with zipfile.ZipFile(missing_payload, "w") as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps({
                "format": local_config_bundle.PACKAGE_FORMAT,
                "version": local_config_bundle.PACKAGE_VERSION,
                "payload": local_config_bundle.PAYLOAD_NAME,
            }),
        )
    with pytest.raises(ValueError, match="缺少 payload"):
        local_config_bundle.inspect_local_config_zip(missing_payload)

    unexpected_payload = tmp_path / "unexpected-payload.zip"
    with zipfile.ZipFile(unexpected_payload, "w") as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps({
                "format": local_config_bundle.PACKAGE_FORMAT,
                "version": local_config_bundle.PACKAGE_VERSION,
                "payload": "other.json",
            }),
        )
        bundle.writestr("other.json", "{}")
    with pytest.raises(ValueError, match="payload 路径异常"):
        local_config_bundle.inspect_local_config_zip(unexpected_payload)


def test_local_config_zip_reports_missing_source_secrets(isolated_local_config, tmp_path):
    missing_ref = "ssh:Missing:password"
    profile_manager.save_ssh_profile(SSHProfile(
        name="Missing",
        host="missing.example.test",
        auth_type="password",
        password_ref=missing_ref,
    ))

    package = tmp_path / "missing-secret.zip"
    exported = local_config_bundle.export_local_config_zip(package, "strong-password")
    assert exported.secret_count == 0
    assert exported.missing_secret_refs == [missing_ref]

    summary = local_config_bundle.inspect_local_config_zip(package)
    assert summary.missing_secret_count == 1

    profile_manager._save_store(profile_manager._get_default_store())
    imported = local_config_bundle.import_local_config_zip(package, "strong-password")
    assert imported.profile_count == 1
    assert imported.secret_count == 0
    assert imported.skipped_secret_refs == [f"{missing_ref} (源包缺少密钥)"]
