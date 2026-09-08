import json
import os

import pytest

from config import paths
from core import backup_manager, persistent_env, switcher


@pytest.fixture
def codex_backup(monkeypatch, tmp_path):
    files = {name: tmp_path / name for name in ("codex_config.toml", "codex_auth.json", "codex_env")}
    monkeypatch.setattr(backup_manager, "BACKUP_FILES", files)
    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(switcher, "_is_windows", lambda: True)
    registry = {}
    monkeypatch.setattr(persistent_env, "_local_user_env_value_strict", registry.get)

    def set_env(updates):
        registry.update(updates)
        os.environ.update(updates)

    def delete_env(names):
        for name in names:
            registry.pop(name, None)
            os.environ.pop(name, None)

    monkeypatch.setattr(persistent_env, "set_local_user_env", set_env)
    monkeypatch.setattr(persistent_env, "delete_local_user_env", delete_env)
    for name in ("OPENAI_API_KEY", "RELAY_API_KEY", "OTHER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return files, registry


def test_default_backup_includes_codex_dotenv():
    assert backup_manager.BACKUP_FILES["codex_env"] == paths.CODEX_ENV


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", "RELAY_API_KEY"])
def test_codex_restore_matches_config_dotenv_and_owned_environment(codex_backup, key):
    files, registry = codex_backup
    original_config = f'model_provider = "relay"\n[model_providers.relay]\nenv_key = "{key}"\n'
    files["codex_config.toml"].write_text(original_config, encoding="utf-8")
    original_env = f'# preserve comments\r\n{key}="test-a"\r\nUNRELATED=value\r\n'.encode()
    files["codex_env"].write_bytes(original_env)
    entry = backup_manager.create_backup("A")
    files["codex_config.toml"].write_text(original_config + 'base_url = "https://b.example.test"\n', encoding="utf-8")
    files["codex_env"].write_text(f'{key}="test-b"\n', encoding="utf-8")
    registry[key] = os.environ[key] = "test-b"

    restored = backup_manager.restore_backup(entry)
    assert "codex_env" in restored
    assert files["codex_env"].read_bytes() == original_env
    assert files["codex_config.toml"].read_text(encoding="utf-8") == original_config
    assert registry[key] == os.environ[key] == "test-a"
    safety = backup_manager.get_latest_backup()
    assert b"test-b" in (safety.directory / "codex_env").read_bytes()


def test_restore_account_snapshot_clears_later_owned_api_key(codex_backup):
    files, registry = codex_backup
    files["codex_auth.json"].write_text('{"tokens":{"access_token":"test-oauth"}}', encoding="utf-8")
    entry = backup_manager.create_backup("account without dotenv")
    files["codex_env"].write_text('OPENAI_API_KEY="test-b"\n', encoding="utf-8")
    files["codex_auth.json"].write_text('{"OPENAI_API_KEY":"test-b"}', encoding="utf-8")
    registry["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"] = "test-b"
    backup_manager.restore_backup(entry)
    assert not files["codex_env"].exists()
    assert "tokens" in json.loads(files["codex_auth.json"].read_text())
    assert "OPENAI_API_KEY" not in registry
    assert "OPENAI_API_KEY" not in os.environ


@pytest.mark.parametrize("override_scope", ["process", "registry", "both"])
def test_restore_preserves_external_credential_overrides(codex_backup, override_scope):
    files, registry = codex_backup
    files["codex_env"].write_text('OPENAI_API_KEY="test-a"\n', encoding="utf-8")
    entry = backup_manager.create_backup("A")
    files["codex_env"].write_text('OPENAI_API_KEY="test-b"\n', encoding="utf-8")
    registry["OPENAI_API_KEY"] = "external" if override_scope in {"registry", "both"} else "test-b"
    os.environ["OPENAI_API_KEY"] = "external" if override_scope in {"process", "both"} else "test-b"
    backup_manager.restore_backup(entry)
    assert registry["OPENAI_API_KEY"] == ("external" if override_scope in {"registry", "both"} else "test-a")
    assert os.environ["OPENAI_API_KEY"] == ("external" if override_scope in {"process", "both"} else "test-a")


def test_restore_does_not_promote_dotenv_proxy_or_unrelated_keys(codex_backup, monkeypatch):
    files, registry = codex_backup
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8888")
    files["codex_config.toml"].write_text('model_provider="unsafe"\n[model_providers.unsafe]\nenv_key="HTTPS_PROXY"', encoding="utf-8")
    files["codex_env"].write_text('HTTPS_PROXY="old-proxy"\nOTHER_API_KEY="other-old"\n', encoding="utf-8")
    entry = backup_manager.create_backup("unrelated env")
    files["codex_env"].write_text('HTTPS_PROXY="http://proxy.example.test:8888"\nOTHER_API_KEY="other-current"\n', encoding="utf-8")
    registry["OTHER_API_KEY"] = os.environ["OTHER_API_KEY"] = "other-current"
    backup_manager.restore_backup(entry)
    assert os.environ["HTTPS_PROXY"] == "http://proxy.example.test:8888"
    assert registry == {"OTHER_API_KEY": "other-current"}
    assert os.environ["OTHER_API_KEY"] == "other-current"


def test_old_v2_backup_leaves_unmanaged_dotenv_and_environment_alone(codex_backup):
    files, registry = codex_backup
    entry = backup_manager.create_backup("old version")
    meta_path = entry.directory / backup_manager.BACKUP_META_FILE
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["managed_files"].remove("codex_env")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    files["codex_env"].write_bytes(b'OPENAI_API_KEY="test-b"\n')
    registry["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"] = "test-b"
    backup_manager.restore_backup(entry)
    assert files["codex_env"].read_bytes() == b'OPENAI_API_KEY="test-b"\n'
    assert registry["OPENAI_API_KEY"] == os.environ["OPENAI_API_KEY"] == "test-b"


def test_persistent_env_failure_rolls_back_files_and_both_env_scopes(codex_backup, monkeypatch):
    files, registry = codex_backup
    files["codex_env"].write_bytes(b'OPENAI_API_KEY="test-a"\n')
    entry = backup_manager.create_backup("A")
    files["codex_env"].write_bytes(b'OPENAI_API_KEY="test-b"\n')
    files["codex_auth.json"].write_bytes(b'{"OPENAI_API_KEY":"test-b"}')
    registry["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"] = "test-b"
    original_set = persistent_env.set_local_user_env
    calls = []

    def fail_once(updates):
        original_set(updates)
        calls.append(updates)
        if len(calls) == 1:
            raise OSError("injected registry failure after mutation")

    monkeypatch.setattr(persistent_env, "set_local_user_env", fail_once)
    with pytest.raises(OSError, match="injected registry failure"):
        backup_manager.restore_backup(entry)
    assert files["codex_env"].read_bytes() == b'OPENAI_API_KEY="test-b"\n'
    assert files["codex_auth.json"].read_bytes() == b'{"OPENAI_API_KEY":"test-b"}'
    assert registry["OPENAI_API_KEY"] == os.environ["OPENAI_API_KEY"] == "test-b"


def test_valid_backup_repairs_corrupt_current_dotenv(codex_backup):
    files, registry = codex_backup
    files["codex_env"].write_bytes(b'OPENAI_API_KEY="test-a"\n')
    entry = backup_manager.create_backup("valid")
    files["codex_env"].write_bytes(b'# damaged comment: \xff\nOPENAI_API_KEY="test-b"\n')
    registry["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"] = "test-b"
    backup_manager.restore_backup(entry)
    assert files["codex_env"].read_bytes() == b'OPENAI_API_KEY="test-a"\n'
    assert registry["OPENAI_API_KEY"] == os.environ["OPENAI_API_KEY"] == "test-a"


@pytest.mark.parametrize("entrypoint", ["tab", "shortcut"])
def test_restore_entrypoints_explain_dotenv_and_restart(monkeypatch, entrypoint):
    from types import SimpleNamespace
    from ui import app
    from ui.dialogs import confirm_dialog
    from ui.tabs import backup_tab
    from ui.widgets import toast

    entry = SimpleNamespace(timestamp="test-timestamp", description="test-description")
    messages, confirmations = [], []
    monkeypatch.setattr(backup_manager, "get_latest_backup", lambda: entry)
    monkeypatch.setattr(backup_manager, "restore_backup", lambda _entry: ["codex_env"])

    def confirm(_parent, **kwargs):
        confirmations.append(kwargs["message"])
        kwargs["on_confirm"]()

    for module in (confirm_dialog, backup_tab):
        monkeypatch.setattr(module, "ConfirmDialog", confirm)
    for module in (toast, backup_tab):
        monkeypatch.setattr(module, "show_toast", lambda _parent, message, **_kwargs: messages.append(message))
    window = SimpleNamespace(refresh_all=lambda: None, _set_app_status=lambda _message: None)
    if entrypoint == "shortcut":
        app.App._restore_latest_backup(window)
    else:
        tab = SimpleNamespace(_portable_operation_blocked=lambda: False, winfo_toplevel=lambda: window)
        backup_tab.BackupTab._restore(tab, entry)
    assert "Codex .env" in confirmations[0]
    assert "不会改动系统代理" in confirmations[0]
    assert "重启 Codex 和旧终端" in messages[0]
