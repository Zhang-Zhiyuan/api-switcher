"""Isolated migration, failure-injection and deterministic account-churn labs.

All credentials, files and Windows environment storage are supplied by the
synthetic two-machine fixture. No network login or credential refresh is used.
"""
import copy
from datetime import datetime, timedelta, timezone
import os
import random

import pytest

from core import account_transfer as transfer, atomic_io
from core import auth_parser, parser, persistent_env, profile_manager, toml_parser
import test_account_transfer_activation as harness


transfer_machine = harness.transfer_machine


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("password", ["", harness.PASSWORD], ids=["plain", "encrypted"])
@pytest.mark.parametrize("destination", ["clean", "other-account", "api"])
def test_two_machine_matrix_preserves_state_until_switch(transfer_machine, tmp_path, kind, password, destination):
    transfer_machine.use("source")
    incoming = harness._credentials(kind, identity="source-user", revision="source")
    transfer_machine.live(kind, incoming)
    package = tmp_path / "迁移 文件夹" / (kind + " 账号.asxaccount")
    source_before = harness._file_snapshot(harness._runtime_files())
    transfer.export_account_login(package, password, kind)
    assert harness._file_snapshot(harness._runtime_files()) == source_before
    assert not transfer_machine.secrets

    transfer_machine.use("destination")
    previous = harness._credentials(kind, identity="destination-user", revision="local")
    if destination != "clean":
        transfer_machine.live(kind, previous)
    if destination == "api":
        if kind == "codex":
            toml_parser.write_codex_config({"cli_auth_credentials_store": "file", "model_provider": "relay",
                "model_providers": {"relay": {"base_url": "https://relay.example.test/v1", "env_key": "RELAY_API_KEY"}}})
        else:
            parser.write_claude_settings({"env": {"ANTHROPIC_AUTH_TOKEN": "synthetic-relay"}})
    runtime = harness._file_snapshot(harness._runtime_files())
    environment, registry = dict(os.environ), dict(transfer_machine.registry)
    imported = transfer.import_account_login(package, password, kind)
    assert harness._file_snapshot(harness._runtime_files()) == runtime
    assert dict(os.environ) == environment and transfer_machine.registry == registry
    repeated = transfer.import_account_login(package, password, kind)
    assert not repeated.created_new and repeated.account_name == imported.account_name
    harness._activate(kind, imported.account_name)
    assert harness._token_pair(kind, harness._read_live(kind)) == harness._token_pair(kind, incoming)
    if destination != "clean":
        previous_profile = next(item for item in harness._saved(kind)
                                if harness._token_pair(kind, harness._saved_value(kind, item))
                                == harness._token_pair(kind, previous))
        harness._activate(kind, previous_profile.name)
        assert harness._token_pair(kind, harness._read_live(kind)) == harness._token_pair(kind, previous)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        assert os.environ.get(key) == environment.get(key)
    assert transfer_machine.registry == registry


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("stage", ["credentials", "config", "registry", "active-marker"])
def test_activation_failure_with_existing_login_preserves_recoverable_pairs(transfer_machine, monkeypatch, kind, stage):
    package, incoming = transfer_machine.exported(kind)
    previous = harness._credentials(kind, identity="destination-user", revision="local")
    transfer_machine.live(kind, previous)
    imported = transfer.import_account_login(package, harness.PASSWORD, kind)
    runtime = harness._file_snapshot(harness._runtime_files())
    environment, registry = dict(os.environ), dict(transfer_machine.registry)
    active = (profile_manager.get_active_codex_account_name() if kind == "codex"
              else profile_manager.get_active_claude_account_name())
    actions = {
        "credentials": (auth_parser, "write_codex_auth") if kind == "codex" else (parser, "write_claude_credentials"),
        "config": (toml_parser, "write_codex_config") if kind == "codex" else (parser, "write_claude_settings"),
        "registry": (persistent_env, "delete_local_user_env"),
        "active-marker": (profile_manager, "set_active_codex_account" if kind == "codex" else "set_active_claude_account"),
    }
    module, method = actions[stage]
    original = getattr(module, method)
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        result = original(*args, **kwargs)
        if not failed:
            failed = True
            raise OSError("synthetic failure after " + stage)
        return result

    monkeypatch.setattr(module, method, fail_once)
    with pytest.raises((OSError, RuntimeError), match="synthetic failure"):
        harness._activate(kind, imported.account_name)
    assert failed
    assert harness._file_snapshot(harness._runtime_files()) == runtime
    assert dict(os.environ) == environment and transfer_machine.registry == registry
    assert (profile_manager.get_active_codex_account_name() if kind == "codex"
            else profile_manager.get_active_claude_account_name()) == active
    stored_pairs = {harness._token_pair(kind, harness._saved_value(kind, item)) for item in harness._saved(kind)}
    assert harness._token_pair(kind, incoming) in stored_pairs
    assert harness._token_pair(kind, previous) in stored_pairs
    # A normal retry must work after the injected transient failure.
    harness._activate(kind, imported.account_name)
    assert harness._token_pair(kind, harness._read_live(kind)) == harness._token_pair(kind, incoming)


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_export_publish_failure_keeps_previous_bundle_and_cleans_temporary_file(transfer_machine, tmp_path, monkeypatch, kind):
    transfer_machine.use("source")
    transfer_machine.live(kind, harness._credentials(kind))
    package = tmp_path / "existing.asxaccount"
    package.write_bytes(b"synthetic prior package")
    before = harness._file_snapshot(harness._runtime_files())
    original = atomic_io.replace_with_retry

    def fail_publish(source, target, *args, **kwargs):
        if target == package:
            assert source.is_file()
            raise PermissionError("synthetic locked export target")
        return original(source, target, *args, **kwargs)

    monkeypatch.setattr(atomic_io, "replace_with_retry", fail_publish)
    with pytest.raises(PermissionError, match="synthetic locked"):
        transfer.export_account_login(package, "", kind)
    assert package.read_bytes() == b"synthetic prior package"
    assert not list(tmp_path.glob("existing.asxaccount.*.tmp"))
    assert harness._file_snapshot(harness._runtime_files()) == before
    assert not transfer_machine.secrets


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_ambiguous_display_name_dedup_cannot_erase_requested_pair(transfer_machine, tmp_path, kind):
    transfer_machine.use("source")
    if kind == "codex":
        incoming = {"name": "同名显示", "auth_mode": "chatgpt", "tokens": {
            "id_token": "synthetic.eyJzeW50aGV0aWMiOnRydWV9.signature",
            "access_token": "synthetic-incoming-access", "refresh_token": "synthetic-incoming-refresh",
        }}
    else:
        incoming = {"name": "同名显示", "claudeAiOauth": {
            "accessToken": "synthetic-incoming-access", "refreshToken": "synthetic-incoming-refresh",
        }}
    transfer_machine.live(kind, incoming)
    package = tmp_path / "ambiguous.asxaccount"
    transfer.export_account_login(package, "", kind)
    transfer_machine.use("destination")
    saved = harness._save_current(kind, incoming)
    set_active = (profile_manager.set_active_codex_account if kind == "codex"
                  else profile_manager.set_active_claude_account)
    set_active(saved.name)
    previous = copy.deepcopy(incoming)
    tokens = previous["tokens"] if kind == "codex" else previous["claudeAiOauth"]
    tokens["access_token" if kind == "codex" else "accessToken"] = "synthetic-live-access"
    tokens["refresh_token" if kind == "codex" else "refreshToken"] = "synthetic-live-refresh"
    transfer_machine.live(kind, previous)
    imported = transfer.import_account_login(package, "", kind)
    assert imported.created_new and imported.account_name != saved.name
    harness._activate(kind, imported.account_name)
    assert harness._token_pair(kind, harness._read_live(kind)) == harness._token_pair(kind, incoming)
    assert any(harness._token_pair(kind, harness._saved_value(kind, item)) == harness._token_pair(kind, previous)
               for item in harness._saved(kind))


@pytest.mark.parametrize("seed", [7, 19, 41, 73])
def test_codex_repeated_rotation_import_and_switch_never_revives_old_pair(transfer_machine, seed):
    package, initial = transfer_machine.exported("codex")
    other = harness._credentials("codex", identity="other-user", revision="other-initial")
    transfer_machine.live("codex", other)
    other_profile = harness._save_current("codex", other)
    imported = transfer.import_account_login(package, harness.PASSWORD, "codex")
    names = {initial["tokens"]["account_id"]: imported.account_name, "other-user": other_profile.name}
    expected = {initial["tokens"]["account_id"]: initial, "other-user": other}
    rng = random.Random(seed)
    registry = dict(transfer_machine.registry)
    for step in range(24):
        action = rng.choice(("rotate", "switch", "old-import"))
        if action == "rotate":
            identity = harness._read_live("codex")["tokens"]["account_id"]
            fresh = harness._credentials("codex", identity=identity, revision=f"rotation-{seed}-{step}")
            fresh["last_refresh"] = (datetime(2026, 9, 22, tzinfo=timezone.utc)
                                     + timedelta(minutes=step)).isoformat()
            transfer_machine.live("codex", fresh)
            expected[identity] = copy.deepcopy(fresh)
        elif action == "switch":
            harness._activate("codex", rng.choice(list(names.values())))
        else:
            before = harness._file_snapshot(harness._runtime_files())
            transfer.import_account_login(package, harness.PASSWORD, "codex")
            assert harness._file_snapshot(harness._runtime_files()) == before
        live = harness._read_live("codex")
        assert harness._token_pair("codex", live) == harness._token_pair("codex", expected[live["tokens"]["account_id"]])
    for identity, name in names.items():
        harness._activate("codex", name)
        assert harness._token_pair("codex", harness._read_live("codex")) == harness._token_pair("codex", expected[identity])
    assert transfer_machine.registry == registry
