"""Cross-machine login transfer uses synthetic files, secrets and registry only."""
import base64
import copy
import json
import os
from types import SimpleNamespace

import pytest

from config import paths
from core import (
    auth_parser, backup_manager, codex_env, parser, persistent_env,
    profile_manager, security, switcher, toml_parser, vscode_parser,
)


PASSWORD = "Synthetic-Transfer-Test-Password-2026!"


def _jwt(identity, revision):
    body = base64.urlsafe_b64encode(json.dumps({
        "email": identity + "@example.test", "sub": identity, "revision": revision,
    }).encode()).decode().rstrip("=")
    return "synthetic." + body + ".signature"


def _credentials(kind, *, identity="same-person", revision="source"):
    if kind == "codex":
        return {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": _jwt(identity, revision), "access_token": _jwt(identity, revision),
                "refresh_token": "synthetic-refresh-" + revision, "account_id": identity,
            },
            "last_refresh": "2026-09-21T00:00:00Z",
        }
    return {"claudeAiOauth": {
        "accessToken": _jwt(identity, revision), "refreshToken": "synthetic-refresh-" + revision,
        "expiresAt": 2100000000000, "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "pro", "rateLimitTier": "default",
    }}


def _token_pair(kind, value):
    container = value["tokens"] if kind == "codex" else value["claudeAiOauth"]
    return (container["access_token"], container["refresh_token"]) if kind == "codex" else (
        container["accessToken"], container["refreshToken"],
    )


def _set_workspace(kind, credentials, workspace):
    if kind == "codex":
        credentials["tokens"]["account_id"] = workspace
    else:
        credentials["oauthAccount"] = {"accountUuid": "synthetic-user", "organizationUuid": workspace}


def _workspace(kind, credentials):
    return credentials["tokens"]["account_id"] if kind == "codex" else credentials["oauthAccount"]["organizationUuid"]


def _save_current(kind, credentials):
    return (profile_manager._save_current_codex_account_auth(credentials) if kind == "codex"
            else profile_manager._save_current_claude_account_credentials(credentials))


@pytest.fixture
def transfer_machine(tmp_path, monkeypatch):
    secrets = {}
    registry = {"HTTP_PROXY": "http://127.0.0.1:19081", "HTTPS_PROXY": "http://127.0.0.1:19081"}
    monkeypatch.setattr(security, "get_secret", lambda key: secrets.get(key))
    monkeypatch.setattr(security, "get_secret_strict", lambda key: secrets.get(key))
    monkeypatch.setattr(security, "set_secret", lambda key, value: secrets.__setitem__(key, value))
    monkeypatch.setattr(security, "delete_secret", lambda key: secrets.pop(key, None))
    monkeypatch.setattr(security, "get_secret_json", lambda key: json.loads(secrets[key]) if key in secrets else None)
    monkeypatch.setattr(security, "set_secret_json", lambda key, value: secrets.__setitem__(key, json.dumps(value)))
    monkeypatch.setattr(security, "_keyring", lambda: pytest.fail("must not access real OS credential manager"))
    monkeypatch.setattr(backup_manager, "create_backup", lambda *a, **k: None)
    monkeypatch.setattr(switcher, "_is_windows", lambda: True)
    monkeypatch.setattr(persistent_env, "_local_user_env_value_strict", lambda name: registry.get(name))

    def delete_env(names):
        for name in names:
            registry.pop(name, None)

    monkeypatch.setattr(persistent_env, "delete_local_user_env", delete_env)
    monkeypatch.setattr(persistent_env, "set_local_user_env", lambda values: registry.update(values))
    for key, value in registry.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:19082")

    def use_machine(name):
        folder = tmp_path / name
        mapping = {
            "PROFILES_FILE": folder / "app" / "profiles.json",
            "CLAUDE_CREDENTIALS": folder / "claude" / ".credentials.json",
            "CLAUDE_SETTINGS": folder / "claude" / "settings.json",
            "CLAUDE_CONFIG": folder / "claude" / "config.json",
            "CODEX_AUTH": folder / "codex" / "auth.json",
            "CODEX_CONFIG": folder / "codex" / "config.toml",
            "CODEX_ENV": folder / "codex" / ".env",
            "VSCODE_SETTINGS": folder / "vscode" / "settings.json",
        }
        for key, value in mapping.items():
            monkeypatch.setattr(paths, key, value)
        monkeypatch.setattr(profile_manager, "PROFILES_FILE", mapping["PROFILES_FILE"])
        monkeypatch.setattr(profile_manager, "CLAUDE_CREDENTIALS", mapping["CLAUDE_CREDENTIALS"])
        for key in ("CLAUDE_CREDENTIALS", "CLAUDE_SETTINGS", "CLAUDE_CONFIG"):
            monkeypatch.setattr(parser, key, mapping[key])
        monkeypatch.setattr(auth_parser, "CODEX_AUTH", mapping["CODEX_AUTH"])
        monkeypatch.setattr(toml_parser, "CODEX_CONFIG", mapping["CODEX_CONFIG"])
        monkeypatch.setattr(vscode_parser, "VSCODE_SETTINGS", mapping["VSCODE_SETTINGS"])
        switcher._restore_switch_caches()
        return mapping

    def live(kind, value):
        if kind == "codex":
            toml_parser.write_codex_config({"cli_auth_credentials_store": "file", "model_provider": "openai"})
            auth_parser.write_codex_auth(value)
        else:
            parser.write_claude_credentials(value)

    def exported(kind):
        from core.account_transfer import ACCOUNT_EXTENSION, export_account_login

        use_machine("source")
        value = _credentials(kind)
        live(kind, value)
        # These source-machine settings must never travel with a login file.
        if kind == "codex":
            toml_parser.write_codex_config({
                "cli_auth_credentials_store": "file", "model_provider": "openai",
                "model": "synthetic-source-model", "approval_policy": "never",
                "sandbox_mode": "danger-full-access",
                "projects": {"C:\\synthetic-source-only": {"trust_level": "trusted"}},
            })
        parser.write_claude_settings({"permissions": {"defaultMode": "bypassPermissions"},
                                      "env": {"HTTPS_PROXY": "http://source.invalid:9999"}})
        file_path = tmp_path / (kind + ACCOUNT_EXTENSION)
        export_account_login(file_path, PASSWORD, kind)
        use_machine("destination")
        assert not secrets
        return file_path, value

    yield SimpleNamespace(use=use_machine, live=live, exported=exported, secrets=secrets, registry=registry)
    switcher._restore_switch_caches()


def _runtime_files():
    return (parser.CLAUDE_CREDENTIALS, parser.CLAUDE_SETTINGS, parser.CLAUDE_CONFIG,
            auth_parser.CODEX_AUTH, toml_parser.CODEX_CONFIG, paths.CODEX_ENV, vscode_parser.VSCODE_SETTINGS)


def _file_snapshot(files):
    return {path: path.read_bytes() if path.exists() else None for path in files}


def _saved(kind):
    return profile_manager.list_codex_account_profiles() if kind == "codex" else profile_manager.list_claude_account_profiles()


def _saved_value(kind, profile):
    return profile_manager.get_codex_account_auth(profile) if kind == "codex" else profile_manager.get_claude_account_credentials(profile)


def _read_live(kind):
    return auth_parser.read_codex_auth() if kind == "codex" else parser.read_claude_credentials()


def _activate(kind, name):
    (switcher.switch_codex_account if kind == "codex" else switcher.switch_claude_account)(name)


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_transfer_to_clean_machine_only_saves_until_explicit_activation(transfer_machine, kind):
    from core.account_transfer import import_account_login

    package, expected = transfer_machine.exported(kind)
    before = _file_snapshot(_runtime_files())
    proxies = {key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    registry = copy.deepcopy(transfer_machine.registry)
    imported = import_account_login(package, PASSWORD, expected_type=kind)
    assert imported.profile_type == kind and imported.created_new
    assert _file_snapshot(_runtime_files()) == before
    assert len(_saved(kind)) == 1
    assert not profile_manager.get_active_codex_account_name()
    assert not profile_manager.get_active_claude_account_name()

    _activate(kind, imported.account_name)
    assert _token_pair(kind, _read_live(kind)) == _token_pair(kind, expected)
    assert {key: os.environ.get(key) for key in proxies} == proxies
    assert transfer_machine.registry == registry
    if kind == "codex":
        config = toml_parser.read_codex_config()
        assert config["cli_auth_credentials_store"] == "file"
        assert config["model_provider"] == "openai"
        assert "model_providers" not in config
        assert "model" not in config and "projects" not in config
        assert config.get("approval_policy") != "never"
        assert config.get("sandbox_mode") != "danger-full-access"
    else:
        assert "source.invalid" not in json.dumps(parser.read_claude_settings())
        assert parser.read_claude_settings().get("permissions", {}).get("defaultMode") != "bypassPermissions"


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("api_active", [False, True])
def test_same_identity_unsaved_live_login_cannot_overwrite_imported_snapshot_on_activation(transfer_machine, kind, api_active):
    from core.account_transfer import import_account_login

    package, imported_credentials = transfer_machine.exported(kind)
    old_live = _credentials(kind, revision="destination-old")
    transfer_machine.live(kind, old_live)
    if api_active:
        if kind == "codex":
            toml_parser.write_codex_config({"cli_auth_credentials_store": "file", "model_provider": "relay",
                "model_providers": {"relay": {"base_url": "https://relay.example.test/v1", "env_key": "RELAY_API_KEY"}}})
        else:
            parser.write_claude_settings({"env": {
                "ANTHROPIC_BASE_URL": "https://relay.example.test", "ANTHROPIC_AUTH_TOKEN": "synthetic-api-token",
            }})
    assert _saved(kind) == []
    runtime = _file_snapshot(_runtime_files())
    imported = import_account_login(package, PASSWORD, expected_type=kind)
    assert _file_snapshot(_runtime_files()) == runtime
    _activate(kind, imported.account_name)
    assert _token_pair(kind, _read_live(kind)) == _token_pair(kind, imported_credentials)
    assert any(_token_pair(kind, _saved_value(kind, profile)) == _token_pair(kind, old_live)
               for profile in _saved(kind))


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_import_preserves_existing_api_runtime_and_proxy_settings(transfer_machine, kind):
    from core.account_transfer import import_account_login

    package, _ = transfer_machine.exported(kind)
    toml_parser.write_codex_config({"model_provider": "relay", "model": "synthetic-relay-model", "model_providers": {
        "relay": {"base_url": "https://relay.example.test/v1", "env_key": "RELAY_API_KEY"},
    }})
    auth_parser.write_codex_auth({"auth_mode": "api_key", "OPENAI_API_KEY": "synthetic-destination-api"})
    codex_env.update_codex_env({"RELAY_API_KEY": "synthetic-relay", "HTTPS_PROXY": "http://127.0.0.1:19081"})
    parser.write_claude_settings({"env": {
        "ANTHROPIC_BASE_URL": "https://relay.example.test", "ANTHROPIC_AUTH_TOKEN": "synthetic-claude-api",
        "HTTP_PROXY": "http://127.0.0.1:19081",
    }, "model": "synthetic-other-model"})
    vscode_parser.write_vscode_settings({"http.proxy": "http://127.0.0.1:19081", "keep.setting": True})
    files_before = _file_snapshot(_runtime_files())
    env_before = dict(os.environ)
    registry_before = dict(transfer_machine.registry)
    import_account_login(package, PASSWORD, expected_type=kind)
    assert _file_snapshot(_runtime_files()) == files_before
    assert dict(os.environ) == env_before and transfer_machine.registry == registry_before


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_import_save_failure_rolls_back_preserved_live_snapshot_too(transfer_machine, monkeypatch, kind):
    from core.account_transfer import import_account_login

    package, source_credentials = transfer_machine.exported(kind)
    transfer_machine.live(kind, _credentials(kind, revision="destination-old"))
    before_files = _file_snapshot((*_runtime_files(), profile_manager.PROFILES_FILE,
                                   profile_manager.PROFILES_FILE.with_suffix(".backup")))
    before_secrets = dict(transfer_machine.secrets)
    original = security.set_secret_json

    def fail_incoming(key, value):
        original(key, value)
        if _token_pair(kind, value) == _token_pair(kind, source_credentials):
            raise OSError("synthetic encrypted snapshot storage failure")

    monkeypatch.setattr(security, "set_secret_json", fail_incoming)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        import_account_login(package, PASSWORD, expected_type=kind)
    assert _file_snapshot(before_files) == before_files
    assert transfer_machine.secrets == before_secrets
    assert _saved(kind) == []


@pytest.mark.parametrize("store", [None, "auto", "keyring"])
def test_codex_import_does_not_harvest_unproven_stale_auth_file(transfer_machine, store):
    from core.account_transfer import import_account_login

    package, incoming = transfer_machine.exported("codex")
    config = {} if store is None else {"cli_auth_credentials_store": store}
    toml_parser.write_codex_config(config)
    auth_parser.write_codex_auth(_credentials("codex", revision="stale-file"))
    runtime = _file_snapshot(_runtime_files())
    imported = import_account_login(package, PASSWORD, expected_type="codex")
    assert _file_snapshot(_runtime_files()) == runtime
    assert len(_saved("codex")) == 1
    _activate("codex", imported.account_name)
    assert _token_pair("codex", _read_live("codex")) == _token_pair("codex", incoming)
    assert toml_parser.read_codex_config()["cli_auth_credentials_store"] == "file"


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_failed_explicit_activation_restores_runtime_and_imported_snapshot(transfer_machine, monkeypatch, kind):
    from core.account_transfer import import_account_login

    package, expected = transfer_machine.exported(kind)
    imported = import_account_login(package, PASSWORD, expected_type=kind)
    before = _file_snapshot((*_runtime_files(), profile_manager.PROFILES_FILE,
                            profile_manager.PROFILES_FILE.with_suffix(".backup")))
    secrets = dict(transfer_machine.secrets)
    registry = dict(transfer_machine.registry)
    module, name = ((toml_parser, "write_codex_config") if kind == "codex" else (parser, "write_claude_settings"))
    write = getattr(module, name)

    def fail_after_write(value):
        write(value)
        raise OSError("synthetic activation persistence failure")

    monkeypatch.setattr(module, name, fail_after_write)
    with pytest.raises((RuntimeError, OSError)):
        _activate(kind, imported.account_name)
    assert _file_snapshot(before) == before
    assert transfer_machine.secrets == secrets and transfer_machine.registry == registry
    saved = next(profile for profile in _saved(kind) if profile.name == imported.account_name)
    assert _token_pair(kind, _saved_value(kind, saved)) == _token_pair(kind, expected)


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_selected_export_does_not_substitute_conflicting_workspace_with_shared_email(transfer_machine, tmp_path, kind):
    from core.account_transfer import ACCOUNT_EXTENSION, export_account_login, import_account_login

    transfer_machine.use("source")
    selected_credentials = _credentials(kind, revision="selected-workspace")
    _set_workspace(kind, selected_credentials, "synthetic-workspace-A")
    selected = _save_current(kind, selected_credentials)
    live_credentials = _credentials(kind, revision="other-workspace")
    _set_workspace(kind, live_credentials, "synthetic-workspace-B")
    transfer_machine.live(kind, live_credentials)
    package = tmp_path / ("selected-workspace" + ACCOUNT_EXTENSION)
    export_account_login(package, PASSWORD, kind, account_name=selected.name)
    transfer_machine.use("destination")
    transfer_machine.secrets.clear()  # Simulate this second machine's empty app secret store.
    imported = import_account_login(package, PASSWORD, expected_type=kind)
    profile = next(profile for profile in _saved(kind) if profile.name == imported.account_name)
    restored = _saved_value(kind, profile)
    assert _workspace(kind, restored) == "synthetic-workspace-A"
    assert _token_pair(kind, restored) == _token_pair(kind, selected_credentials)


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_import_activation_preserves_saved_other_workspace_with_shared_email(transfer_machine, tmp_path, kind):
    from core.account_transfer import ACCOUNT_EXTENSION, export_account_login, import_account_login

    transfer_machine.use("source")
    incoming = _credentials(kind, revision="incoming-D")
    _set_workspace(kind, incoming, "synthetic-workspace-D")
    transfer_machine.live(kind, incoming)
    package = tmp_path / ("workspace-D" + ACCOUNT_EXTENSION)
    export_account_login(package, PASSWORD, kind)
    transfer_machine.use("destination")
    saved_credentials = _credentials(kind, revision="saved-A")
    _set_workspace(kind, saved_credentials, "synthetic-workspace-A")
    existing = _save_current(kind, saved_credentials)
    live_credentials = _credentials(kind, revision="live-B")
    _set_workspace(kind, live_credentials, "synthetic-workspace-B")
    transfer_machine.live(kind, live_credentials)
    imported = import_account_login(package, PASSWORD, expected_type=kind)
    _activate(kind, imported.account_name)
    assert _token_pair(kind, _read_live(kind)) == _token_pair(kind, incoming)
    existing_after = next(profile for profile in _saved(kind) if profile.name == existing.name)
    assert _saved_value(kind, existing_after) == saved_credentials


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_selected_export_uses_rotated_tokens_when_stable_account_email_changes(transfer_machine, tmp_path, kind):
    from core.account_transfer import ACCOUNT_EXTENSION, export_account_login, import_account_login

    transfer_machine.use("source")
    selected_credentials = _credentials(kind, revision="old-email")
    selected_credentials["email"] = "before@example.test"
    _set_workspace(kind, selected_credentials, "synthetic-same-workspace")
    if kind == "codex":
        selected_credentials["tokens"].pop("id_token")
        selected_credentials["tokens"]["access_token"] = "synthetic-opaque-old"
    else:
        selected_credentials["claudeAiOauth"]["accessToken"] = "synthetic-opaque-old"
    selected = _save_current(kind, selected_credentials)
    live_credentials = copy.deepcopy(selected_credentials)
    live_credentials["email"] = "after@example.test"
    if kind == "codex":
        live_credentials["tokens"].update(access_token="synthetic-opaque-new", refresh_token="synthetic-refresh-new")
    else:
        live_credentials["claudeAiOauth"].update(accessToken="synthetic-opaque-new", refreshToken="synthetic-refresh-new")
    transfer_machine.live(kind, live_credentials)
    before_files = _file_snapshot((*_runtime_files(), profile_manager.PROFILES_FILE))
    before_secrets = dict(transfer_machine.secrets)
    package = tmp_path / ("rotated-email" + ACCOUNT_EXTENSION)
    result = export_account_login(package, PASSWORD, kind, account_name=selected.name)
    assert result.used_current_login
    assert _file_snapshot(before_files) == before_files
    assert transfer_machine.secrets == before_secrets

    transfer_machine.use("destination")
    transfer_machine.secrets.clear()
    imported = import_account_login(package, PASSWORD, expected_type=kind)
    restored = next(profile for profile in _saved(kind) if profile.name == imported.account_name)
    assert _token_pair(kind, _saved_value(kind, restored)) == _token_pair(kind, live_credentials)


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_passwordless_cross_machine_transfer_preserves_existing_login_until_switch(transfer_machine, tmp_path, kind):
    from core.account_transfer import export_account_login, import_account_login

    transfer_machine.use("source")
    source_credentials = _credentials(kind, identity="source-owner", revision="source-passwordless")
    transfer_machine.live(kind, source_credentials)
    source_files = _file_snapshot(_runtime_files())
    package = tmp_path / (kind + "-no-password.asxaccount")
    export_account_login(package, "", kind)
    assert _file_snapshot(source_files) == source_files
    assert not transfer_machine.secrets
    envelope = json.loads(package.read_text(encoding="utf-8"))
    assert envelope["version"] == 2
    assert envelope["cipher"] == {"name": "none"}
    assert "kdf" not in envelope

    transfer_machine.use("destination")
    destination_credentials = _credentials(kind, identity="destination-owner", revision="existing")
    transfer_machine.live(kind, destination_credentials)
    destination_files = _file_snapshot(_runtime_files())
    proxies = {key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    registry = dict(transfer_machine.registry)

    imported = import_account_login(package, "", expected_type=kind)
    assert _file_snapshot(_runtime_files()) == destination_files
    assert len(_saved(kind)) == 2
    assert not profile_manager.get_active_codex_account_name()
    assert not profile_manager.get_active_claude_account_name()

    _activate(kind, imported.account_name)
    assert _token_pair(kind, _read_live(kind)) == _token_pair(kind, source_credentials)
    assert any(_token_pair(kind, _saved_value(kind, profile)) == _token_pair(kind, destination_credentials)
               for profile in _saved(kind))
    assert {key: os.environ.get(key) for key in proxies} == proxies
    assert transfer_machine.registry == registry
