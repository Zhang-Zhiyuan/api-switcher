import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import auth_parser, codex_env, parser, persistent_env, profile_manager, switcher, toml_parser, vscode_parser


def _files(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    paths = {
        "settings": tmp_path / "settings.json",
        "claude_config": tmp_path / "claude-config.json",
        "credentials": tmp_path / "credentials.json",
        "vscode": tmp_path / "vscode.json",
        "codex_config": tmp_path / "config.toml",
        "auth": tmp_path / "auth.json",
        "dotenv": tmp_path / ".env",
        "profiles": tmp_path / "profiles.json",
        "profiles_backup": tmp_path / "profiles.backup",
    }
    for key, path in paths.items():
        if key != "vscode":
            path.write_bytes(("\ufeff" + key + "\r\n").encode("utf-8"))
    monkeypatch.setattr(parser, "CLAUDE_SETTINGS", paths["settings"])
    monkeypatch.setattr(parser, "CLAUDE_CONFIG", paths["claude_config"])
    monkeypatch.setattr(parser, "CLAUDE_CREDENTIALS", paths["credentials"])
    monkeypatch.setattr(vscode_parser, "VSCODE_SETTINGS", paths["vscode"])
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", paths["codex_config"])
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", paths["auth"])
    monkeypatch.setattr(codex_env.paths, "CODEX_ENV", paths["dotenv"])
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", paths["profiles"])
    monkeypatch.setattr(switcher.backup_manager, "create_backup", lambda _reason: None)
    monkeypatch.setattr(switcher, "_ensure_switch_target_healthy", lambda _kind, _name: None)
    monkeypatch.setattr(switcher.usage_recorder, "start_session", lambda _name, _kind: None)
    return paths


def _snapshot(paths: dict[str, Path]) -> dict[str, tuple[bool, bytes]]:
    return {key: (path.exists(), path.read_bytes() if path.exists() else b"") for key, path in paths.items()}


def _assert_snapshot(paths: dict[str, Path], before: dict[str, tuple[bool, bytes]]) -> None:
    for key, path in paths.items():
        assert path.exists() is before[key][0], key
        if before[key][0]:
            assert path.read_bytes() == before[key][1], key


def _fault(failure_stage: str):
    state = {"failed": False}

    def stage(label: str) -> None:
        if label == failure_stage and not state["failed"]:
            state["failed"] = True
            raise OSError(f"injected: {label}")

    return stage


def _mutate_profile_store(paths: dict[str, Path], stage, label: str):
    """Model profile_manager._save_store(), including its backup side effect."""

    def write(_data):
        if paths["profiles"].exists():
            paths["profiles_backup"].write_bytes(paths["profiles"].read_bytes())
        paths["profiles"].write_bytes(label.encode())
        stage(label)

    return write


@pytest.mark.parametrize("failure_stage", ["settings", "config", "vscode", "active_api", "active_account"])
@pytest.mark.parametrize("profiles_backup_existed", [True, False], ids=["backup-present", "backup-absent"])
def test_claude_api_switch_rolls_back_every_write_stage(
    tmp_path, monkeypatch, failure_stage, profiles_backup_existed
):
    paths = _files(tmp_path, monkeypatch)
    if not profiles_backup_existed:
        paths["profiles_backup"].unlink()
    before = _snapshot(paths)
    stage = _fault(failure_stage)
    target = SimpleNamespace(
        name="new", auth_token_ref="secret", primary_api_key_ref=None,
        permissions_mode="default", skip_dangerous_prompt=False, model="model",
    )
    monkeypatch.setattr(profile_manager, "list_switchable_claude_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "is_third_party_claude_profile", lambda _target: True)
    monkeypatch.setattr(switcher.security, "get_secret", lambda _ref: "token")
    monkeypatch.setattr(parser, "read_claude_settings", lambda: {})
    monkeypatch.setattr(parser, "apply_claude_profile", lambda _data, _target: {"new": True})
    monkeypatch.setattr(parser, "read_claude_config", lambda: {})
    monkeypatch.setattr(parser, "apply_claude_config", lambda _data, _target: {"new": True})
    monkeypatch.setattr(vscode_parser, "read_vscode_settings", lambda: {})
    monkeypatch.setattr(vscode_parser, "apply_permission_mode", lambda data, *_args: data)
    monkeypatch.setattr(vscode_parser, "apply_model", lambda data, *_args: data)

    def mutate(path, label):
        def write(_data):
            path.write_bytes(label.encode())
            stage(label)
        return write

    monkeypatch.setattr(parser, "write_claude_settings", mutate(paths["settings"], "settings"))
    monkeypatch.setattr(parser, "write_claude_config", mutate(paths["claude_config"], "config"))
    monkeypatch.setattr(vscode_parser, "write_vscode_settings", mutate(paths["vscode"], "vscode"))
    monkeypatch.setattr(profile_manager, "set_active_claude", _mutate_profile_store(paths, stage, "active_api"))
    monkeypatch.setattr(
        profile_manager, "set_active_claude_account", _mutate_profile_store(paths, stage, "active_account")
    )

    with pytest.raises((OSError, RuntimeError), match="injected"):
        switcher.switch_claude_profile("new")
    _assert_snapshot(paths, before)


@pytest.mark.parametrize(
    "failure_stage",
    ["credentials", "persistent_delete", "settings", "config", "vscode", "active_account", "active_api"],
)
@pytest.mark.parametrize("profiles_backup_existed", [True, False], ids=["backup-present", "backup-absent"])
def test_claude_account_switch_rolls_back_every_write_stage(
    tmp_path, monkeypatch, failure_stage, profiles_backup_existed
):
    paths = _files(tmp_path, monkeypatch)
    if not profiles_backup_existed:
        paths["profiles_backup"].unlink()
    before = _snapshot(paths)
    stage = _fault(failure_stage)
    registry = {
        "ANTHROPIC_AUTH_TOKEN": "registry-token",
        "KEEP_UNRELATED": "registry-keep",
    }
    _fake_windows_env(monkeypatch, stage, registry)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "process-token")
    monkeypatch.setenv("KEEP_UNRELATED", "process-keep")
    target = SimpleNamespace(name="official")
    monkeypatch.setattr(profile_manager, "list_claude_account_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "load_claude_account_credentials", lambda _target: {"oauth": "new"})
    monkeypatch.setattr(parser, "read_claude_settings", lambda: {})
    monkeypatch.setattr(parser, "clear_claude_api_overrides", lambda data: data)
    monkeypatch.setattr(parser, "read_claude_config", lambda: {})
    monkeypatch.setattr(parser, "clear_claude_config_auth", lambda _data: {"official": True})
    monkeypatch.setattr(vscode_parser, "read_vscode_settings", lambda: {})
    monkeypatch.setattr(vscode_parser, "clear_claude_profile_overrides", lambda data, _model="": data)

    def mutate(path, label):
        def write(_data):
            path.write_bytes(label.encode())
            stage(label)
        return write

    monkeypatch.setattr(parser, "write_claude_credentials", mutate(paths["credentials"], "credentials"))
    monkeypatch.setattr(parser, "write_claude_settings", mutate(paths["settings"], "settings"))
    monkeypatch.setattr(parser, "write_claude_config", mutate(paths["claude_config"], "config"))
    monkeypatch.setattr(vscode_parser, "write_vscode_settings", mutate(paths["vscode"], "vscode"))
    monkeypatch.setattr(
        profile_manager, "set_active_claude_account", _mutate_profile_store(paths, stage, "active_account")
    )
    monkeypatch.setattr(profile_manager, "set_active_claude", _mutate_profile_store(paths, stage, "active_api"))

    with pytest.raises((OSError, RuntimeError), match="injected"):
        switcher.switch_claude_account("official")
    _assert_snapshot(paths, before)
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "process-token"
    assert os.environ.get("KEEP_UNRELATED") == "process-keep"
    assert registry == {
        "ANTHROPIC_AUTH_TOKEN": "registry-token",
        "KEEP_UNRELATED": "registry-keep",
    }


def test_claude_account_switch_clears_only_managed_process_and_persistent_env(tmp_path, monkeypatch):
    _files(tmp_path, monkeypatch)
    stage = _fault("never")
    registry = {
        "ANTHROPIC_AUTH_TOKEN": "registry-token",
        "ANTHROPIC_BASE_URL": "https://relay.example.test",
        "KEEP_UNRELATED": "registry-keep",
    }
    _fake_windows_env(monkeypatch, stage, registry)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "process-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://relay.example.test")
    monkeypatch.setenv("KEEP_UNRELATED", "process-keep")
    target = SimpleNamespace(name="official")
    monkeypatch.setattr(profile_manager, "list_claude_account_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "load_claude_account_credentials", lambda _target: {"oauth": "new"})
    monkeypatch.setattr(parser, "write_claude_credentials", lambda _data: None)
    monkeypatch.setattr(parser, "read_claude_settings", lambda: {})
    monkeypatch.setattr(parser, "write_claude_settings", lambda _data: None)
    monkeypatch.setattr(parser, "read_claude_config", lambda: {})
    monkeypatch.setattr(vscode_parser, "read_vscode_settings", lambda: {})
    monkeypatch.setattr(vscode_parser, "write_vscode_settings", lambda _data: None)
    monkeypatch.setattr(profile_manager, "set_active_claude_account", lambda _name: None)
    monkeypatch.setattr(profile_manager, "set_active_claude", lambda _name: None)

    switcher.switch_claude_account("official")

    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
    assert "ANTHROPIC_BASE_URL" not in os.environ
    assert os.environ.get("KEEP_UNRELATED") == "process-keep"
    assert registry == {"KEEP_UNRELATED": "registry-keep"}


def _fake_windows_env(monkeypatch, stage, registry):
    monkeypatch.setattr(switcher, "_is_windows", lambda: True)
    monkeypatch.setattr(persistent_env, "_local_user_env_value", lambda name: registry.get(name))
    monkeypatch.setattr(persistent_env, "_local_user_env_value_strict", lambda name: registry.get(name))

    def delete(names):
        for name in names:
            registry.pop(name, None)
            os.environ.pop(name, None)
        stage("persistent_delete")

    def set_values(updates):
        registry.update(updates)
        os.environ.update(updates)
        stage("persistent_set")

    monkeypatch.setattr(persistent_env, "delete_local_user_env", delete)
    monkeypatch.setattr(persistent_env, "set_local_user_env", set_values)


def test_switch_aborts_before_writes_when_persistent_env_snapshot_is_unreadable(
    tmp_path,
    monkeypatch,
):
    paths = _files(tmp_path, monkeypatch)
    before = _snapshot(paths)
    target = SimpleNamespace(name="official")
    monkeypatch.setattr(profile_manager, "list_claude_account_profiles", lambda: [target])
    monkeypatch.setattr(
        profile_manager,
        "load_claude_account_credentials",
        lambda _target: {"oauth": "new"},
    )
    monkeypatch.setattr(switcher, "_is_windows", lambda: True)
    monkeypatch.setattr(
        persistent_env,
        "_local_user_env_value_strict",
        lambda _name: (_ for _ in ()).throw(PermissionError("registry read denied")),
    )
    write_calls = []
    monkeypatch.setattr(
        parser,
        "write_claude_credentials",
        lambda _data: write_calls.append("credentials"),
    )

    with pytest.raises(PermissionError, match="registry read denied"):
        switcher.switch_claude_account("official")

    assert write_calls == []
    _assert_snapshot(paths, before)


@pytest.mark.parametrize(
    "failure_stage",
    ["dotenv_delete", "dotenv_set", "persistent_delete", "persistent_set", "config", "auth", "active_api", "active_account"],
)
@pytest.mark.parametrize("profiles_backup_existed", [True, False], ids=["backup-present", "backup-absent"])
def test_codex_api_switch_restores_files_process_and_persistent_env(
    tmp_path, monkeypatch, failure_stage, profiles_backup_existed
):
    paths = _files(tmp_path, monkeypatch)
    if not profiles_backup_existed:
        paths["profiles_backup"].unlink()
    before = _snapshot(paths)
    stage = _fault(failure_stage)
    registry = {"OLD_KEY": "registry-old"}
    _fake_windows_env(monkeypatch, stage, registry)
    monkeypatch.setenv("OLD_KEY", "process-old")
    monkeypatch.delenv("NEW_KEY", raising=False)
    target = SimpleNamespace(
        name="new", api_key_ref="key", custom_requires_openai_auth=False,
        validated_env_key=lambda: "NEW_KEY",
    )
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "is_third_party_codex_profile", lambda _target: True)
    monkeypatch.setattr(switcher.security, "get_secret", lambda _ref: "new-secret")
    monkeypatch.setattr(switcher.ProviderRegistry, "get_codex_runtime_env_keys_for_profile", lambda _target: ["NEW_KEY"])
    monkeypatch.setattr(switcher, "_codex_active_api_env_names_to_clear", lambda _config: ["OLD_KEY"])
    monkeypatch.setattr(toml_parser, "read_codex_config", lambda: {})
    monkeypatch.setattr(toml_parser, "apply_codex_profile", lambda _data, _target: {"new": True})
    monkeypatch.setattr(auth_parser, "read_codex_auth", lambda: {})
    monkeypatch.setattr(auth_parser, "apply_codex_apikey", lambda data, _target: data)

    def dotenv_delete(_names):
        paths["dotenv"].write_bytes(b"deleted")
        stage("dotenv_delete")

    def dotenv_set(_updates):
        paths["dotenv"].write_bytes(b"set")
        stage("dotenv_set")

    def mutate(path, label):
        def write(_data):
            path.write_bytes(label.encode())
            stage(label)
        return write

    monkeypatch.setattr(codex_env, "delete_codex_env", dotenv_delete)
    monkeypatch.setattr(codex_env, "set_codex_env", dotenv_set)
    monkeypatch.setattr(toml_parser, "write_codex_config", mutate(paths["codex_config"], "config"))
    monkeypatch.setattr(auth_parser, "write_codex_auth", mutate(paths["auth"], "auth"))
    monkeypatch.setattr(profile_manager, "set_active_codex", _mutate_profile_store(paths, stage, "active_api"))
    monkeypatch.setattr(
        profile_manager, "set_active_codex_account", _mutate_profile_store(paths, stage, "active_account")
    )

    with pytest.raises((OSError, RuntimeError), match="injected"):
        switcher.switch_codex_profile("new")
    _assert_snapshot(paths, before)
    assert os.environ.get("OLD_KEY") == "process-old"
    assert "NEW_KEY" not in os.environ
    assert registry == {"OLD_KEY": "registry-old"}


@pytest.mark.parametrize(
    "failure_stage", ["auth", "dotenv_delete", "persistent_delete", "config", "active_account", "active_api"]
)
@pytest.mark.parametrize("profiles_backup_existed", [True, False], ids=["backup-present", "backup-absent"])
def test_codex_account_switch_restores_files_process_and_persistent_env(
    tmp_path, monkeypatch, failure_stage, profiles_backup_existed
):
    paths = _files(tmp_path, monkeypatch)
    if not profiles_backup_existed:
        paths["profiles_backup"].unlink()
    before = _snapshot(paths)
    stage = _fault(failure_stage)
    registry = {"OLD_KEY": "registry-old"}
    _fake_windows_env(monkeypatch, stage, registry)
    monkeypatch.setenv("OLD_KEY", "process-old")
    target = SimpleNamespace(name="official")
    monkeypatch.setattr(profile_manager, "list_codex_account_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "load_codex_account_auth", lambda _target: {"tokens": {"id": "new"}})
    monkeypatch.setattr(switcher, "_codex_api_env_names_to_clear", lambda _config: ["OLD_KEY"])
    monkeypatch.setattr(toml_parser, "read_codex_config", lambda: {})
    monkeypatch.setattr(toml_parser, "apply_codex_official_account", lambda _data: {"official": True})

    def mutate(path, label):
        def write(_data):
            path.write_bytes(label.encode())
            stage(label)
        return write

    monkeypatch.setattr(auth_parser, "write_codex_auth", mutate(paths["auth"], "auth"))

    def dotenv_delete(_names):
        paths["dotenv"].write_bytes(b"deleted")
        stage("dotenv_delete")

    monkeypatch.setattr(codex_env, "delete_codex_env", dotenv_delete)
    monkeypatch.setattr(toml_parser, "write_codex_config", mutate(paths["codex_config"], "config"))
    monkeypatch.setattr(
        profile_manager, "set_active_codex_account", _mutate_profile_store(paths, stage, "active_account")
    )
    monkeypatch.setattr(profile_manager, "set_active_codex", _mutate_profile_store(paths, stage, "active_api"))

    with pytest.raises((OSError, RuntimeError), match="injected"):
        switcher.switch_codex_account("official")
    _assert_snapshot(paths, before)
    assert os.environ.get("OLD_KEY") == "process-old"
    assert registry == {"OLD_KEY": "registry-old"}


def test_switch_lock_prevents_overlapping_switches():
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    @switcher._serialized_switch
    def work(index):
        if index == 1:
            first_entered.set()
            assert release_first.wait(2)
        else:
            second_entered.set()

    first = threading.Thread(target=work, args=(1,))
    second = threading.Thread(target=work, args=(2,))
    first.start()
    assert first_entered.wait(2)
    second.start()
    assert not second_entered.wait(0.1)
    release_first.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()
