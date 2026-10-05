"""Synthetic duplicate account cards must not export/push older refresh bundles."""
import copy
import json
import socket

import pytest

from core import account_transfer, auth_parser, profile_manager, sync_manager, toml_parser
from test_account_transfer import account_env as _account_env_fixture, _save, _write_current
from test_account_transfer_activation import _credentials
from test_remote_sync_transactions import _install_remote


account_env = _account_env_fixture


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("account freshness tests must not connect or refresh tokens")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


def _snapshots():
    older = _credentials("codex", revision="older")
    newer = _credentials("codex", revision="newer")
    older["last_refresh"], newer["last_refresh"] = "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"
    return older, newer


@pytest.mark.parametrize("current_mode", ["other-account", "api", "same-older"])
def test_named_export_uses_newer_complete_saved_duplicate_without_mutation(account_env, current_mode):
    directory, secrets = account_env
    older, newer = _snapshots()
    _save("codex", "chosen-old-card", older)
    _save("codex", "newer-duplicate", newer)
    current = older if current_mode == "same-older" else _credentials("codex", identity="other-account")
    _write_current("codex", current)
    if current_mode == "api":
        toml_parser.write_codex_config({"cli_auth_credentials_store": "file", "model_provider": "synthetic-relay"})
    before = (dict(secrets), auth_parser.CODEX_AUTH.read_bytes(), toml_parser.CODEX_CONFIG.read_bytes(),
              profile_manager.PROFILES_FILE.read_bytes())
    result = account_transfer.export_account_login(directory / "chosen.asxaccount", "", "codex", "chosen-old-card")
    assert account_transfer._read_payload(result.path, "")["credentials"] == newer
    assert result.account_name == "chosen-old-card" and not result.used_current_login
    assert (dict(secrets), auth_parser.CODEX_AUTH.read_bytes(), toml_parser.CODEX_CONFIG.read_bytes(),
            profile_manager.PROFILES_FILE.read_bytes()) == before


def test_named_push_uses_newer_saved_duplicate_not_older_chosen_card(account_env, monkeypatch):
    _, secrets = account_env
    older, newer = _snapshots()
    _save("codex", "chosen-old-card", older)
    _save("codex", "newer-duplicate", newer)
    _write_current("codex", _credentials("codex", identity="other-account"))
    before = (dict(secrets), auth_parser.CODEX_AUTH.read_bytes(), profile_manager.PROFILES_FILE.read_bytes())
    state, _, _ = _install_remote(monkeypatch, "never")
    message = sync_manager.sync_codex_account_to_server("synthetic-server", "chosen-old-card")
    assert json.loads(state["/remote/codex/auth.json"]) == newer
    assert "较新" in message
    assert (dict(secrets), auth_parser.CODEX_AUTH.read_bytes(), profile_manager.PROFILES_FILE.read_bytes()) == before


def test_current_login_export_remains_exact_even_with_newer_saved_duplicate(account_env):
    directory, _ = account_env
    older, newer = _snapshots()
    _save("codex", "newer-duplicate", newer)
    _write_current("codex", older)
    result = account_transfer.export_account_login(directory / "current.asxaccount", "", "codex")
    assert result.used_current_login
    assert account_transfer._read_payload(result.path, "")["credentials"] == older


def test_named_export_keeps_newest_live_bundle_over_saved_duplicates(account_env):
    directory, secrets = account_env
    older, newer = _snapshots()
    _save("codex", "chosen", older)
    _save("codex", "duplicate", newer)
    current = _credentials("codex", revision="newest-live")
    current["last_refresh"] = "2026-10-03T00:00:00Z"
    _write_current("codex", current)
    before = dict(secrets)
    result = account_transfer.export_account_login(directory / "current-newer.asxaccount", "", "codex", "chosen")
    assert result.used_current_login
    assert account_transfer._read_payload(result.path, "")["credentials"] == current
    assert secrets == before


def test_sparse_requested_identity_does_not_bridge_conflicting_saved_workspaces(account_env):
    original, first = _snapshots()
    original["tokens"].pop("account_id")
    first["tokens"]["account_id"] = "workspace-a"
    second = copy.deepcopy(first)
    second["tokens"]["account_id"] = "workspace-b"
    second["last_refresh"] = "2026-10-03T00:00:00Z"
    _save("codex", "first-newer", first)
    _save("codex", "second-conflicting", second)
    assert profile_manager.newest_saved_codex_account_auth(original) == first


def test_display_name_alone_does_not_choose_another_saved_bundle(account_env):
    from test_account_transfer import _credentials as unnamed_credentials
    original = unnamed_credentials("codex", token="synthetic-original")
    original.pop("email")
    original["tokens"].pop("account_id")
    original["name"] = "same display name"
    candidate = copy.deepcopy(original)
    candidate["tokens"]["access_token"] = "synthetic-other"
    candidate["last_refresh"] = "2026-10-03T00:00:00Z"
    _save("codex", "other-card", candidate)
    assert profile_manager.newest_saved_codex_account_auth(original) == original


@pytest.mark.parametrize("invalid", ["workspace", "missing-refresh", "bad-id-token", "unknown-order"])
def test_named_export_does_not_adopt_conflicting_incomplete_or_unordered_duplicate(account_env, invalid):
    directory, _ = account_env
    older, candidate = _snapshots()
    _save("codex", "chosen", older)
    if invalid == "workspace":
        candidate["tokens"]["account_id"] = "different-workspace"
    elif invalid == "missing-refresh":
        candidate["tokens"].pop("refresh_token")
    elif invalid == "bad-id-token":
        candidate["tokens"]["id_token"] = "synthetic-not-jwt"
    else:
        candidate.pop("last_refresh")
    _save("codex", "candidate", candidate)
    _write_current("codex", _credentials("codex", identity="unrelated"))
    result = account_transfer.export_account_login(directory / "guarded.asxaccount", "", "codex", "chosen")
    assert account_transfer._read_payload(result.path, "")["credentials"] == older


def test_newer_remote_bundle_still_beats_newer_local_duplicate(account_env, monkeypatch):
    older, newer = _snapshots()
    _save("codex", "chosen", older)
    _save("codex", "newer", newer)
    _write_current("codex", _credentials("codex", identity="unrelated"))
    state, _, _ = _install_remote(monkeypatch, "never")
    remote = copy.deepcopy(newer)
    remote["tokens"]["refresh_token"] = "synthetic-even-newer-remote"
    remote["last_refresh"] = "2026-10-03T00:00:00Z"
    state["/remote/codex/auth.json"] = json.dumps(remote)
    state["/remote/codex/config.toml"] = 'cli_auth_credentials_store = "file"\n'
    sync_manager.sync_codex_account_to_server("synthetic-server", "chosen")
    assert json.loads(state["/remote/codex/auth.json"]) == remote


def test_push_using_newer_duplicate_retains_exact_rollback(account_env, monkeypatch):
    older, newer = _snapshots()
    _save("codex", "chosen", older)
    _save("codex", "newer", newer)
    _write_current("codex", _credentials("codex", identity="unrelated"))
    state, _, _ = _install_remote(monkeypatch, "codex_config")
    before = dict(state)
    with pytest.raises(OSError, match="injected"):
        sync_manager.sync_codex_account_to_server("synthetic-server", "chosen")
    assert state == before
