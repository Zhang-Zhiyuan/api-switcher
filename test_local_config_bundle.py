import json
import threading
import warnings
import zipfile
import zlib
from types import SimpleNamespace

import pytest

from core import backup_manager, local_config_bundle, network_diagnostic_settings, profile_manager, security
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
    monkeypatch.setattr(security, "get_secret_strict", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "delete_secret", lambda key: secret_store.pop(key, None) if key else None)
    monkeypatch.setattr(security, "set_secret_json", lambda key, data: secret_store.__setitem__(key, json.dumps(data)))
    monkeypatch.setattr(security, "get_secret_json", lambda key: json.loads(secret_store[key]) if key in secret_store else None)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", tmp_path / "network_diagnostics.json")
    monkeypatch.setattr(
        backup_manager,
        "create_backup",
        lambda description="": SimpleNamespace(description=description),
    )
    profile_manager._save_store(profile_manager._get_default_store())
    return secret_store


def _write_encrypted_local_config_package(path, payload, password="strong-password"):
    manifest = {
        "format": local_config_bundle.PACKAGE_FORMAT,
        "version": local_config_bundle.PACKAGE_VERSION,
        "payload": local_config_bundle.PAYLOAD_NAME,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(local_config_bundle.MANIFEST_NAME, json.dumps(manifest))
        bundle.writestr(
            local_config_bundle.PAYLOAD_NAME,
            json.dumps(local_config_bundle._encrypt_payload(payload, password)),
        )


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
    security.set_secret("codex:AllOld:api_key", "old-secret")
    profile_manager.save_codex_profile(CodexProfile(
        name="Keep",
        api_key_ref="codex:Keep:api_key",
        model="keep-model",
        model_provider="custom",
    ))
    profile_manager.save_codex_profile(CodexProfile(
        name="All",
        api_key_ref="codex:AllOld:api_key",
        model="old-model",
        model_provider="custom",
    ))

    imported = local_config_bundle.import_local_config_zip(package, "strong-password")

    assert imported.profile_count == 6
    assert imported.secret_count == len(secret_refs)
    assert imported.backup_description == "导入 ZIP 前客户端运行配置备份"
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
    assert security.get_secret("codex:AllOld:api_key") is None


def test_local_config_import_does_not_overwrite_unreferenced_secret(
    isolated_local_config,
    tmp_path,
):
    secret_store = isolated_local_config
    imported_ref = "codex:Imported:api_key"
    unrelated_ref = "codex:Existing:api_key"

    profile_manager.save_codex_profile(CodexProfile(
        name="Existing",
        api_key_ref=unrelated_ref,
        model="existing-model",
        model_provider="custom",
    ))
    security.set_secret(unrelated_ref, "keep-existing-secret")

    imported_store = profile_manager._get_default_store()
    imported_store["codex_profiles"] = [CodexProfile(
        name="Imported",
        api_key_ref=imported_ref,
        model="imported-model",
        model_provider="custom",
    ).to_dict()]
    payload = {
        "payload_version": local_config_bundle.PAYLOAD_VERSION,
        "store": imported_store,
        "network_diagnostics": {},
        "secrets": {
            imported_ref: "imported-secret",
            unrelated_ref: "attacker-controlled-secret",
        },
    }
    package = tmp_path / "unreferenced-secret.zip"
    manifest = {
        "format": local_config_bundle.PACKAGE_FORMAT,
        "version": local_config_bundle.PACKAGE_VERSION,
        "payload": local_config_bundle.PAYLOAD_NAME,
    }
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(local_config_bundle.MANIFEST_NAME, json.dumps(manifest))
        bundle.writestr(
            local_config_bundle.PAYLOAD_NAME,
            json.dumps(local_config_bundle._encrypt_payload(payload, "strong-password")),
        )

    result = local_config_bundle.import_local_config_zip(package, "strong-password")

    assert result.secret_count == 1
    assert f"{unrelated_ref} (未被导入配置引用)" in result.skipped_secret_refs
    assert secret_store[imported_ref] == "imported-secret"
    assert secret_store[unrelated_ref] == "keep-existing-secret"


def test_local_config_import_rejects_ref_owned_by_unreplaced_profile(
    isolated_local_config,
    tmp_path,
):
    secret_store = isolated_local_config
    shared_ref = "codex:Existing:api_key"
    profile_manager.save_codex_profile(CodexProfile(
        name="Existing",
        api_key_ref=shared_ref,
        model="existing-model",
        model_provider="custom",
    ))
    security.set_secret(shared_ref, "keep-existing-secret")

    imported_store = profile_manager._get_default_store()
    imported_store["codex_profiles"] = [CodexProfile(
        name="Imported",
        api_key_ref=shared_ref,
        model="imported-model",
        model_provider="custom",
    ).to_dict()]
    payload = {
        "payload_version": local_config_bundle.PAYLOAD_VERSION,
        "store": imported_store,
        "network_diagnostics": {},
        "secrets": {shared_ref: "attacker-controlled-secret"},
    }
    package = tmp_path / "conflicting-secret-ref.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(local_config_bundle.MANIFEST_NAME, json.dumps({
            "format": local_config_bundle.PACKAGE_FORMAT,
            "version": local_config_bundle.PACKAGE_VERSION,
            "payload": local_config_bundle.PAYLOAD_NAME,
        }))
        bundle.writestr(
            local_config_bundle.PAYLOAD_NAME,
            json.dumps(local_config_bundle._encrypt_payload(payload, "strong-password")),
        )

    with pytest.raises(ValueError, match="密钥引用与未替换的现有配置冲突"):
        local_config_bundle.import_local_config_zip(package, "strong-password")

    assert secret_store[shared_ref] == "keep-existing-secret"
    assert {profile.name for profile in profile_manager.list_codex_profiles()} == {"Existing"}


def test_local_config_import_allows_same_name_profile_to_replace_its_secret(
    isolated_local_config,
    tmp_path,
):
    secret_store = isolated_local_config
    shared_ref = "codex:Same:api_key"
    profile_manager.save_codex_profile(CodexProfile(
        name="Same",
        api_key_ref=shared_ref,
        model="old-model",
        model_provider="custom",
    ))
    security.set_secret(shared_ref, "old-secret")

    imported_store = profile_manager._get_default_store()
    imported_store["codex_profiles"] = [CodexProfile(
        name="Same",
        api_key_ref=shared_ref,
        model="new-model",
        model_provider="custom",
    ).to_dict()]
    payload = {
        "payload_version": local_config_bundle.PAYLOAD_VERSION,
        "store": imported_store,
        "network_diagnostics": {},
        "secrets": {shared_ref: "new-secret"},
    }
    package = tmp_path / "same-name-secret-ref.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(local_config_bundle.MANIFEST_NAME, json.dumps({
            "format": local_config_bundle.PACKAGE_FORMAT,
            "version": local_config_bundle.PACKAGE_VERSION,
            "payload": local_config_bundle.PAYLOAD_NAME,
        }))
        bundle.writestr(
            local_config_bundle.PAYLOAD_NAME,
            json.dumps(local_config_bundle._encrypt_payload(payload, "strong-password")),
        )

    result = local_config_bundle.import_local_config_zip(package, "strong-password")

    assert result.secret_count == 1
    assert secret_store[shared_ref] == "new-secret"
    assert profile_manager.list_codex_profiles()[0].model == "new-model"


def test_local_config_zip_includes_network_diagnostic_key_pool(isolated_local_config, tmp_path):
    package = tmp_path / "local-config.zip"
    settings = network_diagnostic_settings.settings_from_values(
        {
            network_diagnostic_settings.SERVICE_PROXYCHECK,
            network_diagnostic_settings.SERVICE_VPNAPI,
        },
        {
            network_diagnostic_settings.SERVICE_PROXYCHECK: "proxy-a, proxy-b",
            network_diagnostic_settings.SERVICE_VPNAPI: "vpn-a",
        },
    )
    network_diagnostic_settings.save_settings(settings)

    exported = local_config_bundle.export_local_config_zip(package, "strong-password")

    assert exported.secret_count == 3
    assert exported.missing_secret_refs == []

    network_diagnostic_settings.SETTINGS_FILE.unlink()
    isolated_local_config.clear()

    imported = local_config_bundle.import_local_config_zip(package, "strong-password")
    loaded = network_diagnostic_settings.load_settings()

    assert imported.secret_count == 3
    assert loaded.enabled_services() == [
        network_diagnostic_settings.SERVICE_PROXYCHECK,
        network_diagnostic_settings.SERVICE_VPNAPI,
    ]
    assert loaded.keys_for(network_diagnostic_settings.SERVICE_PROXYCHECK) == ["proxy-a", "proxy-b"]
    assert loaded.keys_for(network_diagnostic_settings.SERVICE_VPNAPI) == ["vpn-a"]


def test_local_config_zip_without_network_settings_keeps_existing_diagnostics(isolated_local_config, tmp_path):
    package = tmp_path / "local-config-without-network.zip"
    local_config_bundle.export_local_config_zip(package, "strong-password")

    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_IPQS},
        {network_diagnostic_settings.SERVICE_IPQS: "current-ipqs-key"},
    )
    network_diagnostic_settings.save_settings(settings)

    local_config_bundle.import_local_config_zip(package, "strong-password")
    loaded = network_diagnostic_settings.load_settings()

    assert loaded.enabled_services() == []
    assert loaded.enabled_services(include_hidden=True) == [network_diagnostic_settings.SERVICE_IPQS]
    assert loaded.keys_for(network_diagnostic_settings.SERVICE_IPQS) == ["current-ipqs-key"]


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
        password_ref="ssh:ProdOld:password",
    ))
    disconnected.clear()

    local_config_bundle.import_local_config_zip(package, "strong-password")

    assert disconnected == ["Prod"]


def test_local_config_zip_disconnects_remaining_ssh_profiles_after_one_failure(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    disconnected: list[str] = []

    def disconnect(name: str) -> None:
        disconnected.append(name)
        if name == "A":
            raise RuntimeError("stale client")

    monkeypatch.setattr(ssh_manager, "disconnect", disconnect)
    for name in ["A", "B"]:
        security.set_secret(f"ssh:{name}:password", f"{name}-password")
        profile_manager.save_ssh_profile(SSHProfile(
            name=name,
            host=f"{name.lower()}.example.test",
            auth_type="password",
            password_ref=f"ssh:{name}:password",
        ))

    package = tmp_path / "ssh-multi.zip"
    local_config_bundle.export_local_config_zip(package, "strong-password")
    profile_manager._save_store(profile_manager._get_default_store())
    disconnected.clear()

    local_config_bundle.import_local_config_zip(package, "strong-password")

    assert disconnected == ["A", "B"]


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


def test_local_config_zip_rejects_duplicate_critical_entries(isolated_local_config, tmp_path):
    package = tmp_path / "duplicate.zip"
    manifest = json.dumps({
        "format": local_config_bundle.PACKAGE_FORMAT,
        "version": local_config_bundle.PACKAGE_VERSION,
        "payload": local_config_bundle.PAYLOAD_NAME,
    })
    with zipfile.ZipFile(package, "w") as bundle:
        bundle.writestr("manifest.json", manifest)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            bundle.writestr("manifest.json", manifest)
        bundle.writestr(local_config_bundle.PAYLOAD_NAME, "{}")

    with pytest.raises(ValueError, match="重复关键条目"):
        local_config_bundle.inspect_local_config_zip(package)


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
    assert imported.skipped_secret_refs == [
        f"{missing_ref} (源包缺少密钥，已清除本机旧值)"
    ]


def test_local_config_import_computes_missing_secret_and_clears_same_name_old_value(
    isolated_local_config,
    tmp_path,
):
    secrets = isolated_local_config
    shared_ref = "codex:SameMissing:api_key"
    security.set_secret(shared_ref, "old-secret-that-must-not-survive")
    profile_manager.save_codex_profile(CodexProfile(
        name="SameMissing",
        api_key_ref=shared_ref,
        model="old-model",
        model_provider="custom",
    ))

    imported_store = profile_manager._get_default_store()
    imported_store["codex_profiles"] = [CodexProfile(
        name="SameMissing",
        api_key_ref=shared_ref,
        model="new-model",
        model_provider="custom",
    ).to_dict()]
    package = tmp_path / "computed-missing-secret.zip"
    _write_encrypted_local_config_package(package, {
        "payload_version": local_config_bundle.PAYLOAD_VERSION,
        "store": imported_store,
        "network_diagnostics": {},
        # Deliberately omit missing_secret_refs: the importer must derive it.
        "secrets": {},
    })

    result = local_config_bundle.import_local_config_zip(package, "strong-password")

    assert shared_ref not in secrets
    assert result.skipped_secret_refs == [
        f"{shared_ref} (源包缺少密钥，已清除本机旧值)"
    ]
    assert profile_manager.list_codex_profiles()[0].model == "new-model"


def test_local_config_missing_secret_clear_rolls_back_after_profile_save_failure(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    secrets = isolated_local_config
    shared_ref = "claude:MissingRollback:auth_token"
    security.set_secret(shared_ref, "old-secret")
    profile_manager.save_claude_profile(ClaudeProfile(
        name="MissingRollback",
        auth_token_ref=shared_ref,
        base_url="https://old.example.test",
        model="old-model",
        provider="custom",
    ))

    imported_store = profile_manager._get_default_store()
    imported_store["claude_profiles"] = [ClaudeProfile(
        name="MissingRollback",
        auth_token_ref=shared_ref,
        base_url="https://new.example.test",
        model="new-model",
        provider="custom",
    ).to_dict()]
    package = tmp_path / "computed-missing-rollback.zip"
    _write_encrypted_local_config_package(package, {
        "payload_version": local_config_bundle.PAYLOAD_VERSION,
        "store": imported_store,
        "network_diagnostics": {},
        "secrets": {},
    })

    original_save = profile_manager._save_store

    def fail_imported_save(store, *args, **kwargs):
        profiles = store.get("claude_profiles", [])
        if any(item.get("model") == "new-model" for item in profiles):
            raise OSError("forced missing-secret save failure")
        return original_save(store, *args, **kwargs)

    monkeypatch.setattr(profile_manager, "_save_store", fail_imported_save)

    with pytest.raises(OSError, match="forced missing-secret save failure"):
        local_config_bundle.import_local_config_zip(package, "strong-password")

    assert secrets[shared_ref] == "old-secret"
    profile_manager.clear_profile_store_cache()
    assert profile_manager.list_claude_profiles()[0].model == "old-model"


def test_local_config_import_rejects_missing_secret_collision_with_unowned_value(
    isolated_local_config,
    tmp_path,
):
    secrets = isolated_local_config
    unowned_ref = "codex:UnownedCollision:api_key"
    secrets[unowned_ref] = "unrelated-local-secret"
    imported_store = profile_manager._get_default_store()
    imported_store["codex_profiles"] = [CodexProfile(
        name="Imported",
        api_key_ref=unowned_ref,
        model="new-model",
        model_provider="custom",
    ).to_dict()]
    package = tmp_path / "unowned-missing-collision.zip"
    _write_encrypted_local_config_package(package, {
        "payload_version": local_config_bundle.PAYLOAD_VERSION,
        "store": imported_store,
        "network_diagnostics": {},
        "secrets": {},
    })

    with pytest.raises(ValueError, match="与本机未归属密钥冲突"):
        local_config_bundle.import_local_config_zip(package, "strong-password")

    assert secrets[unowned_ref] == "unrelated-local-secret"
    assert profile_manager.list_codex_profiles() == []


def test_local_config_import_rolls_back_profiles_and_secrets_after_save_failure(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    secrets = isolated_local_config
    imported_ref = "claude:ImportedOwner:auth_token"
    shared_ref = "claude:SharedOwner:primary_api_key"
    old_ref = "claude:OldOwner:auth_token"
    security.set_secret(imported_ref, "imported-token")
    security.set_secret(shared_ref, "imported-shared")
    profile_manager.save_claude_profile(ClaudeProfile(
        name="Same",
        auth_token_ref=imported_ref,
        primary_api_key_ref=shared_ref,
        base_url="https://imported.example.test",
        provider="custom",
    ))
    package = tmp_path / "rollback-save.zip"
    local_config_bundle.export_local_config_zip(package, "strong-password")

    profile_manager._save_store(profile_manager._get_default_store())
    secrets.clear()
    security.set_secret(old_ref, "old-token")
    security.set_secret(shared_ref, "previous-shared")
    profile_manager.save_claude_profile(ClaudeProfile(
        name="Same",
        auth_token_ref=old_ref,
        primary_api_key_ref=shared_ref,
        base_url="https://old.example.test",
        provider="custom",
    ))
    profile_before = profile_manager.PROFILES_FILE.read_bytes()

    original_save = profile_manager._save_store

    def save_then_fail(store, *args, **kwargs):
        original_save(store, *args, **kwargs)
        raise RuntimeError("forced profile save failure")

    monkeypatch.setattr(profile_manager, "_save_store", save_then_fail)

    with pytest.raises(RuntimeError, match="forced profile save failure"):
        local_config_bundle.import_local_config_zip(package, "strong-password")

    assert profile_manager.PROFILES_FILE.read_bytes() == profile_before
    [profile] = profile_manager.list_claude_profiles()
    assert profile.base_url == "https://old.example.test"
    assert profile.auth_token_ref == old_ref
    assert security.get_secret(old_ref) == "old-token"
    assert security.get_secret(shared_ref) == "previous-shared"
    assert security.get_secret(imported_ref) is None


def test_local_config_import_rolls_back_when_required_secret_write_fails(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    secrets = isolated_local_config
    shared_ref = "codex:Same:api_key"
    security.set_secret(shared_ref, "new-secret")
    profile_manager.save_codex_profile(CodexProfile(
        name="Same",
        api_key_ref=shared_ref,
        model="new-model",
        model_provider="custom",
    ))
    package = tmp_path / "secret-write-failure.zip"
    local_config_bundle.export_local_config_zip(package, "strong-password")

    profile_manager._save_store(profile_manager._get_default_store())
    secrets.clear()
    security.set_secret(shared_ref, "old-secret")
    profile_manager.save_codex_profile(CodexProfile(
        name="Same",
        api_key_ref=shared_ref,
        model="old-model",
        model_provider="custom",
    ))
    profile_before = profile_manager.PROFILES_FILE.read_bytes()

    def set_then_fail(ref, value):
        secrets[ref] = value
        if ref == shared_ref and value == "new-secret":
            raise RuntimeError("forced secret backend failure")

    monkeypatch.setattr(security, "set_secret", set_then_fail)

    with pytest.raises(RuntimeError, match="forced secret backend failure"):
        local_config_bundle.import_local_config_zip(package, "strong-password")

    assert profile_manager.PROFILES_FILE.read_bytes() == profile_before
    assert secrets[shared_ref] == "old-secret"
    [profile] = profile_manager.list_codex_profiles()
    assert profile.model == "old-model"


def test_local_config_import_rolls_back_settings_profiles_and_secrets_after_settings_failure(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    secrets = isolated_local_config
    imported_ref = "codex:Imported:api_key"
    security.set_secret(imported_ref, "imported-key")
    profile_manager.save_codex_profile(CodexProfile(
        name="Imported",
        api_key_ref=imported_ref,
        model="imported-model",
        model_provider="custom",
    ))
    imported_settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_PROXYCHECK},
        {network_diagnostic_settings.SERVICE_PROXYCHECK: "imported-diagnostic-key"},
    )
    network_diagnostic_settings.save_settings(imported_settings)
    package = tmp_path / "rollback-settings.zip"
    local_config_bundle.export_local_config_zip(package, "strong-password")

    profile_manager._save_store(profile_manager._get_default_store())
    secrets.clear()
    current_ref = "codex:Current:api_key"
    security.set_secret(current_ref, "current-key")
    profile_manager.save_codex_profile(CodexProfile(
        name="Current",
        api_key_ref=current_ref,
        model="current-model",
        model_provider="custom",
    ))
    current_settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_VPNAPI},
        {network_diagnostic_settings.SERVICE_VPNAPI: "current-diagnostic-key"},
    )
    network_diagnostic_settings.save_settings(current_settings)
    profile_before = profile_manager.PROFILES_FILE.read_bytes()
    settings_before = network_diagnostic_settings.SETTINGS_FILE.read_bytes()

    original_atomic_write_text = local_config_bundle.atomic_write_text

    def fail_settings_write(path, content, *args, **kwargs):
        if path == network_diagnostic_settings.SETTINGS_FILE:
            original_atomic_write_text(path, content, *args, **kwargs)
            raise OSError("forced settings failure")
        return original_atomic_write_text(path, content, *args, **kwargs)

    monkeypatch.setattr(local_config_bundle, "atomic_write_text", fail_settings_write)

    with pytest.raises(OSError, match="forced settings failure"):
        local_config_bundle.import_local_config_zip(package, "strong-password")

    assert profile_manager.PROFILES_FILE.read_bytes() == profile_before
    assert network_diagnostic_settings.SETTINGS_FILE.read_bytes() == settings_before
    assert {profile.name for profile in profile_manager.list_codex_profiles()} == {"Current"}
    assert security.get_secret(current_ref) == "current-key"
    assert security.get_secret(imported_ref) is None
    loaded_settings = network_diagnostic_settings.load_settings()
    assert loaded_settings.enabled_services() == [network_diagnostic_settings.SERVICE_VPNAPI]
    assert loaded_settings.keys_for(network_diagnostic_settings.SERVICE_VPNAPI) == [
        "current-diagnostic-key"
    ]


def test_local_config_import_removes_obsolete_network_secret_refs(
    isolated_local_config,
    tmp_path,
):
    secrets = isolated_local_config
    service = network_diagnostic_settings.SERVICE_PROXYCHECK
    ref0 = f"network-diagnostics:{service}:0"
    ref1 = f"network-diagnostics:{service}:1"
    network_diagnostic_settings.save_settings(
        network_diagnostic_settings.settings_from_values(
            {service},
            {service: ["old-key-0", "old-key-1"]},
        )
    )
    assert secrets == {ref0: "old-key-0", ref1: "old-key-1"}

    package = tmp_path / "shorter-network-key-pool.zip"
    _write_encrypted_local_config_package(package, {
        "payload_version": local_config_bundle.PAYLOAD_VERSION,
        "store": profile_manager._get_default_store(),
        "network_diagnostics": {
            "version": 1,
            "services": {
                service: {
                    "enabled": True,
                    "key_refs": [ref0],
                }
            },
        },
        "secrets": {ref0: "new-key-0"},
    })

    local_config_bundle.import_local_config_zip(package, "strong-password")

    assert secrets == {ref0: "new-key-0"}
    loaded = network_diagnostic_settings.load_settings()
    assert loaded.keys_for(service) == ["new-key-0"]


def test_successful_local_config_import_does_not_run_rollback(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    package = tmp_path / "success.zip"
    local_config_bundle.export_local_config_zip(package, "strong-password")

    def unexpected_rollback(*_args, **_kwargs):
        raise AssertionError("successful import must not roll back")

    monkeypatch.setattr(local_config_bundle, "_rollback_import", unexpected_rollback)

    result = local_config_bundle.import_local_config_zip(package, "strong-password")

    assert result.profile_count == 0


def test_local_config_payload_decompression_is_bounded(monkeypatch):
    encrypted = local_config_bundle._encrypt_payload(
        {"payload_version": local_config_bundle.PAYLOAD_VERSION, "data": "x" * 1024},
        "strong-password",
    )
    monkeypatch.setattr(local_config_bundle, "MAX_DECRYPTED_PAYLOAD_BYTES", 128)

    with pytest.raises(ValueError, match="解密后内容过大"):
        local_config_bundle._decrypt_payload(encrypted, "strong-password")


def test_local_config_export_cannot_overwrite_live_profile_store(isolated_local_config):
    profile_path = profile_manager.PROFILES_FILE
    original = profile_path.read_bytes()

    with pytest.raises(ValueError, match="不能覆盖"):
        local_config_bundle.export_local_config_zip(profile_path, "strong-password")

    assert profile_path.read_bytes() == original


def test_local_config_export_cannot_write_inside_browser_profile(
    isolated_local_config,
    tmp_path,
):
    browser_dir = tmp_path / "browser"
    browser_dir.mkdir()
    profile_manager.save_browser_profile(BrowserProfile(
        name="Protected Browser",
        browser_type="chrome",
        profile_mode="external",
        user_data_dir=str(browser_dir),
    ))

    with pytest.raises(ValueError, match="浏览器 Profile"):
        local_config_bundle.export_local_config_zip(
            browser_dir / "Preferences",
            "strong-password",
        )


def test_local_config_payload_rejects_excessive_kdf_work():
    encrypted = local_config_bundle._encrypt_payload(
        {"payload_version": local_config_bundle.PAYLOAD_VERSION},
        "strong-password",
    )
    encrypted["kdf"]["iterations"] = local_config_bundle.MAX_KDF_ITERATIONS + 1

    with pytest.raises(ValueError, match="KDF 参数异常"):
        local_config_bundle._decrypt_payload(encrypted, "strong-password")


def test_local_config_import_drops_unknown_profile_ref_fields(
    isolated_local_config,
    tmp_path,
):
    imported_ref = "codex:Imported:api_key"
    unrelated_ref = "app:unrelated:secret"
    isolated_local_config[unrelated_ref] = "keep-existing"
    imported_store = profile_manager._get_default_store()
    imported_profile = CodexProfile(
        name="Imported",
        api_key_ref=imported_ref,
        model="imported-model",
        model_provider="custom",
    ).to_dict()
    imported_profile["surprise_ref"] = unrelated_ref
    imported_store["codex_profiles"] = [imported_profile]
    package = tmp_path / "unknown-profile-ref.zip"
    _write_encrypted_local_config_package(package, {
        "payload_version": local_config_bundle.PAYLOAD_VERSION,
        "store": imported_store,
        "network_diagnostics": {},
        "secrets": {
            imported_ref: "imported-secret",
            unrelated_ref: "attacker-controlled",
        },
    })

    result = local_config_bundle.import_local_config_zip(package, "strong-password")

    assert result.secret_count == 1
    assert f"{unrelated_ref} (未被导入配置引用)" in result.skipped_secret_refs
    assert isolated_local_config[unrelated_ref] == "keep-existing"
    assert isolated_local_config[imported_ref] == "imported-secret"
    [stored_profile] = profile_manager._load_store()["codex_profiles"]
    assert "surprise_ref" not in stored_profile


@pytest.mark.parametrize(
    ("network_payload", "error_match"),
    [
        (
            {"services": {"evil": {"enabled": True, "key_refs": ["app:unrelated:secret"]}}},
            "不支持的服务",
        ),
        (
            {
                "services": {
                    network_diagnostic_settings.SERVICE_PROXYCHECK: {
                        "enabled": True,
                        "key_refs": ["app:unrelated:secret"],
                    }
                }
            },
            "不是规范路径",
        ),
    ],
)
def test_local_config_import_rejects_noncanonical_network_secret_refs(
    isolated_local_config,
    tmp_path,
    network_payload,
    error_match,
):
    unrelated_ref = "app:unrelated:secret"
    isolated_local_config[unrelated_ref] = "keep-existing"
    package = tmp_path / f"invalid-network-{error_match}.zip"
    _write_encrypted_local_config_package(package, {
        "payload_version": local_config_bundle.PAYLOAD_VERSION,
        "store": profile_manager._get_default_store(),
        "network_diagnostics": network_payload,
        "secrets": {unrelated_ref: "attacker-controlled"},
    })

    with pytest.raises(ValueError, match=error_match):
        local_config_bundle.import_local_config_zip(package, "strong-password")

    assert isolated_local_config[unrelated_ref] == "keep-existing"


def test_local_config_import_rejects_cross_namespace_profile_secret_ref(
    isolated_local_config,
    tmp_path,
):
    protected_ref = "network-diagnostics:proxycheck:0"
    isolated_local_config[protected_ref] = "keep-existing"
    imported_store = profile_manager._get_default_store()
    imported_store["claude_profiles"] = [ClaudeProfile(
        name="Malicious",
        auth_token_ref=protected_ref,
        base_url="https://api.example.test",
        provider="custom",
    ).to_dict()]
    package = tmp_path / "cross-namespace-profile-ref.zip"
    _write_encrypted_local_config_package(package, {
        "payload_version": local_config_bundle.PAYLOAD_VERSION,
        "store": imported_store,
        "network_diagnostics": {},
        "secrets": {protected_ref: "attacker-controlled"},
    })

    with pytest.raises(ValueError, match="密钥引用不属于字段 auth_token_ref"):
        local_config_bundle.import_local_config_zip(package, "strong-password")

    assert isolated_local_config[protected_ref] == "keep-existing"
    assert profile_manager.list_claude_profiles() == []


def test_local_config_payload_rejects_trailing_zlib_stream_data():
    password = "strong-password"
    payload = {"payload_version": local_config_bundle.PAYLOAD_VERSION}
    encrypted = local_config_bundle._encrypt_payload(payload, password)
    salt = local_config_bundle._b64decode(encrypted["kdf"]["salt"])
    nonce = local_config_bundle._b64decode(encrypted["cipher"]["nonce"])
    key = local_config_bundle._derive_key(password, salt)
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed_with_trailing_data = zlib.compress(plaintext) + b"unexpected-trailing-data"
    encrypted["payload"] = local_config_bundle._b64encode(
        local_config_bundle.AESGCM(key).encrypt(
            nonce,
            compressed_with_trailing_data,
            None,
        )
    )

    with pytest.raises(ValueError, match="压缩数据损坏"):
        local_config_bundle._decrypt_payload(encrypted, password)


def test_local_config_export_cannot_overwrite_profile_backup(
    isolated_local_config,
):
    backup_path = profile_manager.PROFILES_FILE.with_suffix(".backup")
    backup_path.write_bytes(b"profile-recovery-copy")

    with pytest.raises(ValueError, match="不能覆盖"):
        local_config_bundle.export_local_config_zip(backup_path, "strong-password")

    assert backup_path.read_bytes() == b"profile-recovery-copy"


def test_local_config_export_cannot_write_inside_secret_store(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    monkeypatch.setattr(security, "SECRETS_DIR", secrets_dir)
    secret_path = secrets_dir / "protected.bin"
    secret_path.write_bytes(b"protected-secret-data")

    with pytest.raises(ValueError, match="密钥目录"):
        local_config_bundle.export_local_config_zip(secret_path, "strong-password")

    assert secret_path.read_bytes() == b"protected-secret-data"


def test_local_config_export_rejects_oversized_generated_entry(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    package = tmp_path / "oversized-entry.zip"
    monkeypatch.setattr(local_config_bundle, "MAX_ZIP_ENTRY_BYTES", 64)

    with pytest.raises(ValueError, match="条目过大"):
        local_config_bundle.export_local_config_zip(package, "strong-password")

    assert not package.exists()


def test_local_config_export_rejects_oversized_generated_zip(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    package = tmp_path / "oversized.zip"
    monkeypatch.setattr(local_config_bundle, "MAX_ZIP_BYTES", 1)

    with pytest.raises(ValueError, match="ZIP 过大"):
        local_config_bundle.export_local_config_zip(package, "strong-password")

    assert not package.exists()
    assert list(tmp_path.glob(f".{package.name}.tmp-*")) == []


def test_local_config_import_uses_profile_then_settings_lock_order(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    package = tmp_path / "lock-order.zip"
    local_config_bundle.export_local_config_zip(package, "strong-password")
    events: list[str] = []

    class RecordingRLock:
        def __init__(self, name):
            self.name = name
            self.lock = threading.RLock()

        def __enter__(self):
            self.lock.acquire()
            events.append(f"enter:{self.name}")
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            events.append(f"exit:{self.name}")
            self.lock.release()

    monkeypatch.setattr(profile_manager, "_STORE_CACHE_LOCK", RecordingRLock("profile"))
    monkeypatch.setattr(
        network_diagnostic_settings,
        "_SETTINGS_CACHE_LOCK",
        RecordingRLock("settings"),
    )

    local_config_bundle.import_local_config_zip(package, "strong-password")

    assert events[:2] == ["enter:profile", "enter:settings"]
    assert events[-2:] == ["exit:settings", "exit:profile"]


def test_local_config_export_holds_profile_then_settings_locks_through_commit(
    isolated_local_config,
    tmp_path,
    monkeypatch,
):
    depths = {"profile": 0, "settings": 0}
    events: list[str] = []

    class RecordingRLock:
        def __init__(self, name):
            self.name = name
            self.lock = threading.RLock()

        def __enter__(self):
            self.lock.acquire()
            depths[self.name] += 1
            events.append(f"enter:{self.name}")
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            events.append(f"exit:{self.name}")
            depths[self.name] -= 1
            self.lock.release()

    monkeypatch.setattr(profile_manager, "_STORE_CACHE_LOCK", RecordingRLock("profile"))
    monkeypatch.setattr(
        network_diagnostic_settings,
        "_SETTINGS_CACHE_LOCK",
        RecordingRLock("settings"),
    )
    original_replace = local_config_bundle.replace_with_retry

    def replace_while_locked(*args, **kwargs):
        assert depths == {"profile": 1, "settings": 1}
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(local_config_bundle, "replace_with_retry", replace_while_locked)

    package = tmp_path / "locked-export.zip"
    local_config_bundle.export_local_config_zip(package, "strong-password")

    assert package.is_file()
    assert events[:2] == ["enter:profile", "enter:settings"]
    assert events[-2:] == ["exit:settings", "exit:profile"]
