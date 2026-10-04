"""Synthetic-only SSH account regressions: no credentials or server access."""
import base64
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from core import auth_parser, profile_manager, remote_config, sync_manager
from models.profile import CodexAccountProfile
from test_remote_sync_transactions import _install_remote


def _auth(revision, refresh, expiry=None, *, account_id="synthetic-account"):
    claims = {"sub": account_id, "email": account_id + "@example.test"}
    if expiry is not None:
        claims["exp"] = expiry
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    result = {"auth_mode": "chatgpt", "tokens": {
        "id_token": "synthetic." + encoded + ".signature",
        "access_token": "synthetic." + encoded + ".signature",
        "refresh_token": "synthetic-refresh-" + revision,
        "account_id": account_id,
    }}
    if refresh is not None:
        result["last_refresh"] = refresh
    return result


@pytest.fixture
def remote_accounts(monkeypatch):
    files, ssh_profile, client = _install_remote(monkeypatch, "never")
    files["/remote/codex/config.toml"] = 'model_provider = "openai"\ncli_auth_credentials_store = "file"\n'
    target = CodexAccountProfile("official", "codex-account:official:auth_json", "synthetic-account")
    state = SimpleNamespace(files=files, auth=None, saved=[], target=target, ssh=ssh_profile, client=client)
    monkeypatch.setattr(profile_manager, "list_codex_account_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "_pick_codex_account_import_name", lambda *_args: target.name)
    monkeypatch.setattr(profile_manager, "refresh_codex_account_snapshot_if_current", lambda _name: False)
    monkeypatch.setattr(profile_manager, "get_codex_account_auth", lambda _target: deepcopy(state.auth))
    monkeypatch.setattr(profile_manager, "load_codex_account_auth", lambda _target: deepcopy(state.auth))

    def save(_target, auth):
        state.saved.append(deepcopy(auth))
        state.auth = deepcopy(auth)

    monkeypatch.setattr(profile_manager, "save_codex_account_profile_with_auth", save)
    return state


@pytest.mark.parametrize("local_is_newer", [True, False])
def test_ssh_pull_uses_refresh_rotation_before_jwt_lifetime(remote_accounts, local_is_newer):
    older = _auth("old", "2026-10-01T00:00:00Z", 2100000000)
    newer = _auth("new", "2026-10-04T00:00:00Z", 1900000000)
    remote_accounts.auth = deepcopy(newer if local_is_newer else older)
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(older if local_is_newer else newer)
    assert auth_parser.codex_auth_is_newer(newer, older)
    sync_manager.pull_codex_account_from_server("remote")
    assert remote_accounts.auth == newer


def test_ssh_pull_missing_comparable_metadata_is_not_proof_incoming_is_old(remote_accounts):
    existing = _auth("known-time", "2026-10-04T00:00:00Z", 2100000000)
    incoming = _auth("unknown-time", None)
    remote_accounts.auth = existing
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(incoming)
    assert not auth_parser.codex_auth_is_newer(existing, incoming)
    sync_manager.pull_codex_account_from_server("remote")
    assert remote_accounts.auth == incoming


def test_ssh_push_does_not_replace_same_account_newer_remote_rotation(remote_accounts):
    incoming = _auth("old-local", "2026-10-01T00:00:00Z", 2100000000)
    newer_remote = _auth("new-remote", "2026-10-04T00:00:00Z", 1900000000)
    remote_accounts.auth = incoming
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(newer_remote)
    sync_manager.sync_codex_account_to_server("remote", "official")
    assert json.loads(remote_accounts.files["/remote/codex/auth.json"]) == newer_remote
    assert remote_accounts.auth == incoming  # Pushing must not silently import remote credentials.


def test_ssh_push_different_account_does_not_inherit_another_accounts_freshness(remote_accounts):
    incoming = _auth("selected", "2026-10-01T00:00:00Z", 1900000000)
    other = _auth("other", "2026-10-04T00:00:00Z", 2100000000, account_id="other-synthetic-account")
    remote_accounts.auth = incoming
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(other)
    sync_manager.sync_codex_account_to_server("remote", "official")
    assert json.loads(remote_accounts.files["/remote/codex/auth.json"]) == incoming


def test_ssh_push_missing_comparable_metadata_keeps_explicit_account_choice(remote_accounts):
    incoming = _auth("unknown-time", None)
    existing = _auth("known-time", "2026-10-04T00:00:00Z", 2100000000)
    remote_accounts.auth = incoming
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(existing)
    sync_manager.sync_codex_account_to_server("remote", "official")
    assert json.loads(remote_accounts.files["/remote/codex/auth.json"]) == incoming


@pytest.mark.parametrize("store", ["auto", "keyring", None])
def test_ssh_push_never_promotes_unproven_leftover_file(remote_accounts, store):
    incoming = _auth("selected", "2026-10-01T00:00:00Z")
    leftover = _auth("leftover", "2026-10-04T00:00:00Z")
    remote_accounts.auth = incoming
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(leftover)
    remote_accounts.files["/remote/codex/config.toml"] = (
        "" if store is None else f'cli_auth_credentials_store = "{store}"\n'
    )
    message = sync_manager.sync_codex_account_to_server("remote", "official")
    assert json.loads(remote_accounts.files["/remote/codex/auth.json"]) == incoming
    assert "保留完整远端登录状态" not in message


@pytest.mark.parametrize("missing", ["id_token", "access_token", "refresh_token"])
def test_incomplete_remote_bundle_cannot_block_complete_selected_login(remote_accounts, missing):
    incoming = _auth("selected", "2026-10-01T00:00:00Z")
    incomplete = _auth("incomplete", "2026-10-04T00:00:00Z")
    incomplete["tokens"].pop(missing)
    remote_accounts.auth = incoming
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(incomplete)
    sync_manager.sync_codex_account_to_server("remote", "official")
    assert json.loads(remote_accounts.files["/remote/codex/auth.json"]) == incoming


@pytest.mark.parametrize("invalid", ["external", "alias-conflict", "invalid-claims"])
def test_nonportable_remote_bundle_is_not_promoted(remote_accounts, invalid):
    incoming = _auth("selected", "2026-10-01T00:00:00Z")
    invalid_auth = _auth("invalid", "2026-10-04T00:00:00Z")
    if invalid == "external":
        invalid_auth["auth_mode"] = "chatgptAuthTokens"
    elif invalid == "alias-conflict":
        invalid_auth["tokens"]["refreshToken"] = "synthetic-conflicting-alias"
    else:
        invalid_auth["tokens"]["id_token"] = "synthetic.invalid.signature"
    remote_accounts.auth = incoming
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(invalid_auth)
    sync_manager.sync_codex_account_to_server("remote", "official")
    assert json.loads(remote_accounts.files["/remote/codex/auth.json"]) == incoming


def test_retained_newer_login_still_clears_api_overrides_without_merging_tokens(remote_accounts):
    import tomllib

    incoming = _auth("selected", "2026-10-01T00:00:00Z")
    newer = _auth("remote", "2026-10-04T00:00:00Z")
    newer.update(auth_mode="apikey", OPENAI_API_KEY="synthetic-obsolete-api-key", keep_remote_metadata="retained")
    remote_accounts.auth = incoming
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(newer)
    remote_accounts.files["/remote/codex/config.toml"] = (
        'cli_auth_credentials_store = "file"\nmodel_provider = "relay"\n'
        '[model_providers.relay]\nenv_key = "OLD_KEY"\n'
    )
    message = sync_manager.sync_codex_account_to_server("remote", "official")
    written = json.loads(remote_accounts.files["/remote/codex/auth.json"])
    assert written == profile_manager._normalize_codex_official_auth(newer)
    assert written["tokens"] == newer["tokens"]
    assert "OPENAI_API_KEY" not in written and written["auth_mode"] == "chatgpt"
    config = tomllib.loads(remote_accounts.files["/remote/codex/config.toml"])
    assert config["model_provider"] == "openai" and config["cli_auth_credentials_store"] == "file"
    assert "OLD_KEY" not in remote_accounts.files["/remote/codex/.env"]
    assert "OLD_KEY" not in remote_accounts.files["/home/test/.api_switcher_env"]
    assert "保留完整远端登录状态" in message
    assert "synthetic-refresh" not in message and "synthetic-obsolete-api-key" not in message
    assert remote_accounts.auth == incoming


def test_absent_remote_auth_accepts_explicit_push(remote_accounts):
    incoming = _auth("selected", "2026-10-01T00:00:00Z")
    remote_accounts.auth = incoming
    remote_accounts.files.pop("/remote/codex/auth.json")
    sync_manager.sync_codex_account_to_server("remote", "official")
    assert json.loads(remote_accounts.files["/remote/codex/auth.json"]) == incoming


def test_unreadable_remote_auth_aborts_before_any_write(remote_accounts, monkeypatch):
    remote_accounts.auth = _auth("selected", "2026-10-01T00:00:00Z")
    before = dict(remote_accounts.files)

    def failed_read(*_args, **_kwargs):
        raise OSError("synthetic permission failure")

    monkeypatch.setattr(remote_config, "read_remote_codex_auth", failed_read)
    with pytest.raises(OSError, match="synthetic permission failure"):
        sync_manager.sync_codex_account_to_server("remote", "official")
    assert remote_accounts.files == before


@pytest.mark.parametrize("stage", ["config", "login"])
def test_retained_remote_rotation_keeps_exact_transaction_rollback(remote_accounts, monkeypatch, stage):
    incoming = _auth("selected", "2026-10-01T00:00:00Z")
    newer = _auth("remote", "2026-10-04T00:00:00Z")
    remote_accounts.auth = deepcopy(incoming)
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(newer, indent=3) + "\r\n"
    before = dict(remote_accounts.files)
    if stage == "config":
        original = remote_config.write_remote_codex_config

        def fail_config(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("synthetic config failure")

        monkeypatch.setattr(remote_config, "write_remote_codex_config", fail_config)
    else:
        monkeypatch.setattr(sync_manager, "_remote_codex_login_status", lambda *_a, **_k: (False, "synthetic login failure"))
    with pytest.raises((OSError, RuntimeError), match="synthetic"):
        sync_manager.sync_codex_account_to_server("remote", "official")
    assert remote_accounts.files == before
    assert remote_accounts.auth == incoming and not remote_accounts.saved


def test_rotation_after_snapshot_aborts_before_mutation_or_rollback(remote_accounts, monkeypatch):
    incoming = _auth("selected", "2026-10-01T00:00:00Z")
    original = _auth("remote-before-snapshot", "2026-10-03T00:00:00Z")
    rotated = _auth("remote-after-snapshot", "2026-10-04T00:00:00Z")
    remote_accounts.auth = incoming
    remote_accounts.files["/remote/codex/auth.json"] = json.dumps(original)
    expected_files = dict(remote_accounts.files)
    rotated_text = json.dumps(rotated, indent=3) + "\r\n"
    expected_files["/remote/codex/auth.json"] = rotated_text
    original_read = remote_config.read_remote_codex_auth
    first_read = True
    writes = []

    def rotate_before_strict_read(*args, **kwargs):
        nonlocal first_read
        if first_read:
            first_read = False
            remote_accounts.files["/remote/codex/auth.json"] = rotated_text
        return original_read(*args, **kwargs)

    original_write = remote_config.write_remote_codex_auth

    def record_write(*args, **kwargs):
        writes.append(True)
        return original_write(*args, **kwargs)

    def fail_config(*_args, **_kwargs):
        raise OSError("synthetic later config write failure")

    monkeypatch.setattr(remote_config, "read_remote_codex_auth", rotate_before_strict_read)
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", record_write)
    monkeypatch.setattr(remote_config, "write_remote_codex_config", fail_config)
    with pytest.raises(RuntimeError, match="未写入.*停止远端 Codex"):
        sync_manager.sync_codex_account_to_server("remote", "official")
    assert remote_accounts.files == expected_files
    assert not writes


@pytest.mark.parametrize("path", [
    "/remote/codex/auth.json", "/remote/codex/config.toml", "/remote/codex/.env",
    "/home/test/.api_switcher_env", "/home/test/.profile", "/home/test/.bashrc",
])
def test_prewrite_drift_of_any_rollback_file_never_writes_or_rolls_back(remote_accounts, monkeypatch, path):
    remote_accounts.auth = _auth("selected", "2026-10-01T00:00:00Z")
    original_read = remote_config.read_remote_codex_config
    changed_content = "synthetic externally updated file\r\n"
    expected = dict(remote_accounts.files)
    expected[path] = changed_content

    def change_after_config_read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        remote_accounts.files[path] = changed_content
        return result

    def no_write(*_args, **_kwargs):
        pytest.fail("Prewrite drift must neither write nor enter rollback")

    monkeypatch.setattr(remote_config, "read_remote_codex_config", change_after_config_read)
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", no_write)
    monkeypatch.setattr(remote_config, "write_remote_text", no_write)
    monkeypatch.setattr(remote_config, "delete_remote_file", no_write)
    with pytest.raises(RuntimeError, match="读取期间.*未写入"):
        sync_manager.sync_codex_account_to_server("remote", "official")
    assert remote_accounts.files == expected


def test_prewrite_recheck_read_error_never_writes_or_rolls_back(remote_accounts, monkeypatch):
    remote_accounts.auth = _auth("selected", "2026-10-01T00:00:00Z")
    before = dict(remote_accounts.files)
    original_config_read = remote_config.read_remote_codex_config
    original_text_read = remote_config.read_remote_text
    checks = False

    def config_read(*args, **kwargs):
        nonlocal checks
        result = original_config_read(*args, **kwargs)
        checks = True
        return result

    def unreadable_recheck(*args, **kwargs):
        if checks:
            raise OSError("synthetic disconnected before write")
        return original_text_read(*args, **kwargs)

    def no_write(*_args, **_kwargs):
        pytest.fail("Unreadable prewrite state must neither write nor roll back")

    monkeypatch.setattr(remote_config, "read_remote_codex_config", config_read)
    monkeypatch.setattr(remote_config, "read_remote_text", unreadable_recheck)
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", no_write)
    monkeypatch.setattr(remote_config, "write_remote_text", no_write)
    monkeypatch.setattr(remote_config, "delete_remote_file", no_write)
    with pytest.raises(RuntimeError, match="无法复核.*未写入"):
        sync_manager.sync_codex_account_to_server("remote", "official")
    assert remote_accounts.files == before
